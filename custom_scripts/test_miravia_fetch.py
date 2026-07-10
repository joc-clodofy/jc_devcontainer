#!/usr/bin/env python3
"""Diagnóstico Miravia: instancias OAuth y fetch de pedidos (ejecutar con odoo shell)."""
import json
import traceback
from datetime import datetime

print("=" * 70)
print("MIRAVIA DIAGNÓSTICO -", datetime.now().isoformat())
print("=" * 70)

OAuth = env["miravia.oauth.request"].sudo()
Sale = env["sale.order"].sudo()
ApiLog = env["miravia.api.log"].sudo()
MiraviaUser = env["res.users"].sudo().search([
    ("login", "=", "miravia_system_user"),
], limit=1)

print("\n--- Usuario sistema Miravia ---")
print("  encontrado:", bool(MiraviaUser), "| active:", MiraviaUser.active if MiraviaUser else None)

oauths = OAuth.search([])
print(f"\n--- Instancias miravia.oauth.request: {len(oauths)} ---")
now = datetime.now()
for o in oauths:
    token_ok = bool(o.access_token and o.expires_in and o.expires_in > now)
    print(f"\n  ID={o.id} | {o.name!r}")
    print(f"    state={o.state} | seller_id={o.miravia_seller_id!r}")
    print(f"    token válido (expires_in > now): {token_ok}")
    print(f"    expires_in={o.expires_in} | refresh_expires_in={o.refresh_expires_in}")
    print(f"    auth_code={'***' if o.auth_code else None}")
    print(f"    app_key={'***' if o.miravia_group_api_app_key else 'VACÍO'}")
    print(f"    secret={'***' if o.miravia_group_api_secret else 'VACÍO'}")
    print(f"    server={o.miravia_group_api_server_address!r}")
    print(f"    test_env={o.miravia_group_api_test_environment}")
    print(f"    fetch_from={o.miravia_orders_fetch_from} | country={o.miravia_api_country} | marketplace={o.miravia_api_marketplace}")
    print(f"    cron_fetch_active={o.miravia_cron_fetch_orders_active}")
    print(f"    last_fetch_orders_on={o.last_fetch_orders_on}")
    print(f"    warehouse_id={o.warehouse_id.id if o.warehouse_id else None} ({o.warehouse_id.name if o.warehouse_id else '-'})")
    print(f"    pricelist={o.miravia_group_product_pricelist.id if o.miravia_group_product_pricelist else None}")
    print(f"    delivery={o.miravia_group_delivery_carrier.id if o.miravia_group_delivery_carrier else None}")
    orders_count = Sale.search_count([
        ("miravia_oauth_request_id", "=", o.id),
        ("miravia_order_id", "!=", False),
    ])
    products_count = env["product.product"].sudo().search_count([
        ("miravia_oauth_request_id", "=", o.id),
    ])
    wh_codes = env["stock.warehouse"].sudo().search([
        ("miravia_warehouse_code", "!=", False),
    ]).mapped("miravia_warehouse_code")
    print(f"    pedidos_odoo={orders_count} | productos_vinculados={products_count}")
    print(f"    almacenes_con_codigo_miravia={wh_codes}")

completed = oauths.filtered(lambda x: x.state == "completed")
print(f"\n--- Instancias completed: {len(completed)} ---")

# Crons
crons = env["ir.cron"].sudo().search([("name", "ilike", "Miravia%")])
print("\n--- Crons Miravia ---")
for c in crons:
    print(f"  [{c.id}] active={c.active} | {c.name} | interval={c.interval_number} {c.interval_type}")

# queue_job recientes miravia
if "queue.job" in env:
    jobs = env["queue.job"].sudo().search([
        "|", ("name", "ilike", "miravia"), ("func_string", "ilike", "miravia"),
    ], order="date_created desc", limit=5)
    print(f"\n--- Últimos queue.job con 'miravia' ({len(jobs)} mostrados) ---")
    for j in jobs:
        print(f"  {j.date_created} | state={j.state} | {j.name[:80] if j.name else j.func_string[:80]}")
else:
    print("\n--- queue.job no disponible ---")

print("\n" + "=" * 70)
print("PRUEBA API /orders/get (sin crear pedidos en Odoo)")
print("=" * 70)

for oauth in completed:
    print(f"\n>>> Instancia {oauth.id} - {oauth.name}")
    if not oauth.auth_code or not oauth.access_token:
        print("  SKIP: falta auth_code o access_token")
        continue
    if oauth.expires_in and oauth.expires_in <= now:
        print("  SKIP: token expirado")
        continue

    test_env = bool(oauth.miravia_group_api_test_environment)
    iso_from = Sale._miravia_orders_datetime_iso_start(oauth)
    limit = max(1, min(int(oauth.miravia_orders_page_limit or 80), 200))
    country = (oauth.miravia_api_country or "ES").strip() or "ES"
    marketplace = (oauth.miravia_api_marketplace or "miravia").strip() or "miravia"

    from odoo.addons.miravia_odoo_multi_connector.models.common import enrich_payload_with_oauth

    params = {
        "status": "pending",
        "sort_direction": "DESC",
        "country": country,
        "created_after": iso_from,
        "update_after": iso_from,
        "marketplace": marketplace,
        "limit": limit,
        "offset": 0,
        "sort_by": "updated_at",
    }
    params = enrich_payload_with_oauth(params, oauth)
    params = Sale._miravia_apply_test_env_order_params(params, test_env)

    print(f"  params clave: status=pending, country={country}, marketplace={marketplace}")
    print(f"  created_after={iso_from}, test_env={test_env}")

    logs_before = ApiLog.search_count([])
    try:
        m_orders = env["miravia.api"].get_request(
            "/orders/get", params, test_environment=test_env
        )
        code = m_orders.get("response_code")
        raw = m_orders.get("response_data", "")
        try:
            parsed = json.loads(raw)
        except Exception as e:
            parsed = {"_parse_error": str(e), "_raw_preview": raw[:500]}
        api_code = parsed.get("code") if isinstance(parsed, dict) else None
        msg = parsed.get("message") if isinstance(parsed, dict) else None
        data = (parsed.get("data") or {}) if isinstance(parsed, dict) else {}
        batch = data.get("orders") or []
        count = data.get("count")
        print(f"  HTTP response_code={code} | miravia code={api_code} | message={msg}")
        print(f"  orders en respuesta: len(batch)={len(batch)} | count={count}")
        if batch:
            o0 = batch[0]
            print(f"  primer pedido: order_id={o0.get('order_id')} status={o0.get('statuses')} warehouse={o0.get('warehouse_code')}")
    except Exception as e:
        print(f"  ERROR API: {e}")
        traceback.print_exc()

    logs_after = ApiLog.search([], order="id desc", limit=1)
    if logs_after:
        lg = logs_after[0]
        print(f"  último api.log: id={lg.id} http={lg.response_code} url={lg.url[:80] if lg.url else ''}...")

print("\n" + "=" * 70)
print("PRUEBA fetch_orders_from_miravia_api (completo)")
print("=" * 70)

for oauth in completed:
    print(f"\n>>> FETCH COMPLETO instancia {oauth.id} - {oauth.name}")
    orders_before = Sale.search_count([
        ("miravia_oauth_request_id", "=", oauth.id),
        ("miravia_order_id", "!=", False),
    ])
    try:
        Sale.fetch_orders_from_miravia_api(oauth)
        print("  fetch_orders_from_miravia_api: terminó sin excepción")
    except Exception as e:
        print(f"  EXCEPCIÓN: {e}")
        traceback.print_exc()
    orders_after = Sale.search_count([
        ("miravia_oauth_request_id", "=", oauth.id),
        ("miravia_order_id", "!=", False),
    ])
    oauth.invalidate_recordset(["last_fetch_orders_on"])
    print(f"  pedidos antes={orders_before} después={orders_after} (+{orders_after - orders_before})")
    print(f"  last_fetch_orders_on={oauth.last_fetch_orders_on}")

print("\n--- action_fetch_orders (como botón UI) ---")
for oauth in completed[:1]:
    try:
        result = oauth.action_fetch_orders()
        print(f"  instancia {oauth.id} retorno: {result}")
    except Exception as e:
        print(f"  EXCEPCIÓN action_fetch_orders: {e}")
        traceback.print_exc()

print("\n" + "=" * 70)
print("FIN")
print("=" * 70)
