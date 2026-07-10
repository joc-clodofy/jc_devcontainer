#!/usr/bin/env python3
"""
Script para eliminar attachments huérfanos del filestore (referencias en DB sin archivo físico).
Útil cuando Odoo falla con FileNotFoundError en archivos del filestore.

Uso:
    python scripts/fix_missing_filestore_attachment.py -c arte.conf -d arte

O para un store_fname específico:
    python scripts/fix_missing_filestore_attachment.py -c arte.conf -d arte --store-fname 7f/7fa1e6c1075f12a3f8bf5127b258aff5cc9d6f3b
"""
import argparse
import os
import sys

sys.path.insert(0, '/workspace/odoo')

import odoo
from odoo.tools import config

BATCH_SIZE = 500


def find_missing_ids(cr, filestore, store_fname=None):
    if store_fname:
        cr.execute(
            "SELECT id, store_fname FROM ir_attachment WHERE store_fname = %s",
            (store_fname,),
        )
    else:
        cr.execute(
            "SELECT id, store_fname FROM ir_attachment WHERE store_fname IS NOT NULL"
        )
    missing = []
    for att_id, fname in cr.fetchall():
        if not os.path.exists(os.path.join(filestore, fname)):
            missing.append(att_id)
    return missing


def main():
    parser = argparse.ArgumentParser(
        description='Eliminar attachments con archivos faltantes en filestore'
    )
    parser.add_argument('-c', '--config', required=True, help='Archivo de configuración Odoo')
    parser.add_argument('-d', '--database', required=True, help='Nombre de la base de datos')
    parser.add_argument(
        '--store-fname',
        help='Store_fname específico a eliminar (ej: 7f/7fa1e6c1...)',
    )
    parser.add_argument('--dry-run', action='store_true', help='Solo mostrar qué se eliminaría')
    args = parser.parse_args()

    odoo.tools.config.parse_config(['-c', args.config, '-d', args.database])
    odoo.registry(args.database)

    with odoo.registry(args.database).cursor() as cr:
        env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
        filestore = config.filestore(args.database)
        print(f"Filestore: {filestore}")

        missing_ids = find_missing_ids(cr, filestore, args.store_fname)
        if not missing_ids:
            print("No se encontraron attachments huérfanos.")
            return

        print(f"Attachments huérfanos: {len(missing_ids)}")
        if args.dry_run:
            cr.execute(
                "SELECT id, store_fname, res_model, res_field, name "
                "FROM ir_attachment WHERE id = ANY(%s) LIMIT 20",
                (missing_ids[:20],),
            )
            for row in cr.fetchall():
                print(f"  - id={row[0]} {row[1]} ({row[2]}.{row[3] or '-'}: {row[4] or 'sin nombre'})")
            if len(missing_ids) > 20:
                print(f"  ... y {len(missing_ids) - 20} más")
            print("\n[DRY-RUN] No se eliminó nada. Ejecuta sin --dry-run para aplicar.")
            return

        Attachment = env['ir.attachment'].with_context(skip_res_field_check=True)
        deleted = 0
        for i in range(0, len(missing_ids), BATCH_SIZE):
            batch = missing_ids[i : i + BATCH_SIZE]
            Attachment.browse(batch).unlink()
            cr.commit()
            deleted += len(batch)
            print(f"  Eliminados {deleted}/{len(missing_ids)}...", flush=True)

        print(f"\nListo: {deleted} attachments huérfanos eliminados.")


if __name__ == '__main__':
    main()
