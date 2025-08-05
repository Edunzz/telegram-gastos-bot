import os
import json
import logging
import re
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

# === Categorías válidas ===
CATEGORIAS_VALIDAS = [
    "salud", "limpieza", "alimentacion", "transporte",
    "salidas", "ropa", "plantas", "arreglos casa", "vacaciones"
]

# === MongoDB ===
mongo_client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = mongo_client["telegram_gastos"]
movimientos = db["movimientos"]

# === Prompt OpenRouter ===
def generar_prompt(texto_usuario):
    return f"""
Eres un asistente que interpreta mensajes financieros enviados por usuarios en lenguaje natural.
A partir del mensaje del usuario, devuelve un JSON con tres claves: "tipo", "monto" y "categoria".

Reglas:
- "tipo" puede ser uno de los siguientes valores:
    • "gasto": si el mensaje describe un gasto. (Ej: "gasté", "pagué", "compré"). Tambien es el valor por defecto si no se identifica otro tipo o no se da mayor detalle.
    • "ingreso": si el mensaje describe un ingreso, ahorro o cualquier sinonimo de agregar. (Ej: "ahorré", "guardé", "recibí").
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
    except:
        return False

def obtener_saldo(categoria, chat_id):
    pipeline = [
        {"$match": {"chat_id": chat_id, "categoria": categoria}},
        {"$group": {"_id": "$tipo", "total": {"$sum": "$monto"}}}
    ]
    result = list(movimientos.aggregate(pipeline))
    ingresos = sum(r["total"] for r in result if r["_id"] == "ingreso")
    gastos = sum(r["total"] for r in result if r["_id"] == "gasto")
    return ingresos - gastos

def obtener_reporte_general(chat_id):
    pipeline = [
        {"$match": {"chat_id": chat_id}},
        {"$group": {"_id": {"categoria": "$categoria", "tipo": "$tipo"}, "total": {"$sum": "$monto"}}}
    ]
    result = list(movimientos.aggregate(pipeline))
    saldos = {}
    for r in result:
        cat = r["_id"]["categoria"]
        tipo = r["_id"]["tipo"]
        saldos.setdefault(cat, {"ingreso": 0, "gasto": 0})
        saldos[cat][tipo] += r["total"]

    mensaje = "📊 *Reporte general de categorías:*\n"
    for cat, vals in saldos.items():
        saldo = vals["ingreso"] - vals["gasto"]
        mensaje += f"• {cat}: S/ {saldo:.2f}\n"
    mensaje += f"\n[📄 Ver reporte en Google Sheets]({GOOGLE_SHEET_URL})"
    return mensaje


# === Rutas ===
@app.get("/")
async def root():
    return {"message": "Bot activo con MongoDB y OpenRouter ✅"}

# === Webhook ===
@app.post(f"/{TOKEN}")
async def telegram_webhook(req: Request):
    body = await req.json()
    chat_id = body["message"]["chat"]["id"]
    text = body["message"].get("text", "").strip()
    resultado = procesar_con_openrouter(text)

    if "error" in resultado:
        msg = "⚠️ No pude interpretar tu mensaje. Intenta de nuevo."
    else:
        tipo = resultado.get("tipo")
        monto = resultado.get("monto", 0)
        categoria = resultado.get("categoria", "")

        if tipo == "info":
            msg = (
                "ℹ️ *Opciones disponibles:*\n"
                "- Registrar gasto: 'gasté 50 en transporte'\n"
                "- Registrar ingreso: 'ahorré 20 para salud'\n"
                "- Ver reporte: 'reporte de ropa' o 'reporte general'\n"
                "- Eliminar: 'eliminar <ID>'\n"
                "\nCategorías válidas:\n" + "\n".join(f"- {c}" for c in CATEGORIAS_VALIDAS)
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
                msg = f"💼 *Saldo en '{categoria}':*\nS/ {saldo:.2f}\n\n[📄 Ver reporte en Google Sheets]({GOOGLE_SHEET_URL})"
            else:
                msg = obtener_reporte_general(chat_id)
        elif tipo in ["gasto", "ingreso"] and categoria in CATEGORIAS_VALIDAS and monto > 0:
            doc_id = guardar_movimiento(chat_id, tipo, monto, categoria, text)
            saldo = obtener_saldo(categoria, chat_id)
            msg = (
                f"✅ {tipo.title()} de S/ {monto:.2f} registrado en '{categoria}'.\n"
                f"🆔 ID: `{doc_id}`\n"
                f"💰 Saldo actual: S/ {saldo:.2f}\n"
                f"\n[📄 Ver reporte en Google Sheets]({GOOGLE_SHEET_URL})"
            )
        else:
            msg = "⚠️ No pude interpretar tu mensaje o faltan datos válidos."

    httpx.post(f"{BASE_URL}/sendMessage", json={
        "chat_id": chat_id,
        "text": msg,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    })
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
        except:
            return JSONResponse(status_code=400, content={"error": "Fechas inválidas"})

    docs = list(movimientos.find(query, {"_id": 0}))
    for doc in docs:
        if isinstance(doc.get("fecha"), datetime):
            doc["fecha"] = doc["fecha"].strftime("%Y-%m-%d %H:%M:%S")
    return JSONResponse(content=jsonable_encoder(docs))
