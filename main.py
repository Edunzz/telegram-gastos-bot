from fastapi import FastAPI, Request
import httpx
import os
import json

app = FastAPI()

# Claves de entorno
TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = "mistralai/mistral-7b-instruct"  # puedes cambiarlo si quieres

# Categorías válidas
CATEGORIAS_VALIDAS = [
    "salud", "limpieza", "alimentacion", "transporte",
    "salidas", "ropa", "plantas", "arreglos casa", "vacaciones"
]

# Función para llamar al modelo
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
        "Content-Type": "application/json",
        "HTTP-Referer": "https://tubot.com"  # opcional
    }

    body = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "user", "content": prompt}
        ]
    }

    try:
        response = httpx.post("https://openrouter.ai/api/v1/chat/completions", json=body, headers=headers, timeout=20)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        resultado = json.loads(content)
        return resultado
    except Exception as e:
        return {"error": str(e), "raw": content if 'content' in locals() else ""}

# Endpoint base
@app.get("/")
async def root():
    return {"message": "Bot activo con OpenRouter y variables seguras ✅"}

# Webhook de Telegram
@app.post(f"/{TOKEN}")
async def telegram_webhook(req: Request):
    data = await req.json()
    chat_id = data["message"]["chat"]["id"]
    text = data["message"].get("text", "")

    resultado = procesar_con_openrouter(text)

    if "error" in resultado or resultado.get("categoria") not in CATEGORIAS_VALIDAS:
        respuesta = (
            "⚠️ No pude interpretar tu mensaje correctamente.\n"
            "Asegúrate de incluir una categoría válida y un monto.\n"
            "Categorías disponibles:\n" + "\n".join(f"- {c}" for c in CATEGORIAS_VALIDAS)
        )
    else:
        respuesta = (
            f"🧾 *Movimiento detectado por IA:*\n"
            f"- 💸 Monto: {resultado['monto']} soles\n"
            f"- 🗂️ Categoría: {resultado['categoria']}"
        )

    # Enviar respuesta
    httpx.post(f"{BASE_URL}/sendMessage", json={
        "chat_id": chat_id,
        "text": respuesta,
        "parse_mode": "Markdown"
    })

    return {"ok": True}
