#!/bin/bash

# Script para actualizar módulos de Odoo sin iniciar el servidor
# Uso: ./scripts/update_modules.sh [database_name] [config_file]

# Configuración por defecto
DB_NAME="${1:-cloud101}"
CONFIG_FILE="${2:-/workspace/cloud.conf}"

# Colores para output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${YELLOW}Actualizando módulos de Odoo...${NC}"
echo -e "Base de datos: ${GREEN}${DB_NAME}${NC}"
echo -e "Archivo de configuración: ${GREEN}${CONFIG_FILE}${NC}"
echo ""

# Ejecutar actualización con --stop-after-init para evitar conflictos de puerto
python /workspace/odoo/odoo-bin \
    -u all \
    -d "${DB_NAME}" \
    -c "${CONFIG_FILE}" \
    --stop-after-init

if [ $? -eq 0 ]; then
    echo -e "\n${GREEN}✓ Actualización completada exitosamente${NC}"
else
    echo -e "\n${RED}✗ Error durante la actualización${NC}"
    exit 1
fi
