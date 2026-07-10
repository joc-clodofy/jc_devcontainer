#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Archiva duplicados por nombre normalizado (Happy Japan / staging|prod).

Normalización del nombre:
  - Quita prefijo "El correcto:"
  - Quita sufijo entre paréntesis al final, ej. "(1 x 100m)"
  - Quita puntuación (. ,) y espacios extra
  - Mayúsculas para comparar

Reglas por grupo de nombres equivalentes:
  1. Si hay productos enlazados a WC → conservar todos los WC, archivar el resto.
  2. Si ninguno es WC → conservar el más antiguo (create_date, luego id menor), archivar el resto.

Seguridad: no archivar si tiene mapping WC (producto a conservar).

Uso staging:
  python3 scripts/jap_archive_name_duplicates.py \\
    --xmlrpc-url https://stg20260707-odoo17-happy-japan.n3.clodofy.cloud \\
    --db odoo_stg20260707_odoo17_happy_japan --user admin --password admin

  python3 scripts/jap_archive_name_duplicates.py ... --execute
"""

from __future__ import annotations

import argparse
import csv
import re
import xmlrpc.client
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

INTEGRATION_ID = 1


def norm_name(name: str) -> str:
    """Normaliza solo patrones legacy; no borra medidas que distinguen productos."""
    if not name:
        return ''
    n = name.strip()
    n = re.sub(r'^El correcto:\s*', '', n, flags=re.IGNORECASE)
    n = re.sub(
        r'\s*\(\s*\d+\s*[mM]?\s*[xX×]\s*\d+\s*[mM]?\s*\)\s*$',
        '',
        n,
    )
    n = n.rstrip(' .')
    n = re.sub(r'\s+', ' ', n).strip().upper()
    # Núcleo entretela arrancar: unifica "70WL BLANCA ENTRETELA BASE DE ARRANCAR, 70 GRAMOS"
    m = re.search(
        r'ENTRETELA\s+BASE\s+DE\s+ARRANCAR[,.]?\s*(\d+)\s*GRAMOS?',
        n,
        flags=re.IGNORECASE,
    )
    if m:
        return f'ENTRETELA BASE DE ARRANCAR, {m.group(1)} GRAMOS'
    return n


def connect(url: str, db: str, user: str, password: str):
    common = xmlrpc.client.ServerProxy(f'{url.rstrip("/")}/xmlrpc/2/common')
    uid = common.authenticate(db, user, password, {})
    if not uid:
        raise SystemExit('Autenticación fallida')
    models = xmlrpc.client.ServerProxy(f'{url.rstrip("/")}/xmlrpc/2/object')
    return uid, models


def kw(models, db, uid, password, model, method, args=None, kwargs=None):
    return models.execute_kw(db, uid, password, model, method, args or [], kwargs or {})


def load_wc_mapped(models, db, uid, password) -> set[int]:
    map_ids = kw(models, db, uid, password, 'integration.product.template.mapping', 'search', [[
        ('integration_id', '=', INTEGRATION_ID),
        ('template_id', '!=', False),
    ]])
    if not map_ids:
        return set()
    maps = kw(models, db, uid, password, 'integration.product.template.mapping', 'read', [map_ids], {
        'fields': ['template_id'],
    })
    return {m['template_id'][0] for m in maps if m.get('template_id')}


def tmpl_has_orders(models, db, uid, password, tmpl_id: int) -> bool:
    variant_ids = kw(models, db, uid, password, 'product.product', 'search', [[
        ('product_tmpl_id', '=', tmpl_id),
    ]])
    if not variant_ids:
        return False
    return kw(models, db, uid, password, 'sale.order.line', 'search_count', [[
        ('product_id', 'in', variant_ids),
    ]]) > 0


def tmpl_has_stock(models, db, uid, password, tmpl_id: int) -> bool:
    variant_ids = kw(models, db, uid, password, 'product.product', 'search', [[
        ('product_tmpl_id', '=', tmpl_id),
    ]])
    if not variant_ids:
        return False
    quants = kw(models, db, uid, password, 'stock.quant', 'search_read', [[
        ('product_id', 'in', variant_ids),
        ('quantity', '!=', 0),
    ]], {'fields': ['id'], 'limit': 1})
    return bool(quants)


def unsafe_reason(models, db, uid, password, p: dict, wc_mapped: set[int]) -> str | None:
    if p['id'] in wc_mapped:
        return 'mapping WC'
    return None


def pick_keep_archive(group: list[dict], wc_mapped: set[int]) -> tuple[list[dict], list[dict], str]:
    wc = [p for p in group if p['id'] in wc_mapped]
    non_wc = [p for p in group if p['id'] not in wc_mapped]

    if wc:
        keep = wc
        archive = non_wc
        reason = 'conservar WC, archivar legacy'
    else:
        sorted_g = sorted(group, key=lambda p: (p.get('create_date') or '', p['id']))
        keep = [sorted_g[0]]
        archive = sorted_g[1:]
        reason = 'sin WC: conservar más antiguo'
    return keep, archive, reason


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--xmlrpc-url', required=True)
    parser.add_argument('--db', required=True)
    parser.add_argument('--user', required=True)
    parser.add_argument('--password', required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--report', default='scripts/jap_wc_product_diagnostic/jap_archive_name_dup_stg.csv')
    args = parser.parse_args()

    uid, models = connect(args.xmlrpc_url, args.db, args.user, args.password)
    dry_run = not args.execute

    print(f'Conectado: {args.xmlrpc_url} db={args.db}')
    wc_mapped = load_wc_mapped(models, args.db, uid, args.password)
    print(f'Productos con mapping WC: {len(wc_mapped)}')

    ids = kw(models, args.db, uid, args.password, 'product.template', 'search', [[('active', '=', True)]])
    products = kw(models, args.db, uid, args.password, 'product.template', 'read', [ids], {
        'fields': ['id', 'default_code', 'name', 'create_date', 'type', 'list_price'],
    })

    by_norm = defaultdict(list)
    for p in products:
        key = norm_name(p.get('name') or '')
        if key:
            by_norm[key].append(p)

    duplicate_groups = {k: v for k, v in by_norm.items() if len(v) > 1}
    print(f'Grupos con nombre equivalente: {len(duplicate_groups)}')
    print(f'Productos en grupos duplicados: {sum(len(v) for v in duplicate_groups.values())}')

    to_archive: list[dict] = []
    skipped: list[dict] = []
    rows_csv = []

    for norm, group in sorted(duplicate_groups.items(), key=lambda x: x[0]):
        keep, archive_candidates, rule = pick_keep_archive(group, wc_mapped)
        for p in archive_candidates:
            unsafe = unsafe_reason(models, args.db, uid, args.password, p, wc_mapped)
            keep_names = ' | '.join((k.get('name') or '')[:50] for k in keep)
            row = {
                'norm_name': norm,
                'archive_id': p['id'],
                'archive_code': p.get('default_code') or '',
                'archive_name': p.get('name') or '',
                'keep_ids': ','.join(str(k['id']) for k in keep),
                'keep_names': keep_names,
                'rule': rule,
                'status': 'SKIP_' + unsafe if unsafe else ('ARCHIVE' if not dry_run else 'DRY_ARCHIVE'),
            }
            rows_csv.append(row)
            if unsafe:
                skipped.append(row)
            else:
                to_archive.append(p)

    print(f'\nCandidatos a archivar: {len(to_archive)}')
    print(f'Omitidos (WC): {len(skipped)}')

    print('\n=== Ejemplos (máx 12) ===')
    for row in rows_csv[:12]:
        print(f"  [{row['status']}] archivar id={row['archive_id']} | {row['archive_name'][:55]}")
        print(f"    conservar: {row['keep_names'][:70]}")

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(rows_csv[0].keys()) if rows_csv else [
            'norm_name', 'archive_id', 'archive_code', 'archive_name', 'keep_ids', 'keep_names', 'rule', 'status',
        ], delimiter=';')
        w.writeheader()
        w.writerows(rows_csv)
    print(f'\nInforme: {report_path}')

    if to_archive and not dry_run:
        archive_ids = [p['id'] for p in to_archive]
        kw(models, args.db, uid, args.password, 'product.template', 'write', [archive_ids, {'active': False}])
        print(f'Archivados: {len(archive_ids)}')
    elif dry_run:
        print(f'\n[DRY-RUN] Usa --execute para archivar {len(to_archive)} productos')


if __name__ == '__main__':
    main()
