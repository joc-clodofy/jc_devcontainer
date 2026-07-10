#!/usr/bin/env python3
"""Prueba API Miravia con distintos status y ventanas de fecha."""
import json
from datetime import datetime, timedelta

from odoo import fields
from odoo.addons.miravia_odoo_multi_connector.models.common import enrich_payload_with_oauth

OAuth = env["miravia.oauth.request"].sudo()
Sale = env["sale.order"].sudo()
now = datetime.now()

for oauth in OAuth.search([("state", "=", "completed")]):
    print(f"\n{'='*60}\nTIENDA {oauth.id}: {oauth.name}\n{'='*60}")
    test_env = bool(oauth.miravia_group_api_test_environment)
    country = (oauth.miravia_api_country or "ES").strip()
    marketplace = (oauth.miravia_api_marketplace or "miravia").strip()

    # Pedidos ya en Odoo
    existing = Sale.search([
        ("miravia_oauth_request_id", "=", oauth.id),
        ("miravia_order_id", "!=", False),
    ])
    print(f"Pedidos en Odoo: {len(existing)}")
    for so in existing[:5]:
        print(f"  SO {so.name} | miravia_id={so.miravia_order_id} | status={so.miravia_status} | date={so.date_order}")

    statuses = ["pending", "ready_to_ship", "packed", "shipped", "delivered", ""]
    date_windows = [
        ("hoy (fetch_from)", oauth.miravia_orders_fetch_from),
        ("últimos 30 días", fields.Date.to_string(fields.Date.today() - timedelta(days=30))),
        ("últimos 90 días", fields.Date.to_string(fields.Date.today() - timedelta(days=90))),
        ("2025-01-01", "2025-01-01"),
    ]

    for label, base_date in date_windows:
        tz = (oauth.miravia_orders_tz_suffix or "+08:00").strip()
        if not tz.startswith(("+", "-")):
            tz = "+08:00"
        iso_from = f"{fields.Date.to_string(base_date)}T00:00:00{tz}"

        for status in statuses:
            params = {
                "sort_direction": "DESC",
                "country": country,
                "created_after": iso_from,
                "update_after": iso_from,
                "marketplace": marketplace,
                "limit": 10,
                "offset": 0,
                "sort_by": "updated_at",
            }
            if status:
                params["status"] = status
            params = enrich_payload_with_oauth(params, oauth)
            params = Sale._miravia_apply_test_env_order_params(params, test_env)
            try:
                resp = env["miravia.api"].get_request("/orders/get", params, test_environment=test_env)
                parsed = json.loads(resp["response_data"])
                if parsed.get("code") != "0":
                    print(f"  [{label}] status={status or 'ALL'} -> API error {parsed.get('code')}: {parsed.get('message')}")
                    continue
                data = parsed.get("data") or {}
                batch = data.get("orders") or []
                count = data.get("count", len(batch))
                if count or batch:
                    print(f"  [{label}] status={status or 'ALL'} -> count={count} sample_ids={[o.get('order_id') for o in batch[:3]]}")
            except Exception as e:
                print(f"  [{label}] status={status} -> EXC: {e}")

# Usuario sistema (dominio real del módulo)
from odoo.addons.miravia_odoo_multi_connector.models.common import MIRAVIA_SYSTEM_USER_DOMAIN
mu = env["res.users"].sudo().search(MIRAVIA_SYSTEM_USER_DOMAIN, limit=1)
print(f"\nMiravia system user (MIRAVIA_SYSTEM_USER_DOMAIN): id={mu.id if mu else None} login={mu.login if mu else None} active={mu.active if mu else None}")

# Cron fetch orders custom name
cron = env["ir.cron"].sudo().search([("name", "ilike", "importar pedidos")], limit=1)
print(f"Cron importar pedidos: id={cron.id} active={cron.active} code={cron.code}")

print("\nNOTA: action_fetch_orders NO usa queue_job; es síncrono y solo muestra notificación.")
