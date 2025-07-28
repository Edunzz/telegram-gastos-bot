from fastapi import FastAPI, Request
import httpx
import os
import json
from pymongo import MongoClient
from datetime import datetime

app = FastAPI()

# Variables de entorno
TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = "mistralai/mistral-7b-instruct"
MONGO_URI = os.getenv("MONGO_URI")

# Categorías válidas
CATEGORIAS_VALIDAS = [
    "salud", "limpieza", "alimentacion", "transporte",
    "salidas", "ropa", "plantas", "arreglos casa", "vacaciones"
]

# MongoDB
client = MongoClient(MONGO_URI)
db = client["gastos_bot"]
movimientos = db["movimientos"]

# Detectar si es un mensaje de reporte
def es_reporte(texto: str):
    texto = texto.lower()
    for categoria in CATEGORIAS_VALIDAS:
        if categoria in texto and ("reporte" in texto or "estado" in texto or "saldo" in texto or "cuánto" in texto):
            return categoria
    if "reporte" in texto and ("todo" in texto or "general" in texto or "categorías" in texto):
        return "general"
    return None

# Llamar a OpenRouter
def procesar_con_openrouter(texto_usuario: str):
    prompt = f"""
Extrae el monto y la categoría de gasto desde el siguiente texto. La categoría debe estar dentro del siguiente listado: salud, limpieza, alimentacion, transporte, salidas, ropa, plantas, arreglos casa, vacaciones.

Si el texto indica que se debe agregar dinero, el monto debe ser positivo.
Si el texto indica que se debe resetear, el monto debe ser 0.
Si el texto indica que es un gasto, el monto debe ser negativo.

Devuelve solo un JSON con las claves: "monto" (número) y "categoria" (texto exacto del listado). Nada más.

Texto: "{texto_usuario}"
"""
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    body = {
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}]
    }

    try:
        response = httpx.post("https://openrouter.ai/api/v1/chat/completions", json=body, headers=headers, timeout=20)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        return {"error": str(e)}

# Guardar un movimiento
def guardar_movimiento(categoria, monto, concepto, chat_id):
    movimientos.insert_one({
        "categoria": categoria,
        "monto": monto,
        "concepto": concepto,
        "chat_id": chat_id,
        "fecha": datetime.utcnow()
    })

# Obtener saldo por categoría
def obtener_saldo(categoria, chat_id):
    pipeline = [
        {"$match": {"categoria": categoria, "chat_id": chat_id}},
        {"$group": {"_id": "$categoria", "total": {"$sum": "$monto"}}}
    ]
    result = list(movimientos.aggregate(pipeline))
    return result[0]["total"] if result else 0

# Obtener reporte general
def obtener_reporte_general(chat_id):
    pipeline = [
        {"$match": {"chat_id": chat_id}},
        {"$group": {"_id": "$categoria", "total": {"$sum": "$monto"}}}
    ]
    result = list(movimientos.aggregate(pipeline))
    return {r["_id"]: r["total"] for r in result}

@app.get("/")
async def root():
    return {"message": "Bot activo con MongoDB y OpenRouter"}

@app.post(f"/{TOKEN}")
async def telegram_webhook(req: Request):
    data = await req.json()
    chat_id = data["message"]["chat"]["id"]
    text = data["message"].get("text", "")

    categoria_reporte = es_reporte(text)

    if categoria_reporte == "general":
        saldos = obtener_reporte_general(chat_id)
        if not saldos:
            respuesta = "📉 No hay movimientos registrados aún."
        else:
            respuesta = "📊 *Reporte general:*\n" + "\n".join(
                f"- {cat}: {round(saldos.get(cat, 0), 2)} soles"
                for cat in CATEGORIAS_VALIDAS if cat in saldos
            )

    elif categoria_reporte:
        saldo = obtener_saldo(categoria_reporte, chat_id)
        respuesta = f"📊 *Saldo actual de `{categoria_reporte}`:* {round(saldo, 2)} soles"

    else:
        resultado = procesar_con_openrouter(text)

        if "error" in resultado or resultado.get("categoria") not in CATEGORIAS_VALIDAS:
            respuesta = (
                "⚠️ No pude interpretar tu mensaje correctamente.\n"
                "Incluye un monto y una categoría válida.\n"
                "Categorías disponibles:\n" + "\n".join(f"- {c}" for c in CATEGORIAS_VALIDAS)
            )
        else:
            categoria = resultado["categoria"]
            monto = resultado["monto"]
            guardar_movimiento(categoria, monto, text, chat_id)
            saldo = obtener_saldo(categoria, chat_id)

            respuesta = (
                f"🧾 *Movimiento guardado:*\n"
                f"- 💸 Monto: {monto} soles\n"
                f"- 🗂️ Categoría: {categoria}\n"
                f"📌 *Saldo actual:* {round(saldo, 2)} soles"
            )

    httpx.post(f"{BASE_URL}/sendMessage", json={
        "chat_id": chat_id,
        "text": respuesta,
        "parse_mode": "Markdown"
    })

    return {"ok": True}
