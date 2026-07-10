# Validación deduplicación — arte (local)

**Fecha:** 2026-06-05  
**Comando:**

```bash
ODOO_URL=http://localhost:8069 ODOO_DB=arte ODOO_USER=admin ODOO_PASSWORD=123 \
python3 scripts/arte_sync_categories_pos_odoo_only.py --skip-categories --cleanup-duplicates
```

**Resultado script:** OK — eliminados **107** mappings plantilla + **7171** mappings variante.

---

## Resumen antes / después (psql)

| Métrica | Antes | Después | Δ |
|---------|------:|--------:|---|
| `product.template` totales | 19 170 | 19 170 | **0** |
| Templates **sin** mapping integración | **14 791** | **14 791** | **0** |
| Mappings plantilla (total) | 4 498 | 4 391 | **−107** |
| Mappings plantilla con Odoo | 4 486 | 4 379 | **−107** |
| Grupos duplicados plantilla (`template_id` en >1 mapping) | **99** | **0** | **−99** |
| `product.product` totales | 27 278 | 27 278 | **0** |
| Variantes **sin** mapping integración | 14 951 | 14 951 | **0** |
| Mappings variante (total) | 19 556 | 12 385 | **−7 171** |
| Mappings variante con Odoo | 19 498 | 12 327 | **−7 171** |
| Grupos duplicados variante (`product_id` en >1 mapping) | **414** | **0** | **−414** |
| Variantes con >10 mappings externos | **121** | **0** | **−121** |
| Mappings variante con `product_id` NULL | 58 | 58 | 0 |
| Mapeados sin categoría pública | 43 | 43 | 0 |

---

## Qué se arregló

- Duplicidad de **vínculos** PrestaShop ↔ Odoo (mappings), no borrado masivo de productos.
- Casos graves de “colapso” (1 variante Odoo con decenas/cientos de mappings PS): **0** tras la limpieza.
- Cada `product.template` mapeado queda con **como máximo 1** mapping plantilla.
- Cada `product.product` mapeado queda con **como máximo 1** mapping variante.

---

## Qué **no** cambió (importante)

Los **~14 791** `product.template` sin mapping **siguen en la base**.  
La fase `--cleanup-duplicates` **no elimina** registros de `product.template` ni `product.product`; solo borra filas duplicadas en:

- `integration_product_template_mapping`
- `integration_product_product_mapping`

Por eso `tpl_total` y `tpl_sin_mapping` no bajan: la “basura” de catálogo Odoo sin enlace PS requiere **otra fase** (archivar/inactivar o borrar plantillas sin mapping, con reglas y dry-run).

Lo mismo para **14 951** variantes sin mapping: en su mayoría pertenecen a esas plantillas huérfanas o a productos nunca enlazados a PS.

---

## Pendientes menores

| Caso | Cantidad |
|------|----------|
| Variantes en template **mapeado** pero sin mapping variante | 17 |
| Mappings variante con `product_id` NULL | 58 |
| Templates mapeados sin `public_categ_ids` | 43 |

---

## Muestra templates sin mapping (siguen existiendo)

| ID | Ref. | Nombre |
|----|------|--------|
| 1 | DUA21 | DUA Valoración IVA 21% |
| 2 | DUA10 | DUA Valoración IVA 10% |
| 3 | DUA4 | DUA Valoración IVA 4% |
| 1286 | 5 | Oleo Winton 200 ml |
| 1287 | 7 | Oleo al agua Artisan Winsor Newton |

*(+14 786 más)*

---

## Conclusión

| Objetivo | Estado |
|----------|--------|
| Quitar duplicados de mappings plantilla/variante | **Hecho** |
| Quitar ~14k plantillas sin mapping de Odoo | **No hecho** (fuera de alcance de dedupe) |
| Variantes “que no deberían estar” por colapso de mappings | **Corregido** (0 grupos duplicados, 0 con >10 mappings) |

**Siguiente paso recomendado:** añadir fase `--purge-unmapped` (dry-run primero) para archivar o marcar `active=False` en `product.template` sin mapping y sin movimientos/stock, si quieres bajar de 19k a ~4,4k plantillas “útiles”.

---

## Archivos

- `scripts/arte_reports/validation_before_dedupe.txt`
- `scripts/arte_reports/validation_after_dedupe.txt`
- `scripts/arte_reports/sync_dedupe_real.log`
