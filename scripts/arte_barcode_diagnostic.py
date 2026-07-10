#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Arte / Bellas Artes — Diagnóstico de códigos de barras (PrestaShop ↔ Odoo ↔ TPV).

Audita cobertura EAN/barcode en productos mapeados, detecta duplicados y propone
candidatos para lote piloto TPV escaneable.

Uso:
  python3 scripts/arte_barcode_diagnostic.py
  python3 scripts/arte_barcode_diagnostic.py --integration-id 1
  python3 scripts/arte_barcode_diagnostic.py --pilot-candidates --pilot-limit 20
  python3 scripts/arte_barcode_diagnostic.py --fetch-ps --pilot-limit 50
  python3 scripts/arte_barcode_diagnostic.py --mark-pilot --pilot-limit 15 --dry-run
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import ssl
import sys
import time
import xmlrpc.client
from collections import Counter, defaultdict
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
INVALID_BARCODES = {"", "0", "00"}

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
        return code, None
    return m.group(1), m.group(2)


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


def fetch_ps_ean_by_code(url, key, template_ids=None, limit=0):
    """Devuelve {code_variante_ps: ean13} desde API PS."""
    session = requests.Session()
    result = {}
    tpl_ids = list(template_ids or [])
    if not tpl_ids:
        offset = 0
        block = 500
        while True:
            params = {
                "ws_key": key,
                "output_format": "JSON",
                "display": "[id]",
                "limit": f"{offset},{block}",
                "filter[active]": "1",
            }
            resp = session.get(
                f"{url}/api/products", params=params, auth=(key, ""), timeout=120
            )
            resp.raise_for_status()
            batch = _parse_ps_list(resp.json(), "products", "product")
            if not batch:
                break
            for item in batch:
                pid = _ps_id(item)
                if pid:
                    tpl_ids.append(str(pid))
            if len(batch) < block:
                break
            offset += block
            time.sleep(0.03)
            if limit and len(tpl_ids) >= limit:
                tpl_ids = tpl_ids[:limit]
                break

    for tpl_id in tpl_ids:
        resp = session.get(
            f"{url}/api/products/{tpl_id}",
            params={
                "ws_key": key,
                "output_format": "JSON",
                "display": "full",
            },
            auth=(key, ""),
            timeout=120,
        )
        if resp.status_code == 404:
            continue
        resp.raise_for_status()
        data = resp.json()
        product = data.get("product") or data
        if isinstance(product, list):
            product = product[0] if product else {}
        ean_tpl = _ps_field(product, "ean13")
        if ean_tpl:
            result[str(tpl_id)] = ean_tpl
        combos = product.get("associations", {}).get("combinations", {}).get(
            "combination", []
        )
        if isinstance(combos, dict):
            combos = [combos]
        for combo_ref in combos or []:
            combo_id = combo_ref.get("id") if isinstance(combo_ref, dict) else combo_ref
            if not combo_id:
                continue
            cresp = session.get(
                f"{url}/api/combinations/{combo_id}",
                params={
                    "ws_key": key,
                    "output_format": "JSON",
                    "display": "[ean13]",
                },
                auth=(key, ""),
                timeout=60,
            )
            if cresp.status_code != 200:
                continue
            cdata = cresp.json()
            combo = cdata.get("combination") or cdata
            if isinstance(combo, list):
                combo = combo[0] if combo else {}
            ean = _ps_field(combo, "ean13")
            code = f"{tpl_id}-{combo_id}"
            if ean:
                result[code] = ean
        time.sleep(0.02)
    return result


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


def _ps_id(item):
    pid = item.get("id") if isinstance(item, dict) else item
    if isinstance(pid, dict):
        pid = pid.get("value")
    return str(pid) if pid else None


def _ps_field(record, field):
    val = record.get(field) if isinstance(record, dict) else None
    if isinstance(val, dict):
        val = val.get("value") or val.get("#text")
    return (val or "").strip()


def load_mapped_variants(models, uid, integration_id):
    map_ids = execute(
        models,
        uid,
        "integration.product.product.mapping",
        "search",
        [[("integration_id", "=", integration_id), ("product_id", "!=", False)]],
    )
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
            fields=["code", "external_barcode", "name"],
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
            fields=[
                "barcode",
                "default_code",
                "display_name",
                "active",
                "product_tmpl_id",
                "tracking",
            ],
        ):
            products_by_id[row["id"]] = row

    tmpl_ids = list(
        {m2o_id(p["product_tmpl_id"]) for p in products_by_id.values()}
    )
    tmpl_by_id = {}
    for batch in chunked(tmpl_ids, READ_BATCH):
        for row in execute(
            models,
            uid,
            "product.template",
            "read",
            [batch],
            fields=["available_in_pos", "sale_ok", "active"],
        ):
            tmpl_by_id[row["id"]] = row

    records = []
    for mapping in rows:
        pid = m2o_id(mapping["product_id"])
        eid = m2o_id(mapping["external_product_id"])
        product = products_by_id.get(pid, {})
        ext = ext_by_id.get(eid, {})
        tmpl_id = m2o_id(product.get("product_tmpl_id"))
        tmpl = tmpl_by_id.get(tmpl_id, {})
        ext_bc = (ext.get("external_barcode") or "").strip()
        odoo_bc = (product.get("barcode") or "").strip()
        records.append(
            {
                "product_id": pid,
                "template_id": tmpl_id,
                "external_id": eid,
                "ps_code": ext.get("code") or "",
                "external_barcode": ext_bc,
                "odoo_barcode": odoo_bc,
                "default_code": product.get("default_code") or "",
                "display_name": product.get("display_name") or "",
                "active": product.get("active"),
                "tracking": product.get("tracking") or "none",
                "available_in_pos": bool(tmpl.get("available_in_pos")),
                "sale_ok": bool(tmpl.get("sale_ok")),
            }
        )
    return records


def compute_metrics(records):
    total = len(records)
    active = [r for r in records if r["active"]]
    with_ext = [r for r in active if r["external_barcode"]]
    with_odoo = [r for r in active if r["odoo_barcode"]]
    ext_no_odoo = [
        r
        for r in active
        if r["external_barcode"] and not r["odoo_barcode"]
    ]
    no_ext = [r for r in active if not r["external_barcode"]]
    pos_ready = [
        r
        for r in active
        if r["available_in_pos"]
        and r["odoo_barcode"]
        and r["odoo_barcode"] not in INVALID_BARCODES
    ]
    invalid_odoo = [
        r for r in active if r["odoo_barcode"] in INVALID_BARCODES
    ]

    ext_dup = Counter(
        r["external_barcode"] for r in active if r["external_barcode"]
    )
    odoo_dup = Counter(r["odoo_barcode"] for r in active if r["odoo_barcode"])

    return {
        "mapped_variants_total": total,
        "mapped_variants_active": len(active),
        "with_external_barcode": len(with_ext),
        "with_odoo_barcode": len(with_odoo),
        "external_barcode_no_odoo": len(ext_no_odoo),
        "no_external_barcode": len(no_ext),
        "pos_scan_ready": len(pos_ready),
        "invalid_odoo_barcode": len(invalid_odoo),
        "duplicate_external_ean_groups": sum(1 for c in ext_dup.values() if c > 1),
        "duplicate_odoo_barcode_groups": sum(1 for c in odoo_dup.values() if c > 1),
        "top_duplicate_external": [
            {"barcode": bc, "count": cnt}
            for bc, cnt in ext_dup.most_common(20)
            if cnt > 1
        ],
        "top_duplicate_odoo": [
            {"barcode": bc, "count": cnt}
            for bc, cnt in odoo_dup.most_common(20)
            if cnt > 1
        ],
    }


def select_pilot_candidates(records, limit=20):
    ext_counts = Counter(
        r["external_barcode"] for r in records if r["external_barcode"]
    )
    odoo_counts = Counter(r["odoo_barcode"] for r in records if r["odoo_barcode"])

    candidates = []
    for r in records:
        if not r["active"]:
            continue
        if r["tracking"] != "none":
            continue
        bc = r["odoo_barcode"] or r["external_barcode"]
        if not bc or bc in INVALID_BARCODES:
            continue
        if ext_counts.get(r["external_barcode"], 0) > 1:
            continue
        if r["odoo_barcode"] and odoo_counts.get(r["odoo_barcode"], 0) > 1:
            continue
        score = 0
        if r["odoo_barcode"]:
            score += 2
        if r["external_barcode"]:
            score += 1
        if r["sale_ok"]:
            score += 1
        candidates.append({**r, "pilot_score": score, "pilot_barcode": bc})

    candidates.sort(
        key=lambda x: (-x["pilot_score"], x["display_name"] or ""),
    )
    return candidates[:limit]


def read_integration_flags(models, uid, integration_id):
    fields = [
        "name",
        "state",
        "validate_barcode",
        "prestashop_validate_barcode",
        "prestashop_skip_invalid_barcodes",
    ]
    available = execute(models, uid, "sale.integration", "fields_get", [], attributes=["string"])
    read_fields = [f for f in fields if f in available]
    rows = execute(
        models,
        uid,
        "sale.integration",
        "read",
        [[integration_id]],
        fields=read_fields,
    )
    return rows[0] if rows else {}


def mark_pilot_pos(models, uid, candidates, dry_run=False):
    tmpl_ids = sorted({c["template_id"] for c in candidates if c.get("template_id")})
    if not tmpl_ids:
        log("No hay plantillas piloto para marcar.", "!")
        return []
    log(
        f"{'[DRY-RUN] ' if dry_run else ''}Marcar available_in_pos=True en {len(tmpl_ids)} plantillas",
        "+",
    )
    if dry_run:
        return tmpl_ids
    execute(
        models,
        uid,
        "product.template",
        "write",
        [tmpl_ids, {"available_in_pos": True, "sale_ok": True}],
    )
    return tmpl_ids


def write_report(report, integration_id):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = REPORT_DIR / f"arte_barcode_diagnostic_{stamp}.json"
    md_path = REPORT_DIR / f"arte_barcode_diagnostic_{stamp}.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    m = report["metrics"]
    lines = [
        f"# Diagnóstico barcode Arte — {report['generated_at']}",
        "",
        f"- Integración: **{report['integration']['name']}** (id={integration_id})",
        f"- DB: `{ODOO_DB}` @ `{ODOO_URL}`",
        "",
        "## Configuración integración",
        "",
    ]
    for k, v in report["integration"].items():
        lines.append(f"- `{k}`: {v}")
    lines.extend(
        [
            "",
            "## Métricas",
            "",
            f"| Métrica | Valor |",
            f"|---------|------:|",
            f"| Variantes mapeadas (activas) | {m['mapped_variants_active']} |",
            f"| Con external_barcode (EAN externo) | {m['with_external_barcode']} |",
            f"| Con barcode Odoo | {m['with_odoo_barcode']} |",
            f"| EAN externo sin barcode Odoo | {m['external_barcode_no_odoo']} |",
            f"| Sin external_barcode | {m['no_external_barcode']} |",
            f"| **TPV escaneables** (pos+barcode) | **{m['pos_scan_ready']}** |",
            f"| Barcode inválido en Odoo | {m['invalid_odoo_barcode']} |",
            f"| Grupos EAN duplicados (externo) | {m['duplicate_external_ean_groups']} |",
            f"| Grupos barcode duplicados (Odoo) | {m['duplicate_odoo_barcode_groups']} |",
            "",
        ]
    )
    if report.get("pilot_candidates"):
        lines.append("## Candidatos piloto TPV")
        lines.append("")
        lines.append("| product_id | ps_code | barcode | nombre |")
        lines.append("|-----------:|---------|---------|--------|")
        for c in report["pilot_candidates"]:
            name = (c.get("display_name") or "")[:60].replace("|", "/")
            lines.append(
                f"| {c['product_id']} | {c['ps_code']} | {c['pilot_barcode']} | {name} |"
            )
        lines.append("")
    if report.get("ps_compare_mismatches"):
        lines.append("## Divergencias PS API vs Odoo (muestra)")
        lines.append("")
        for row in report["ps_compare_mismatches"][:30]:
            lines.append(f"- `{row['ps_code']}`: PS={row['ps_ean']} Odoo={row['odoo_barcode']}")
        lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    log(f"Informe JSON: {json_path}", "OK")
    log(f"Informe MD:   {md_path}", "OK")
    return json_path, md_path


def main():
    parser = argparse.ArgumentParser(description="Diagnóstico barcode Arte PS↔Odoo↔TPV")
    parser.add_argument("--integration-id", type=int, default=INTEGRATION_ID)
    parser.add_argument("--pilot-candidates", action="store_true")
    parser.add_argument("--pilot-limit", type=int, default=20)
    parser.add_argument("--mark-pilot", action="store_true", help="Marcar available_in_pos en piloto")
    parser.add_argument("--fetch-ps", action="store_true", help="Comparar ean13 API PS vs Odoo")
    parser.add_argument("--fetch-ps-limit", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    log(f"Conectando a {ODOO_URL} / {ODOO_DB}")
    uid, models = connect_odoo()
    integration_id = args.integration_id

    integration = read_integration_flags(models, uid, integration_id)
    log(f"Integración: {integration.get('name')} ({integration.get('state')})")

    log("Cargando variantes mapeadas...")
    records = load_mapped_variants(models, uid, integration_id)
    metrics = compute_metrics(records)
    log(
        f"Mapeadas activas: {metrics['mapped_variants_active']} | "
        f"con barcode Odoo: {metrics['with_odoo_barcode']} | "
        f"TPV escaneables: {metrics['pos_scan_ready']}"
    )

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "odoo_url": ODOO_URL,
        "odoo_db": ODOO_DB,
        "integration_id": integration_id,
        "integration": integration,
        "metrics": metrics,
    }

    if args.pilot_candidates or args.mark_pilot:
        pilots = select_pilot_candidates(records, limit=args.pilot_limit)
        report["pilot_candidates"] = pilots
        log(f"Candidatos piloto: {len(pilots)}")
        for c in pilots[:10]:
            log(
                f"  pp={c['product_id']} code={c['ps_code']} "
                f"ean={c['pilot_barcode']} pos={c['available_in_pos']}"
            )
        if args.mark_pilot:
            marked = mark_pilot_pos(models, uid, pilots, dry_run=args.dry_run)
            report["marked_template_ids"] = marked

    if args.fetch_ps:
        log("Consultando EAN en API PrestaShop (muestra)...")
        url, key = read_integration_api(models, uid, integration_id)
        sample_codes = [r["ps_code"] for r in records if r["ps_code"]][: args.fetch_ps_limit]
        tpl_ids = sorted({parse_ps_code(c)[0] for c in sample_codes if c})
        ps_eans = fetch_ps_ean_by_code(url, key, template_ids=tpl_ids)
        mismatches = []
        for r in records:
            if r["ps_code"] not in ps_eans:
                continue
            ps_ean = ps_eans[r["ps_code"]]
            odoo_bc = r["odoo_barcode"] or r["external_barcode"]
            if ps_ean and ps_ean != odoo_bc:
                mismatches.append(
                    {
                        "ps_code": r["ps_code"],
                        "ps_ean": ps_ean,
                        "odoo_barcode": odoo_bc,
                        "product_id": r["product_id"],
                    }
                )
        report["ps_compare_mismatches"] = mismatches
        log(f"Divergencias PS vs Odoo en muestra: {len(mismatches)}")

    write_report(report, integration_id)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Interrumpido.", "!")
        sys.exit(130)
