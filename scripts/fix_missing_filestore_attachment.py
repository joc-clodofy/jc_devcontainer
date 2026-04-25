#!/usr/bin/env python3
"""
Script para eliminar attachments huérfanos del filestore (referencias en DB sin archivo físico).
Útil cuando Odoo falla con FileNotFoundError en archivos del filestore.

Uso:
    python scripts/fix_missing_filestore_attachment.py -c segu.conf -d segu

O para un store_fname específico:
    python scripts/fix_missing_filestore_attachment.py -c segu.conf -d segu --store-fname 7f/7fa1e6c1075f12a3f8bf5127b258aff5cc9d6f3b
"""
import argparse
import os
import sys

# Añadir el directorio de Odoo al path
sys.path.insert(0, '/workspace/odoo')

import odoo
from odoo.tools import config


def main():
    parser = argparse.ArgumentParser(description='Eliminar attachments con archivos faltantes en filestore')
    parser.add_argument('-c', '--config', required=True, help='Archivo de configuración Odoo')
    parser.add_argument('-d', '--database', required=True, help='Nombre de la base de datos')
    parser.add_argument('--store-fname', help='Store_fname específico a eliminar (ej: 7f/7fa1e6c1...)')
    parser.add_argument('--dry-run', action='store_true', help='Solo mostrar qué se eliminaría')
    args = parser.parse_args()

    odoo.tools.config.parse_config(['-c', args.config, '-d', args.database])
    odoo.registry(args.database)

    with odoo.registry(args.database).cursor() as cr:
        env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
        Attachment = env['ir.attachment']
        filestore = Attachment._filestore()

        if args.store_fname:
            # Eliminar attachment específico
            attachments = Attachment.search([('store_fname', '=', args.store_fname)])
        else:
            # Buscar todos los attachments con store_fname y verificar si el archivo existe
            attachments = Attachment.search([('store_fname', '!=', False)])
            missing = []
            for att in attachments:
                path = os.path.join(filestore, att.store_fname)
                if not os.path.exists(path):
                    missing.append(att)
            attachments = env['ir.attachment'].browse([a.id for a in missing])

        if not attachments:
            print("No se encontraron attachments para eliminar.")
            return

        print(f"Se eliminarán {len(attachments)} attachment(s) con archivos faltantes:")
        for att in attachments:
            path = os.path.join(filestore, att.store_fname)
            exists = "EXISTE" if os.path.exists(path) else "NO EXISTE"
            print(f"  - {att.store_fname} ({att.name or 'sin nombre'}) [{exists}]")

        if not args.dry_run:
            attachments.unlink()
            cr.commit()
            print("\nAttachments eliminados correctamente. Odoo los regenerará cuando sea necesario.")
        else:
            print("\n[DRY-RUN] No se eliminó nada. Ejecuta sin --dry-run para aplicar.")


if __name__ == '__main__':
    main()
