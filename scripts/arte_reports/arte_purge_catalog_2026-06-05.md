# Purga catálogo arte — solo productos PS mapeados

**Fecha:** 2026-06-05  
**DB:** `arte` (local)  
**Comando principal:**

```bash
ODOO_URL=http://localhost:8069 ODOO_DB=arte ODOO_USER=admin ODOO_PASSWORD=123 \
python3 scripts/arte_sync_categories_pos_odoo_only.py \
  --skip-categories --skip-products --purge-catalog
```

Segunda pasada (resto bloqueado por POS):

```bash
... --purge-unmapped
```

Consolidación duplicados (referencia con mapping PS):

```bash
... --consolidate-duplicates
```

---

## Estado final (activos = catálogo usable)

| Métrica | Antes purga | Después |
|---------|------------:|--------:|
| Plantillas **activas** totales | 19 170 | **4 388** |
| Plantillas activas **con mapping PS** | 4 379 | **4 379** |
| Plantillas activas **sin mapping** | 14 791 | **9** (protegidas: DUA/servicios) |
| Plantillas inactivas sin mapping (archivadas) | 0 | **4 215** |
| Grupos duplicados mapping plantilla | 99 | **0** |
| Grupos duplicados mapping variante | 414 | **0** |
| Variantes con >10 mappings PS | 121 | **0** |

**Resultado:** el catálogo **activo** queda en **4 379 productos PrestaShop mapeados + 9 registros protegidos** (IVA DUA, etc.).

---

## Qué hizo cada fase

### 1. `--cleanup-duplicates` (ya ejecutado antes)
- Eliminó mappings duplicados plantilla/variante.

### 2. `--consolidate-duplicates`
- Agrupó por `default_code` duplicado.
- Si alguno tenía mapping PS → conservó ese y eliminó/archivó el resto.
- **15 grupos** (30 plantillas duplicadas → 15 conservadas).

### 3. `--purge-unmapped`
- Candidatos iniciales: **14 782** plantillas sin vínculo PS.
- **10 582** borradas (`unlink`).
- **4 200** no se pudieron borrar (sesión POS abierta) → **archivadas** (`active=False`, `available_in_pos=False`).
- **9** protegidas por prefijo (`DUA`, etc.) o tipo servicio.

---

## Nuevas opciones del script

| Flag | Descripción |
|------|-------------|
| `--purge-catalog` | Atajo: dedupe mappings + consolidar + purgar |
| `--consolidate-duplicates` | Duplicados por referencia; gana el que tiene PS |
| `--purge-unmapped` | Quita plantillas sin mapping PS |
| `--purge-mode unlink\|archive` | Borrar o solo archivar (default: `unlink`) |
| `--no-skip-stock` | Purgar aunque tengan stock |
| `--consolidate-by-name` | También agrupar por nombre si no hay referencia |

**Fallback automático:** si `unlink` falla por POS abierto, archiva el lote en lugar de borrarlo.

---

## Pendiente opcional

- **4 215 plantillas inactivas** siguen en BD (archivadas, sin mapping). No aparecen en catálogo activo. Se pueden borrar cuando cierres sesiones POS y relances `--purge-unmapped --purge-mode unlink`.
- **9 activas sin mapping:** revisar si deben enlazarse o archivarse manualmente.
- **58 mappings variante** con `product_id` NULL y **17 variantes** en template mapeado sin mapping variante.

---

## Comando recomendado en staging/producción

```bash
# 1) Simular
python3 scripts/arte_sync_categories_pos_odoo_only.py --purge-catalog --dry-run

# 2) Aplicar (cerrar sesiones POS antes si quieres unlink total)
python3 scripts/arte_sync_categories_pos_odoo_only.py --purge-catalog
```
