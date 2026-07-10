#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diagnóstico de productos ausentes en TPV (Protocolo).

Uso:
  python scripts/protocolo_pos_product_visibility_diagnostic.py -c protocolo.conf -d protocolo
  python scripts/protocolo_pos_product_visibility_diagnostic.py -c protocolo.conf -d protocolo --apply
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "odoo"))

import odoo
from odoo import api


def _print_header(title):
    print(f"\n{'=' * 60}")
    print(title)
    print('=' * 60)


def diagnose(env, apply=False):
    PosConfig = env['pos.config']
    ProductTmpl = env['product.template']
    Product = env['product.product']
    PosCateg = env['pos.category']

    configs = PosConfig.search([])
    if not configs:
        print('No hay configuraciones TPV.')
        return 1

    exit_code = 0
    for cfg in configs:
        _print_header(f'TPV: {cfg.name} (id={cfg.id})')
        print(f'limit_categories: {cfg.limit_categories}')
        allowed = cfg.iface_available_categ_ids
        print(f'categorías permitidas: {allowed.mapped("name") or ["(todas)"]}')

        domain = cfg._get_available_product_domain()
        visible = Product.search_count(domain)
        all_pos_ready = Product.search_count([
            ('active', '=', True),
            ('sale_ok', '=', True),
            ('available_in_pos', '=', True),
        ])
        print(f'variantes visibles en TPV: {visible} / {all_pos_ready} disponibles para TPV')

        if not cfg.limit_categories:
            print('Sin límite de categorías: los productos con available_in_pos deberían salir.')
            continue

        used_categs = PosCateg.search([
            ('id', 'in', ProductTmpl.search([
                ('available_in_pos', '=', True),
                ('sale_ok', '=', True),
                ('active', '=', True),
            ]).mapped('pos_categ_ids').ids),
        ])
        missing = used_categs.filtered(lambda c: c.id not in allowed.ids)
        if not missing:
            print('OK: todas las categorías POS usadas por productos están permitidas.')
            continue

        exit_code = 1
        print('\nCategorías POS usadas por productos pero NO permitidas en este TPV:')
        for categ in missing.sorted('name'):
            excluded = Product.search_count([
                ('active', '=', True),
                ('sale_ok', '=', True),
                ('available_in_pos', '=', True),
                ('pos_categ_ids', 'in', categ.id),
            ])
            print(f'  - {categ.name}: {excluded} variantes excluidas')

        traje_categs = missing.filtered(lambda c: 'traje' in (c.name or '').lower())
        if traje_categs:
            print('\n>>> Causa probable de trajes ausentes: categorías TRAJE no incluidas en el TPV.')
            print('>>> Arreglo manual: Punto de venta > Configuración >', cfg.name,
                  '> Productos > Categorías de producto del TPV')
            print('>>> Añadir:', ', '.join(traje_categs.mapped('name')))

        if apply:
            cfg.iface_available_categ_ids = [(4, c.id) for c in missing]
            print('\nAPLICADO: categorías añadidas al TPV:', missing.mapped('name'))
        else:
            print('\nPara añadirlas automáticamente, ejecuta con --apply')

    return exit_code


def main():
    parser = argparse.ArgumentParser(description='Diagnóstico productos TPV Protocolo')
    parser.add_argument('-c', '--config', required=True, help='Ruta al archivo .conf de Odoo')
    parser.add_argument('-d', '--database', required=True, help='Nombre de la base de datos')
    parser.add_argument('--apply', action='store_true',
                        help='Añade al TPV las categorías POS faltantes detectadas')
    args = parser.parse_args()

    odoo.tools.config.parse_config(['-c', args.config, '-d', args.database])
    registry = odoo.registry(args.database)
    with registry.cursor() as cr:
        env = api.Environment(cr, odoo.SUPERUSER_ID, {})
        code = diagnose(env, apply=args.apply)
        if args.apply:
            cr.commit()
    sys.exit(code)


if __name__ == '__main__':
    main()
