#!/usr/bin/env bash
# Sincroniza el filestore de Odoo desde un servidor remoto (p. ej. staging Clodofy)
# hacia el volumen local del dev container (/var/lib/odoo/filestore/<db_local>).
#
# Requisitos:
#   - Acceso SSH al servidor (clave en ~/.ssh, montada en el dev container)
#   - Odoo detenido o al menos sin escrituras concurrentes en el filestore destino
#
# Ejemplo (Bellas Artes staging -> local arte):
#   REMOTE_HOST=stg20260604-odoo17-jer-bellas-artes.n3.clodofy.cloud \
#   REMOTE_USER=odoo \
#   REMOTE_DB=odoo_stg20260604_odoo17_jer_bellas_artes \
#   LOCAL_DB=arte \
#   REMOTE_FILESTORE_BASE=/var/lib/odoo/filestore \
#   ./scripts/sync_filestore_from_remote.sh
#
# Si el backup de Clodofy viene como tarball:
#   tar -xzf filestore.tar.gz -C /var/lib/odoo/filestore/arte --strip-components=1
#
set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:?Define REMOTE_HOST (hostname SSH)}"
REMOTE_USER="${REMOTE_USER:-odoo}"
REMOTE_DB="${REMOTE_DB:?Define REMOTE_DB (nombre BD en el servidor)}"
LOCAL_DB="${LOCAL_DB:-arte}"
REMOTE_FILESTORE_BASE="${REMOTE_FILESTORE_BASE:-/var/lib/odoo/filestore}"
LOCAL_FILESTORE_BASE="${LOCAL_FILESTORE_BASE:-/var/lib/odoo/filestore}"

REMOTE_PATH="${REMOTE_FILESTORE_BASE%/}/${REMOTE_DB}/"
LOCAL_PATH="${LOCAL_FILESTORE_BASE%/}/${LOCAL_DB}/"

echo "Origen:  ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_PATH}"
echo "Destino: ${LOCAL_PATH}"
echo ""

if [[ ! -d "${LOCAL_FILESTORE_BASE}" ]]; then
  echo "ERROR: no existe ${LOCAL_FILESTORE_BASE}. ¿Está montado el volumen odoo-data?" >&2
  exit 1
fi

mkdir -p "${LOCAL_PATH}"

# --partial: reanudar transferencias interrumpidas
# --info=progress2: barra de progreso global
rsync -avz --partial --info=progress2 \
  "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_PATH}" \
  "${LOCAL_PATH}"

echo ""
echo "Sincronización completada. Verifica con:"
echo "  python3 scripts/fix_missing_filestore_attachment.py -c arte.conf -d ${LOCAL_DB} --dry-run"
