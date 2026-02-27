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

# === Categorías válidas ===
CATEGORIAS_VALIDAS = [
    "salud", "skincare", "limpieza", "alimentacion", "transporte",
    "salidas", "ropa", "plantas", "arreglos casa", "vacaciones", "visita familia", "festividades"
]

# === MongoDB ===
mongo_client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = mongo_client["telegram_gastos"]
movimientos = db["movimientos"]

# === Prompt ===
def generar_prompt(texto_usuario: str) -> str:
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
- "categoria": debe ser una de las siguientes (sin tildes ni errores ortográficos): salud, skincare, limpieza, alimentacion, transporte, salidas, ropa, plantas, arreglos casa, vacaciones, visita familia, festividades.
  Si el texto no menciona una categoría válida o no aplica (como en "info" o "eliminar"), puede ir como cadena vacía "".

Ejemplo:
{{"tipo": "gasto", "monto": 25, "categoria": "transporte"}}

Mensaje del usuario: "{texto_usuario}"
""".strip()

# === OpenAI (reemplazo de OpenRouter) ===
def procesar_con_openai(texto_usuario: str):
    if not openai_client:
        return {"error": "OPENAI_API_KEY no configurada"}

    prompt = generar_prompt(texto_usuario)

    try:
        # Pedimos SOLO JSON para evitar texto extra
        resp = openai_client.responses.create(
            model=OPENAI_MODEL,
            input=[
                {"role": "system", "content": "Devuelve únicamente un JSON válido. No agregues texto extra."},
                {"role": "user", "content": prompt},
            ],
        )

        # Forma más directa
        content = (getattr(resp, "output_text", "") or "").strip()

        # Fallback por si no viniera output_text
        if not content:
            parts = []
            for item in (getattr(resp, "output", []) or []):
                if getattr(item, "type", "") == "message":
                    for c in (getattr(item, "content", []) or []):
                        if getattr(c, "type", "") in ("output_text", "text"):
                            parts.append(getattr(c, "text", ""))
            content = "".join(parts).strip()

        # Extraer JSON aunque venga con algo de texto accidental
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise ValueError(f"No se encontró JSON. Respuesta: {content[:250]}")

        data = json.loads(match.group())

        # Normalización mínima
        tipo = data.get("tipo", "gasto")
        monto = data.get("monto", 0)
        categoria = data.get("categoria", "")

        if tipo in ("reporte", "info", "eliminar"):
            monto = 0

        # Asegurar monto numérico
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
    """
    Saldo GLOBAL por categoría (sin filtrar por chat_id).
    """
    pipeline = [
        {"$match": {"categoria": categoria}},
        {"$group": {"_id": "$tipo", "total": {"$sum": "$monto"}}}
    ]
    result = list(movimientos.aggregate(pipeline))
    ingresos = sum(r["total"] for r in result if r["_id"] == "ingreso")
    gastos = sum(r["total"] for r in result if r["_id"] == "gasto")
    return ingresos - gastos

def obtener_reporte_general(chat_id=None):
    """
    Reporte GLOBAL por categorías (sin filtrar por chat_id).
    """
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

    mensaje = "📊 *Reporte general de categorías:*\n"
    for cat, vals in saldos.items():
        saldo = vals["ingreso"] - vals["gasto"]
        mensaje += f"• {cat if cat else '(sin categoría)'}: S/ {saldo:.2f}\n"
    if GOOGLE_SHEET_URL:
        mensaje += f"\n[📄 Ver reporte en Google Sheets]({GOOGLE_SHEET_URL})"
    return mensaje

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

    resultado = procesar_con_openai(text)

    if "error" in resultado:
        msg = "⚠️ No pude interpretar tu mensaje. Intenta de nuevo."
    else:
        tipo = resultado.get("tipo")
        monto = resultado.get("monto", 0)
        categoria = (resultado.get("categoria", "") or "").strip()

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
                msg = f"💼 *Saldo en '{categoria}':*\nS/ {saldo:.2f}"
                if GOOGLE_SHEET_URL:
                    msg += f"\n\n[📄 Ver reporte en Google Sheets]({GOOGLE_SHEET_URL})"
            else:
                msg = obtener_reporte_general(chat_id)

        elif tipo in ["gasto", "ingreso"] and categoria in CATEGORIAS_VALIDAS and monto > 0:
            doc_id = guardar_movimiento(chat_id, tipo, float(monto), categoria, text)
            saldo = obtener_saldo(categoria, chat_id)
            msg = (
                f"✅ {tipo.title()} de S/ {float(monto):.2f} registrado en '{categoria}'.\n"
                f"🆔 ID: `{doc_id}`\n"
                f"💰 Saldo actual: S/ {saldo:.2f}"
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
