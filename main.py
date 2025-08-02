import os
import logging
from fastapi import FastAPI, Request
from pymongo import MongoClient
from dotenv import load_dotenv
from datetime import datetime
from pydantic import BaseModel
from fastapi.responses import JSONResponse
import certifi

# Cargar variables de entorno
load_dotenv()

# Configurar logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("main")

# Inicializar app FastAPI
app = FastAPI()

# Conexión MongoDB
MONGO_URI = os.getenv("MONGO_URI")
mongo_client = MongoClient(MONGO_URI, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=60000, socketTimeoutMS=60000)
db = mongo_client["telegram_gastos"]
movimientos = db["movimientos"]

# Categorías válidas
CATEGORIAS_VALIDAS = [
    "salud", "limpieza", "alimentacion", "transporte",
    "salidas", "ropa", "plantas", "arreglos casa", "vacaciones"
]

class TelegramMessage(BaseModel):
    message: dict

def guardar_movimiento(chat_id, tipo, monto, categoria, mensaje_original):
    movimiento = {
        "chat_id": chat_id,
        "tipo": tipo,
        "monto": monto,
        "categoria": categoria,
        "mensaje_original": mensaje_original,
        "fecha": datetime.utcnow()
    }
    movimientos.insert_one(movimiento)
    logger.info(f"💾 Guardado: {movimiento}")

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
        mensaje += f"• {categoria}: S/ {saldo:.2f}\n"
    return mensaje

@app.post("/")
async def telegram_webhook(req: Request):
    try:
        body = await req.json()
        message = body.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        text = message.get("text", "").strip()

        if not chat_id or not text:
            logger.warning("❗ Mensaje sin chat_id o texto.")
            return JSONResponse(content={})

        logger.debug(f"📩 Mensaje recibido: {text}")

        if text.startswith("+") or text.startswith("-"):
            signo = text[0]
            partes = text[1:].strip().split(" ")
            if len(partes) < 2:
                return JSONResponse(content={"method": "sendMessage", "chat_id": chat_id,
                                             "text": "❌ Formato incorrecto. Usa +monto categoría."})
            try:
                monto = float(partes[0])
            except ValueError:
                return JSONResponse(content={"method": "sendMessage", "chat_id": chat_id,
                                             "text": "❌ El monto no es válido."})

            categoria = " ".join(partes[1:]).lower()
            if categoria not in CATEGORIAS_VALIDAS:
                return JSONResponse(content={"method": "sendMessage", "chat_id": chat_id,
                                             "text": f"❌ Categoría no válida. Usa una de estas: {', '.join(CATEGORIAS_VALIDAS)}"})

            tipo = "ingreso" if signo == "+" else "gasto"
            guardar_movimiento(chat_id, tipo, monto, categoria, text)
            saldo = obtener_saldo(categoria, chat_id)

            respuesta = f"✅ {tipo.title()} de S/ {monto:.2f} registrado en '{categoria}'.\n💰 Saldo actual en '{categoria}': S/ {saldo:.2f}"
            return {"method": "sendMessage", "chat_id": chat_id, "text": respuesta}

        elif text.lower().startswith("reporte de "):
            categoria = text.lower().replace("reporte de ", "").strip()
            if categoria not in CATEGORIAS_VALIDAS:
                return {"method": "sendMessage", "chat_id": chat_id,
                        "text": f"❌ Categoría no válida. Usa una de estas: {', '.join(CATEGORIAS_VALIDAS)}"}
            saldo = obtener_saldo(categoria, chat_id)
            return {"method": "sendMessage", "chat_id": chat_id,
                    "text": f"💼 Saldo en '{categoria}': S/ {saldo:.2f}"}

        elif text.lower() in ["reporte", "reporte general", "todo"]:
            reporte = obtener_reporte_general(chat_id)
            return {"method": "sendMessage", "chat_id": chat_id, "text": reporte}

        else:
            ayuda = "🤖 Usa:\n+50 comida\n-20 transporte\n'reporte de comida'\n'reporte general'"
            return {"method": "sendMessage", "chat_id": chat_id, "text": ayuda}

    except Exception as e:
        logger.exception("❌ Error inesperado:")
        return JSONResponse(content={"error": "Error interno"}, status_code=500)
