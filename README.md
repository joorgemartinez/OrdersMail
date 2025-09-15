# 📧 Daily Holded Orders Report

Script en **Python** que consulta los **pedidos de venta (Sales Orders) en Holded** del día anterior (zona horaria Madrid) y envía un **reporte por email** con una tabla en HTML.  
El envío puede ejecutarse **manualmente en local** o de forma **automática cada mañana con GitHub Actions**.

---

## 🚀 ¿Qué hace?

- Consulta la API de **Holded** para obtener los pedidos de **ayer**.
- Convierte los resultados en una tabla HTML con:
  - Nº de pedido  
  - Cliente  
  - Importe total (€)  
  - Fecha del pedido  
- Envía un **correo electrónico** con el resumen:
  - Asunto → `Pedidos de AYER (X) — DD/MM/YYYY — Total XXX €`
  - Cuerpo → tabla con todos los pedidos
- Si no hubo pedidos, también envía un email indicando **"0 pedidos"** (esto se puede desactivar comentando 3 líneas en `main()`).

---

## 🛠️ Requisitos

- Python **3.9+** (probado con 3.13 en GitHub Actions).
- Librerías:
  ```bash
  pip install -r requirements.txt
    ```
    Donde `requirements.txt` incluye:
  
    ```bash
    requests
    python-dotenv
    ```
---
## ⚙️ Configuración

El script necesita varias variables de entorno (se leen desde `.env` en local o `secrets` en GitHub Actions):

| Variable          | Descripción |
|-------------------|-------------|
| `HOLDED_API_KEY`  | API Key de Holded |
| `HOLDED_USE_BEARER` | `true` si la API usa Bearer token, `false` para `key` |
| `MAIL_FROM`       | Dirección remitente (ej. `report@tuempresa.com`) |
| `MAIL_TO`         | Destinatarios (varios separados por coma) |
| `SMTP_HOST`       | Servidor SMTP (ej. `smtp.gmail.com`) |
| `SMTP_PORT`       | Puerto SMTP (`587` STARTTLS o `465` SSL) |
| `SMTP_USER`       | Usuario SMTP |
| `SMTP_PASS`       | Contraseña o **App Password** |


### Ejemplo `.env` para pruebas locales

```ini
HOLDED_API_KEY=tu_api_key
HOLDED_USE_BEARER=true
MAIL_FROM=report@tuempresa.com
MAIL_TO=ventas@tuempresa.com,gerencia@tuempresa.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=report@tuempresa.com
SMTP_PASS=tu_app_password
```
---

## ▶️ Ejecución local

```bash
python mail.py
``` 
Esto imprimirá en consola los pedidos y enviará el email.

---

## 🤖 Automatización con GitHub Actions

Este repositorio incluye un workflow (`.github/workflows/daily-report.yml`) que ejecuta el script **cada día a las 08:00 (Madrid, verano CEST)**.

```yaml
on:
  schedule:
    - cron: "0 6 * * *"   # 06:00 UTC → 08:00 Madrid (verano)
  workflow_dispatch:       # ejecución manual
```

Pasos principales:

- Instala dependencias.
- Lanza `python mail.py`.
- Usa los `secrets` configurados en el repositorio para las variables sensibles.

---

## 📬 Resultado del email

Ejemplo de correo recibido:

**Asunto**:

Pedidos de AYER (5) — 14/09/2025 — Total 12.345,67 €

**Cuerpo**:

| Nº     | Cliente    | Total      | Fecha              |
|--------|------------|------------|--------------------|
| SO-101 | Cliente A  | 1.200,00 € | 2025-09-14 09:15:00 |
| SO-102 | Cliente B  |   950,00 € | 2025-09-14 11:20:00 |
| …      | …          | …          | …                  |

---

## 📝 Notas

- La hora de corte es **00:00–23:59 Madrid**, gracias a `zoneinfo`.
- El script tolera distintas claves de pedido: `number`, `code` o `serial`.
- En Gmail, recuerda usar una **Contraseña de aplicación** y asegurarte de que `MAIL_FROM = SMTP_USER`.
