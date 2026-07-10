#!/bin/bash
# Vigila la terminal de Odoo y extrae líneas WooCommerce / 403 / variations
OUT="/tmp/odoo_wc_watch.log"
TERMINAL="${1:-/home/odoo/.cursor/projects/workspace/terminals/4.txt}"
echo "=== watch $(date -Iseconds) terminal=$TERMINAL ===" >> "$OUT"
grep -E 'WooCommerce|403|variations|WooCommerceApiException|get_templates_and_products|Link Products|integration_woocommerce' "$TERMINAL" 2>/dev/null >> "$OUT" || true
echo "--- tail $(wc -l < "$TERMINAL" 2>/dev/null || echo 0) lines in terminal ---" >> "$OUT"
