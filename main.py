import os
import json
import logging
import re
import unicodedata
from datetime import datetime

from fastapi import FastAPI, Request, Query
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

from pymongo import MongoClient
from bson import ObjectId
from dateutil import parser
from dotenv import load_dotenv
import certifi
import httpx

from openai import OpenAI

load_dotenv()
app = FastAPI()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")

# === Variables de entorno ===
TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
GOOGLE_SHEET_URL = os.getenv("GOOGLE_SHEET_URL")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")

BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

# Cliente OpenAI (una sola vez)
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# === Categorías base (hardcoded) ===
CATEGORIAS_BASE = [
    "salud", "skincare", "limpieza", "alimentacion", "transporte",
    "salidas", "ropa", "plantas", "arreglos casa", "vacaciones", "visita familia", "festividades"
]

# === MongoDB ===
mongo_client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = mongo_client["telegram_gastos"]
movimientos = db["movimientos"]
col_categorias = db["categorias"]  # colección para categorías personalizadas


def _normalizar(texto: str) -> str:
    """Convierte a minúsculas y elimina tildes para comparaciones insensibles."""
    nfkd = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def cargar_categorias() -> list:
    """Devuelve la lista unificada de categorías (base + persistidas en MongoDB)."""
    extras = [doc["nombre"] for doc in col_categorias.find({}, {"_id": 0, "nombre": 1})]
    base_norm = {_normalizar(c) for c in CATEGORIAS_BASE}
    nuevas = [e for e in extras if _normalizar(e) not in base_norm]
    return CATEGORIAS_BASE + nuevas


# Lista dinámica de categorías (se recarga en cada request para reflejar altas)
def get_categorias() -> list:
    return cargar_categorias()


# === Prompt ===
def generar_prompt(texto_usuario: str) -> str:
    categorias_str = ", ".join(get_categorias())
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
- "categoria": debe ser una de las siguientes (sin tildes ni errores ortográficos): {categorias_str}.
  Si el texto no menciona una categoría válida o no aplica (como en "info" o "eliminar"), puede ir como cadena vacía "".

Ejemplo:
{{"tipo": "gasto", "monto": 25, "categoria": "transporte"}}

Mensaje del usuario: "{texto_usuario}"
""".strip()


# === OpenAI ===
def procesar_con_openai(texto_usuario: str):
    if not openai_client:
        return {"error": "OPENAI_API_KEY no configurada"}

    prompt = generar_prompt(texto_usuario)

    try:
        resp = openai_client.responses.create(
            model=OPENAI_MODEL,
            input=[
                {"role": "system", "content": "Devuelve únicamente un JSON válido. No agregues texto extra."},
                {"role": "user", "content": prompt},
            ],
        )

        content = (getattr(resp, "output_text", "") or "").strip()

        if not content:
            parts = []
            for item in (getattr(resp, "output", []) or []):
                if getattr(item, "type", "") == "message":
                    for c in (getattr(item, "content", []) or []):
                        if getattr(c, "type", "") in ("output_text", "text"):
                            parts.append(getattr(c, "text", ""))
            content = "".join(parts).strip()

        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise ValueError(f"No se encontró JSON. Respuesta: {content[:250]}")

        data = json.loads(match.group())

        tipo = data.get("tipo", "gasto")
        monto = data.get("monto", 0)
        categoria = data.get("categoria", "")

        if tipo in ("reporte", "info", "eliminar"):
            monto = 0

        if not isinstance(monto, (int, float)):
            try:
                monto = float(monto)
            except Exception:
                monto = 0

        data["tipo"] = tipo
        data["monto"] = max(0, monto)
        data["categoria"] = categoria if isinstance(categoria, str) else ""

        return data

    except Exception as e:
        logger.exception("❌ Error en OpenAI:")
        return {"error": str(e)}


# === Utilidades MongoDB ===
def guardar_movimiento(chat_id, tipo, monto, categoria, mensaje_original):
    doc = {
        "chat_id": chat_id,
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
        result = movimientos.delete_one({"_id": ObjectId(doc_id), "chat_id": chat_id})
        return result.deleted_count > 0
    except Exception:
        return False


def obtener_saldo(categoria, chat_id=None):
    """Saldo GLOBAL por categoría (sin filtrar por chat_id)."""
    pipeline = [
        {"$match": {"categoria": categoria}},
        {"$group": {"_id": "$tipo", "total": {"$sum": "$monto"}}}
    ]
    result = list(movimientos.aggregate(pipeline))
    ingresos = sum(r["total"] for r in result if r["_id"] == "ingreso")
    gastos = sum(r["total"] for r in result if r["_id"] == "gasto")
    return ingresos - gastos


def obtener_movimientos_categoria(categoria: str, n: int = 5) -> list:
    """Devuelve los últimos N movimientos de una categoría, del más reciente al más antiguo."""
    docs = list(
        movimientos.find({"categoria": categoria})
        .sort("fecha", -1)
        .limit(n)
    )
    return docs


def obtener_reporte_general(chat_id=None):
    """Reporte GLOBAL por categorías, ordenado alfabéticamente (insensible a mayúsculas/acentos)."""
    pipeline = [
        {"$group": {"_id": {"categoria": "$categoria", "tipo": "$tipo"}, "total": {"$sum": "$monto"}}}
    ]
    result = list(movimientos.aggregate(pipeline))

    saldos = {}
    for r in result:
        cat = r["_id"]["categoria"]
        tipo = r["_id"]["tipo"]
        saldos.setdefault(cat, {"ingreso": 0, "gasto": 0})
        saldos[cat][tipo] += r["total"]

    # Ordenar alfabéticamente ignorando mayúsculas y tildes
    categorias_ordenadas = sorted(saldos.items(), key=lambda x: _normalizar(x[0] or ""))

    mensaje = "📊 *Reporte general de categorías*\n"
    mensaje += "─────────────────────────\n"
    for cat, vals in categorias_ordenadas:
        saldo = vals["ingreso"] - vals["gasto"]
        signo = "🟢" if saldo >= 0 else "🔴"
        nombre = cat if cat else "(sin categoría)"
        mensaje += f"{signo} *{nombre.title()}:* S/ {saldo:,.2f}\n"
    mensaje += "─────────────────────────"
    if GOOGLE_SHEET_URL:
        mensaje += f"\n\n[📄 Ver reporte en Google Sheets]({GOOGLE_SHEET_URL})"
    return mensaje


def _agregar_categoria(nombre: str) -> tuple[bool, str]:
    """
    Persiste una nueva categoría en MongoDB.
    Devuelve (éxito, mensaje).
    """
    nombre = nombre.strip()
    if not nombre:
        return False, "❌ El nombre de la categoría no puede estar vacío."

    todas = get_categorias()
    norm_nuevo = _normalizar(nombre)
    for cat in todas:
        if _normalizar(cat) == norm_nuevo:
            return False, f"⚠️ La categoría *'{cat}'* ya existe."

    col_categorias.insert_one({"nombre": nombre.lower(), "fecha": datetime.utcnow()})
    return True, f"✅ Categoría *'{nombre}'* creada correctamente."


def _parsear_n_movimientos(texto: str, default: int = 5) -> int:
    """
    Extrae el número N del texto si el usuario lo especifica (ej. 'ropa 10').
    Devuelve default si no se encuentra o es inválido.
    """
    numeros = re.findall(r"\b(\d+)\b", texto)
    if numeros:
        n = int(numeros[-1])
        return n if n > 0 else default
    return default


# === Rutas ===
@app.get("/")
async def root():
    return {"message": "Bot activo con MongoDB y OpenAI ✅", "model": OPENAI_MODEL}


# === Webhook ===
@app.post(f"/{TOKEN}")
async def telegram_webhook(req: Request):
    body = await req.json()
    logger.info(f"Update recibido: {body}")

    message = body.get("message") or body.get("edited_message") or body.get("channel_post")
    if not message:
        logger.info("Update sin 'message'/'edited_message'/'channel_post'. Se ignora.")
        return {"ok": True}

    chat_id = message["chat"]["id"]

    text = message.get("text", "")
    if not isinstance(text, str):
        text = ""
    text = text.strip()

    if not text:
        msg = "Solo puedo procesar mensajes de texto por ahora 😊"
        httpx.post(f"{BASE_URL}/sendMessage", json={"chat_id": chat_id, "text": msg})
        return {"ok": True}

    # === Comando especial /nueva_categoria (antes de pasar a OpenAI) ===
    cmd_nueva = re.match(r"^/?(nueva_categoria|agregar_categoria)\s*(.*)", text, re.IGNORECASE)
    if cmd_nueva:
        nombre_cat = cmd_nueva.group(2).strip()
        ok, msg = _agregar_categoria(nombre_cat)
        httpx.post(
            f"{BASE_URL}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}
        )
        return {"ok": True}

    resultado = procesar_con_openai(text)

    CATEGORIAS_VALIDAS = get_categorias()

    if "error" in resultado:
        msg = "⚠️ No pude interpretar tu mensaje. Intenta de nuevo."
    else:
        tipo = resultado.get("tipo")
        monto = resultado.get("monto", 0)
        categoria = (resultado.get("categoria", "") or "").strip()

        if tipo == "info":
            cats_lista = "\n".join(f"  • {c}" for c in sorted(CATEGORIAS_VALIDAS, key=_normalizar))
            msg = (
                "ℹ️ *Opciones disponibles:*\n\n"
                "💸 Registrar gasto: _'gasté 50 en transporte'_\n"
                "💰 Registrar ingreso: _'ahorré 20 para salud'_\n"
                "📊 Reporte general: _'reporte general'_\n"
                "🗂️ Reporte categoría: _'reporte de ropa'_ o _'reporte ropa 10'_\n"
                "➕ Nueva categoría: _'/nueva_categoria <nombre>'_\n"
                "🗑️ Eliminar: _'eliminar <ID>'_\n\n"
                f"📋 *Categorías válidas:*\n{cats_lista}"
            )

        elif tipo == "eliminar":
            match = re.search(r"[0-9a-f]{24}", text)
            if match and eliminar_movimiento_por_id(match.group(), chat_id):
                msg = f"🗑️ Movimiento con ID `{match.group()}` eliminado correctamente."
            else:
                msg = "❌ No se pudo eliminar. Verifica el ID."

        elif tipo == "reporte":
            if categoria in CATEGORIAS_VALIDAS:
                saldo = obtener_saldo(categoria, chat_id)
                n = _parsear_n_movimientos(text)
                ultimos = obtener_movimientos_categoria(categoria, n)

                msg = (
                    f"🗂️ *Categoría: {categoria.title()}*\n"
                    f"─────────────────────────\n"
                    f"💰 *Saldo total:* S/ {saldo:,.2f}\n"
                    f"─────────────────────────\n"
                    f"🧾 *Últimos {n} movimientos:*\n"
                )

                if ultimos:
                    for mov in ultimos:
                        fecha = mov.get("fecha")
                        fecha_str = fecha.strftime("%d/%m/%Y %H:%M") if isinstance(fecha, datetime) else "—"
                        tipo_mov = mov.get("tipo", "")
                        emoji_tipo = "📥" if tipo_mov == "ingreso" else "📤"
                        monto_mov = mov.get("monto", 0)
                        concepto = mov.get("mensaje_original", "")
                        # Truncar concepto largo
                        if len(concepto) > 60:
                            concepto = concepto[:57] + "..."
                        msg += (
                            f"\n{emoji_tipo} *S/ {monto_mov:,.2f}* — {tipo_mov}\n"
                            f"  📅 {fecha_str}\n"
                            f"  📝 _{concepto}_\n"
                        )
                else:
                    msg += "_No hay movimientos registrados._\n"

                msg += "─────────────────────────"
                if GOOGLE_SHEET_URL:
                    msg += f"\n\n[📄 Ver reporte en Google Sheets]({GOOGLE_SHEET_URL})"
            else:
                msg = obtener_reporte_general(chat_id)

        elif tipo in ["gasto", "ingreso"] and categoria in CATEGORIAS_VALIDAS and monto > 0:
            doc_id = guardar_movimiento(chat_id, tipo, float(monto), categoria, text)
            saldo = obtener_saldo(categoria, chat_id)
            emoji_op = "📤" if tipo == "gasto" else "📥"
            msg = (
                f"{emoji_op} *{tipo.title()} registrado*\n"
                f"─────────────────────────\n"
                f"🗂️ Categoría: *{categoria.title()}*\n"
                f"💵 Monto: *S/ {float(monto):,.2f}*\n"
                f"🆔 ID: `{doc_id}`\n"
                f"─────────────────────────\n"
                f"💰 Saldo actual: *S/ {saldo:,.2f}*"
            )
            if GOOGLE_SHEET_URL:
                msg += f"\n\n[📄 Ver reporte en Google Sheets]({GOOGLE_SHEET_URL})"
        else:
            msg = "⚠️ No pude interpretar tu mensaje o faltan datos válidos."

    httpx.post(
        f"{BASE_URL}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": msg,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
    )
    return {"ok": True}


# === Exportar ===
@app.get("/exportar")
async def exportar_data(clave: str = Query(...), desde: str = None, hasta: str = None):
    if clave != os.getenv("EXPORT_PASS", "0000"):
        return JSONResponse(status_code=401, content={"error": "No autorizado"})

    query = {}
    if desde or hasta:
        try:
            desde_dt = parser.parse(desde) if desde else datetime.min
            hasta_dt = parser.parse(hasta) if hasta else datetime.max
            query["fecha"] = {"$gte": desde_dt, "$lte": hasta_dt}
        except Exception:
            return JSONResponse(status_code=400, content={"error": "Fechas inválidas"})

    docs = list(movimientos.find(query, {"_id": 0}))
    for doc in docs:
        if isinstance(doc.get("fecha"), datetime):
            doc["fecha"] = doc["fecha"].strftime("%Y-%m-%d %H:%M:%S")

    return JSONResponse(content=jsonable_encoder(docs))
