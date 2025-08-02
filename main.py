import os
import logging
from fastapi import FastAPI, Request
from pymongo import MongoClient
from dotenv import load_dotenv
from datetime import datetime
from pydantic import BaseModel
from fastapi.responses import JSONResponse
import certifi
import httpx

# Cargar .env
load_dotenv()

# Logger
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("bot")

# App FastAPI
app = FastAPI()

# Variables de entorno
MONGO_URI = os.getenv("MONGO_URI")
TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

# MongoDB client
mongo_client = MongoClient(MONGO_URI, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=60000, socketTimeoutMS=60000)
db = mongo_client["telegram_gastos"]
movimientos = db["movimientos"]

# Categorías válidas
CATEGORIAS_VALIDAS = [
    "salud", "limpieza", "alimentacion", "transporte",
    "salidas", "ropa", "plantas", "arreglos casa", "vacaciones"
]

def guardar_movimiento(chat_id, tipo, monto, categoria, mensaje_original):
    movimientos.insert_one({
        "chat_id": chat_id,
        "tipo": tipo,
        "monto": monto,
        "categoria": categoria,
        "mensaje_original": mensaje_original,
        "fecha": datetime.utcnow()
    })
    logger.info(f"💾 Guardado: {tipo} S/ {monto} en {categoria} (chat {chat_id})")

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

    mensaje = "📊 Reporte general:\n"
    for cat, vals in saldos.items():
        saldo = vals["ingreso"] - vals["gasto"]
        mensaje += f"• {cat}: S/ {saldo:.2f}\n"
    return mensaje

@app.get("/")
async def root():
    return {"message": "Bot activo con MongoDB y Telegram ✅"}

@app.post(f"/{TOKEN}")
async def telegram_webhook(req: Request):
    try:
        body = await req.json()
        logger.debug(f"📩 Recibido: {body}")

        chat_id = body["message"]["chat"]["id"]
        text = body["message"].get("text", "").strip()

        if not text:
            return {"ok": True}

        if text.startswith("+") or text.startswith("-"):
            signo = text[0]
            partes = text[1:].strip().split(" ")
            if len(partes) < 2:
                msg = "❌ Usa +monto categoría (ej: +30 comida)"
            else:
                try:
                    monto = float(partes[0])
                    categoria = " ".join(partes[1:]).lower()
                    if categoria not in CATEGORIAS_VALIDAS:
                        msg = f"❌ Categoría inválida. Usa: {', '.join(CATEGORIAS_VALIDAS)}"
                    else:
                        tipo = "ingreso" if signo == "+" else "gasto"
                        guardar_movimiento(chat_id, tipo, monto, categoria, text)
                        saldo = obtener_saldo(categoria, chat_id)
                        msg = (
                            f"✅ {tipo.title()} de S/ {monto:.2f} registrado en '{categoria}'.\n"
                            f"💰 Saldo actual en '{categoria}': S/ {saldo:.2f}"
                        )
                except Exception:
                    msg = "❌ Monto inválido. Usa: +30 comida"
        elif text.lower().startswith("reporte de "):
            categoria = text.lower().replace("reporte de ", "").strip()
            if categoria not in CATEGORIAS_VALIDAS:
                msg = f"❌ Categoría inválida. Usa: {', '.join(CATEGORIAS_VALIDAS)}"
            else:
                saldo = obtener_saldo(categoria, chat_id)
                msg = f"💼 Saldo en '{categoria}': S/ {saldo:.2f}"
        elif text.lower() in ["reporte", "reporte general", "todo"]:
            msg = obtener_reporte_general(chat_id)
        else:
            msg = "🤖 Usa:\n+50 comida\n-20 transporte\n'reporte de comida'\n'reporte general'"

        httpx.post(f"{BASE_URL}/sendMessage", json={
            "chat_id": chat_id,
            "text": msg
        })
        return {"ok": True}

    except Exception as e:
        logger.exception("❌ Error en el webhook:")
        return JSONResponse(status_code=500, content={"error": str(e)})
