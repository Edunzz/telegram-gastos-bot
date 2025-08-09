import os
import json
import logging
import re
import random
import string
from datetime import datetime
from fastapi import FastAPI, Request, Query
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from pymongo import MongoClient, ASCENDING
from bson import ObjectId
from dateutil import parser
from dotenv import load_dotenv
import certifi
import httpx

load_dotenv()
app = FastAPI()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")

# === Variables de entorno ===
TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "mistralai/mistral-7b-instruct")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
GOOGLE_SHEET_URL = os.getenv("GOOGLE_SHEET_URL")
GROUP_CODE_LENGTH = int(os.getenv("GROUP_CODE_LENGTH", "6"))

# === Categorías válidas ===
CATEGORIAS_VALIDAS = [
    "salud", "limpieza", "alimentacion", "transporte",
    "salidas", "ropa", "plantas", "arreglos casa", "vacaciones"
]

# === MongoDB ===
mongo_client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = mongo_client["telegram_gastos"]
movimientos = db["movimientos"]
usuarios = db["usuarios"]
grupos = db["grupos"]

# Índices para escalar
movimientos.create_index([("group_code", ASCENDING), ("categoria", ASCENDING), ("tipo", ASCENDING), ("fecha", ASCENDING)])
movimientos.create_index([("group_code", ASCENDING), ("fecha", ASCENDING)])
usuarios.create_index([("chat_id", ASCENDING)], unique=True)
grupos.create_index([("code", ASCENDING)], unique=True)

# === Utilidades de grupos/usuarios ===
def _codigo_grupo_unico(length=GROUP_CODE_LENGTH):
    # Evita caracteres confusos
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    while True:
        code = "".join(random.choice(alphabet) for _ in range(length))
        if not grupos.find_one({"code": code}):
            return code

def obtener_usuario(chat_id: int):
    return usuarios.find_one({"chat_id": chat_id})

def crear_usuario(chat_id: int):
    usuarios.insert_one({
        "chat_id": chat_id,
        "group_code": None,
        "pending": {"step": "await_group_choice"},
        "created_at": datetime.utcnow()
    })
    return obtener_usuario(chat_id)

def set_pending(chat_id: int, step: str, extra: dict | None = None):
    usuarios.update_one({"chat_id": chat_id}, {"$set": {"pending": {"step": step, **(extra or {})}}})

def clear_pending(chat_id: int):
    usuarios.update_one({"chat_id": chat_id}, {"$set": {"pending": None}})

def set_group_for_user(chat_id: int, code: str):
    usuarios.update_one({"chat_id": chat_id}, {"$set": {"group_code": code}})
    clear_pending(chat_id)

def crear_grupo(nombre: str, owner_chat_id: int) -> str:
    code = _codigo_grupo_unico()
    grupos.insert_one({
        "code": code,
        "name": nombre.strip() or "Mi grupo",
        "owner_chat_id": owner_chat_id,
        "members": [owner_chat_id],
        "created_at": datetime.utcnow()
    })
    set_group_for_user(owner_chat_id, code)
    return code

def unir_a_grupo(code: str, chat_id: int) -> bool:
    g = grupos.find_one({"code": code})
    if not g:
        return False
    # agrega si no está
    if chat_id not in g.get("members", []):
        grupos.update_one({"code": code}, {"$addToSet": {"members": chat_id}})
    set_group_for_user(chat_id, code)
    return True

def obtener_group_code(chat_id: int) -> str | None:
    u = obtener_usuario(chat_id)
    return u.get("group_code") if u else None

# === Prompt OpenRouter ===
def generar_prompt(texto_usuario):
    return f"""
Eres un asistente que interpreta mensajes financieros enviados por usuarios en lenguaje natural.
A partir del mensaje del usuario, devuelve un JSON con tres claves: "tipo", "monto" y "categoria".

Reglas:
- "tipo" puede ser uno de los siguientes valores:
    • "gasto": si el mensaje describe un gasto. (Ej: "gasté", "pagué", "compré"). También es el valor por defecto si no se identifica otro tipo o no se da mayor detalle.
    • "ingreso": si el mensaje describe un ingreso, ahorro o cualquier sinónimo de agregar. (Ej: "ahorré", "guardé", "recibí").
    • "reporte": si el mensaje solicita un resumen, reporte o saldo de alguna categoría o general.
    • "info": si el usuario pide ayuda, ejemplos o funcionamiento del bot.
    • "eliminar": si el usuario desea borrar un movimiento por su ID.
- "monto": número positivo extraído del texto. Si el tipo es "reporte", "info" o "eliminar", debe colocarse como 0.
- "categoria": debe ser una de las siguientes (sin tildes ni errores ortográficos): salud, limpieza, alimentacion, transporte, salidas, ropa, plantas, arreglos casa, vacaciones. Si el texto no menciona una categoría válida o no aplica (como en "info" o "eliminar"), puede ir como cadena vacía "".

Ejemplo:
{{"tipo": "gasto", "monto": 25, "categoria": "transporte"}}

Mensaje del usuario: "{texto_usuario}"
""".strip()

# === Procesamiento con modelo ===
def procesar_con_openrouter(texto_usuario: str):
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://tubot.com"
    }
    body = {
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": generar_prompt(texto_usuario)}]
    }
    try:
        response = httpx.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=body, timeout=30)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        match = re.search(r'\{.*?\}', content, re.DOTALL)
        if match:
            return json.loads(match.group())
        else:
            raise ValueError("❌ No se encontró JSON válido en la respuesta.")
    except Exception as e:
        logger.exception("❌ Error en OpenRouter:")
        return {"error": str(e)}

# === Utilidades MongoDB (con partición por grupo) ===
def guardar_movimiento(chat_id, tipo, monto, categoria, mensaje_original):
    group_code = obtener_group_code(chat_id)
    doc = {
        "chat_id": chat_id,
        "group_code": group_code,
        "tipo": tipo,
        "monto": monto,
        "categoria": categoria,
        "mensaje_original": mensaje_original,
        "fecha": datetime.utcnow()
    }
    result = movimientos.insert_one(doc)
    return result.inserted_id

def eliminar_movimiento_por_id(doc_id, chat_id):
    try:
        group_code = obtener_group_code(chat_id)
        result = movimientos.delete_one({"_id": ObjectId(doc_id), "group_code": group_code})
        return result.deleted_count > 0
    except:
        return False

def obtener_saldo(categoria, group_code: str):
    pipeline = [
        {"$match": {"group_code": group_code, "categoria": categoria}},
        {"$group": {"_id": "$tipo", "total": {"$sum": "$monto"}}}
    ]
    result = list(movimientos.aggregate(pipeline))
    ingresos = sum(r["total"] for r in result if r["_id"] == "ingreso")
    gastos = sum(r["total"] for r in result if r["_id"] == "gasto")
    return ingresos - gastos

def obtener_reporte_general(group_code: str):
    pipeline = [
        {"$match": {"group_code": group_code}},
        {"$group": {"_id": {"categoria": "$categoria", "tipo": "$tipo"}, "total": {"$sum": "$monto"}}}
    ]
    result = list(movimientos.aggregate(pipeline))

    saldos = {}
    for r in result:
        cat = r["_id"]["categoria"]
        tipo = r["_id"]["tipo"]
        saldos.setdefault(cat, {"ingreso": 0, "gasto": 0})
        saldos[cat][tipo] += r["total"]

    mensaje = "📊 *Reporte general del grupo:*\n"
    if not saldos:
        mensaje += "No hay movimientos aún.\n"
    else:
        for cat, vals in saldos.items():
            saldo = vals["ingreso"] - vals["gasto"]
            cat_show = cat if cat else "(sin categoría)"
            mensaje += f"• {cat_show}: S/ {saldo:.2f}\n"
    if GOOGLE_SHEET_URL:
        mensaje += f"\n[📄 Ver reporte en Google Sheets]({GOOGLE_SHEET_URL})"
    return mensaje

# === Mensajes de ayuda / onboarding ===
AYUDA_BASICA = (
    "ℹ️ *Opciones disponibles:*\n"
    "- Registrar gasto: `gasté 50 en transporte`\n"
    "- Registrar ingreso: `ahorré 20 para salud`\n"
    "- Ver reporte: `reporte de ropa` o `reporte general`\n"
    "- Eliminar por ID: `eliminar <ID>`\n"
    "\nCategorías válidas:\n" + "\n".join(f"- {c}" for c in CATEGORIAS_VALIDAS)
)

ONBOARDING_MSG = (
    "👋 ¡Bienvenido! Para empezar, elige una opción:\n"
    "• *Crear* un grupo nuevo: `crear <nombre-del-grupo>`\n"
    "• *Unirme* a un grupo existente: `unir <CÓDIGO>`\n\n"
    "Ejemplos:\n"
    "• `crear Familia`\n"
    "• `unir ABC123`\n\n"
    "Con el *código de grupo* podrás invitar a más personas (por ejemplo, tu pareja) para compartir los gastos sin mezclar con otros grupos."
)

def info_con_grupo(chat_id: int) -> str:
    code = obtener_group_code(chat_id)
    g = grupos.find_one({"code": code}) if code else None
    nombre = g["name"] if g else "—"
    if not code:
        return ONBOARDING_MSG
    return (
        f"👥 *Grupo:* {nombre} (`{code}`)\n\n"
        + AYUDA_BASICA
        + "\n\nComparte este código para que otros se unan: "
        f"`{code}`"
    )

# === Rutas ===
@app.get("/")
async def root():
    return {"message": "Bot activo con MongoDB y OpenRouter ✅ (multi-grupo)"}

# === Webhook ===
@app.post(f"/{TOKEN}")
async def telegram_webhook(req: Request):
    body = await req.json()
    chat_id = body["message"]["chat"]["id"]
    text = (body["message"].get("text", "") or "").strip()
    text_l = text.lower()

    # 1) Usuario
    user = obtener_usuario(chat_id)
    if not user:
        user = crear_usuario(chat_id)
        # Primer mensaje: pedir elección
        httpx.post(f"{BASE_URL}/sendMessage", json={
            "chat_id": chat_id,
            "text": ONBOARDING_MSG,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        })
        return {"ok": True}

    group_code = user.get("group_code")
    pending = (user.get("pending") or {}) if user else {}

    # 2) Manejo de onboarding / comandos de grupo
    # crear <nombre>
    m_crear = re.match(r"^\s*crear\s+(.+)$", text_l)
    m_unir = re.match(r"^\s*unir\s+([a-z0-9]+)$", text_l, re.IGNORECASE)

    if group_code is None:
        # Usuario sin grupo -> debe crear o unirse
        if m_crear:
            nombre = m_crear.group(1).strip()
            code = crear_grupo(nombre, chat_id)
            msg = (
                f"✅ Grupo *{nombre}* creado.\n"
                f"🔑 Código: `{code}`\n\n"
                "Comparte este código para invitar a otros. ¡Ya puedes registrar gastos o pedir reportes!"
            )
        elif m_unir:
            code_try = m_unir.group(1).strip().upper()
            ok = unir_a_grupo(code_try, chat_id)
            if ok:
                msg = (
                    f"✅ Te uniste al grupo con código `{code_try}`.\n"
                    "¡Ya puedes registrar gastos o pedir reportes!"
                )
            else:
                msg = "❌ Código de grupo inválido. Intenta de nuevo con `unir <CÓDIGO>`."
        else:
            msg = ONBOARDING_MSG

        httpx.post(f"{BASE_URL}/sendMessage", json={
            "chat_id": chat_id,
            "text": msg,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        })
        return {"ok": True}

    # 3) Ya tiene grupo -> flujo normal
    resultado = procesar_con_openrouter(text)

    if "error" in resultado:
        msg = "⚠️ No pude interpretar tu mensaje. Escribe `info` para ver ejemplos."
    else:
        tipo = resultado.get("tipo")
        monto = resultado.get("monto", 0)
        categoria = resultado.get("categoria", "")

        if tipo == "info":
            msg = info_con_grupo(chat_id)

        elif tipo == "eliminar":
            match = re.search(r"[0-9a-f]{24}", text)
            if match and eliminar_movimiento_por_id(match.group(), chat_id):
                msg = f"🗑️ Movimiento con ID `{match.group()}` eliminado correctamente."
            else:
                msg = "❌ No se pudo eliminar. Verifica el ID (debe ser del grupo actual)."

        elif tipo == "reporte":
            if categoria in CATEGORIAS_VALIDAS:
                saldo = obtener_saldo(categoria, obtener_group_code(chat_id))
                msg = (
                    f"💼 *Saldo en '{categoria}' (grupo actual):*\n"
                    f"S/ {saldo:.2f}\n"
                )
                if GOOGLE_SHEET_URL:
                    msg += f"\n[📄 Ver reporte en Google Sheets]({GOOGLE_SHEET_URL})"
            else:
                msg = obtener_reporte_general(obtener_group_code(chat_id))

        elif tipo in ["gasto", "ingreso"] and categoria in CATEGORIAS_VALIDAS and monto > 0:
            doc_id = guardar_movimiento(chat_id, tipo, monto, categoria, text)
            saldo = obtener_saldo(categoria, obtener_group_code(chat_id))
            msg = (
                f"✅ {tipo.title()} de S/ {monto:.2f} registrado en '{categoria}'.\n"
                f"🆔 ID: `{doc_id}`\n"
                f"💰 Saldo actual (grupo): S/ {saldo:.2f}\n"
            )
            if GOOGLE_SHEET_URL:
                msg += f"\n[📄 Ver reporte en Google Sheets]({GOOGLE_SHEET_URL})"
        else:
            # Atajos para comandos de grupo aunque ya tenga grupo
            if m_crear:
                nombre = m_crear.group(1).strip()
                code = crear_grupo(nombre, chat_id)
                msg = (
                    f"✅ Nuevo grupo *{nombre}* creado y asignado.\n"
                    f"🔑 Código: `{code}`"
                )
            elif m_unir:
                code_try = m_unir.group(1).strip().upper()
                ok = unir_a_grupo(code_try, chat_id)
                msg = f"✅ Te uniste al grupo `{code_try}`." if ok else "❌ Código de grupo inválido."
            else:
                msg = "⚠️ No pude interpretar tu mensaje o faltan datos válidos. Escribe `info` para ver ejemplos."

    httpx.post(f"{BASE_URL}/sendMessage", json={
        "chat_id": chat_id,
        "text": msg,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    })
    return {"ok": True}

# === Exportar ===
@app.get("/exportar")
async def exportar_data(clave: str = Query(...), desde: str = None, hasta: str = None, group: str = Query(None)):
    if clave != os.getenv("EXPORT_PASS", "0000"):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})

    query = {}
    if group:
        query["group_code"] = group.upper()

    if desde or hasta:
        try:
            desde_dt = parser.parse(desde) if desde else datetime.min
            hasta_dt = parser.parse(hasta) if hasta else datetime.max
            query["fecha"] = {"$gte": desde_dt, "$lte": hasta_dt}
        except:
            return JSONResponse(status_code=400, content={"error": "Fechas inválidas"})

    docs = list(movimientos.find(query, {"_id": 0}))
    for doc in docs:
        if isinstance(doc.get("fecha"), datetime):
            doc["fecha"] = doc["fecha"].strftime("%Y-%m-%d %H:%M:%S")
    return JSONResponse(content=jsonable_encoder(docs))
