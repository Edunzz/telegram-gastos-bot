## 🧠 ¿Qué hace?
- Interpreta mensajes en lenguaje natural (vía OpenRouter) y registra **gastos/ingresos** en MongoDB.
- **Onboarding automático**: primer mensaje pide *crear* o *unir* grupo.
- **Aislamiento por `group_code`**: cada grupo ve solo sus movimientos y reportes.
- **Exportar a Excel** por comando en Telegram y por HTTP.
- Eliminar movimientos por ID (valida que el ID pertenezca al grupo).
- Índices en Mongo para escalar.

## ✨ Diferencias vs `main.py`
| Aspecto | `main.py` | `main-multisala.py` |
|---|---|---|
| Tenancy | Único libro global | **Multi-tenant** por `group_code` |
| Onboarding | No aplica | `crear <nombre>` / `unir <CÓDIGO>` |
| Exportación | JSON (`/exportar`) | JSON + **Excel** (`/exportar_excel` y `reporte excel`) |
| Escalabilidad | Básica | Índices por grupo y fecha |
| Aislamiento de datos | Compartido | **Aislado por grupo** |

## 💬 Comandos de Telegram
- **Onboarding**  
  - `crear <nombre>` → Crea grupo y te asigna.  
  - `unir <CÓDIGO>` → Te une a un grupo existente.
- **Operación**  
  - `gasté 50 en transporte`  
  - `ahorré 100 para salud`  
  - `reporte de ropa` / `reporte general`  
  - `eliminar <ID>`  
  - `reporte excel`  
  - `reporte excel 01.08.2025 09.08.2025`  
  - `reporte excel desde 01/08/2025 hasta 09/08/2025`  
  - `info` (muestra tu grupo y ayuda)

## 🌐 Endpoints
- `GET /` → Ping del servicio.
- `POST /{TOKEN}` → Webhook Telegram.
- `GET /exportar?clave=XXXX&group=AB3KZ9&desde=01.08.2025&hasta=09.08.2025` → JSON.
- `GET /exportar_excel?clave=XXXX&group=AB3KZ9&desde=01.08.2025&hasta=09.08.2025` → Descarga **.xlsx**.

### Formatos de fecha aceptados
`dd.mm.yyyy`, `dd/mm/yyyy`, `dd-mm-yyyy`, `yyyy-mm-dd` (interpretación **day-first**).

## 🧱 Modelo de datos y partición
- `usuarios`: `{ chat_id, group_code, created_at }`
- `grupos`: `{ code, name, owner_chat_id, members[], created_at }`
- `movimientos`: `{ chat_id, group_code, tipo, monto, categoria, mensaje_original, fecha }`

**Índices (incluidos en el código):**
- `movimientos`: `[(group_code, categoria, tipo, fecha)]`, `[(group_code, fecha)]`
- `usuarios`: `[(chat_id)]` único
- `grupos`: `[(code)]` único

## 🔧 Variables de entorno
- `BOT_TOKEN` (obligatorio)
- `MONGO_URI` (obligatorio)
- `OPENROUTER_API_KEY` (obligatorio)
- `OPENROUTER_MODEL` (opcional, por defecto `mistralai/mistral-7b-instruct`)
- `GROUP_CODE_LENGTH` (opcional, por defecto `6`)
- `EXPORT_PASS` (opcional, por defecto `0000`)

## 📦 Dependencias (requirements.txt sugerido)
```
fastapi==0.111.0
uvicorn==0.30.1
pymongo==4.7.3
python-dateutil==2.9.0
httpx==0.27.0
python-dotenv==1.0.1
certifi==2024.7.4
openpyxl==3.1.5
```
> Python 3.11 recomendado.

## 🖥️ Correr local (quick start)
1. `python -m venv .venv && source .venv/bin/activate` (Windows: `.\.venv\Scriptsctivate`)
2. `pip install -r requirements.txt`
3. Crea `.env` con las variables de entorno.
4. **Renombra** el archivo a `main_multisala.py` (sin guion).
5. `uvicorn main_multisala:app --host 0.0.0.0 --port 8000`
6. (Opcional) Exponer con ngrok y setear webhook:
   ```bash
   ngrok http 8000
   curl -F "url=https://TU-NGROK/{BOT_TOKEN}" https://api.telegram.org/bot$BOT_TOKEN/setWebhook
   ```

## ☁️ Despliegue en Render (paso a paso)
1. **New Web Service** → Conecta el repo.
2. **Build Command**: `pip install -r requirements.txt`
3. **Start Command**:  
   ```
   uvicorn main_multisala:app --host 0.0.0.0 --port $PORT
   ```
4. **Environment**: agrega todas las variables (token, Mongo, OpenRouter…).
5. **Instance type**: empieza con Basic 512 MB. Sube CPU/RAM si el bot crece.
6. MongoDB Atlas: habilita IP de Render (o `0.0.0.0/0` si no hay egress fijo; ideal VPC egres dedicada).
7. Revisa logs de Render tras hacer `setWebhook` para confirmar 200 OK.

## 📦 Exportación a Excel (detalle)
- Telegram: `reporte excel …` → genera XLSX temporal en **/tmp** y lo envía al chat.
- HTTP: `/exportar_excel` → descarga directa.
- Hoja **movimientos**: `_id, chat_id, group_code, tipo, monto, categoria, mensaje_original, fecha`.
- *(Opcional)* Agregar segunda hoja con **totales por categoría** (ingresos, gastos, saldo) y resumen general.

## 🔐 Seguridad
- Cambia `EXPORT_PASS` por uno robusto y **no** lo compartas.
- Webhook **HTTPS** público (Render ya lo provee).
- Considera rate-limit básico si dejas `/exportar_excel` abierto.

## 🛠️ Troubleshooting
- **Webhook** no responde: confirma que el path sea `/{BOT_TOKEN}`, revisa logs y la URL registrada.
- **Excel** no llega: verifica movimientos en ese `group_code` y el rango de fechas.
- **Mongo** falla: revisa IP allowlist/credenciales en Atlas.
