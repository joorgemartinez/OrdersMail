# 📧 Daily Holded Orders & Invoices Report

Script en **Python** que consulta los **Pedidos de Venta (Sales Orders) y Facturas (Invoices) en Holded** del día anterior (zona horaria Madrid) y envía un **reporte por email** con una tabla en HTML.  
El envío puede ejecutarse **manualmente en local** o de forma **automática cada mañana con GitHub Actions**.

---

## 🚀 ¿Qué hace?

- Consulta la API de **Holded** para obtener:
  - **Pedidos** del día anterior
  - **Facturas** del día anterior
- Convierte los resultados en **dos tablas HTML** (una para pedidos y otra para facturas) con:
  - Nº de pedido / factura  
  - Cliente  
  - Importe total (€)  
  - Fecha del documento
- Envía un **correo electrónico** con el resumen:
  - **Asunto**:
    ```
    Pedidos (X) y Facturas (Y) — DD/MM/YYYY
    ```
  - **Cuerpo**: dos tablas (Pedidos + Facturas)
- Si no hubo pedidos o facturas, aparece una sección indicando **"No hay pedidos"** o **"No hay facturas"**  
  *(esto se puede desactivar comentando líneas en `main()`)*.

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

Pedidos (5) y Facturas (3) — 14/09/2025


**Cuerpo**:

### Pedidos
| Nº     | Cliente    | Total      | Fecha              |
|--------|------------|------------|--------------------|
| SO-101 | Cliente A  | 1.200,00 € | 2025-09-14 09:15:00 |
| SO-102 | Cliente B  |   950,00 € | 2025-09-14 11:20:00 |

### Facturas
| Nº     | Cliente    | Total      | Fecha              |
|--------|------------|------------|--------------------|
| INV-55 | Cliente A  |  500,00 €  | 2025-09-14 12:00:00 |
| INV-56 | Cliente C  |  750,00 €  | 2025-09-14 13:30:00 |

---

## 📝 Notas

- La hora de corte es **00:00–23:59 Madrid**, gracias a `zoneinfo`.
- El script tolera distintas claves de documento: `number`, `docNumber`, `code`, `serial`.
- En Gmail, recuerda usar una **Contraseña de aplicación** y asegurarte de que `MAIL_FROM = SMTP_USER`.

---

## ⏸️ Desactivar el workflow

Si no quieres que GitHub Actions lo ejecute automáticamente (por ejemplo, mientras haces pruebas o si no has configurado las variables de entorno), tienes varias opciones:

1. **Deshabilitarlo desde GitHub**  
   - Entra en la pestaña **Actions** → selecciona el workflow → botón **…** → **Disable workflow**.

2. **Editar el trigger en el YAML**  
   - Comenta o elimina la parte `schedule:` para que no se lance cada día.  
   - Ejemplo:
     ```yaml
     on:
       # schedule:
       #   - cron: "0 6 * * *"
       workflow_dispatch:
     ```
     De esta forma solo podrá ejecutarse manualmente.

3. **Bloquear el job**  
   - Añade un guardado en el YAML para que nunca corra:
     ```yaml
     jobs:
       run-script:
         if: ${{ false }}
         runs-on: ubuntu-latest
         steps:
           - run: echo "Workflow deshabilitado"
     ```

4. **Renombrar/mover el archivo**  
   - Si renombrás el archivo a algo distinto de `.yml` o lo mueves fuera de `.github/workflows/`, GitHub Actions lo ignorará.

Así evitas errores por variables de entorno faltantes hasta que quieras volver a habilitarlo.
