# Informe sync local — arte (Bellas Artes)

**Fecha:** 2026-06-05  
**Script:** `scripts/arte_sync_categories_pos_odoo_only.py`  
**Entorno:** local (`http://localhost:8069`, DB `arte`)  
**Credenciales:** `admin` / `123`  
**Duración:** ~23 s  
**Resultado:** OK (exit 0, 0 errores)

---

## Resumen ejecutivo

| Área | Resultado |
|------|-----------|
| Categorías `product.category` | Sin cambios (588 ya alineadas) |
| Categorías `pos.category` | Sin cambios (588 ya alineadas) |
| Productos con mapping PrestaShop | 4 379 procesados |
| Productos escritos en Odoo | **43** |
| Errores | 0 |

El catálogo local **ya tenía el árbol de categorías sincronizado** (probablemente de una ejecución anterior o del restore de staging). En esta pasada solo se **reescribieron 43 plantillas** mapeadas que **no tienen categoría pública** en Odoo: se les forzó `available_in_pos`, `sale_ok` y `purchase_ok` a `true` (comportamiento esperado del script).

Los **4 336** productos mapeados con categoría pública **no necesitaron cambios** (ya tenían `categ_id`, `pos_categ_ids` y flags correctos).

---

## Métricas antes / después (psql)

| Métrica | Antes | Después | Δ |
|---------|------:|--------:|---|
| `product.category` | 593 | 593 | 0 |
| `pos.category` | 588 | 588 | 0 |
| `product.public.category` | 588 | 588 | 0 |
| Mappings integración (total) | 4 498 | 4 498 | 0 |
| Mappings con `product.template` | 4 486 | 4 486 | 0 |
| `product.template` totales | 19 170 | 19 170 | 0 |
| Templates `available_in_pos=true` | 8 151 | 8 151 | 0 |
| Templates `sale_ok=true` | 19 166 | 19 166 | 0 |
| Templates `purchase_ok=true` | 19 167 | 19 167 | 0 |
| Mapeados con `pos_categ_ids` | 4 336 | 4 336 | 0 |
| Mapeados `available_in_pos=true` | 4 379 | 4 379 | 0 |
| Mapeados **sin** categoría pública | 43 | 43 | 0 |

Los contadores globales no variaron porque los 43 productos **ya cumplían** los booleanos; el script igual ejecutó `write()` (actualiza `write_date`, no cambia datos visibles en agregados).

---

## Detalle de la ejecución (log)

```
Integración: tiendabellasartesjer.com (prestashop)
product.category: +0 creadas, ~0 actualizadas, 588 sin cambios
pos.category: +0 creadas, ~0 actualizadas, 588 sin cambios
Productos a procesar: 4379
Productos: 4379 procesados, 43 actualizados, 43 sin categoría pública, 0 errores
```

Log completo: `scripts/arte_reports/sync_run.log`

---

## Los 43 productos tocados

Son plantillas **con mapping PrestaShop** pero **sin** `public_categ_ids`. El script les aplica solo flags TPV/venta/compra (no puede asignar `categ_id` / `pos_categ_ids` sin categoría pública).

| ID | Referencia | Nombre (extracto) |
|----|------------|-------------------|
| 1336 | 214 | Aceite linaza purificado Talens 75 ml |
| 1410 | 433 | Gel bolas de vidrio Liquitex |
| 1411 | 434 | Gel lava negra Liquitex |
| 1544 | 788 | Polonesa poney Escoda |
| 1844 | 2181 | Plumilla Index/Shakespeare |
| 1845 | 2182 | Plumilla ornamental |
| 1847 | 2184 | Plumilla tape (oblicua) |
| 2526 | 4030 | Caja William Mitchell plumillas caligrafía |
| 2549 | 4069 | Tubo acuarela metalizada Van Gogh |
| 2582 | 4135 | Set rotuladores Liquitex finos fluorescentes |
| 2635 | 4220 | Set rotuladores Faber Grip Finepen 30 |
| 2652 | 4245 | Copic activador |
| 3199 | 5016 | Pack 4 bastidores algodón Talens misma medida |
| 3262 | 5108 | Bloc marrón Talens A4 |
| 3324 | 5208 | Pack 6 bastidores lino Phoenix |
Todos con `write_date` ≈ `2026-06-05 06:52:05` y `available_in_pos=sale_ok=purchase_ok=true`.

### Lista completa (43)

| ID | Ref. | Nombre |
|----|------|--------|
| 1336 | 214 | Aceite linaza purificado Talens 75 ml |
| 1410 | 433 | Gel bolas de vidrio Liquitex |
| 1411 | 434 | Gel lava negra Liquitex |
| 1544 | 788 | Polonesa poney Escoda |
| 1844 | 2181 | Plumilla Index/Shakespeare |
| 1845 | 2182 | Plumilla ornamental |
| 1847 | 2184 | Plumilla tape (oblicua) |
| 2526 | 4030 | Caja William Mitchell plumillas caligrafía |
| 2549 | 4069 | Tubo acuarela metalizada Van Gogh |
| 2582 | 4135 | Set rotuladores Liquitex finos fluorescentes |
| 2635 | 4220 | Set rotuladores Faber Grip Finepen 30 |
| 2652 | 4245 | Copic activador |
| 3199 | 5016 | Pack 4 bastidores algodón Talens misma medida |
| 3262 | 5108 | Bloc marrón Talens A4 |
| 3324 | 5208 | Pack 6 bastidores lino Phoenix |
| 3332 | 5216 | Barniz mate Winsor and Newton 250 ml |
| 3346 | 5232 | Gama pinceles serie 7 Winsor |
| 3379 | 5269 | Multitécnicas set Carand´ache |
| 3408 | 5308 | Set plumillas sketching Speedball |
| 3416 | 5316 | Bloc pastel al óleo Sennelier 40 x 30 |
| 3430 | 5332 | Bloc Tombow bristol lettering |
| 3481 | 5393 | Glazing medium Cobra 250 ml |
| 3489 | 5404 | Pincel plano Van Gogh |
| 3492 | 5408 | Caja acuarelas Cesc Farré Schmincke |
| 3514 | 5432 | Brocha para proyectar Liquitex plana |
| 3567 | 5500 | Herramienta repujado doble Reig 05 |
| 3569 | 5502 | Herramienta repujado doble Reig 07 |
| 3581 | 5516 | Fimo air light blanco |
| 3592 | 5532 | Fimo Effect estrellado |
| 3653 | 5601 | Bol rojo en pasta 300 ml |
| 3667 | 5620 | Caja acuarelas White Nights 36 pastillas |
| 3887 | 5908 | Bloc para acrílico Winsor 30 x 40 |
| 3977 | 6016 | Paletina pelo sintético Van Gogh |
| 4059 | 6116 | Cartón entelado para acuarelas |
| 4213 | 6304 | Barniz mate Old Holland 250 ml |
| 4387 | 6508 | Sketch book 120 grs Hahnemühle A5 |
| 4431 | 6560 | Set pinceles Escoda Ceramics |
| 4719 | 6908 | Trementina veneciana 75 ml. |
| 4894 | 7109 | Rotulador acuarelable Faber Castell |
| 5248 | 7508 | Caja lápices Derwent Procolour 72 |
| 5367 | 7632 | Pincel de viaje Casaneo redondo |
| 5371 | 7636 | Gama de óleos Rembrandt 150 ml. |
| 5434 | 7708 | Caja Grip Fine pen 0,4 colores básicos |

---

## Qué **no** hace este script (recordatorio)

- No importa productos nuevos desde PrestaShop.
- No enlaza los **~14 791** templates sin mapping.
- No corrige mappings duplicados ni variantes colapsadas (414 casos detectados en auditoría previa).

---

## Conclusión

**Comportamiento correcto en local:** categorías ya estaban OK; productos mapeados con categoría pública ya estaban OK; solo 43 casos sin categoría pública recibieron `write` de flags.

**Siguiente paso recomendado** (fuera de este script): revisar en PrestaShop/Odoo por qué esos 43 no tienen `public_categ_ids`, o ejecutar en staging/producción si allí el árbol de categorías aún no está creado (como en el primer staging `stg20260513`, donde se crearon 565+588 categorías).

---

## Comando usado

```bash
ODOO_URL=http://localhost:8069 \
ODOO_DB=arte \
ODOO_USER=admin \
ODOO_PASSWORD=123 \
python3 scripts/arte_sync_categories_pos_odoo_only.py
```
