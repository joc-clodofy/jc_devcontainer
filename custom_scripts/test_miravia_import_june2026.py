#!/usr/bin/env python3
"""Importación forzada de pedidos Miravia de junio 2026 (ejecutar con odoo shell)."""
import json
from datetime import datetime

from odoo import fields

ISO_FROM = "2026-06-01T00:00:00+08:00"
JUNE_START = "2026-06-01"
JUNE_END = "2026-06-30"

Sale = env["sale.order"].sudo()
OAuth = env["miravia.oauth.request"].sudo()

print("=" * 72)
print("IMPORTACIÓN FORZADA MIRAVIA — JUNIO 2026", datetime.now().isoformat())
print("=" * 72)

summary = []

for oauth in OAuth.search([("state", "=", "completed")]):
    status_codes = Sale._miravia_order_status_codes(oauth)
    print(f"\n{'='*60}")
    print(f"TIENDA {oauth.id}: {oauth.name}")
    print(f"  estados configurados: {status_codes}")
    print(f"  ventana API: created_after={ISO_FROM}")

    before_ids = set(Sale.search([
        ("miravia_oauth_request_id", "=", oauth.id),
        ("miravia_order_id", "!=", False),
        ("date_order", ">=", JUNE_START),
        ("date_order", "<=", f"{JUNE_END} 23:59:59"),
    ]).mapped("miravia_order_id"))

    test_env = bool(oauth.miravia_group_api_test_environment)
    country = (oauth.miravia_api_country or "ES").strip() or "ES"
    marketplace = (oauth.miravia_api_marketplace or "miravia").strip() or "miravia"
    limit = max(1, min(int(oauth.miravia_orders_page_limit or 80), 200))

    api_orders_by_id = {}
    api_by_status = {}
    for status_code in status_codes:
        batch = Sale._fetch_orders_from_miravia_api_for_status(
            oauth, status_code, test_env, ISO_FROM, country, marketplace, limit,
        )
        api_by_status[status_code] = len(batch)
        for order in batch:
            api_orders_by_id[str(order["order_id"])] = order

    print(f"  pedidos API junio (deduplicados): {len(api_orders_by_id)}")
    for st, cnt in api_by_status.items():
        print(f"    status={st}: {cnt}")

    new_orders = []
    skipped = []
    for oid, order in api_orders_by_id.items():
        existing = Sale.search([
            ("miravia_order_id", "=", oid),
            ("miravia_oauth_request_id", "=", oauth.id),
        ], limit=1)
        if existing:
            skipped.append((oid, existing.name))
        else:
            new_orders.append(order)

    print(f"  ya en Odoo: {len(skipped)} | nuevos a importar: {len(new_orders)}")

    order_items = Sale._fetch_orders_items_from_miravia_api(new_orders, test_env, oauth)
    errors = []
    imported = []

    for order in new_orders:
        oid = str(order["order_id"])
        statuses = order.get("statuses") or ["?"]
        wh = order.get("warehouse_code", "?")
        try:
            result = Sale.create_miravia_order(order, order_items, oauth)
            if result:
                errors.append((oid, statuses, wh, "; ".join(result)))
                continue
            so = Sale.search([
                ("miravia_order_id", "=", oid),
                ("miravia_oauth_request_id", "=", oauth.id),
            ], limit=1)
            if so:
                Sale._miravia_finalize_imported_order(so, order)
                imported.append((oid, so.name, so.miravia_status, so.state, wh))
            else:
                errors.append((oid, statuses, wh, "create_miravia_order sin error pero SO no encontrado"))
        except Exception as exc:
            errors.append((oid, statuses, wh, str(exc)))

    after_ids = set(Sale.search([
        ("miravia_oauth_request_id", "=", oauth.id),
        ("miravia_order_id", "!=", False),
        ("date_order", ">=", JUNE_START),
        ("date_order", "<=", f"{JUNE_END} 23:59:59"),
    ]).mapped("miravia_order_id"))

    print(f"\n  RESULTADO {oauth.name}:")
    print(f"    importados OK: {len(imported)}")
    for row in imported:
        print(f"      + {row[0]} -> {row[1]} status={row[2]} state={row[3]} wh={row[4]}")
    print(f"    omitidos (ya existían): {len(skipped)}")
    for oid, name in skipped[:10]:
        print(f"      = {oid} -> {name}")
    if len(skipped) > 10:
        print(f"      ... y {len(skipped) - 10} más")
    print(f"    errores: {len(errors)}")
    for row in errors:
        print(f"      ! {row[0]} statuses={row[1]} wh={row[2]}")
        print(f"        {row[3]}")

    # Pedidos API que no están en Odoo tras el proceso
    missing_in_odoo = []
    for oid, order in api_orders_by_id.items():
        so = Sale.search([
            ("miravia_order_id", "=", oid),
            ("miravia_oauth_request_id", "=", oauth.id),
        ], limit=1)
        if not so:
            missing_in_odoo.append((oid, order.get("statuses"), order.get("warehouse_code")))

    if missing_in_odoo:
        print(f"    FALTAN en Odoo tras import: {len(missing_in_odoo)}")
        for row in missing_in_odoo:
            print(f"      ? {row[0]} statuses={row[1]} wh={row[2]}")

    summary.append({
        "oauth": oauth.name,
        "api_total": len(api_orders_by_id),
        "imported": len(imported),
        "skipped": len(skipped),
        "errors": len(errors),
        "missing": len(missing_in_odoo),
        "june_odoo_before": len(before_ids),
        "june_odoo_after": len(after_ids),
    })

    oauth.write({"last_fetch_orders_on": fields.Datetime.now()})

print("\n" + "=" * 72)
print("RESUMEN GLOBAL")
for row in summary:
    print(
        f"  {row['oauth']}: API={row['api_total']} | "
        f"+{row['imported']} nuevos | ={row['skipped']} existentes | "
        f"!{row['errors']} errores | ?{row['missing']} faltantes | "
        f"junio Odoo {row['june_odoo_before']}->{row['june_odoo_after']}"
    )

env.cr.commit()
print("\nCOMMIT realizado.")
print("=" * 72)
