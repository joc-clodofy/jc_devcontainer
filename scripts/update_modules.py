#!/usr/bin/env python3
"""
Script para actualizar módulos de Odoo sin iniciar el servidor.
Evita conflictos de puerto usando --stop-after-init.

Uso:
    python scripts/update_modules.py --db <database_name> --conf <config_file>
    
Ejemplos:
    python scripts/update_modules.py --db cloud101
    python scripts/update_modules.py --db cloud101 --conf /workspace/cloud.conf
    python scripts/update_modules.py --db cloud101 --conf /workspace/cloud.conf -u account,sale
    python scripts/update_modules.py segu -c /workspace/segu.conf
"""

import sys
import subprocess
import argparse
from pathlib import Path


def update_modules(db_name="cloud101", config_file="/workspace/segu.conf", modules=None, no_http=True):
    """
    Actualiza módulos de Odoo usando --stop-after-init.
    
    Args:
        db_name: Nombre de la base de datos
        config_file: Ruta al archivo de configuración
        modules: Lista de módulos específicos a actualizar (None = todos)
        no_http: Evita levantar puerto HTTP durante la actualización
    """
    odoo_bin = Path("/workspace/odoo/odoo-bin")
    
    if not odoo_bin.exists():
        print(f"❌ Error: No se encuentra odoo-bin en {odoo_bin}")
        sys.exit(1)
    
    if not Path(config_file).exists():
        print(f"❌ Error: No se encuentra el archivo de configuración: {config_file}")
        sys.exit(1)
    
    # Construir comando
    cmd = [
        "python",
        str(odoo_bin),
        "-c", config_file,
        "-d", db_name,
        "--stop-after-init",  # Esto evita que inicie el servidor
    ]
    if no_http:
        cmd.append("--no-http")
    
    # Agregar módulos específicos o todos
    if modules:
        cmd.extend(["-u", modules])
    else:
        cmd.append("-u")
        cmd.append("all")
    
    print("🔄 Actualizando módulos de Odoo...")
    print(f"   Base de datos: {db_name}")
    print(f"   Configuración: {config_file}")
    if modules:
        print(f"   Módulos: {modules}")
    else:
        print("   Módulos: todos")
    print()
    
    # Ejecutar comando
    try:
        subprocess.run(cmd, check=True)
        print("\n✅ Actualización completada exitosamente")
        return 0
    except subprocess.CalledProcessError as e:
        print(f"\n❌ Error durante la actualización: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n⚠️  Actualización cancelada por el usuario")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Actualiza módulos de Odoo sin iniciar el servidor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    
    parser.add_argument(
        "database_positional",
        nargs="?",
        default=None,
        help="(Compatibilidad) Nombre de la base de datos como argumento posicional."
    )
    
    parser.add_argument(
        "--db",
        dest="database",
        default=None,
        help="Nombre de la base de datos"
    )
    
    parser.add_argument(
        "-c", "--config",
        default="/workspace/segu.conf",
        help="Ruta al archivo de configuración (default: /workspace/segu.conf)"
    )
    
    parser.add_argument(
        "--conf",
        dest="config_alias",
        default=None,
        help="Alias de --config para facilitar reutilización"
    )
    
    parser.add_argument(
        "-u", "--update",
        dest="modules",
        help="Módulos específicos a actualizar (separados por comas). Si no se especifica, actualiza todos."
    )
    
    parser.add_argument(
        "--http",
        action="store_true",
        help="Permite levantar HTTP (por defecto está desactivado para evitar conflictos de puerto)."
    )
    
    args = parser.parse_args()
    
    db_name = args.database or args.database_positional or "cloud101"
    config_file = args.config_alias or args.config
    
    sys.exit(update_modules(
        db_name=db_name,
        config_file=config_file,
        modules=args.modules,
        no_http=not args.http,
    ))


if __name__ == "__main__":
    main()
