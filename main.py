from fastapi import FastAPI, Request
import httpx
import os

app = FastAPI()

TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

@app.get("/")
async def root():
    return {"message": "Bot activo!"}

@app.post(f"/{TOKEN}")
async def telegram_webhook(req: Request):
    data = await req.json()
    chat_id = data["message"]["chat"]["id"]
    text = data["message"].get("text", "")

    # Respuesta simple
    await httpx.post(f"{BASE_URL}/sendMessage", json={
        "chat_id": chat_id,
        "text": f"📩 Recibí tu mensaje: {text}"
    })

    return {"ok": True}
