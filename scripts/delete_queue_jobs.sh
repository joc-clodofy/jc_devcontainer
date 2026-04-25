#!/bin/bash

# Script para borrar todos los queue jobs de una base de datos Odoo por SQL.
# Uso:
#   ./scripts/delete_queue_jobs.sh -d nombredb
#
# Opciones:
#   -d  Nombre de la base de datos (obligatorio)
#   -h  Host de PostgreSQL (por defecto: localhost)
#   -p  Puerto de PostgreSQL (por defecto: 5432)
#   -U  Usuario de PostgreSQL (por defecto: odoo)

set -e

DB_NAME=""
PGHOST="localhost"
PGPORT="5432"
PGUSER="odoo"

while getopts ":d:h:p:U:" opt; do
  case "$opt" in
    d) DB_NAME="$OPTARG" ;;
    h) PGHOST="$OPTARG" ;;
    p) PGPORT="$OPTARG" ;;
    U) PGUSER="$OPTARG" ;;
    \?)
      echo "Opción inválida: -$OPTARG" >&2
      exit 1
      ;;
    :)
      echo "La opción -$OPTARG requiere un valor." >&2
      exit 1
      ;;
  esac
done

if [ -z "$DB_NAME" ]; then
  echo "Debe indicar la base de datos con -d nombredb"
  echo "Ejemplo: ./scripts/delete_queue_jobs.sh -d mydb"
  exit 1
fi

echo "Borrando todos los queue jobs de la base de datos '${DB_NAME}'..."
echo "Host: ${PGHOST}, Puerto: ${PGPORT}, Usuario: ${PGUSER}"

read -p "¿Está seguro? Esto borrará TODOS los queue jobs. (escriba 'SI' para continuar): " confirm
if [ "$confirm" != "SI" ]; then
  echo "Operación cancelada."
  exit 0
fi

SQL="
DELETE FROM queue_job_function;
DELETE FROM queue_job;
"

PGHOST="$PGHOST" PGPORT="$PGPORT" PGUSER="$PGUSER" psql -d "$DB_NAME" -v ON_ERROR_STOP=1 -q -c "$SQL"

echo "Queue jobs borrados correctamente en la base de datos '${DB_NAME}'."

