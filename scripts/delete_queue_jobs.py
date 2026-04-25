#!/usr/bin/env python3
"""
Script para borrar todos los queue jobs de una base de datos Odoo usando SQL
directo (como si se hiciera desde psql), pero ejecutado desde Python.

Uso:
    python scripts/delete_queue_jobs.py -c segu.conf -d segu

Solo borra registros del modelo `queue.job` y `queue.job.function` en la BD
indicada. Pide confirmación antes de ejecutar.
"""

import argparse
import sys

sys.path.insert(0, "/workspace/odoo")

import odoo  # type: ignore[import-untyped]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Borra todos los queue jobs de una base de datos Odoo."
    )
    parser.add_argument(
        "-c",
        "--config",
        required=True,
        help="Archivo de configuración Odoo (por ejemplo: segu.conf)",
    )
    parser.add_argument(
        "-d",
        "--database",
        required=True,
        help="Nombre de la base de datos donde se borrarán los queue jobs",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="No pedir confirmación interactiva (usar con cuidado).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    db_name = args.database
    config_file = args.config

    print(f"Base de datos: {db_name}")
    print(f"Config file : {config_file}")
    print("Esta operación borrará TODOS los queue jobs (queue.job y queue.job.function).")

    if not args.yes:
        confirm = input(
            "¿Está seguro? Escriba exactamente 'SI' (mayúsculas) para continuar: "
        )
        if confirm != "SI":
            print("Operación cancelada por el usuario.")
            return

    # Inicializar configuración de Odoo
    odoo.tools.config.parse_config(["-c", config_file, "-d", db_name])
    registry = odoo.registry(db_name)

    with registry.cursor() as cr:
        # Contar antes de borrar usando SQL directo
        cr.execute("SELECT count(*) FROM queue_job;")
        total_jobs = cr.fetchone()[0]
        cr.execute("SELECT count(*) FROM queue_job_function;")
        total_functions = cr.fetchone()[0]

        # Contar por estado para informar mejor
        cr.execute("SELECT state, count(*) FROM queue_job GROUP BY state;")
        state_counts = cr.fetchall()

        print(f"Queue jobs totales: {total_jobs}")
        if state_counts:
            print("Detalle por estado:")
            for state, count in state_counts:
                print(f"  - {state or '<vacio>'}: {count}")
        print(f"Queue job functions encontrados: {total_functions}")

        if total_jobs == 0 and total_functions == 0:
            print("No hay registros para borrar. Saliendo.")
            return

        try:
            # Borramos SOLO los jobs que no están en ejecución.
            # En OCA queue_job los estados habituales son:
            # pending, enqueued, started, done, failed, cancelled.
            # Aquí respetamos los que están en 'started'.
            cr.execute("SELECT count(*) FROM queue_job WHERE state != 'started';")
            deletable_jobs = cr.fetchone()[0]
            print(
                f"Borrando {deletable_jobs} registros de queue_job con state != 'started'..."
            )
            cr.execute("DELETE FROM queue_job WHERE state != 'started';")

            # Limpiar funciones no referenciadas por ningún job restante
            cr.execute(
                """
                DELETE FROM queue_job_function
                WHERE id NOT IN (
                    SELECT DISTINCT function_id
                    FROM queue_job
                    WHERE function_id IS NOT NULL
                );
                """
            )

            cr.commit()
            print(
                "Queue jobs borrados correctamente por SQL (se mantuvieron los que están en 'started')."
            )
        except Exception as exc:  # noqa: BLE001
            cr.rollback()
            print(f"Error borrando queue jobs: {exc}")
            raise


if __name__ == "__main__":
    main()

