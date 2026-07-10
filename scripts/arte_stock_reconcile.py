#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Arte / Bellas Artes — Reconciliación stock PrestaShop vs Odoo.

Compara stock_available (API PS) con stock.quant (Odoo) para variantes mapeadas
con tracking=none. Solo lectura (dry-run por defecto).

Uso:
  python3 scripts/arte_stock_reconcile.py --integration-id 1
  python3 scripts/arte_stock_reconcile.py --integration-id 1 --limit 100
  python3 scripts/arte_stock_reconcile.py --pilot-only --pilot-limit 20
"""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import os
import re
import ssl
import sys
import time
import xmlrpc.client
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests

ODOO_URL = os.environ.get("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.environ.get("ODOO_DB", "arte")
ODOO_USER = os.environ.get("ODOO_USER", "admin")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "123")
ODOO_SSL_VERIFY = os.environ.get("ODOO_SSL_VERIFY", "1").lower() not in ("0", "false", "no")

INTEGRATION_ID = int(os.environ.get("INTEGRATION_ID", "1"))
REPORT_DIR = Path(os.environ.get("ARTE_REPORT_DIR", "scripts/arte_reports"))
READ_BATCH = 2000

if http.client._MAXHEADERS < 1000:
    http.client._MAXHEADERS = 1000

_T0 = time.monotonic()
PS_CODE_RE = re.compile(r"^(\d+)(?:-(\d+))?$")


def log(msg, level="*"):
    elapsed = time.monotonic() - _T0
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts} +{elapsed:6.0f}s] [{level}] {msg}", flush=True)


def connect_odoo():
    base = ODOO_URL.rstrip("/")
    ctx = None
    if base.startswith("https://") and not ODOO_SSL_VERIFY:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    common = xmlrpc.client.ServerProxy(
        f"{base}/xmlrpc/2/common", allow_none=True, context=ctx
    )
    models = xmlrpc.client.ServerProxy(
        f"{base}/xmlrpc/2/object", allow_none=True, context=ctx
    )
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        raise RuntimeError("Autenticación Odoo fallida.")
    return uid, models


def execute(models, uid, model, method, args=None, **kwargs):
    kw = dict(kwargs)
    ctx = kw.pop("context", None)
    if ctx:
        kw["context"] = ctx
    return models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, model, method, args or [], kw
    )


def m2o_id(field_val):
    if not field_val:
        return False
    if isinstance(field_val, (list, tuple)):
        return field_val[0]
    return int(field_val)


def chunked(ids, size):
    ids = list(ids)
    for i in range(0, len(ids), size):
        yield ids[i : i + size]


def parse_ps_code(code):
    code = str(code or "").strip()
    m = PS_CODE_RE.match(code)
    if not m:
        return None, None
    attr = m.group(2)
    return int(m.group(1)), int(attr) if attr else 0


def ps_code_from_ids(id_product, id_product_attribute):
    id_product_attribute = int(id_product_attribute or 0)
    if id_product_attribute:
        return f"{id_product}-{id_product_attribute}"
    return str(id_product)


def read_integration_api(models, uid, integration_id):
    rows = execute(
        models,
        uid,
        "sale.integration.api.field",
        "search_read",
        [[("sia_id", "=", integration_id), ("name", "in", ["url", "key"])]],
        fields=["name", "value"],
    )
    cfg = {r["name"]: (r["value"] or "").strip() for r in rows}
    url = cfg.get("url", "").rstrip("/")
    key = cfg.get("key", "")
    if not url or not key:
        raise RuntimeError("Faltan url/key PS en sale.integration.api.field")
    return url, key


def read_mapped_locations(models, uid, integration_id):
    lines = execute(
        models,
        uid,
        "external.stock.location.line",
        "search_read",
        [[("integration_id", "=", integration_id)]],
        fields=["erp_location_id", "location_name", "warehouse_id"],
    )
    loc_ids = []
    for line in lines:
        loc_id = m2o_id(line.get("erp_location_id"))
        if loc_id:
            loc_ids.append(loc_id)
    # Incluir ubicaciones hijas (internal) del almacén mapeado
    expanded = set(loc_ids)
    if loc_ids:
        children = execute(
            models,
            uid,
            "stock.location",
            "search",
            [[("id", "child_of", loc_ids), ("usage", "=", "internal")]],
        )
        expanded.update(children)
    return lines, sorted(expanded)


def load_mapped_variants(models, uid, integration_id, limit=0, pilot_product_ids=None):
    domain = [("integration_id", "=", integration_id), ("product_id", "!=", False)]
    if pilot_product_ids:
        domain.append(("product_id", "in", list(pilot_product_ids)))
    map_ids = execute(models, uid, "integration.product.product.mapping", "search", [domain])
    if limit:
        map_ids = map_ids[:limit]

    rows = []
    for batch in chunked(map_ids, READ_BATCH):
        rows.extend(
            execute(
                models,
                uid,
                "integration.product.product.mapping",
                "read",
                [batch],
                fields=["product_id", "external_product_id"],
            )
        )

    ext_ids = list({m2o_id(r["external_product_id"]) for r in rows if r.get("external_product_id")})
    ext_by_id = {}
    for batch in chunked(ext_ids, READ_BATCH):
        for row in execute(
            models,
            uid,
            "integration.product.product.external",
            "read",
            [batch],
            fields=["code", "name"],
        ):
            ext_by_id[row["id"]] = row

    product_ids = list({m2o_id(r["product_id"]) for r in rows})
    products_by_id = {}
    for batch in chunked(product_ids, READ_BATCH):
        for row in execute(
            models,
            uid,
            "product.product",
            "read",
            [batch],
            fields=["display_name", "default_code", "active", "tracking"],
        ):
            products_by_id[row["id"]] = row

    records = []
    for mapping in rows:
        pid = m2o_id(mapping["product_id"])
        eid = m2o_id(mapping["external_product_id"])
        product = products_by_id.get(pid, {})
        ext = ext_by_id.get(eid, {})
        if product.get("tracking") and product.get("tracking") != "none":
            continue
        id_product, id_attr = parse_ps_code(ext.get("code"))
        records.append(
            {
                "product_id": pid,
                "ps_code": ext.get("code") or "",
                "id_product": id_product,
                "id_product_attribute": id_attr if id_attr is not None else None,
                "display_name": product.get("display_name") or "",
                "default_code": product.get("default_code") or "",
                "active": product.get("active"),
            }
        )
    return records


def fetch_ps_stock(url, key, product_filter=None, retries=3):
    """Devuelve {ps_code: quantity} desde stock_availables."""
    session = requests.Session()
    stock_by_code = {}
    product_filter = sorted({int(p) for p in (product_filter or []) if p})

    if product_filter:
        for pid in product_filter:
            for attempt in range(retries):
                try:
                    params = {
                        "ws_key": key,
                        "output_format": "JSON",
                        "display": "[id,id_product,id_product_attribute,quantity]",
                        "filter[id_product]": str(pid),
                    }
                    resp = session.get(
                        f"{url}/api/stock_availables",
                        params=params,
                        auth=(key, ""),
                        timeout=90,
                    )
                    resp.raise_for_status()
                    items = _parse_ps_list(
                        resp.json(), "stock_availables", "stock_available"
                    )
                    for item in items:
                        id_product = _ps_int(item, "id_product")
                        id_attr = _ps_int(item, "id_product_attribute") or 0
                        qty = _ps_float(item, "quantity")
                        if id_product is None:
                            continue
                        code = ps_code_from_ids(id_product, id_attr)
                        stock_by_code[code] = qty
                    break
                except (requests.RequestException, ValueError) as exc:
                    if attempt + 1 >= retries:
                        log(f"PS stock id_product={pid} falló: {exc}", "!")
                    else:
                        time.sleep(1.5 * (attempt + 1))
            time.sleep(0.05)
        return stock_by_code

    offset = 0
    block = 500
    while True:
        params = {
            "ws_key": key,
            "output_format": "JSON",
            "display": "[id,id_product,id_product_attribute,quantity]",
            "limit": f"{offset},{block}",
        }
        for attempt in range(retries):
            try:
                resp = session.get(
                    f"{url}/api/stock_availables",
                    params=params,
                    auth=(key, ""),
                    timeout=120,
                )
                resp.raise_for_status()
                break
            except requests.RequestException as exc:
                if attempt + 1 >= retries:
                    raise
                time.sleep(2 * (attempt + 1))
        items = _parse_ps_list(resp.json(), "stock_availables", "stock_available")
        if not items:
            break
        for item in items:
            id_product = _ps_int(item, "id_product")
            id_attr = _ps_int(item, "id_product_attribute") or 0
            qty = _ps_float(item, "quantity")
            if id_product is None:
                continue
            code = ps_code_from_ids(id_product, id_attr)
            stock_by_code[code] = qty
        if len(items) < block:
            break
        offset += block
        time.sleep(0.05)
    return stock_by_code


def _parse_ps_list(data, root_key, item_key):
    if isinstance(data, list):
        root = data
    elif isinstance(data, dict):
        root = data.get(root_key, data)
    else:
        return []
    if isinstance(root, dict):
        inner = root.get(item_key, [])
        return [inner] if isinstance(inner, dict) else (inner or [])
    return root if isinstance(root, list) else []


def _ps_int(record, field):
    val = record.get(field) if isinstance(record, dict) else None
    if isinstance(val, dict):
        val = val.get("value") or val.get("#text")
    try:
        return int(val) if val not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _ps_float(record, field):
    val = record.get(field) if isinstance(record, dict) else None
    if isinstance(val, dict):
        val = val.get("value") or val.get("#text")
    try:
        return float(val) if val not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def read_odoo_stock(models, uid, product_ids, location_ids):
    qty_by_product = defaultdict(float)
    if not product_ids:
        return qty_by_product

    domain = [("product_id", "in", list(product_ids))]
    if location_ids:
        domain.append(("location_id", "in", location_ids))
    else:
        domain.append(("location_id.usage", "=", "internal"))

    quant_ids = execute(models, uid, "stock.quant", "search", [domain])
    for batch in chunked(quant_ids, READ_BATCH):
        for q in execute(
            models,
            uid,
            "stock.quant",
            "read",
            [batch],
            fields=["product_id", "quantity", "location_id"],
        ):
            pid = m2o_id(q["product_id"])
            qty_by_product[pid] += float(q.get("quantity") or 0)
    return qty_by_product


def reconcile(records, ps_stock, odoo_stock):
    rows = []
    matched = diff = missing_ps = missing_odoo = 0
    for rec in records:
        code = rec["ps_code"]
        ps_qty = ps_stock.get(code)
        odoo_qty = odoo_stock.get(rec["product_id"], 0.0)
        if code not in ps_stock:
            status = "missing_ps"
            missing_ps += 1
        elif abs((ps_qty or 0) - odoo_qty) < 0.001:
            status = "match"
            matched += 1
        else:
            status = "diff"
            diff += 1
        rows.append(
            {
                "product_id": rec["product_id"],
                "ps_code": code,
                "display_name": rec["display_name"],
                "default_code": rec["default_code"],
                "ps_qty": ps_qty if code in ps_stock else "",
                "odoo_qty": odoo_qty,
                "delta": (ps_qty - odoo_qty) if code in ps_stock else "",
                "status": status,
            }
        )
    summary = {
        "total_compared": len(rows),
        "match": matched,
        "diff": diff,
        "missing_ps": missing_ps,
    }
    return rows, summary


def write_outputs(report_rows, summary, meta):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = REPORT_DIR / f"arte_stock_reconcile_{stamp}.json"
    csv_path = REPORT_DIR / f"arte_stock_reconcile_{stamp}.csv"
    md_path = REPORT_DIR / f"arte_stock_reconcile_{stamp}.md"

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "meta": meta,
        "summary": summary,
        "rows": report_rows,
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "product_id",
                "ps_code",
                "default_code",
                "display_name",
                "ps_qty",
                "odoo_qty",
                "delta",
                "status",
            ],
        )
        writer.writeheader()
        writer.writerows(report_rows)

    lines = [
        f"# Reconciliación stock Arte — {payload['generated_at']}",
        "",
        f"- Integración id={meta['integration_id']}",
        f"- DB: `{ODOO_DB}` @ `{ODOO_URL}`",
        f"- Ubicaciones Odoo: {meta.get('location_ids') or 'todas internal (sin location_line_ids)'}",
        "",
        "## Resumen",
        "",
        f"| Estado | Cantidad |",
        f"|--------|--------:|",
        f"| Coinciden | {summary['match']} |",
        f"| Diferencia | {summary['diff']} |",
        f"| Sin stock PS | {summary['missing_ps']} |",
        f"| Total | {summary['total_compared']} |",
        "",
        "## Top diferencias",
        "",
        "| ps_code | product_id | PS | Odoo | delta |",
        "|---------|-----------:|---:|-----:|------:|",
    ]
    diffs = sorted(
        [r for r in report_rows if r["status"] == "diff"],
        key=lambda x: abs(float(x["delta"] or 0)),
        reverse=True,
    )
    for r in diffs[:30]:
        lines.append(
            f"| {r['ps_code']} | {r['product_id']} | {r['ps_qty']} | {r['odoo_qty']} | {r['delta']} |"
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")

    log(f"CSV:  {csv_path}", "OK")
    log(f"JSON: {json_path}", "OK")
    log(f"MD:   {md_path}", "OK")
    return csv_path, json_path, md_path


def main():
    parser = argparse.ArgumentParser(description="Reconciliación stock PS vs Odoo (Arte)")
    parser.add_argument("--integration-id", type=int, default=INTEGRATION_ID)
    parser.add_argument("--limit", type=int, default=0, help="Limitar variantes mapeadas")
    parser.add_argument("--pilot-only", action="store_true", help="Solo productos piloto TPV")
    parser.add_argument("--pilot-limit", type=int, default=20)
    args = parser.parse_args()

    log(f"Conectando a {ODOO_URL} / {ODOO_DB}")
    uid, models = connect_odoo()
    integration_id = args.integration_id

    location_lines, location_ids = read_mapped_locations(models, uid, integration_id)
    if not location_lines:
        log(
            "Sin external.stock.location.line: se usarán todas las ubicaciones internal",
            "!",
        )

    pilot_ids = None
    if args.pilot_only:
        from pathlib import Path as _Path
        # Reutilizar lógica piloto importando diagnóstico
        sys.path.insert(0, str(_Path(__file__).resolve().parent))
        import arte_barcode_diagnostic as abd

        abd.ODOO_URL = ODOO_URL
        abd.ODOO_DB = ODOO_DB
        abd.ODOO_USER = ODOO_USER
        abd.ODOO_PASSWORD = ODOO_PASSWORD
        all_records = abd.load_mapped_variants(models, uid, integration_id)
        pilots = abd.select_pilot_candidates(all_records, limit=args.pilot_limit)
        pilot_ids = [p["product_id"] for p in pilots]
        log(f"Modo piloto: {len(pilot_ids)} productos")

    log("Cargando variantes mapeadas (tracking=none)...")
    records = load_mapped_variants(
        models, uid, integration_id, limit=args.limit, pilot_product_ids=pilot_ids
    )
    log(f"Variantes a comparar: {len(records)}")

    url, key = read_integration_api(models, uid, integration_id)
    product_ids_ps = {r["id_product"] for r in records if r.get("id_product")}
    log("Consultando stock_available en PrestaShop...")
    ps_stock = fetch_ps_stock(url, key, product_filter=product_ids_ps)
    log(f"Registros stock PS: {len(ps_stock)}")

    product_ids = [r["product_id"] for r in records]
    log("Leyendo stock.quant en Odoo...")
    odoo_stock = read_odoo_stock(models, uid, product_ids, location_ids)
    log(f"Productos con quant Odoo: {len(odoo_stock)}")

    report_rows, summary = reconcile(records, ps_stock, odoo_stock)
    log(
        f"match={summary['match']} diff={summary['diff']} missing_ps={summary['missing_ps']}"
    )

    meta = {
        "integration_id": integration_id,
        "location_lines": location_lines,
        "location_ids": location_ids,
        "pilot_only": args.pilot_only,
    }
    write_outputs(report_rows, summary, meta)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Interrumpido.", "!")
        sys.exit(130)
