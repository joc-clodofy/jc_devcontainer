# Diagnóstico productos WooCommerce → Odoo (BD `jap`)

**Integración:** latiendadelbordado  
**Fecha:** 2026-07-07  
**Separador CSV:** `;` (compatible Excel en español)

## Archivos

| Archivo | Contenido | Uso |
|---------|-----------|-----|
| `00_resumen_*.csv` | Cifras clave del diagnóstico | Resumen ejecutivo para el cliente |
| `01_archivar_duplicados_import_*.csv` | **40 productos** seguros para archivar | Acción prioritaria en producción |
| `02_todos_duplicados_default_code_*.csv` | Los 188 productos en 94 grupos duplicados | Referencia completa |
| `03_wc_sin_enlazar_fallidos_*.csv` | **26 productos WC** sin enlace a Odoo | Corregir SKU duplicado y reintentar |
| `04_wc_enlazados_ok_*.csv` | **662 productos** correctamente enlazados | No tocar |
| `05_import_reciente_sin_wc_*.csv` | 301 productos del import sin mapping WC | Revisar caso a caso |
| `06_legacy_sin_wc_con_pedidos_*.csv` | 259 productos legacy con ventas | **NO archivar** |
| `07_jobs_import_fallidos_*.csv` | Detalle de jobs fallidos en cola | Técnico |

## Plan de acción recomendado

1. **Archivar** los 40 de `01_` (copias duplicadas del import, id alto, sin pedidos ni stock).
2. **Unificar** referencias internas duplicadas para los 26 de `03_` (MIGHTYHOOP, etc.).
3. **Reintentar** import/link WC de los 26 fallidos.
4. **Revisar** `05_` con el cliente antes de archivar.
5. **No tocar** `04_` (enlazados OK) ni `06_` (tienen pedidos).
