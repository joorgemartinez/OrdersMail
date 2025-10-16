import os
import smtplib
import ssl
import requests
import json
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# --- Cargar .env ---
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"))  # Busca .env en la misma carpeta que mail.py
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

BASE_URL_ORDERS   = "https://api.holded.com/api/invoicing/v1/documents/salesorder"
BASE_URL_INVOICES = "https://api.holded.com/api/invoicing/v1/documents/invoice"
PAGE_LIMIT = 200

# Archivos de estado
STATE_FILE_INVOICES = Path(".state/processed_invoices.json")

# Zona horaria Madrid
TZ_MADRID = ZoneInfo("Europe/Madrid")

# --- Helpers ---
def _as_float(x, default=0.0):
    """Convierte a float con tolerancia a str/None/locale."""
    try:
        if x is None:
            return default
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip().replace(",", ".")
        return float(s)
    except Exception:
        return default

def _norm_text(x):
    """Normaliza a texto minúscula sin espacios. Si no es convertible, devuelve cadena vacía."""
    if x is None:
        return ""
    try:
        return str(x).strip().lower()
    except Exception:
        return ""

MONTHS_ES = ["", "enero","febrero","marzo","abril","mayo","junio","julio","agosto","septiembre","octubre","noviembre","diciembre"]

def month_name_es(dt: datetime) -> str:
    """Devuelve el nombre del mes en español con mayúscula inicial."""
    try:
        return MONTHS_ES[dt.month].capitalize()
    except Exception:
        return ""

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
    now_mad = datetime.now(TZ_MADRID)
    ayer = now_mad - timedelta(days=1)
    start_mad = datetime(ayer.year, ayer.month, ayer.day, 0, 0, 0, tzinfo=TZ_MADRID)
    end_mad   = datetime(ayer.year, ayer.month, ayer.day, 23, 59, 59, tzinfo=TZ_MADRID)
    start_utc = start_mad.astimezone(timezone.utc)
    end_utc   = end_mad.astimezone(timezone.utc)
    return int(start_utc.timestamp()), int(end_utc.timestamp())

def madrid_yesterday_label():
    return (datetime.now(TZ_MADRID) - timedelta(days=1)).strftime("%d/%m/%Y")

def epoch_to_local_str(s):
    """Convierte epoch (s o ms) a cadena local. Si no es epoch, devuelve str(s)."""
    try:
        si = int(str(s))
        if si >= 10**12:  # milisegundos
            si = si // 1000
        return datetime.fromtimestamp(int(si), tz=timezone.utc).astimezone(TZ_MADRID).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(s)

def doc_number(d: dict) -> str:
    return (
        d.get("number")
        or d.get("docNumber")
        or d.get("code")
        or d.get("serial")
        or d.get("_id")
        or d.get("id")
        or "-"
    )

# --- Totales (sin IVA) ---
def get_subtotal(doc: dict) -> float:
    """
    Obtiene la base imponible (sin IVA) de un documento Holded.
    Estrategia:
      1) Busca claves típicas en raíz y en 'totals'
      2) Si no, suma líneas (precio*qty - descuentos)
      3) Si no, usa total - impuesto
    """
    if not isinstance(doc, dict):
        return 0.0

    candidate_keys = (
        "subtotal", "subTotal", "taxBase", "base", "baseAmount",
        "untaxed", "untaxedAmount", "totalNoTax", "total_without_tax",
        "totalWithoutTax", "net", "netAmount", "amountNet"
    )
    # 1) Raíz
    for k in candidate_keys:
        if k in doc:
            return _as_float(doc.get(k))

    # 1b) totals{}
    totals = doc.get("totals") or {}
    if isinstance(totals, dict):
        for k in candidate_keys:
            if k in totals:
                return _as_float(totals.get(k))

    # 2) Sumar líneas
    lines = doc.get("lines") or doc.get("products") or []
    if isinstance(lines, list) and lines:
        base_sum = 0.0
        for ln in lines:
            if not isinstance(ln, dict):
                continue
            # Si la línea trae base, úsala
            for lk in ("subtotal", "subTotal", "base", "amountNet", "totalNoTax", "net"):
                if lk in ln:
                    base_sum += _as_float(ln.get(lk))
                    break
            else:
                price = _as_float(ln.get("price") or ln.get("unitPrice") or ln.get("unit_price") or ln.get("amount"))
                qty   = _as_float(ln.get("quantity") or ln.get("qty") or ln.get("units") or 1)
                line  = price * qty
                # Descuento %
                disc_pct = _as_float(ln.get("discount") or ln.get("discountRate") or ln.get("discountPercent"))
                if disc_pct:
                    line *= (1 - (disc_pct if disc_pct <= 1 else disc_pct/100.0))
                # Descuento absoluto
                disc_abs = _as_float(ln.get("discountAmount") or ln.get("discount_amount"))
                line -= disc_abs
                if line < 0:
                    line = 0.0
                base_sum += line
        return base_sum

    # 3) total - impuesto
    total = _as_float(doc.get("total") or (totals.get("total") if isinstance(totals, dict) else 0))
    tax   = _as_float(
        doc.get("tax") or doc.get("taxAmount") or doc.get("vatAmount")
        or (totals.get("tax") if isinstance(totals, dict) else 0)
        or (totals.get("taxAmount") if isinstance(totals, dict) else 0)
        or (totals.get("vatAmount") if isinstance(totals, dict) else 0)
    )
    if total:
        return max(total - tax, 0.0)
    return 0.0

# --- Estado facturas ---
def load_processed_invoices():
    if STATE_FILE_INVOICES.exists():
        try:
            return set(json.loads(STATE_FILE_INVOICES.read_text()))
        except Exception:
            return set()
    return set()

def save_processed_invoices(ids):
    STATE_FILE_INVOICES.parent.mkdir(exist_ok=True)
    STATE_FILE_INVOICES.write_text(json.dumps(sorted(ids), ensure_ascii=False, indent=2))

# --- API genérica por rango ---
def fetch_range(base_url, start_s, end_s):
    """Obtiene documentos en un rango de tiempo (epoch segundos UTC)."""
    items, page = [], 1
    while True:
        params = {"page": page, "limit": PAGE_LIMIT, "starttmp": str(start_s), "endtmp": str(end_s)}
        r = requests.get(base_url, headers=headers(), params=params, timeout=60)
        if r.status_code == 401:
            raise SystemExit(f"401 Unauthorized: {r.text}")
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        if isinstance(batch, dict):
            raise SystemExit(f"Respuesta inesperada de API: {batch}")
        items.extend(batch)
        if len(batch) < PAGE_LIMIT:
            break
        page += 1
    return items

def fetch_yesterday(base_url):
    start_s, end_s = madrid_yesterday_bounds_epoch_seconds()
    return fetch_range(base_url, start_s, end_s)

def fetch_last_days(base_url, days=10):
    now_mad = datetime.now(TZ_MADRID)
    start_mad = now_mad - timedelta(days=days)
    start_utc = datetime(start_mad.year, start_mad.month, start_mad.day, 0, 0, 0, tzinfo=TZ_MADRID).astimezone(timezone.utc)
    end_utc   = now_mad.astimezone(timezone.utc)
    start_s, end_s = int(start_utc.timestamp()), int(end_utc.timestamp())
    return fetch_range(base_url, start_s, end_s)

# --- Rangos MTD / YTD ---
def month_bounds_epoch_seconds_now():
    now_mad = datetime.now(TZ_MADRID)
    start_mad = datetime(now_mad.year, now_mad.month, 1, 0, 0, 0, tzinfo=TZ_MADRID)
    end_mad   = now_mad  # hasta ahora
    return int(start_mad.astimezone(timezone.utc).timestamp()), int(end_mad.astimezone(timezone.utc).timestamp())

def year_bounds_epoch_seconds_now():
    now_mad = datetime.now(TZ_MADRID)
    start_mad = datetime(now_mad.year, 1, 1, 0, 0, 0, tzinfo=TZ_MADRID)
    end_mad   = now_mad  # hasta ahora
    return int(start_mad.astimezone(timezone.utc).timestamp()), int(end_mad.astimezone(timezone.utc).timestamp())

# --- Filtro facturas "finalizadas" robusto ---
def is_invoice_finalized(inv: dict) -> bool:
    """
    Consideramos facturas 'válidas' para facturación acumulada si NO están en borrador ni anuladas.
    Acepta status en texto o codificado como número.
    """
    raw = inv.get("status") or inv.get("state") or inv.get("docStatus") or inv.get("statusCode")
    status_txt = _norm_text(raw)

    # Flags explícitos tipo boolean
    if any(inv.get(k) in (True, "true", 1, "1") for k in ("cancelled", "canceled", "isCanceled", "void", "voided")):
        return False

    # Si es numérico: heurística común (ajustable si conoces los códigos de Holded)
    if isinstance(raw, (int, float)):
        code = int(raw)
        if code in (0,):     # borrador
            return False
        if code in (9, 99):  # anulada/void
            return False
        # otros códigos -> se consideran finalizadas

    # Si es texto, busca tokens típicos
    if any(tok in status_txt for tok in ("cancel", "anul", "void")):
        return False
    if any(tok in status_txt for tok in ("draft", "borrador", "temp")):
        return False

    return True

def subtotal_sum_finalized(invoices):
    total = 0.0
    for inv in invoices:
        if is_invoice_finalized(inv):
            total += get_subtotal(inv)
    return total

# --- HTML ---
def build_html_table(items, date_label, base_sum, titulo, subtitulo):
    if not items:
        return f"<p>No hay {titulo.lower()} nuevos hasta {date_label}.</p>"

    rows = []
    for d in items:
        number   = doc_number(d)
        customer = (d.get("customer") or {}).get("name") or d.get("contactName") or "-"
        subtotal = get_subtotal(d)
        fecha    = d.get("date") or d.get("createdAt") or d.get("issuedOn") or d.get("updatedAt") or "-"
        fecha_hr = epoch_to_local_str(fecha) if str(fecha).isdigit() else fecha
        rows.append(
            f"<tr>"
            f"<td style='white-space:nowrap'>{number}</td>"
            f"<td>{customer}</td>"
            f"<td style='text-align:right'>{fmt_eur(subtotal)}</td>"
            f"<td style='white-space:nowrap'>{fecha_hr}</td>"
            f"</tr>"
        )

    rows_html = "\n".join(rows)
    return f"""
    <div style="font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif">
      <h3 style="margin:0 0 8px">{titulo} nuevos — hasta {date_label}</h3>
      <p style="margin:0 0 12px">Total {subtitulo}: <b>{len(items)}</b> &nbsp;|&nbsp; Base imponible total: <b>{fmt_eur(base_sum)}</b></p>
      <table border="1" cellspacing="0" cellpadding="6" style="border-collapse:collapse">
        <thead><tr><th>Nº</th><th>Cliente</th><th>Subtotal (sin IVA)</th><th>Fecha</th></tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
    """

def build_html_summary_mtd_ytd(mtd_total, ytd_total, mes_nombre):
    return f"""
    <div style="font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;margin:0 0 16px">
      <h3 style="margin:0 0 8px">Facturación acumulada (sin IVA)</h3>
      <ul style="margin:0 0 0 18px;padding:0">
        <li><b>Mes en curso</b> ({mes_nombre}): <b>{fmt_eur(mtd_total)}</b></li>
        <li><b>Año en curso</b>: <b>{fmt_eur(ytd_total)}</b></li>
      </ul>
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

# --- Print helper ---
def print_section(items, date_label, titulo):
    print(f"{titulo} — hasta {date_label}: {len(items)}\n")
    base_sum = 0.0
    for d in items:
        number   = doc_number(d)
        customer = (d.get("customer") or {}).get("name") or d.get("contactName") or "-"
        subtotal = get_subtotal(d)
        fecha    = d.get("date") or d.get("createdAt") or d.get("issuedOn") or d.get("updatedAt") or "-"
        fecha_hr = epoch_to_local_str(fecha) if str(fecha).isdigit() else fecha
        base_sum += subtotal
        print(f"{number:>12} | {customer} | {fmt_eur(subtotal):>12} | {fecha_hr}")
    print("\n" + "-"*60)
    print(f"BASE IMPONIBLE TOTAL: {fmt_eur(base_sum)}")
    print("-"*60)
    return base_sum

# --- Main ---
def main():
    date_label = madrid_yesterday_label()

    # Pedidos de AYER
    orders = fetch_yesterday(BASE_URL_ORDERS)
    base_orders = print_section(orders, date_label, "Pedidos")

    # Facturas últimos 10 días (nuevas según estado)
    invoices_all = fetch_last_days(BASE_URL_INVOICES, days=10)
    processed_invoices = load_processed_invoices()
    new_invoices = [inv for inv in invoices_all if doc_number(inv) not in processed_invoices]

    base_invoices = print_section(new_invoices, date_label, "Facturas NUEVAS")

    # --- Acumulado MTD / YTD (sin IVA) ---
    # Rango mes en curso
    m_start_s, m_end_s = month_bounds_epoch_seconds_now()
    invoices_mtd = fetch_range(BASE_URL_INVOICES, m_start_s, m_end_s)
    mtd_total = subtotal_sum_finalized(invoices_mtd)

    # Rango año en curso
    y_start_s, y_end_s = year_bounds_epoch_seconds_now()
    invoices_ytd = fetch_range(BASE_URL_INVOICES, y_start_s, y_end_s)
    ytd_total = subtotal_sum_finalized(invoices_ytd)

    # Print resumen MTD/YTD (sin fechas)
    now_mad = datetime.now(TZ_MADRID)
    mes_nombre = month_name_es(now_mad)

    print("\nFACTURACIÓN ACUMULADA (sin IVA)")
    print(f"Mes en curso ({mes_nombre}): {fmt_eur(mtd_total)}")
    print(f"Año en curso: {fmt_eur(ytd_total)}\n")

    # Email
    html_summary = build_html_summary_mtd_ytd(mtd_total, ytd_total, mes_nombre)
    html_orders   = build_html_table(orders, date_label, base_orders, "Pedidos", "pedidos")
    html_invoices = build_html_table(new_invoices, date_label, base_invoices, "Facturas", "facturas")

    html = html_summary + html_orders + "<br><br>" + html_invoices
    subject = f"Pedidos ({len(orders)}) y Facturas nuevas ({len(new_invoices)}) — {date_label}"
    send_email(subject, html)

    print("Email enviado.")

    # Guardar estado de facturas (por número/código como antes)
    all_ids = processed_invoices.union(doc_number(inv) for inv in invoices_all)
    save_processed_invoices(all_ids)

if __name__ == "__main__":
    main()
