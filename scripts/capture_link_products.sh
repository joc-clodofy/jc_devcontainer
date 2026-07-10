#!/bin/bash
# Ejecutar ANTES de pulsar "Link Products" o el script de variaciones.
# Guarda salida sin que se pierda en la terminal de Odoo.

set -e
LOG_DIR="/tmp/airbici-wc-debug"
mkdir -p "$LOG_DIR"
TS=$(date +%Y%m%d_%H%M%S)

echo "Logs en: $LOG_DIR"
echo ""
echo "Opción A - Script (replica Odoo):"
echo "  python3 /workspace/scripts/replicate_odoo_variations_fetch.py -v 2>&1 | tee $LOG_DIR/script_${TS}.log"
echo ""
echo "Opción B - Odoo shell (método exacto):"
echo "  python3 odoo/odoo-bin shell -c air.conf -d air --no-http <<'PY' 2>&1 | tee $LOG_DIR/odoo_shell_${TS}.log"
echo "import traceback"
echo "i = env['sale.integration'].browse(1)"
echo "print('use_async:', i.use_async)"
echo "try:"
echo "    hub = i.adapter.get_templates_and_products_for_validation_test()"
echo "    print('OK', len(hub.get_template_ids()))"
echo "except Exception as e:"
echo "    print('ERROR', e)"
echo "    traceback.print_exc()"
echo "PY"
echo ""
echo "Opción C - Vigilar terminal Odoo (en otra pestaña):"
echo "  tail -f /tmp/odoo_wc_watch.log"
