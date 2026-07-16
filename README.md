# 🤖 telegram-gastos-bot

Bot de Telegram escrito en **Python (FastAPI)** que registra y reporta **gastos e ingresos por categoría**, interpretando mensajes en lenguaje natural mediante **OpenAI**. Los datos se persisten en **MongoDB**.

## ✨ Funcionalidades

- **Registro por lenguaje natural**: escribe "gasté 50 en transporte" o "ahorré 20 para salud" y el bot interpreta tipo, monto y categoría automáticamente.
- **Reporte general**: muestra el saldo de todas las categorías, ordenadas alfabéticamente (insensible a mayúsculas/tildes).
- **Reporte por categoría**: muestra el saldo total de una categoría junto con sus últimos movimientos (5 por defecto, configurable).
- **Categorías personalizadas**: permite crear nuevas categorías desde el propio chat, con validación de duplicados y nombres vacíos.
- **Eliminar movimientos** por ID.
- **Exportar datos** vía endpoint HTTP protegido por clave.
- **Mensajes con formato Markdown**: negritas, emojis y separadores para una lectura clara en Telegram.

## 🗂️ Categorías por defecto

```
salud, skincare, limpieza, alimentacion, transporte, salidas,
ropa, plantas, arreglos casa, vacaciones, visita familia, festividades
```

Se pueden agregar más desde el bot (ver comandos abajo); las nuevas quedan persistidas en MongoDB y aparecen automáticamente en los reportes.

## 💬 Comandos y ejemplos de uso

| Acción | Ejemplo de mensaje |
|---|---|
| Registrar gasto | `gasté 50 en transporte` |
| Registrar ingreso | `ahorré 20 para salud` |
| Reporte general | `reporte general` |
| Reporte de una categoría | `reporte de ropa` |
| Reporte con N movimientos | `reporte ropa 10` |
| Crear categoría | `/nueva_categoria <nombre>` (también `/agregar_categoria <nombre>`) |
| Eliminar movimiento | `eliminar <ID>` |
| Ver ayuda | `Opciones` / `info` |

## 🏗️ Arquitectura

- **`main.py`** — API FastAPI que expone el webhook de Telegram (`POST /{TOKEN}`), interpreta el mensaje con OpenAI, guarda/consulta movimientos en MongoDB y responde vía la API de Telegram.
- **`requirements.txt`** — dependencias del proyecto.
- **`Spacefile`** — configuración de despliegue en [Deta Space](https://deta.space/) (`entrypoint = "main:app"`).

### Flujo de un mensaje

1. Telegram envía el update al webhook `POST /{TOKEN}`.
2. Si el texto coincide con `/nueva_categoria` o `/agregar_categoria`, se crea la categoría directamente (sin pasar por OpenAI).
3. En cualquier otro caso, el texto se envía a OpenAI (`procesar_con_openai`), que devuelve un JSON con `tipo`, `monto` y `categoria`.
4. Según el `tipo` (`gasto`, `ingreso`, `reporte`, `info`, `eliminar`), el bot ejecuta la acción correspondiente contra MongoDB.
5. El bot responde al usuario vía `sendMessage` de la API de Telegram, con formato Markdown.

### Colecciones de MongoDB (`telegram_gastos`)

- **`movimientos`**: cada gasto/ingreso registrado (`chat_id`, `tipo`, `monto`, `categoria`, `mensaje_original`, `fecha`).
- **`categorias`**: categorías creadas por los usuarios (además de las categorías base definidas en el código).

## ⚙️ Variables de entorno

| Variable | Descripción |
|---|---|
| `BOT_TOKEN` | Token del bot de Telegram. |
| `MONGO_URI` | Cadena de conexión a MongoDB. |
| `OPENAI_API_KEY` | API key de OpenAI. |
| `OPENAI_MODEL` | Modelo de OpenAI a usar (default: `gpt-5-mini`). |
| `GOOGLE_SHEET_URL` | (Opcional) enlace a una hoja de cálculo mostrado en los reportes. |
| `EXPORT_PASS` | Clave requerida para usar el endpoint `/exportar` (default: `0000`). |

Crea un archivo `.env` en la raíz del proyecto con estas variables antes de ejecutar el bot localmente.

## 🚀 Ejecutar localmente

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

Configura el webhook de Telegram para que apunte a tu URL pública + `/{BOT_TOKEN}` (por ejemplo usando [ngrok](https://ngrok.com/) en desarrollo).

## 📤 Endpoints

- `GET /` — health check, confirma que el bot está activo.
- `POST /{TOKEN}` — webhook que recibe los updates de Telegram.
- `GET /exportar?clave=...&desde=...&hasta=...` — exporta los movimientos registrados en JSON (filtrable por rango de fechas).

## ☁️ Despliegue

El proyecto está preparado para desplegarse en **Deta Space** mediante el `Spacefile` incluido.
