#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os, sys, json, math, re, argparse, ssl, smtplib
from pathlib import Path
from datetime import datetime, timedelta, timezone
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# --- .env local opcional ---
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"))
except Exception:
    pass

# --- Config ---
API_KEY     = os.getenv("HOLDED_API_KEY")
USE_BEARER  = os.getenv("HOLDED_USE_BEARER", "false").lower() in ("1","true","yes")

MAIL_FROM   = os.getenv("MAIL_FROM")
MAIL_TO     = os.getenv("MAIL_TO")      # varios separados por coma
SMTP_HOST   = os.getenv("SMTP_HOST")
SMTP_PORT   = int(os.getenv("SMTP_PORT", "587"))  # 587 STARTTLS | 465 SSL
SMTP_USER   = os.getenv("SMTP_USER")
SMTP_PASS   = os.getenv("SMTP_PASS")

BASE_DOCS   = "https://api.holded.com/api/invoicing/v1/documents"
BASE_PROD   = "https://api.holded.com/api/invoicing/v1/products"
PAGE_LIMIT  = 200

# Zona horaria para impresión / cómputo de días
try:
    from zoneinfo import ZoneInfo
    TZ_MADRID = ZoneInfo("Europe/Madrid")
except Exception:
    TZ_MADRID = timezone.utc  # fallback

# Preferencias de pack
POSSIBLE_PACK_SIZES = [36, 37, 35, 33, 31, 30]
PACK_RULES = [
    (r"AIKO.*MAH72M", 36),
    (r"AIKO.*\b605\b", 36),
]

# ----------------------------- Helpers HTTP / tiempo -----------------------------
def H():
    if not API_KEY:
        raise SystemExit("ERROR: falta HOLDED_API_KEY en variables de entorno.")
    h = {"Accept": "application/json"}
    if USE_BEARER:
        h["Authorization"] = f"Bearer {API_KEY}"
    else:
        h["key"] = API_KEY
    return h

def to_madrid_str_from_epoch(s):
    try:
        ts = int(s)
    except Exception:
        return str(s)
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ_MADRID).strftime("%Y-%m-%d %H:%M:%S")

def utc_bounds_last_minutes(minutes=10):
    now_tz = datetime.now(TZ_MADRID)
    start = now_tz - timedelta(minutes=minutes)
    return int(start.astimezone(timezone.utc).timestamp()), int(now_tz.astimezone(timezone.utc).timestamp())

def madrid_day_bounds_epoch_seconds(day_offset=0):
    """
    (start_utc, end_utc) para el día 'hoy + day_offset' en Europe/Madrid.
    day_offset=0 -> hoy; -1 -> ayer; +1 -> mañana.
    """
    now_mad = datetime.now(TZ_MADRID)
    target = (now_mad + timedelta(days=day_offset)).date()
    start_mad = datetime(target.year, target.month, target.day, 0, 0, 0, tzinfo=TZ_MADRID)
    end_mad   = datetime(target.year, target.month, target.day, 23, 59, 59, tzinfo=TZ_MADRID)
    return int(start_mad.astimezone(timezone.utc).timestamp()), int(end_mad.astimezone(timezone.utc).timestamp())

def fmt_eur(n, decimals=4):
    try:
        v = float(n or 0)
    except Exception:
        return str(n)
    s = f"{v:,.{decimals}f} €"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")

# ----------------------------- API calls -----------------------------
def get_salesorder(doc_id):
    url = f"{BASE_DOCS}/salesorder/{doc_id}"
    r = requests.get(url, headers=H(), timeout=60)
    if r.status_code == 404:
        url = f"{BASE_DOCS}/{doc_id}"
        r = requests.get(url, headers=H(), timeout=60)
    r.raise_for_status()
    return r.json()

def list_salesorders_between(start_epoch_utc, end_epoch_utc, page_limit=PAGE_LIMIT):
    url = f"{BASE_DOCS}/salesorder"
    page = 1
    out = []
    while True:
        params = {"page": page, "limit": page_limit,
                  "starttmp": str(start_epoch_utc), "endtmp": str(end_epoch_utc)}
        r = requests.get(url, headers=H(), params=params, timeout=60)
        if r.status_code == 401:
            raise SystemExit(f"401 Unauthorized: {r.text}")
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page_limit:
            break
        page += 1
    return out

_prod_cache = {}
def get_product(product_id):
    if not product_id:
        return {}
    if product_id in _prod_cache:
        return _prod_cache[product_id]
    url = f"{BASE_PROD}/{product_id}"
    r = requests.get(url, headers=H(), timeout=60)
    r.raise_for_status()
    data = r.json()
    _prod_cache[product_id] = data
    return data

# ----------------------------- Helpers de estado -----------------------------
def load_processed_ids(path):
    try:
        p = Path(path)
        if not p.exists():
            return set()
        data = json.loads(p.read_text(encoding="utf-8"))
        return set(data if isinstance(data, list) else [])
    except Exception:
        return set()

def save_processed_ids(path, ids_set):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(ids_set), ensure_ascii=False, indent=2), encoding="utf-8")

# ----------------------------- Detectar Transporte por NOMBRE -----------------------------
TRANSPORT_NAME_PATTERNS = [
    r"^\s*transporte\s*$",
    r"^\s*shipping\s*costs?\s*$",
    r"^\s*shipping\s*$",
    r"^\s*shipment\s*$",
    r"^\s*transport\s*$",
    r"^\s*flete\s*$",
    r"^\s*portes?\s*$",
    r"^\s*env[ií]o\s*$",
]

def is_transport_name(name: str) -> bool:
    n = (name or "").strip().lower()
    for pat in TRANSPORT_NAME_PATTERNS:
        if re.match(pat, n):
            return True
    return False

# ----------------------------- Extractores robustos -----------------------------
def dig(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        if k in cur:
            cur = cur[k]
        else:
            return default
    return cur

def try_fields(container, candidates, default=None):
    if not isinstance(container, dict):
        return default
    for key in candidates:
        if key in container:
            val = container[key]
            if val not in (None, "", []):
                return val
        attrs = container.get("attributes") or {}
        if key in attrs:
            val = attrs.get(key)
            if val not in (None, "", []):
                return val
        cfs = container.get("customFields")
        if isinstance(cfs, dict) and key in cfs:
            val = cfs.get(key)
            if val not in (None, "", []):
                return val
        if isinstance(cfs, list):
            for entry in cfs:
                if isinstance(entry, dict) and entry.get("field") == key:
                    val = entry.get("value")
                    if val not in (None, "", []):
                        return val
    return default

def extract_power_w(product, *, item_name="", item_sku=""):
    val = try_fields(product, ["power_w", "Potencia", "potencia_w", "power", "watt", "W"])
    if val not in (None, "", []):
        try:
            return float(val)
        except Exception:
            pass
    texts = [
        item_name or "",
        item_sku or "",
        str(try_fields(product, ["name"]) or ""),
        str(try_fields(product, ["sku"]) or ""),
    ]
    for txt in texts:
        m = re.findall(r"(?<!\d)(\d{3,4})\s*[Ww]\s*(?:[Pp])?", txt)
        cands = [int(x) for x in m if 300 <= int(x) <= 1000]
        if cands:
            return float(max(cands))
    generic = []
    for txt in texts:
        for x in re.findall(r"(?<!\d)(\d{3,4})(?!\d)", txt):
            n = int(x)
            if 300 <= n <= 1000:
                generic.append(n)
    if generic:
        return float(max(generic))
    return 0.0

def extract_units_per_pallet(product):
    val = try_fields(product, [
        "units_per_pallet", "unitsPerPallet", "pallet_units",
        "ud_pallet", "uds_pallet", "unitsPallet"
    ])
    try:
        return float(val)
    except Exception:
        return 0.0

def compute_price_per_w(line_amount, qty, power_w):
    if qty and power_w:
        return float(line_amount) / (float(qty) * float(power_w))
    return 0.0

def extract_transport_amount_from_doc(doc):
    total = 0.0
    found = False
    for p in (doc.get("products") or []):
        name = (p.get("name") or "")
        if is_transport_name(name):
            price = float(p.get("price") or 0)
            units = float(p.get("units") or 0)
            total += price * units
            found = True
    return total if found else "-"

def has_transport_line(doc):
    for p in (doc.get("products") or []):
        if is_transport_name(p.get("name") or ""):
            return True
    return False

def to_date_label(doc):
    v = doc.get("date") or doc.get("createdAt") or doc.get("issuedOn") or doc.get("updatedAt")
    if v is None:
        return "-"
    return to_madrid_str_from_epoch(v) if str(v).isdigit() else str(v)

# ----------------------------- Normalización de líneas -----------------------------
def iter_document_lines(doc):
    for it in (doc.get("products") or []):
        name = (it.get("name") or "").strip()
        is_transport = is_transport_name(name)  # <-- SOLO por nombre
        yield {
            "name": name,
            "desc": it.get("desc"),
            "qty": float(it.get("units") or 0),
            "unit_price": float(it.get("price") or 0),
            "amount": float(it.get("price") or 0) * float(it.get("units") or 0),
            "productId": it.get("productId"),
            "sku": (str(it.get("sku")) if it.get("sku") is not None else ""),
            "is_transport": is_transport,
        }

# ----------------------------- Inferencia de packs -----------------------------
def hint_units_per_pallet_by_pattern(name="", sku="", product=None):
    text = " ".join([
        (name or ""), (sku or ""),
        str((product or {}).get("name") or ""),
        str((product or {}).get("sku") or "")
    ])
    for pat, val in PACK_RULES:
        if re.search(pat, text, flags=re.IGNORECASE):
            return float(val)
    return 0.0

def infer_units_per_pallet(product, *, name="", sku="", qty=0):
    if (upp := extract_units_per_pallet(product)) > 0:
        leftover = qty % upp if qty and upp else 0
        return upp, "attr", [], int(leftover)
    upp = hint_units_per_pallet_by_pattern(name=name, sku=sku, product=product)
    if upp > 0:
        leftover = qty % upp if qty and upp else 0
        return upp, "pattern", [], int(leftover)
    if qty:
        exact = [p for p in POSSIBLE_PACK_SIZES if qty % p == 0]
        if len(exact) == 1:
            return float(exact[0]), "divisible", [], 0
        elif len(exact) > 1:
            preferred = 36 if 36 in exact else max(exact)
            others = [p for p in exact if p != preferred]
            return float(preferred), "ambiguous_divisible", others, 0
        best_p = None
        best_leftover = None
        for p in POSSIBLE_PACK_SIZES:
            rem = qty % p
            score = (rem, -p)
            if best_leftover is None or score < (best_leftover, -best_p):
                best_leftover = rem
                best_p = p
        return float(best_p), "closest", [], int(best_leftover or 0)
    return 0.0, "unknown", [], 0

# ----------------------------- Render/Debug -----------------------------
def build_row(doc, line):
    """€/W si hay potencia; si no, €/ud. Pallets solo si hay potencia."""
    cliente_name = doc.get("contactName") or "-"
    item_name = line["name"] or "-"
    qty = float(line["qty"] or 0)
    amount = float(line["amount"] or 0)

    product = {}
    if line.get("productId"):
        try:
            product = get_product(line["productId"])
        except Exception:
            product = {}

    power_w = extract_power_w(product, item_name=item_name, item_sku=line.get("sku",""))

    pallets_display = "-"
    pallets_num = 0
    if power_w:
        upp, _, _, leftover = infer_units_per_pallet(
            product, name=item_name, sku=line.get("sku",""), qty=int(qty)
        )
        pallets = math.ceil(qty / upp) if (qty > 0 and upp > 0) else "-"
        pallets_display = (
            f"{int(pallets)} (+{leftover})" if (isinstance(pallets, (int,float)) and leftover)
            else (str(int(pallets)) if pallets != "-" else "-")
        )
        pallets_num = int(pallets) if isinstance(pallets, (int,float)) else 0

    if power_w:
        precio_valor = compute_price_per_w(amount, qty, power_w)   # €/W
        precio_unidad = "€/W"
        decs = 4
    else:
        precio_valor = float(line.get("unit_price") or 0)          # €/ud
        precio_unidad = "€/ud"
        decs = 2

    return {
        "Fecha reserva": to_date_label(doc),
        "Material": item_name,
        "Potencia (W)": int(power_w) if power_w else "-",
        "Cantidad uds": int(qty),
        "Nº Pallets": pallets_display,
        "PalletsNum": pallets_num,
        "Cliente": (cliente_name or "-"),
        "PrecioValor": precio_valor,
        "PrecioUnidad": precio_unidad,
        "PrecioDecs": decs,
        "Transporte": "-"  # se rellena luego solo en primera fila
    }

def _display_rows_for_console(rows):
    disp = []
    for r in rows:
        precio_txt = fmt_eur(r["PrecioValor"], r["PrecioDecs"]).replace(" €", f" {r['PrecioUnidad']}")
        transp_txt = fmt_eur(r["Transporte"], 2) if isinstance(r["Transporte"], (int,float)) else str(r["Transporte"])
        disp.append({
            "Fecha reserva": str(r["Fecha reserva"]),
            "Material": str(r["Material"]),
            "Potencia (W)": str(r["Potencia (W)"]),
            "Cantidad uds": str(r["Cantidad uds"]),
            "Nº Pallets": str(r["Nº Pallets"]),
            "Cliente": str(r["Cliente"]),
            "Precio": precio_txt,
            "Transporte": transp_txt
        })
    return disp

def print_table(rows):
    if not rows:
        print("No hay líneas que mostrar.")
        return
    headers = ["Fecha reserva","Material","Potencia (W)","Cantidad uds","Nº Pallets","Cliente","Precio","Transporte"]
    disp = _display_rows_for_console(rows)
    widths = {h: max(len(h), max(len(d[h]) for d in disp)) for h in headers}
    sep = " | "
    line = "-+-".join("-"*widths[h] for h in headers)
    print(sep.join(h.ljust(widths[h]) for h in headers))
    print(line)
    for d in disp:
        print(sep.join(d[h].ljust(widths[h]) for h in headers))

def dump_json(obj, path):
    path = Path(path)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[dump] JSON guardado en: {path}")

# ----------------------------- Email -----------------------------
def build_email_subject(doc, rows):
    """Con pallets: VENDIDO {n_pallets} pallets {material} a {Cliente}
       Sin pallets: VENDIDO {n_unidades} uds {material} a {Cliente}."""
    cliente = doc.get("contactName") or "-"

    if rows:
        materials = [r.get("Material") or "-" for r in rows]
        distinct = []
        for m in materials:
            if m not in distinct:
                distinct.append(m)
        material_label = distinct[0] if len(distinct) <= 1 else f"{distinct[0]} (+{len(distinct)-1} más)"
    else:
        material_label = "Transporte" if has_transport_line(doc) else "Sin líneas"

    pallets_total = sum(int(r.get("PalletsNum") or 0) for r in rows) if rows else 0

    if pallets_total > 0:
        qty = pallets_total
        unit_word = "pallets" if qty != 1 else "pallet"
    else:
        units_total = sum(int(r.get("Cantidad uds") or 0) for r in rows) if rows else 0
        qty = units_total
        unit_word = "uds" if qty != 1 else "ud"

    return f"VENDIDO {qty} {unit_word} {material_label} a {cliente}"

def build_html_table(doc, rows):
    number = doc.get("number") or doc.get("code") or doc.get("docNumber") or (doc.get("_id") or doc.get("id") or "-")
    cliente = doc.get("contactName") or "-"
    fecha = to_date_label(doc)
    transporte_amount = extract_transport_amount_from_doc(doc)

    head = (
        f"<h3 style='margin:0 0 8px'>Reserva de material — Pedido {number}</h3>"
        f"<p style='margin:0 0 10px'>Cliente: <b>{cliente}</b> &nbsp;|&nbsp; Fecha: <b>{fecha}</b>"
        f" &nbsp;|&nbsp; Transporte: <b>{(fmt_eur(transporte_amount,2) if isinstance(transporte_amount,(int,float)) else transporte_amount)}</b></p>"
    )

    headers = ["Fecha reserva","Material","Potencia (W)","Cantidad uds","Nº Pallets","Cliente","Precio","Transporte"]
    tr = []
    for r in rows:
        precio_html = fmt_eur(r["PrecioValor"], r["PrecioDecs"]).replace(" €", f" {r['PrecioUnidad']}")
        transp_html = fmt_eur(r["Transporte"], 2) if isinstance(r["Transporte"], (int,float)) else r["Transporte"]
        tr.append(
            "<tr>"
            f"<td>{r['Fecha reserva']}</td>"
            f"<td>{r['Material']}</td>"
            f"<td style='text-align:right'>{r['Potencia (W)']}</td>"
            f"<td style='text-align:right'>{r['Cantidad uds']}</td>"
            f"<td style='text-align:right'>{r['Nº Pallets']}</td>"
            f"<td>{r['Cliente']}</td>"
            f"<td style='text-align:right'>{precio_html}</td>"
            f"<td style='text-align:right'>{transp_html}</td>"
            "</tr>"
        )
    body = (
        "<table border='1' cellspacing='0' cellpadding='6' style='border-collapse:collapse'>"
        "<thead><tr>"
        + "".join(f"<th>{h}</th>" for h in headers) +
        "</tr></thead>"
        f"<tbody>{''.join(tr) if tr else '<tr><td colspan=8>Sin líneas</td></tr>'}</tbody>"
        "</table>"
    )

    return "<div style='font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif'>" + head + body + "</div>"

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

# ----------------------------- Main -----------------------------
def main():
    ap = argparse.ArgumentParser(description="Poller de Sales Orders (Holded) → reserva (+ email opcional)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--doc-id", help="ID de documento (salesorder) a descargar")
    g.add_argument("--minutes", type=int, help="Buscar pedidos creados en los últimos X minutos")
    g.add_argument("--days", type=int, help="Buscar pedidos de los últimos N días incluyendo hoy (1 = hoy+yesterday)")
    ap.add_argument("--limit", type=int, default=200, help="Máximo de documentos a procesar")
    ap.add_argument("--dump-json", help="Ruta base para volcar el JSON crudo de cada documento (añade sufijo con el id)")
    ap.add_argument("--send-email", action="store_true", help="Enviar email con la tabla para cada documento procesado")
    ap.add_argument("--state-file", default=".state/processed_salesorders.json",
                    help="Ruta al JSON de IDs ya procesados (para evitar duplicados)")
    args = ap.parse_args()

    # Obtener docs según el modo
    if args.doc_id:
        docs = [get_salesorder(args.doc_id)]
    elif args.minutes:
        start, end = utc_bounds_last_minutes(args.minutes)
        docs = list_salesorders_between(start, end)
    else:  # args.days
        days = max(0, int(args.days or 0))
        docs = []
        for d in range(0, -(days) - 1, -1):  # 0, -1, -2, ...
            s, e = madrid_day_bounds_epoch_seconds(day_offset=d)
            docs.extend(list_salesorders_between(s, e))

    # Deduplicar por ID y ordenar por fecha (si hay)
    def _doc_id(d):
        return d.get("_id") or d.get("id") or d.get("docNumber") or d.get("number") or ""
    uniq = {}
    for d in docs:
        did = _doc_id(d)
        if did and did not in uniq:
            uniq[did] = d
    docs = list(uniq.values())
    try:
        docs.sort(key=lambda d: int(d.get("date") or 0), reverse=True)
    except Exception:
        pass
    docs = docs[:args.limit]

    if not docs:
        print("No se han encontrado documentos en la ventana solicitada.")
        return

    # Cargar estado y filtrar nuevos
    processed = load_processed_ids(args.state_file)
    docs_new = [d for d in docs if _doc_id(d) and _doc_id(d) not in processed]

    if not docs_new and not args.doc_id:
        print("No hay documentos NUEVOS para procesar.")
        return

    for doc in (docs_new if not args.doc_id else docs):
        doc_id = _doc_id(doc)
        number = doc.get("number") or doc.get("code") or doc.get("docNumber") or doc_id
        print(f"\n=== Sales Order: {number} (id: {doc_id}) ===")

        if args.dump_json:
            dump_json(doc, f"{args.dump_json.rstrip('.json')}_{doc_id}.json")

        lines = list(iter_document_lines(doc))
        material_lines = [ln for ln in lines if not ln.get("is_transport")]

        rows = [build_row(doc, ln) for ln in material_lines]

        # Transporte global solo en la PRIMERA fila
        transp_amount = extract_transport_amount_from_doc(doc)
        for i, r in enumerate(rows):
            r["Transporte"] = transp_amount if i == 0 else "-"

        print_table(rows)

        if args.send_email:
            html = build_html_table(doc, rows)
            subject = build_email_subject(doc, rows)
            send_email(subject, html)
            print("Email enviado.")

        # Marcar como procesado y GUARDAR INCREMENTALMENTE (salvo --doc-id)
        if not args.doc_id:
            processed.add(doc_id)
            save_processed_ids(args.state_file, processed)

    # Guardado final (por si acaso)
    if not args.doc_id:
        save_processed_ids(args.state_file, processed)
        print(f"[estado] Guardados {len(processed)} IDs en {args.state_file}")

if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print(f"[HTTPError] {e.response.status_code}: {e.response.text}")
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)
