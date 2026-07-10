#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diagnóstico SKU Odoo ↔ WooCommerce para Airbici (BD air).

Compara:
  - product.product.default_code (Odoo)
  - integration.product.product.external.external_reference (registro externo Odoo)
  - sku en vivo desde WooCommerce REST API

Uso:
  export ODOO_URL=http://localhost:8069 ODOO_DB=air ODOO_USER=admin ODOO_PASSWORD=admin
  python3 custom_scripts/airbici_sku_diagnostic.py
  python3 custom_scripts/airbici_sku_diagnostic.py --limit 50 --output /tmp/airbici_sku_report.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

import psycopg2
import psycopg2.extras
import requests
from requests.auth import HTTPBasicAuth

INTEGRATION_ID = int(os.environ.get('INTEGRATION_ID', '1'))
DB_HOST = os.environ.get('PGHOST', 'pgdb')
DB_USER = os.environ.get('PGUSER', 'odoo')
DB_PASSWORD = os.environ.get('PGPASSWORD', 'odoo')
DB_NAME = os.environ.get('PGDATABASE', os.environ.get('ODOO_DB', 'air'))

USER_AGENT = 'Odoo-Integration-Woocommerce/1.0'


def db_connect():
    return psycopg2.connect(
        host=DB_HOST, user=DB_USER, password=DB_PASSWORD, dbname=DB_NAME,
    )


def load_integration_settings(cur):
    cur.execute("""
        SELECT woocommerce_import_without_sku,
               woocommerce_import_allow_duplicate_sku,
               woocommerce_validate_barcode
        FROM sale_integration WHERE id = %s
    """, (INTEGRATION_ID,))
    row = cur.fetchone()
    cur.execute("""
        SELECT name, value FROM sale_integration_api_field
        WHERE sia_id = %s AND name IN ('url', 'consumer_key', 'consumer_secret')
    """, (INTEGRATION_ID,))
    api = {r['name']: (r['value'] or '').strip() for r in cur.fetchall()}
    return row, api


def effective_reference(code, sku, import_without_sku):
    ref = (sku or '').strip()
    if ref:
        return ref
    if import_without_sku:
        return str(code)
    return ''


def load_mapped_variants(cur, limit=None):
    sql = """
        SELECT
            e.code AS wc_variant_id,
            e.external_reference AS ext_ref,
            e.external_barcode AS ext_barcode,
            pp.id AS odoo_variant_id,
            pp.default_code AS odoo_sku,
            pp.barcode AS odoo_barcode,
            pt.id AS template_id,
            pt.name::text AS template_name,
            te.code AS wc_template_id,
            te.external_reference AS template_ext_ref,
            pt.default_code AS template_odoo_sku
        FROM integration_product_product_mapping m
        JOIN integration_product_product_external e ON e.id = m.external_product_id
        JOIN integration_product_template_external te ON te.id = e.external_product_template_id
        JOIN product_product pp ON pp.id = m.product_id
        JOIN product_template pt ON pt.id = pp.product_tmpl_id
        WHERE m.integration_id = %s
        ORDER BY e.id
    """
    if limit:
        sql += f' LIMIT {int(limit)}'
    cur.execute(sql, (INTEGRATION_ID,))
    return cur.fetchall()


def wc_fetch_skus(base_url, key, secret, variant_rows):
    """Fetch live SKU for variants; batch parent products for simple SKUs."""
    auth = HTTPBasicAuth(key, secret)
    headers = {'Accept': 'application/json', 'User-Agent': USER_AGENT}
    session = requests.Session()

    by_template = {}
    for row in variant_rows:
        by_template.setdefault(row['wc_template_id'], []).append(row)

    live = {}
    for tpl_id, variants in by_template.items():
        url = f"{base_url.rstrip('/')}/wp-json/wc/v3/products/{tpl_id}"
        try:
            resp = session.get(
                url, auth=auth, headers=headers,
                params={'lang': 'es', '_fields': 'id,type,sku'},
                timeout=45,
            )
            if resp.status_code != 200:
                live[tpl_id] = {'error': f'HTTP {resp.status_code}'}
                continue
            product = resp.json()
            ptype = product.get('type')
            if ptype in ('simple', 'external'):
                live[tpl_id] = {'type': ptype, 'variants': {
                    str(product['id']): (product.get('sku') or '').strip(),
                }}
            elif ptype == 'variable':
                vurl = f"{url}/variations"
                vresp = session.get(
                    vurl, auth=auth, headers=headers,
                    params={'lang': 'es', 'per_page': 100, '_fields': 'id,sku'},
                    timeout=45,
                )
                if vresp.status_code != 200:
                    live[tpl_id] = {'error': f'variations HTTP {vresp.status_code}'}
                    continue
                vmap = {str(v['id']): (v.get('sku') or '').strip() for v in vresp.json()}
                live[tpl_id] = {'type': ptype, 'variants': vmap}
            else:
                live[tpl_id] = {'type': ptype, 'variants': {}}
        except requests.RequestException as exc:
            live[tpl_id] = {'error': str(exc)}

    return live


def diagnose(rows, settings, live_data):
    import_without_sku = settings['woocommerce_import_without_sku']
    issues = []
    stats = Counter()

    for row in rows:
        wc_vid = row['wc_variant_id']
        tpl_id = row['wc_template_id']
        tpl_live = live_data.get(tpl_id, {})
        wc_sku_live = ''
        if 'variants' in tpl_live:
            wc_sku_live = tpl_live['variants'].get(wc_vid, '')
        elif 'error' in tpl_live:
            stats['wc_api_error'] += 1
            continue

        expected_ref = effective_reference(wc_vid, wc_sku_live, import_without_sku)
        ext_ref = (row['ext_ref'] or '').strip()
        odoo_sku = (row['odoo_sku'] or '').strip()

        if wc_sku_live:
            stats['wc_has_sku'] += 1
        else:
            stats['wc_empty_sku'] += 1

        if not odoo_sku:
            stats['odoo_empty_sku'] += 1
        if ext_ref and ext_ref.isdigit() and wc_sku_live:
            stats['ext_ref_is_id_but_wc_has_sku'] += 1

        mismatch_live_vs_ext = expected_ref and ext_ref != expected_ref
        mismatch_live_vs_odoo = expected_ref and odoo_sku and odoo_sku != expected_ref
        mismatch_ext_vs_odoo = ext_ref != odoo_sku

        if mismatch_live_vs_ext or mismatch_live_vs_odoo or (not odoo_sku and expected_ref):
            kind = []
            if mismatch_live_vs_ext:
                kind.append('live_vs_external')
                stats['live_vs_external'] += 1
            if mismatch_live_vs_odoo:
                kind.append('live_vs_odoo')
                stats['live_vs_odoo'] += 1
            if mismatch_ext_vs_odoo:
                kind.append('external_vs_odoo')
                stats['external_vs_odoo'] += 1
            if not odoo_sku and expected_ref:
                kind.append('odoo_missing_sku')
                stats['odoo_missing_sku_actionable'] += 1

            issues.append({
                'wc_template_id': tpl_id,
                'wc_variant_id': wc_vid,
                'template_name': row['template_name'],
                'wc_sku_live': wc_sku_live,
                'external_reference': ext_ref,
                'odoo_sku': odoo_sku,
                'expected_ref_after_sync': expected_ref,
                'issue_types': kind,
            })

    return issues, stats


def main():
    parser = argparse.ArgumentParser(description='Diagnóstico SKU Airbici Odoo vs WooCommerce')
    parser.add_argument('--limit', type=int, default=0, help='Limitar variantes a consultar en WC API (0=todas)')
    parser.add_argument('--output', default='', help='Guardar JSON con incidencias')
    parser.add_argument('--skip-wc', action='store_true', help='Solo análisis SQL, sin llamar WC API')
    args = parser.parse_args()

    conn = db_connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    settings, api = load_integration_settings(cur)
    rows = load_mapped_variants(cur, limit=args.limit or None)

    print('=== Config integración id=%s ===' % INTEGRATION_ID)
    print('  woocommerce_import_without_sku:', settings['woocommerce_import_without_sku'])
    print('  woocommerce_import_allow_duplicate_sku:', settings['woocommerce_import_allow_duplicate_sku'])
    print('  Variantes mapeadas analizadas:', len(rows))

    live_data = {}
    if not args.skip_wc:
        if not all(api.get(k) for k in ('url', 'consumer_key', 'consumer_secret')):
            print('ERROR: faltan credenciales WC en sale_integration_api_field', file=sys.stderr)
            sys.exit(1)
        print('Consultando WooCommerce en vivo (%s)…' % api['url'])
        live_data = wc_fetch_skus(api['url'], api['consumer_key'], api['consumer_secret'], rows)
        issues, stats = diagnose(rows, settings, live_data)
    else:
        # SQL-only quick stats
        issues, stats = [], Counter()
        for row in rows:
            if not (row['odoo_sku'] or '').strip():
                stats['odoo_empty_sku'] += 1
            ext = (row['ext_ref'] or '').strip()
            if ext.isdigit():
                stats['ext_ref_is_wc_id'] += 1

    print('\n=== Resumen ===')
    for key, val in sorted(stats.items()):
        print(f'  {key}: {val}')

    if issues:
        print('\n=== Muestra incidencias (hasta 20) ===')
        for item in issues[:20]:
            print(
                f"  WC#{item['wc_variant_id']} | live={item['wc_sku_live']!r} "
                f"ext={item['external_reference']!r} odoo={item['odoo_sku']!r} "
                f"→ esperado={item['expected_ref_after_sync']!r} | {','.join(item['issue_types'])}"
            )
        print(f'\nTotal incidencias detectadas: {len(issues)}')
    else:
        print('\nSin incidencias live vs Odoo/external en el alcance analizado.')

    if args.output:
        payload = {
            'integration_id': INTEGRATION_ID,
            'settings': dict(settings),
            'stats': dict(stats),
            'issues_count': len(issues),
            'issues': issues,
        }
        with open(args.output, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print('Reporte guardado en', args.output)

    cur.close()
    conn.close()


if __name__ == '__main__':
    main()
