#!/usr/bin/env python3
"""Prueba importación Miravia con auto-creación de producto y estado shipped."""
from datetime import datetime

print("=" * 70)
print("TEST IMPORT MIRAVIA (shipped + auto-producto)", datetime.now().isoformat())
print("=" * 70)

Sale = env["sale.order"].sudo()
OAuth = env["miravia.oauth.request"].sudo()

TARGET_ORDER_ID = "9054856460129"

for oauth in OAuth.search([("state", "=", "completed")]):
    print(f"\n--- OAuth {oauth.id} {oauth.name} ---")
    print(f"  seller_id={oauth.miravia_seller_id!r}")
    print(f"  stock_field={oauth.field_stock_quantity.name if oauth.field_stock_quantity else 'free_qty'}")
    print(f"  import_statuses={Sale._miravia_order_status_codes(oauth)}")

    before = Sale.search_count([
        ("miravia_oauth_request_id", "=", oauth.id),
        ("miravia_order_id", "!=", False),
    ])
    existing = Sale.search([
        ("miravia_order_id", "=", TARGET_ORDER_ID),
        ("miravia_oauth_request_id", "=", oauth.id),
    ], limit=1)
    print(f"  pedidos antes={before} | target {TARGET_ORDER_ID} existe={bool(existing)} ({existing.name if existing else '-'})")

    try:
        Sale.fetch_orders_from_miravia_api(oauth)
        print("  fetch_orders_from_miravia_api: OK")
    except Exception as exc:
        print(f"  EXCEPCIÓN: {exc}")
        import traceback
        traceback.print_exc()

    after = Sale.search_count([
        ("miravia_oauth_request_id", "=", oauth.id),
        ("miravia_order_id", "!=", False),
    ])
    so = Sale.search([
        ("miravia_order_id", "=", TARGET_ORDER_ID),
        ("miravia_oauth_request_id", "=", oauth.id),
    ], limit=1)
    print(f"  pedidos después={after} (+{after - before})")
    if so:
        print(f"  IMPORTADO: {so.name} state={so.state} miravia_status={so.miravia_status}")
        for line in so.order_line:
            p = line.product_id
            print(
                f"    línea: {p.default_code} miravia_id={p.miravia_id} "
                f"oauth={p.miravia_oauth_request_id.name if p.miravia_oauth_request_id else '-'}"
            )
        for pick in so.picking_ids:
            print(f"    picking {pick.name}: state={pick.state}")

env.cr.commit()
print("\nCOMMIT realizado en la base de datos.")

print("\n" + "=" * 70)
print("FIN")
print("=" * 70)
