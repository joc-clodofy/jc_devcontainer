#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Archiva copias duplicadas de productos en Odoo (Happy Japan / WC import).

Criterios (pares de 2 con mismo default_code):
  1. Legacy + import (min < 3000, max >= 3000):
     - Si la copia import tiene pedidos → archivar legacy, conservar import.
     - Si no → archivar import (si no tiene pedidos/stock/WC), conservar legacy.
  2. Ambas copias import (ambos id >= 3000): archivar el más antiguo (id menor).
  3. Ambos legacy (ambos id < 3000): archivar el id mayor (conservar el más antiguo).
  4. Nunca archivar un producto con mapping WC, stock distinto de 0, o pedidos.

Uso local (shell):
  odoo/odoo-bin shell -c happy.conf -d jap --no-http < scripts/jap_archive_duplicate_products.py

Uso directo:
  python3 scripts/jap_archive_duplicate_products.py --config happy.conf --db jap --dry-run
  python3 scripts/jap_archive_duplicate_products.py --config happy.conf --db jap --execute

XML-RPC (producción):
  python3 scripts/jap_archive_duplicate_products.py \\
    --xmlrpc-url https://... --db PROD --user admin --password '...' --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

INTEGRATION_ID = 1
LEGACY_MAX_ID = 3000  # id < 3000 = legacy; id >= 3000 = copia import

DUPLICATE_PAIRS_SQL = """
SELECT default_code, MIN(id) AS low_id, MAX(id) AS high_id
FROM product_template
WHERE active AND sale_ok AND COALESCE(default_code, '') != ''
GROUP BY default_code
HAVING COUNT(*) = 2
ORDER BY default_code
"""

DUPLICATE_STATS_SQL = """
SELECT
    (SELECT COUNT(*) FROM (
        SELECT default_code FROM product_template
        WHERE active AND sale_ok AND COALESCE(default_code, '') != ''
        GROUP BY default_code HAVING COUNT(*) > 1
    ) g) AS duplicate_groups,
    (SELECT COUNT(*) FROM product_template pt
     WHERE active AND sale_ok AND COALESCE(default_code, '') != ''
       AND default_code IN (
           SELECT default_code FROM product_template
           WHERE active AND sale_ok AND COALESCE(default_code, '') != ''
           GROUP BY default_code HAVING COUNT(*) > 1
       )
    ) AS products_in_duplicate_groups
"""

TMPL_HAS_ORDERS_SQL = """
SELECT EXISTS (
    SELECT 1 FROM sale_order_line sol
    JOIN product_product pp ON pp.id = sol.product_id
    WHERE pp.product_tmpl_id = %(tmpl_id)s
)
"""

TMPL_HAS_STOCK_SQL = """
SELECT EXISTS (
    SELECT 1 FROM stock_quant sq
    JOIN product_product pp ON pp.id = sq.product_id
    WHERE pp.product_tmpl_id = %(tmpl_id)s AND sq.quantity != 0
)
"""

WC_MAPPED_TMPLS_SQL = """
SELECT template_id FROM integration_product_template_mapping
WHERE integration_id = %(integration_id)s AND template_id IS NOT NULL
"""


def _tmpl_has_orders(cr, tmpl_id):
    cr.execute(TMPL_HAS_ORDERS_SQL, {'tmpl_id': tmpl_id})
    return cr.fetchone()[0]


def _tmpl_has_stock(cr, tmpl_id):
    cr.execute(TMPL_HAS_STOCK_SQL, {'tmpl_id': tmpl_id})
    return cr.fetchone()[0]


def _get_wc_mapped(cr):
    cr.execute(WC_MAPPED_TMPLS_SQL, {'integration_id': INTEGRATION_ID})
    return {row[0] for row in cr.fetchall()}


def _unsafe_to_archive(cr, tmpl_id, wc_mapped):
    if tmpl_id in wc_mapped:
        return 'mapping WC'
    if _tmpl_has_orders(cr, tmpl_id):
        return 'pedidos'
    if _tmpl_has_stock(cr, tmpl_id):
        return 'stock'
    return None


def resolve_archive_id(cr, low_id, high_id, wc_mapped):
    """
    Decide qué id archivar en un par (low_id < high_id).
    Devuelve (archive_id, keep_id, reason) o (None, None, skip_reason).
    """
    if high_id < LEGACY_MAX_ID:
        archive_id, keep_id = high_id, low_id
        reason = 'ambos legacy: archivar id mayor'
    elif low_id >= LEGACY_MAX_ID:
        archive_id, keep_id = low_id, high_id
        reason = 'ambas import: conservar más nuevo'
    else:
        legacy_id, import_id = low_id, high_id
        if _tmpl_has_orders(cr, import_id):
            archive_id, keep_id = legacy_id, import_id
            reason = 'legacy+import: import con pedidos, archivar legacy'
        else:
            archive_id, keep_id = import_id, legacy_id
            reason = 'legacy+import: archivar copia import'

    unsafe = _unsafe_to_archive(cr, archive_id, wc_mapped)
    if unsafe:
        return None, None, f'no archivar id={archive_id}: {unsafe}'

    return archive_id, keep_id, reason


def get_candidates_odoo(env):
    cr = env.cr
    wc_mapped = _get_wc_mapped(cr)
    cr.execute(DUPLICATE_PAIRS_SQL)
    rows = []
    for default_code, low_id, high_id in cr.fetchall():
        archive_id, keep_id, reason = resolve_archive_id(cr, low_id, high_id, wc_mapped)
        if archive_id is None:
            continue
        cr.execute(
            "SELECT name->>'en_US' FROM product_template WHERE id = %s",
            (archive_id,),
        )
        archive_name = cr.fetchone()[0]
        cr.execute(
            "SELECT name->>'en_US' FROM product_template WHERE id = %s",
            (keep_id,),
        )
        keep_name = cr.fetchone()[0]
        rows.append((archive_id, default_code, archive_name, keep_id, keep_name, reason))
    return rows


def get_stats_odoo(env):
    env.cr.execute(DUPLICATE_STATS_SQL)
    return env.cr.fetchone()


def archive_via_odoo(env, candidate_ids, dry_run):
    ProductTemplate = env['product.template']
    products = ProductTemplate.browse(candidate_ids).exists()
    missing = set(candidate_ids) - set(products.ids)
    if missing:
        print(f'  AVISO: IDs no encontrados: {sorted(missing)}')

    to_archive = products.filtered(lambda p: p.active)
    already = products.filtered(lambda p: not p.active)

    print(f'\n  Candidatos: {len(candidate_ids)} | Activos a archivar: {len(to_archive)} | Ya archivados: {len(already)}')

    if dry_run:
        print('\n  [DRY-RUN] No se modifica la base de datos.')
        return len(to_archive)

    if to_archive:
        to_archive.write({'active': False})
        env.cr.commit()
        print(f'\n  Archivados: {len(to_archive)} productos.')
    return len(to_archive)


def run_with_odoo_env(env, dry_run):
    before = get_stats_odoo(env)
    print('=== ANTES ===')
    print(f'  Grupos duplicados: {before[0]}')
    print(f'  Productos en grupos duplicados: {before[1]}')

    candidates = get_candidates_odoo(env)
    print(f'\n=== CANDIDATOS: {len(candidates)} ===')
    for row in candidates[:8]:
        print(f'  archivar id={row[0]} ref={row[1]!r}  →  conservar id={row[3]}  ({row[5]})')
    if len(candidates) > 8:
        print(f'  ... y {len(candidates) - 8} más')

    archive_ids = [r[0] for r in candidates]
    archived = archive_via_odoo(env, archive_ids, dry_run)

    after = get_stats_odoo(env)
    print('\n=== DESPUÉS ===')
    print(f'  Grupos duplicados: {after[0]}')
    print(f'  Productos en grupos duplicados: {after[1]}')
    print(f'\n  Reducción grupos: {before[0] - after[0]}')
    print(f'  Reducción productos en duplicados: {before[1] - after[1]}')

    return {
        'before_groups': before[0],
        'after_groups': after[0],
        'archived': archived if not dry_run else 0,
        'candidates': len(candidates),
    }


def _xmlrpc_tmpl_has_orders(execute_kw, tmpl_id):
    variant_ids = execute_kw(
        'product.product', 'search',
        [[('product_tmpl_id', '=', tmpl_id)]],
    )
    if not variant_ids:
        return False
    return execute_kw(
        'sale.order.line', 'search_count',
        [[('product_id', 'in', variant_ids)]],
    ) > 0


def _xmlrpc_tmpl_has_stock(execute_kw, tmpl_id):
    variant_ids = execute_kw(
        'product.product', 'search',
        [[('product_tmpl_id', '=', tmpl_id)]],
    )
    if not variant_ids:
        return False
    quants = execute_kw(
        'stock.quant', 'search_read',
        [[('product_id', 'in', variant_ids), ('quantity', '!=', 0)]],
        {'fields': ['id'], 'limit': 1},
    )
    return bool(quants)


def run_xmlrpc(url, db, user, password, dry_run):
    import xmlrpc.client

    common = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/common')
    uid = common.authenticate(db, user, password, {})
    if not uid:
        raise SystemExit('Error: autenticación XML-RPC fallida')

    models = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/object')

    def execute_kw(model, method, args=None, kwargs=None):
        return models.execute_kw(db, uid, password, model, method, args or [], kwargs or {})

    print(f'XML-RPC conectado: {url} db={db} uid={uid}')

    all_ids = execute_kw('product.template', 'search', [[('active', '=', True), ('sale_ok', '=', True)]])
    products = execute_kw(
        'product.template', 'read',
        [all_ids],
        {'fields': ['id', 'default_code', 'name', 'active']},
    )

    by_code = {}
    for p in products:
        code = (p.get('default_code') or '').strip()
        if code:
            by_code.setdefault(code, []).append(p)

    duplicate_groups = {code: ps for code, ps in by_code.items() if len(ps) > 1}
    before_groups = len(duplicate_groups)
    before_products = sum(len(ps) for ps in duplicate_groups.values())

    print('=== ANTES ===')
    print(f'  Grupos duplicados: {before_groups}')
    print(f'  Productos en grupos duplicados: {before_products}')

    mapping_ids = execute_kw(
        'integration.product.template.mapping', 'search',
        [[('integration_id', '=', INTEGRATION_ID), ('template_id', '!=', False)]],
    )
    mappings = execute_kw(
        'integration.product.template.mapping', 'read',
        [mapping_ids],
        {'fields': ['template_id']},
    ) if mapping_ids else []
    wc_mapped = {m['template_id'][0] for m in mappings if m.get('template_id')}

    archive_ids = []
    for code, ps in sorted(duplicate_groups.items()):
        if len(ps) != 2:
            continue
        low = min(ps, key=lambda x: x['id'])
        high = max(ps, key=lambda x: x['id'])
        low_id, high_id = low['id'], high['id']

        if high_id < LEGACY_MAX_ID:
            archive_id, keep_id = high_id, low_id
        elif low_id >= LEGACY_MAX_ID:
            archive_id, keep_id = low_id, high_id
        else:
            import_has_orders = _xmlrpc_tmpl_has_orders(execute_kw, high_id)
            if import_has_orders:
                archive_id, keep_id = low_id, high_id
            else:
                archive_id, keep_id = high_id, low_id

        if archive_id in wc_mapped:
            continue
        if _xmlrpc_tmpl_has_orders(execute_kw, archive_id):
            continue
        if _xmlrpc_tmpl_has_stock(execute_kw, archive_id):
            continue
        archive_ids.append(archive_id)

    print(f'\n=== CANDIDATOS: {len(archive_ids)} ===')
    print(f'  IDs: {archive_ids[:10]}{"..." if len(archive_ids) > 10 else ""}')

    if dry_run:
        print('\n  [DRY-RUN] No se modifica la base de datos.')
    else:
        if archive_ids:
            execute_kw('product.template', 'write', [archive_ids, {'active': False}])
            print(f'\n  Archivados: {len(archive_ids)} productos.')

    if not dry_run:
        products = execute_kw(
            'product.template', 'read',
            [all_ids],
            {'fields': ['id', 'default_code', 'active']},
        )
        products = [p for p in products if p.get('active')]
        by_code = {}
        for p in products:
            code = (p.get('default_code') or '').strip()
            if code:
                by_code.setdefault(code, []).append(p)
        duplicate_groups = {c: ps for c, ps in by_code.items() if len(ps) > 1}
        after_groups = len(duplicate_groups)
        after_products = sum(len(ps) for ps in duplicate_groups.values())
    else:
        after_groups = before_groups - len(archive_ids)
        after_products = before_products - len(archive_ids)

    print('\n=== DESPUÉS ===')
    print(f'  Grupos duplicados: {after_groups}')
    print(f'  Productos en grupos duplicados: {after_products}')
    print(f'\n  Reducción grupos: {before_groups - after_groups}')
    print(f'  Reducción productos: {before_products - after_products}')


def main():
    parser = argparse.ArgumentParser(description='Archivar duplicados seguros de productos Odoo')
    parser.add_argument('--dry-run', action='store_true', default=True, help='Solo simular (default)')
    parser.add_argument('--execute', action='store_true', help='Ejecutar archivado real')
    parser.add_argument('--config', default='happy.conf', help='Odoo config (modo local)')
    parser.add_argument('--db', default='jap', help='Nombre BD')
    parser.add_argument('--xmlrpc-url', help='URL Odoo producción, ej. https://odoo.example.com')
    parser.add_argument('--user', help='Usuario XML-RPC')
    parser.add_argument('--password', help='Password XML-RPC')
    args = parser.parse_args()

    dry_run = not args.execute

    if args.xmlrpc_url:
        if not args.user or not args.password:
            parser.error('--user y --password son obligatorios con --xmlrpc-url')
        run_xmlrpc(args.xmlrpc_url.rstrip('/'), args.db, args.user, args.password, dry_run)
        return

    workspace = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(workspace / 'odoo'))

    import odoo
    from odoo.tools import config as odoo_config

    config_file = workspace / args.config
    odoo_config.parse_config(['-c', str(config_file), '-d', args.db])

    registry = odoo.registry(args.db)
    with registry.cursor() as cr:
        env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
        run_with_odoo_env(env, dry_run)


if 'env' in dir() and env is not None:  # noqa: F821
    import sys as _sys
    dry = '--execute' not in _sys.argv
    if dry:
        print('Modo shell: dry-run (añade --execute al comando shell para aplicar)')
    run_with_odoo_env(env, dry_run=dry)  # noqa: F821

if __name__ == '__main__':
    if 'env' not in dir() or env is None:  # noqa: F821
        main()
