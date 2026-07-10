#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Audita precios Odoo vs WooCommerce para productos enlazados (Happy Japan / LTB).

Compara list_price en Odoo con regular_price en WC y detecta el patrón
"copiado tal cual" (bruto WC guardado como neto Odoo) vs "ya corregido" (neto = WC/1.21).

Uso local:
  python3 scripts/jap_wc_price_audit.py --config happy.conf --db jap

Uso producción (solo lectura):
  python3 scripts/jap_wc_price_audit.py \\
    --xmlrpc-url https://... --db DB --user admin --password '...'

Aplicar corrección (divide WC regular_price entre factor IVA si aplica):
  python3 scripts/jap_wc_price_audit.py --config happy.conf --db jap --execute
"""

from __future__ import annotations

import argparse
import csv
import sys
import xmlrpc.client
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

INTEGRATION_ID = 1
TAX_RATE = 0.21  # IVA España estándar
TOLERANCE = 0.02  # € tolerancia redondeo


@dataclass
class Row:
    odoo_id: int
    sku: str
    name: str
    odoo_price: float
    wc_price: float | None
    wc_id: str | None
    expected_net: float | None
    status: str
    delta_if_fix: float | None


def round2(x: float) -> float:
    return round(x, 2)


def classify(odoo_price: float, wc_gross: float) -> tuple[str, float, float | None]:
    """Devuelve (status, expected_net, delta_if_fix)."""
    expected_net = round2(wc_gross / (1 + TAX_RATE))
    gross_from_odoo = round2(odoo_price * (1 + TAX_RATE))

    if abs(odoo_price - wc_gross) <= TOLERANCE:
        return 'MAL_COPIADO_BRUTO', expected_net, round2(odoo_price - expected_net)
    if abs(odoo_price - expected_net) <= TOLERANCE:
        return 'OK_NETO', expected_net, 0.0
    if abs(gross_from_odoo - wc_gross) <= TOLERANCE:
        return 'OK_BRUTO_CALCULADO', expected_net, 0.0
    if odoo_price < 0.02 and wc_gross > 0:
        return 'ODOO_CERO', expected_net, round2(odoo_price - expected_net)
    return 'OTRO', expected_net, round2(odoo_price - expected_net)


def fetch_wc_prices_by_sku(url: str, user: str, password: str, skus: list[str]) -> dict[str, dict]:
    import requests
    from requests.auth import HTTPBasicAuth

    auth = HTTPBasicAuth(user, password)
    base = f'{url.rstrip("/")}/wp-json/wc/v3/products'
    result = {}
    # WC API: batch by sku one at a time (no bulk sku filter for many)
    for i, sku in enumerate(skus):
        if not sku:
            continue
        try:
            r = requests.get(base, params={'sku': sku, 'per_page': 5}, auth=auth, timeout=30)
            r.raise_for_status()
            for p in r.json():
                if (p.get('sku') or '').strip() == sku.strip():
                    result[sku] = {
                        'id': str(p['id']),
                        'regular_price': float(p.get('regular_price') or 0),
                        'name': p.get('name', ''),
                    }
                    break
        except Exception as e:
            result[sku] = {'error': str(e)}
        if (i + 1) % 50 == 0:
            print(f'  WC API: {i + 1}/{len(skus)} SKUs consultados...', flush=True)
    return result


def _odoo_env(config: str, db: str):
    workspace = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(workspace / 'odoo'))
    import odoo
    from odoo.tools import config as odoo_config

    odoo_config.parse_config(['-c', str(workspace / config), '-d', db])
    registry = odoo.registry(db)
    cr = registry.cursor()
    return odoo.api.Environment(cr, odoo.SUPERUSER_ID, {}), cr, registry


def get_odoo_products_local(config: str, db: str) -> list[dict]:
    env, cr, registry = _odoo_env(config, db)
    try:
        mappings = env['integration.product.template.mapping'].search([
            ('integration_id', '=', INTEGRATION_ID),
            ('template_id', '!=', False),
        ])
        products = mappings.mapped('template_id').filtered('active')
        rows = []
        for pt in products:
            ext = mappings.filtered(lambda m: m.template_id == pt)[:1].external_template_id
            rows.append({
                'id': pt.id,
                'sku': (pt.default_code or '').strip(),
                'name': pt.name or '',
                'list_price': float(pt.list_price or 0),
                'wc_ext_code': ext.code if ext else '',
            })
        return rows
    finally:
        cr.close()


def get_odoo_products_xmlrpc(url: str, db: str, user: str, password: str) -> list[dict]:
    common = xmlrpc.client.ServerProxy(f'{url.rstrip("/")}/xmlrpc/2/common')
    uid = common.authenticate(db, user, password, {})
    if not uid:
        raise SystemExit('Autenticación XML-RPC fallida')
    models = xmlrpc.client.ServerProxy(f'{url.rstrip("/")}/xmlrpc/2/object')

    def kw(model, method, args=None, kwargs=None):
        return models.execute_kw(db, uid, password, model, method, args or [], kwargs or {})

    map_ids = kw('integration.product.template.mapping', 'search', [[
        ('integration_id', '=', INTEGRATION_ID),
        ('template_id', '!=', False),
    ]])
    mappings = kw('integration.product.template.mapping', 'read', [map_ids], {
        'fields': ['template_id', 'external_template_id'],
    })
    tmpl_ids = list({m['template_id'][0] for m in mappings if m.get('template_id')})
    products = kw('product.template', 'read', [tmpl_ids], {
        'fields': ['id', 'default_code', 'name', 'list_price', 'active'],
    })
    ext_ids = list({m['external_template_id'][0] for m in mappings if m.get('external_template_id')})
    exts = {e['id']: e for e in kw('integration.product.template.external', 'read', [ext_ids], {'fields': ['code', 'name']})}
    tmpl_to_ext = {}
    for m in mappings:
        if m.get('template_id') and m.get('external_template_id'):
            tmpl_to_ext[m['template_id'][0]] = m['external_template_id'][0]

    rows = []
    for p in products:
        if not p.get('active'):
            continue
        ext_id = tmpl_to_ext.get(p['id'])
        ext = exts.get(ext_id, {}) if ext_id else {}
        rows.append({
            'id': p['id'],
            'sku': (p.get('default_code') or '').strip(),
            'name': p.get('name') or '',
            'list_price': float(p.get('list_price') or 0),
            'wc_ext_code': ext.get('code', ''),
        })
    return rows


def get_wc_credentials_local(env) -> tuple[str, str, str]:
    fields = env['sale.integration.api.field'].search([
        ('sia_id', '=', INTEGRATION_ID),
        ('name', 'in', ['url', 'consumer_key', 'consumer_secret']),
    ])
    vals = {f.name: f.value for f in fields}
    return vals['url'].rstrip('/'), vals['consumer_key'], vals['consumer_secret']


def _pg_connect(config: str, db: str):
    import configparser
    import psycopg2

    cfg = configparser.ConfigParser()
    cfg.read(Path(__file__).resolve().parents[1] / config)
    opts = cfg['options']
    return psycopg2.connect(
        host=opts.get('db_host', 'localhost'),
        port=int(opts.get('db_port', 5432)),
        user=opts.get('db_user', 'odoo'),
        password=opts.get('db_password', 'odoo'),
        dbname=db,
    )


def apply_fix_local(config: str, db: str, fixes: list[tuple[int, float]], dry_run: bool):
    if dry_run:
        print(f'  [DRY-RUN] Actualizados: {len(fixes)} productos')
        return
    conn = _pg_connect(config, db)
    try:
        with conn.cursor() as cur:
            for tmpl_id, new_price in fixes:
                cur.execute(
                    'UPDATE product_template SET list_price = %s, write_date = NOW() WHERE id = %s',
                    (new_price, tmpl_id),
                )
        conn.commit()
    finally:
        conn.close()
    print(f'  Actualizados: {len(fixes)} productos')


def apply_fix_xmlrpc(url: str, db: str, user: str, password: str, fixes: list[tuple[int, float]], dry_run: bool):
    if dry_run:
        print(f'  [DRY-RUN] Se actualizarían {len(fixes)} productos')
        return
    common = xmlrpc.client.ServerProxy(f'{url.rstrip("/")}/xmlrpc/2/common')
    uid = common.authenticate(db, user, password, {})
    models = xmlrpc.client.ServerProxy(f'{url.rstrip("/")}/xmlrpc/2/object')
    for tmpl_id, new_price in fixes:
        models.execute_kw(db, uid, password, 'product.template', 'write', [[tmpl_id], {'list_price': new_price}])
    print(f'  Actualizados: {len(fixes)} productos')


def main():
    parser = argparse.ArgumentParser(description='Auditar/corregir precios WC → Odoo (IVA)')
    parser.add_argument('--config', default='happy.conf')
    parser.add_argument('--db', default='jap')
    parser.add_argument('--xmlrpc-url')
    parser.add_argument('--user')
    parser.add_argument('--password')
    parser.add_argument('--execute', action='store_true', help='Aplicar corrección MAL_COPIADO_BRUTO')
    parser.add_argument('--report', default='scripts/jap_wc_product_diagnostic/jap_wc_price_audit_2026-07-07.csv')
    parser.add_argument('--limit', type=int, default=0, help='Limitar SKUs WC a consultar (0=todos)')
    parser.add_argument('--apply-from-csv', help='Aplicar corrección desde CSV previo (sin consultar WC)')
    args = parser.parse_args()

    dry_run = not args.execute

    fixes: list[tuple[int, float]] = []
    results: list[Row] = []
    stats: dict[str, int] = {}

    if args.apply_from_csv:
        print(f'=== Aplicando desde {args.apply_from_csv} ===')
        with open(args.apply_from_csv, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f, delimiter=';'):
                if row['status'] != 'MAL_COPIADO_BRUTO':
                    continue
                fixes.append((int(row['odoo_id']), float(row['neto_esperado'])))
        print(f'  Correcciones a aplicar: {len(fixes)}')
        if args.xmlrpc_url:
            apply_fix_xmlrpc(args.xmlrpc_url, args.db, args.user, args.password, fixes, dry_run)
        else:
            apply_fix_local(args.config, args.db, fixes, dry_run)
        return

    print('=== Cargando productos Odoo enlazados a WC ===')
    if args.xmlrpc_url:
        if not args.user or not args.password:
            parser.error('--user y --password requeridos con --xmlrpc-url')
        odoo_rows = get_odoo_products_xmlrpc(args.xmlrpc_url, args.db, args.user, args.password)
        wc_url, wc_key, wc_secret = args.xmlrpc_url, None, None
        # credenciales WC desde prod api fields
        common = xmlrpc.client.ServerProxy(f'{args.xmlrpc_url.rstrip("/")}/xmlrpc/2/common')
        uid = common.authenticate(args.db, args.user, args.password, {})
        models = xmlrpc.client.ServerProxy(f'{args.xmlrpc_url.rstrip("/")}/xmlrpc/2/object')
        field_ids = models.execute_kw(args.db, uid, args.password, 'sale.integration.api.field', 'search', [[
            ('sia_id', '=', INTEGRATION_ID),
            ('name', 'in', ['url', 'consumer_key', 'consumer_secret']),
        ]])
        fields = models.execute_kw(args.db, uid, args.password, 'sale.integration.api.field', 'read', [field_ids], {'fields': ['name', 'value']})
        fvals = {f['name']: f['value'] for f in fields}
        wc_url, wc_key, wc_secret = fvals['url'].rstrip('/'), fvals['consumer_key'], fvals['consumer_secret']
    else:
        env, cr, _registry = _odoo_env(args.config, args.db)
        try:
            wc_url, wc_key, wc_secret = get_wc_credentials_local(env)
            mappings = env['integration.product.template.mapping'].search([
                ('integration_id', '=', INTEGRATION_ID),
                ('template_id', '!=', False),
            ])
            products = mappings.mapped('template_id').filtered('active')
            odoo_rows = []
            for pt in products:
                ext = mappings.filtered(lambda m: m.template_id == pt)[:1].external_template_id
                odoo_rows.append({
                    'id': pt.id,
                    'sku': (pt.default_code or '').strip(),
                    'name': pt.name or '',
                    'list_price': float(pt.list_price or 0),
                    'wc_ext_code': ext.code if ext else '',
                })
        finally:
            cr.close()

    skus = [r['sku'] for r in odoo_rows if r['sku']]
    if args.limit:
        skus = skus[:args.limit]

    print(f'Productos activos enlazados: {len(odoo_rows)}')
    print(f'Consultando WC API ({wc_url})...')
    wc_prices = fetch_wc_prices_by_sku(wc_url, wc_key, wc_secret, skus)

    results: list[Row] = []
    stats: dict[str, int] = {}
    fixes: list[tuple[int, float]] = []

    for r in odoo_rows:
        sku = r['sku']
        wc = wc_prices.get(sku) if sku else None
        if not sku or not wc or wc.get('error'):
            status = 'SIN_SKU' if not sku else ('WC_ERROR' if wc and wc.get('error') else 'NO_EN_WC')
            results.append(Row(r['id'], sku or '', r['name'], r['list_price'], None, None, None, status, None))
            stats[status] = stats.get(status, 0) + 1
            continue

        wc_gross = wc['regular_price']
        if wc_gross <= 0:
            status = 'WC_PRECIO_CERO'
            results.append(Row(r['id'], sku, r['name'], r['list_price'], wc_gross, wc['id'], None, status, None))
            stats[status] = stats.get(status, 0) + 1
            continue

        status, expected_net, delta = classify(r['list_price'], wc_gross)
        results.append(Row(r['id'], sku, r['name'], r['list_price'], wc_gross, wc['id'], expected_net, status, delta))
        stats[status] = stats.get(status, 0) + 1
        if status == 'MAL_COPIADO_BRUTO' and expected_net is not None:
            fixes.append((r['id'], expected_net))

    print('\n=== RESUMEN ===')
    for k, v in sorted(stats.items(), key=lambda x: -x[1]):
        print(f'  {k}: {v}')

    mal = stats.get('MAL_COPIADO_BRUTO', 0)
    ok = stats.get('OK_NETO', 0) + stats.get('OK_BRUTO_CALCULADO', 0)
    print(f'\n  Corregibles (bruto copiado como neto): {mal}')
    print(f'  Ya correctos: {ok}')
    print(f'  Acción: {"APLICAR" if not dry_run else "DRY-RUN"} → {len(fixes)} productos → list_price = WC_regular / 1.21')

    # CSV report
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open('w', newline='', encoding='utf-8') as f:
        w = csv.writer(f, delimiter=';')
        w.writerow([
            'odoo_id', 'sku', 'nombre', 'odoo_list_price', 'wc_regular_price',
            'neto_esperado', 'status', 'delta_actual_vs_esperado', 'wc_id',
        ])
        for row in results:
            w.writerow([
                row.odoo_id, row.sku, row.name, row.odoo_price,
                row.wc_price if row.wc_price is not None else '',
                row.expected_net if row.expected_net is not None else '',
                row.status, row.delta_if_fix if row.delta_if_fix is not None else '',
                row.wc_id or '',
            ])
    print(f'\nInforme: {report_path}')

    # ejemplos
    print('\n=== Ejemplos MAL_COPIADO_BRUTO (máx 5) ===')
    for row in [x for x in results if x.status == 'MAL_COPIADO_BRUTO'][:5]:
        print(f"  id={row.odoo_id} {row.sku}: Odoo {row.odoo_price} → corregir a {row.expected_net} (WC {row.wc_price})")

    print('\n=== Ejemplos OK_NETO (máx 3) ===')
    for row in [x for x in results if x.status == 'OK_NETO'][:3]:
        print(f"  id={row.odoo_id} {row.sku}: Odoo {row.odoo_price} WC {row.wc_price}")

    if fixes and not dry_run:
        print(f'\n=== Aplicando corrección ===')
        if args.xmlrpc_url:
            apply_fix_xmlrpc(args.xmlrpc_url, args.db, args.user, args.password, fixes, dry_run)
        else:
            apply_fix_local(args.config, args.db, fixes, dry_run)
    elif fixes:
        print(f'\n=== DRY-RUN: no se escribe en BD. Usa --execute para aplicar {len(fixes)} cambios ===')


if __name__ == '__main__':
    main()
