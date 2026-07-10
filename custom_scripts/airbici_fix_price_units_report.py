#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dry-run report: _airbici_fix_tax_included_price_units on air2 to-invoice WC orders."""

import http.client
import json
import sys

import requests
from requests.auth import HTTPBasicAuth

if http.client._MAXHEADERS < 1000:
    http.client._MAXHEADERS = 1000

# Run via: odoo-bin shell -c air.conf -d air2 < custom_scripts/airbici_fix_price_units_report.py

from odoo.tools.float_utils import float_compare  # noqa: E402


def main(env):
    if not hasattr(env['sale.order'], '_airbici_fix_tax_included_price_units'):
        print('ERROR: actualiza integration_airbici_tools primero', file=sys.stderr)
        return 1

    precision = env['decimal.precision'].precision_get('Product Price')
    orders = env['sale.order'].search([
        ('integration_id.type_api', '=', 'woocommerce'),
        ('invoice_status', '=', 'to invoice'),
    ], order='name')

    updated = []
    unchanged = []

    for order in orders:
        before_units = {
            line.id: line.price_unit
            for line in order.order_line.filtered(lambda l: not l.display_type)
        }
        before_total = order.amount_total
        before_tax_totals = order.tax_totals.get('amount_total') if order.tax_totals else None

        order._airbici_fix_tax_included_price_units()
        order.invalidate_recordset(['amount_total', 'amount_untaxed', 'amount_tax', 'tax_totals'])

        changed_lines = []
        for line in order.order_line.filtered(lambda l: not l.display_type):
            old = before_units.get(line.id)
            if old is not None and float_compare(old, line.price_unit, precision_digits=precision) != 0:
                changed_lines.append({
                    'name': line.name[:55],
                    'old_unit': old,
                    'new_unit': line.price_unit,
                    'is_delivery': line.is_delivery,
                })

        if changed_lines:
            updated.append({
                'name': order.name,
                'wc_id': order.name[3:] if order.name.startswith('WC-') else None,
                'before_total': before_total,
                'after_total': order.amount_total,
                'before_tax_totals': before_tax_totals,
                'after_tax_totals': order.tax_totals.get('amount_total'),
                'integration_amount_total': order.integration_amount_total,
                'lines': changed_lines,
            })
        else:
            unchanged.append(order.name)

    fields = env['sale.integration.api.field'].search([
        ('sia_id', '=', 1),
        ('name', 'in', ['url', 'consumer_key', 'consumer_secret']),
    ])
    cfg = {f.name: (f.value or '').strip() for f in fields}
    auth = HTTPBasicAuth(cfg['consumer_key'], cfg['consumer_secret'])
    base = cfg['url'].rstrip('/')
    headers = {'User-Agent': 'Odoo-Integration-Woocommerce/1.0'}

    wc_results = []
    for item in updated:
        wc_id = item['wc_id']
        if not wc_id or not wc_id.isdigit():
            continue
        try:
            r = requests.get(
                f'{base}/wp-json/wc/v3/orders/{wc_id}',
                auth=auth, headers=headers,
                params={'_fields': 'id,number,status,total'},
                timeout=45,
            )
            if r.status_code != 200:
                wc_results.append({**item, 'wc_total': None, 'wc_error': f'HTTP {r.status_code}'})
                continue
            data = r.json()
            wc_total = float(data.get('total', 0))
            odoo_total = item['after_total']
            wc_results.append({
                **item,
                'wc_total': wc_total,
                'wc_status': data.get('status'),
                'match_wc': float_compare(odoo_total, wc_total, precision_digits=2) == 0,
                'delta': round(odoo_total - wc_total, 2),
            })
        except Exception as exc:
            wc_results.append({**item, 'wc_total': None, 'wc_error': str(exc)})

    spot = []
    for name in unchanged[:10]:
        wc_id = name.replace('WC-', '')
        order = env['sale.order'].search([('name', '=', name)], limit=1)
        try:
            r = requests.get(
                f'{base}/wp-json/wc/v3/orders/{wc_id}',
                auth=auth, headers=headers,
                params={'_fields': 'id,total,status'}, timeout=45,
            )
            if r.status_code == 200:
                wc_total = float(r.json()['total'])
                spot.append({
                    'name': name,
                    'odoo_total': order.amount_total,
                    'tax_totals': order.tax_totals.get('amount_total'),
                    'integration_amount_total': order.integration_amount_total,
                    'wc_total': wc_total,
                    'match_odoo_wc': float_compare(order.amount_total, wc_total, 2) == 0,
                    'match_tax_totals_wc': float_compare(
                        order.tax_totals.get('amount_total', 0), wc_total, 2,
                    ) == 0,
                })
        except Exception as exc:
            spot.append({'name': name, 'error': str(exc)})

    mismatches = [x for x in wc_results if x.get('wc_total') is not None and not x.get('match_wc')]
    tax_fixed = [x for x in wc_results if x.get('before_tax_totals') != x.get('after_tax_totals')]

    report = {
        'total_to_invoice': len(orders),
        'updated_count': len(updated),
        'unchanged_count': len(unchanged),
        'tax_totals_fixed_count': len(tax_fixed),
        'wc_match_after_fix': sum(1 for x in wc_results if x.get('match_wc')),
        'wc_mismatch_after_fix': len(mismatches),
        'updated_orders': wc_results,
        'wc_mismatches': mismatches,
        'unchanged_sample_vs_wc': spot,
        'note': 'DRY-RUN: transaction rolled back, no DB changes saved',
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    # When executed inside odoo shell, `env` is injected
    raise SystemExit(main(env))  # noqa: F821
