#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Archiva duplicados en producción y genera informe MD de reversión."""

from __future__ import annotations

import argparse
import xmlrpc.client
from datetime import datetime, timezone
from pathlib import Path

INTEGRATION_ID = 1
LEGACY_MAX_ID = 3000


def execute_kw(models, db, uid, password, model, method, args=None, kwargs=None):
    return models.execute_kw(db, uid, password, model, method, args or [], kwargs or {})


def tmpl_has_orders(execute, tmpl_id):
    variant_ids = execute('product.product', 'search', [[('product_tmpl_id', '=', tmpl_id)]])
    if not variant_ids:
        return False
    return execute('sale.order.line', 'search_count', [[('product_id', 'in', variant_ids)]]) > 0


def tmpl_has_stock(execute, tmpl_id):
    variant_ids = execute('product.product', 'search', [[('product_tmpl_id', '=', tmpl_id)]])
    if not variant_ids:
        return False
    quants = execute(
        'stock.quant', 'search_read',
        [[('product_id', 'in', variant_ids), ('quantity', '!=', 0)]],
        {'fields': ['id'], 'limit': 1},
    )
    return bool(quants)


def resolve_archive_id(execute, low_id, high_id, wc_mapped):
    if high_id < LEGACY_MAX_ID:
        archive_id, keep_id = high_id, low_id
        reason = 'ambos legacy: archivar id mayor'
    elif low_id >= LEGACY_MAX_ID:
        archive_id, keep_id = low_id, high_id
        reason = 'ambas import: conservar más nuevo'
    else:
        if tmpl_has_orders(execute, high_id):
            archive_id, keep_id = low_id, high_id
            reason = 'legacy+import: import con pedidos, archivar legacy'
        else:
            archive_id, keep_id = high_id, low_id
            reason = 'legacy+import: archivar copia import'

    if archive_id in wc_mapped:
        return None, None, f'no archivar id={archive_id}: mapping WC'
    if tmpl_has_orders(execute, archive_id):
        return None, None, f'no archivar id={archive_id}: pedidos'
    if tmpl_has_stock(execute, archive_id):
        return None, None, f'no archivar id={archive_id}: stock'
    return archive_id, keep_id, reason


def get_candidates(execute):
    all_ids = execute(
        'product.template', 'search',
        [[('active', '=', True), ('sale_ok', '=', True)]],
    )
    products = execute(
        'product.template', 'read', [all_ids],
        {'fields': ['id', 'default_code', 'name', 'active']},
    )

    by_code = {}
    for p in products:
        code = (p.get('default_code') or '').strip()
        if code:
            by_code.setdefault(code, []).append(p)

    mapping_ids = execute(
        'integration.product.template.mapping', 'search',
        [[('integration_id', '=', INTEGRATION_ID), ('template_id', '!=', False)]],
    )
    mappings = execute(
        'integration.product.template.mapping', 'read', [mapping_ids],
        {'fields': ['template_id']},
    ) if mapping_ids else []
    wc_mapped = {m['template_id'][0] for m in mappings if m.get('template_id')}

    candidates = []
    for code, ps in sorted(by_code.items()):
        if len(ps) != 2:
            continue
        low = min(ps, key=lambda x: x['id'])
        high = max(ps, key=lambda x: x['id'])
        archive_id, keep_id, reason = resolve_archive_id(
            execute, low['id'], high['id'], wc_mapped,
        )
        if archive_id is None:
            continue
        archive_p = next(p for p in ps if p['id'] == archive_id)
        keep_p = next(p for p in ps if p['id'] == keep_id)
        candidates.append({
            'archive_id': archive_id,
            'archive_code': archive_p.get('default_code') or '',
            'archive_name': archive_p.get('name') or '',
            'keep_id': keep_id,
            'keep_code': keep_p.get('default_code') or '',
            'keep_name': keep_p.get('name') or '',
            'reason': reason,
        })
    return candidates


def write_report(path: Path, url: str, db: str, candidates: list, dry_run: bool):
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    lines = [
        '# Productos archivados en producción — Happy Japan',
        '',
        f'**Fecha:** {now}',
        f'**URL:** {url}',
        f'**Base de datos:** `{db}`',
        f'**Acción:** {"DRY-RUN (simulación)" if dry_run else "ARCHIVADO (active=False)"}',
        f'**Total productos:** {len(candidates)}',
        '',
        '## Cómo revertir',
        '',
        'Para reactivar un producto archivado, en Odoo ir a Inventario → Productos,',
        'activar el filtro "Archivados", buscar por ID o referencia, y pulsar "Desarchivar".',
        '',
        'Vía XML-RPC (ejemplo para un ID):',
        '',
        '```python',
        "models.execute_kw(db, uid, password, 'product.template', 'write',",
        "    [[PRODUCT_ID], {'active': True}])",
        '```',
        '',
        '## Listado completo',
        '',
        '| # | ID archivado | Referencia | Nombre archivado | ID conservado | Motivo |',
        '|---|-------------|------------|------------------|---------------|--------|',
    ]
    for i, c in enumerate(candidates, 1):
        name = (c['archive_name'] or '').replace('|', '\\|').replace('\n', ' ')[:80]
        lines.append(
            f"| {i} | {c['archive_id']} | `{c['archive_code']}` | {name} | {c['keep_id']} | {c['reason']} |"
        )

    lines.extend([
        '',
        '## IDs archivados (lista plana)',
        '',
        '```',
        ', '.join(str(c['archive_id']) for c in candidates),
        '```',
        '',
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines), encoding='utf-8')
    print(f'Informe guardado: {path}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--xmlrpc-url', required=True)
    parser.add_argument('--db', required=True)
    parser.add_argument('--user', required=True)
    parser.add_argument('--password', required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--report', default='scripts/jap_wc_product_diagnostic/jap_archive_prod_2026-07-07.md')
    args = parser.parse_args()

    url = args.xmlrpc_url.rstrip('/')
    common = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/common')
    uid = common.authenticate(args.db, args.user, args.password, {})
    if not uid:
        raise SystemExit('Error: autenticación fallida')

    models = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/object')

    def execute(model, method, args_list=None, kwargs=None):
        return execute_kw(models, args.db, uid, args.password, model, method, args_list, kwargs)

    print(f'Conectado: {url} db={args.db} uid={uid}')
    candidates = get_candidates(execute)
    print(f'Candidatos: {len(candidates)}')

    dry_run = not args.execute
    if not dry_run and candidates:
        archive_ids = [c['archive_id'] for c in candidates]
        execute('product.template', 'write', [archive_ids, {'active': False}])
        print(f'Archivados: {len(archive_ids)} productos')

    report_path = Path(args.report)
    write_report(report_path, url, args.db, candidates, dry_run)


if __name__ == '__main__':
    main()
