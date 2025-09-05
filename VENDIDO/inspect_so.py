#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os, json, argparse
from pathlib import Path
import requests
from datetime import datetime, timezone

# --- .env local opcional ---
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"))
except Exception:
    pass

API_KEY    = os.getenv("HOLDED_API_KEY")
USE_BEARER = os.getenv("HOLDED_USE_BEARER", "false").lower() in ("1","true","yes")

BASE_DOCS  = "https://api.holded.com/api/invoicing/v1/documents"

def H():
    if not API_KEY:
        raise SystemExit("ERROR: falta HOLDED_API_KEY en variables de entorno.")
    h = {"Accept": "application/json"}
    if USE_BEARER:
        h["Authorization"] = f"Bearer {API_KEY}"
    else:
        h["key"] = API_KEY
    return h

def fetch_salesorder_detail(doc_id):
    """Intenta primero /documents/salesorder/{id} y si da 404, /documents/{id}."""
    urls = [f"{BASE_DOCS}/salesorder/{doc_id}", f"{BASE_DOCS}/{doc_id}"]
    last_exc = None
    for url in urls:
        try:
            r = requests.get(url, headers=H(), timeout=60)
            if r.status_code == 404:
                last_exc = f"404 en {url}"
                continue
            r.raise_for_status()
            return r.json(), url
        except Exception as e:
            last_exc = e
    raise SystemExit(f"No se pudo obtener el documento: {last_exc}")

def is_transport_line(item):
    name = (item.get("name") or "").strip().lower()
    tags = [t.lower() for t in (item.get("tags") or [])]
    return (name == "transporte") or ("transporte" in tags) or ("envío" in name) or ("envio" in name)

def to_local(ts):
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)

def main():
    ap = argparse.ArgumentParser(description="Inspector de Sales Order (Holded)")
    ap.add_argument("--doc-id", required=True, help="ID del documento (salesorder)")
    ap.add_argument("--dump-json", help="Ruta para volcar el JSON crudo (p.ej. so_raw.json)")
    args = ap.parse_args()

    data, url = fetch_salesorder_detail(args.doc_id)

    # Cabecera básica
    doc_id   = data.get("_id") or data.get("id") or args.doc_id
    number   = data.get("number") or data.get("code") or data.get("docNumber") or "-"
    contact  = data.get("contactName") or "-"
    status   = data.get("status")
    date_raw = data.get("date") or data.get("createdAt") or data.get("issuedOn") or data.get("updatedAt")
    date_hr  = to_local(date_raw) if (date_raw is not None) else "-"

    print(f"== Detalle obtenido desde: {url}")
    print(f"ID: {doc_id} | Nº: {number} | Cliente: {contact} | Status: {status} | Fecha: {date_hr}")
    print("-"*80)

    # ¿Dónde vienen las líneas?
    keys = set(data.keys())
    print(f"Claves de nivel raíz presentes ({len(keys)}): {', '.join(sorted(list(keys)))}")
    print("-"*80)

    products = data.get("products", None)
    alt_lines = data.get("lines", None) or data.get("items", None)

    if products is None and alt_lines is not None:
        print("ATENCIÓN: No hay 'products' pero sí hay 'lines/items'. Tu código debería mirar ese campo.")
        lines_src = "lines/items"
        lines = alt_lines
    else:
        lines_src = "products"
        lines = products

    # Volcado de líneas
    if not isinstance(lines, list):
        print(f"No hay lista de líneas en '{lines_src}' (valor: {type(lines).__name__}).")
        lines = []

    print(f"Líneas encontradas en '{lines_src}': {len(lines)}")
    if not lines:
        print("=> Posibles causas: pedido vacío, solo cabecera, o estructura distinta en tu tenant.")
    else:
        print("\n# Tabla de líneas")
        print("idx | transporte | name                               | units | price     | tags")
        print("----+------------+------------------------------------+-------+-----------+-----------------------------")
        for i, it in enumerate(lines, start=1):
            name  = (it.get("name") or "-")[:36]
            units = it.get("units")
            price = it.get("price")
            tags  = ",".join(it.get("tags") or [])
            flag_t = "YES" if is_transport_line(it) else "no"
            print(f"{str(i).rjust(3)} | {flag_t.ljust(10)} | {name.ljust(36)} | {str(units or '-').rjust(5)} | {str(price or '-').rjust(9)} | {tags}")

        # Diagnóstico rápido
        only_transport = all(is_transport_line(it) for it in lines)
        if only_transport:
            print("\nConclusión probable: TODAS las líneas son de transporte ⇒ tu script no muestra materiales → 'No hay líneas que mostrar'.")
        else:
            non_trans = [it for it in lines if not is_transport_line(it)]
            if not non_trans:
                print("\nNo se han encontrado líneas de material no-transporte.")
            else:
                print(f"\nSe han encontrado {len(non_trans)} líneas de material no-transporte (de {len(lines)} totales).")

    # Dump opcional
    if args.dump_json:
        Path(args.dump_json).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[OK] JSON crudo guardado en: {args.dump_json}")

if __name__ == "__main__":
    main()
