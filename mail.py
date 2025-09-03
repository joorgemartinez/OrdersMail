import os
import smtplib
import ssl
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone, timedelta
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


# Carga .env si existe (útil en local). En GitHub Actions leerá de os.environ (secrets).
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"))
except Exception:
    pass

# --- Config ---
API_KEY     = os.getenv("HOLDED_API_KEY")
USE_BEARER  = os.getenv("HOLDED_USE_BEARER", "false").lower() in ("1","true","yes")

MAIL_FROM   = os.getenv("MAIL_FROM")
MAIL_TO     = os.getenv("MAIL_TO")  # admite varios separados por coma
SMTP_HOST   = os.getenv("SMTP_HOST")
SMTP_PORT   = int(os.getenv("SMTP_PORT", "587"))  # 587 STARTTLS | 465 SSL
SMTP_USER   = os.getenv("SMTP_USER")
SMTP_PASS   = os.getenv("SMTP_PASS")

BASE_URL   = "https://api.holded.com/api/invoicing/v1/documents/salesorder"
PAGE_LIMIT = 200

# Zona horaria Madrid (necesita tzdata instalado en el entorno)
from zoneinfo import ZoneInfo
TZ_MADRID = ZoneInfo("Europe/Madrid")

# --- Helpers API ---
def headers():
    if not API_KEY:
        raise SystemExit("ERROR: falta HOLDED_API_KEY en variables de entorno")
    h = {"Accept": "application/json"}
    if USE_BEARER:
        h["Authorization"] = f"Bearer {API_KEY}"
    else:
        h["key"] = API_KEY
    return h

def fmt_eur(n):
    try:
        v = float(n or 0)
    except Exception:
        return str(n)
    return f"{v:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")

def madrid_yesterday_bounds_epoch_seconds():
    """
    00:00–23:59:59 de AYER en Europe/Madrid -> epoch segundos (UTC).
    """
    now_mad = datetime.now(TZ_MADRID)
    ayer = now_mad - timedelta(days=1)
    start_mad = datetime(ayer.year, ayer.month, ayer.day, 0, 0, 0, tzinfo=TZ_MADRID)
    end_mad   = datetime(ayer.year, ayer.month, ayer.day, 23, 59, 59, tzinfo=TZ_MADRID)
    start_utc = start_mad.astimezone(timezone.utc)
    end_utc   = end_mad.astimezone(timezone.utc)
    return int(start_utc.timestamp()), int(end_utc.timestamp())

def madrid_yesterday_label():
    return (datetime.now(TZ_MADRID) - timedelta(days=1)).strftime("%d/%m/%Y")

def fetch_yesterday_orders():
    start_s, end_s = madrid_yesterday_bounds_epoch_seconds()
    orders = []
    page = 1
    while True:
        params = {"page": page, "limit": PAGE_LIMIT,
                  "starttmp": str(start_s), "endtmp": str(end_s)}
        r = requests.get(BASE_URL, headers=headers(), params=params, timeout=60)
        if r.status_code == 401:
            raise SystemExit(f"401 Unauthorized: {r.text}")
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        orders.extend(batch)
        if len(batch) < PAGE_LIMIT:
            break
        page += 1
    return orders

def epoch_to_local_str(s):
    try:
        return datetime.fromtimestamp(int(s), tz=timezone.utc).astimezone(TZ_MADRID).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(s)

# --- HTML ---
def build_html_table(orders, date_label, total_day):
    if not orders:
        return f"<p>No hay pedidos AYER ({date_label}).</p>"

    rows = []
    for d in orders:
        number   = d.get("number") or d.get("code") or d.get("serial") or "-"
        customer = (d.get("customer") or {}).get("name") or d.get("contactName") or "-"
        total    = float(d.get("total", 0) or 0)  # euros
        fecha    = d.get("date") or d.get("createdAt") or d.get("issuedOn") or d.get("updatedAt") or "-"
        fecha_hr = epoch_to_local_str(fecha) if str(fecha).isdigit() else fecha
        rows.append(
            f"<tr>"
            f"<td style='white-space:nowrap'>{number}</td>"
            f"<td>{customer}</td>"
            f"<td style='text-align:right'>{fmt_eur(total)}</td>"
            f"<td style='white-space:nowrap'>{fecha_hr}</td>"
            f"</tr>"
        )

    rows_html = "\n".join(rows)
    return f"""
    <div style="font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif">
      <h3 style="margin:0 0 8px">Pedidos de AYER — {date_label}</h3>
      <p style="margin:0 0 12px">Total pedidos: <b>{len(orders)}</b> &nbsp;|&nbsp; Importe total: <b>{fmt_eur(total_day)}</b></p>
      <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse">
        <thead><tr><th>Nº</th><th>Cliente</th><th>Total</th><th>Fecha</th></tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
    """

# --- Email ---
def send_email(subject, html):
    missing = [k for k,v in {
        "MAIL_FROM":MAIL_FROM, "MAIL_TO":MAIL_TO, "SMTP_HOST":SMTP_HOST,
        "SMTP_PORT":SMTP_PORT, "SMTP_USER":SMTP_USER, "SMTP_PASS":SMTP_PASS
    }.items() if not v]
    if missing:
        raise SystemExit(f"Faltan variables SMTP en entorno: {', '.join(missing)}")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    msg.attach(MIMEText(html, "html"))

    recipients = [e.strip() for e in (MAIL_TO or "").split(",") if e.strip()]

    try:
        if SMTP_PORT == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=60) as server:
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(MAIL_FROM, recipients, msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(MAIL_FROM, recipients, msg.as_string())
    except smtplib.SMTPAuthenticationError as e:
        raise SystemExit(
            "Autenticación SMTP fallida (535). En Gmail usa una CONTRASEÑA DE APLICACIÓN "
            "y verifica que MAIL_FROM = SMTP_USER."
        ) from e

# --- Main ---
def main():
    date_label = madrid_yesterday_label()
    orders = fetch_yesterday_orders()

    print(f"Pedidos de AYER ({date_label}): {len(orders)}\n")
    if not orders:
        print("No hubo pedidos ayer.")
        # Si NO quieres email cuando no haya pedidos, comenta estas 3 líneas:
        html = build_html_table(orders, date_label, 0.0)
        subject = f"Pedidos de AYER (0) — {date_label}"
        send_email(subject, html)
        print("Email enviado (0 pedidos).")
        return

    total_day = 0.0
    for d in orders:
        number   = d.get("number") or d.get("code") or d.get("serial") or "-"
        customer = (d.get("customer") or {}).get("name") or d.get("contactName") or "-"
        total    = float(d.get("total", 0) or 0)
        fecha    = d.get("date") or d.get("createdAt") or d.get("issuedOn") or d.get("updatedAt") or "-"
        fecha_hr = epoch_to_local_str(fecha) if str(fecha).isdigit() else fecha
        total_day += total
        print(f"{number:>12} | {customer} | {fmt_eur(total):>12} | {fecha_hr}")

    print("\n" + "-"*60)
    print(f"TOTAL del día (AYER): {fmt_eur(total_day)}")
    print("-"*60)

    html = build_html_table(orders, date_label, total_day)
    subject = f"Pedidos de AYER ({len(orders)}) — {date_label} — Total {fmt_eur(total_day)}"
    send_email(subject, html)
    print("Email enviado.")

if __name__ == "__main__":
    main()
