import os
import logging
from fastapi import FastAPI, Request
from pymongo import MongoClient
from dotenv import load_dotenv
from datetime import datetime
from pydantic import BaseModel
from fastapi.responses import JSONResponse
import certifi

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

app = FastAPI()

MONGO_URI = os.getenv("MONGO_URI")
mongo_client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
db = mongo_client["telegram_gastos"]
movimientos = db["movimientos"]

class TelegramMessage(BaseModel):
    message: dict

def guardar_movimiento(chat_id, tipo, monto, categoria):
    movimientos.insert_one({
        "chat_id": chat_id,
        "tipo": tipo,
        "monto": monto,
        "categoria": categoria,
        "fecha": datetime.utcnow()
    })
    logger.info(f"💾 Guardado: {tipo} {monto} en {categoria} (chat {chat_id})")

def obtener_saldo(categoria, chat_id):
    pipeline = [
        {"$match": {"chat_id": chat_id, "categoria": categoria}},
        {"$group": {
            "_id": "$tipo",
            "total": {"$sum": "$monto"}
        }}
    ]
    result = list(movimientos.aggregate(pipeline))
    ingresos = sum(item["total"] for item in result if item["_id"] == "ingreso")
    gastos = sum(item["total"] for item in result if item["_id"] == "gasto")
    return ingresos - gastos

def obtener_reporte_general(chat_id):
    pipeline = [
        {"$match": {"chat_id": chat_id}},
        {"$group": {
            "_id": {"categoria": "$categoria", "tipo": "$tipo"},
            "total": {"$sum": "$monto"}
        }}
    ]
    result = list(movimientos.aggregate(pipeline))
    saldos = {}
    for item in result:
        categoria = item["_id"]["categoria"]
        tipo = item["_id"]["tipo"]
        saldos.setdefault(categoria, {"ingreso": 0, "gasto": 0})
        saldos[categoria][tipo] += item["total"]

    mensaje = "📊 Reporte general:\n"
    for categoria, valores in saldos.items():
        saldo = valores["ingreso"] - valores["gasto"]
        mensaje += f"• {categoria}: {saldo:.2f}\n"
    return mensaje

@app.post("/")
async def telegram_webhook(req: Request):
    try:
        body = await req.json()
        message = body.get("message", {})
        chat_id = message["chat"]["id"]
        text = message.get("text", "").strip()

        if not text:
            return JSONResponse(content={})

        if text.startswith("+") or text.startswith("-"):
            signo = text[0]
            monto_categoria = text[1:].strip().split(" ")
            if len(monto_categoria) < 2:
                return JSONResponse(content={})

            monto = float(monto_categoria[0])
            categoria = " ".join(monto_categoria[1:]).lower()
            tipo = "ingreso" if signo == "+" else "gasto"

            guardar_movimiento(chat_id, tipo, monto, categoria)
            saldo = obtener_saldo(categoria, chat_id)

            respuesta = f"✅ {tipo.title()} de S/ {monto:.2f} registrado en '{categoria}'.\n💰 Saldo actual en '{categoria}': S/ {saldo:.2f}"
            return {"method": "sendMessage", "chat_id": chat_id, "text": respuesta}

        elif text.lower().startswith("reporte de "):
            categoria_reporte = text.lower().replace("reporte de ", "").strip()
            saldo = obtener_saldo(categoria_reporte, chat_id)
            return {"method": "sendMessage", "chat_id": chat_id, "text": f"💼 Saldo en '{categoria_reporte}': S/ {saldo:.2f}"}

        elif text.lower() in ["reporte", "reporte general", "todo"]:
            reporte = obtener_reporte_general(chat_id)
            return {"method": "sendMessage", "chat_id": chat_id, "text": reporte}

        else:
            return {"method": "sendMessage", "chat_id": chat_id, "text": "🤖 Usa +monto categoria para ingresos, -monto categoria para gastos, o pide 'reporte de comida' o 'reporte general'."}

    except Exception as e:
        logger.exception("❌ Error inesperado")
        return JSONResponse(content={"error": "Error interno"}, status_code=500)
