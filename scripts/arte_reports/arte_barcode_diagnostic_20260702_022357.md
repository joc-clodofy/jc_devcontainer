# Diagnóstico barcode Arte — 2026-07-02T02:23:57

- Integración: **tiendabellasartesjer.com** (id=1)
- DB: `arte` @ `http://localhost:8069`

## Configuración integración

- `id`: 1
- `name`: tiendabellasartesjer.com
- `state`: draft
- `validate_barcode`: False
- `prestashop_validate_barcode`: False
- `prestashop_skip_invalid_barcodes`: True

## Métricas

| Métrica | Valor |
|---------|------:|
| Variantes mapeadas (activas) | 18751 |
| Con external_barcode (EAN externo) | 10837 |
| Con barcode Odoo | 10882 |
| EAN externo sin barcode Odoo | 0 |
| Sin external_barcode | 7914 |
| **TPV escaneables** (pos+barcode) | **188** |
| Barcode inválido en Odoo | 7871 |
| Grupos EAN duplicados (externo) | 22 |
| Grupos barcode duplicados (Odoo) | 126 |
