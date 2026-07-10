# Manual: integrar tienda WooCommerce y PrestaShop con Odoo 17

Guía operativa basada en el conector **Ventor Integration** (`integration` + `integration_woocommerce` / `integration_prestashop`), los scripts del repositorio y las lecciones del proyecto **Arte** (PrestaShop) y **Airbici** (WooCommerce).

---

## 1. Principios que evitan desastres

Estas reglas salen de un caso real (Arte): ~19k productos en Odoo vs ~4.5k en PrestaShop por un import fuera del conector.

| Regla | Por qué |
|-------|---------|
| **Un solo camino de importación** | Todo el catálogo debe entrar por Ventor (`sale.integration`), no por CSV/API custom aparte. |
| **Import completo, no a medias** | `integrationApiImportProducts` solo crea externos + auto-match. Las categorías, imágenes y campos finos vienen en el **import completo** del producto. |
| **Contar antes de decidir** | Comparar productos en tienda vs Odoo vs externos (`integration.product.template.external`). |
| **No mezclar imports viejos** | Si ya hay catálogo “legacy” sin mapping, arreglar o limpiar antes de duplicar. |
| **XML-RPC con workers** | Con `workers > 0`, incluir `base` en `server_wide_modules` o usar instancia temporal `--workers=0` para scripts. |
| **Jobs en cola** | Activar `queue_job` y revisar que los cron/workers procesen la cola. |

---

## 2. Requisitos comunes (Odoo)

### 2.1 Módulos

Instalar y actualizar:

- `queue_job`
- `integration`
- `integration_woocommerce` **o** `integration_prestashop`
- `website_sale` (dependencia del conector)

### 2.2 `addons_path` y `server_wide_modules`

Ejemplo **PrestaShop** (`arte.conf`):

```ini
addons_path=...,/workspace/oca/queue,/workspace/oca/ecommerce-integration,...
server_wide_modules=base,web,queue_job,integration,integration_prestashop

[queue_job]
channels=root:2
```

Ejemplo **WooCommerce** (`wow.conf`, `forte.conf`):

```ini
server_wide_modules=base,web,queue_job,integration,integration_woocommerce
```

> Sin `base` en `server_wide_modules`, XML-RPC puede devolver **404** con varios workers.

### 2.3 Wizard inicial en Odoo

1. Completar el **Getting Started** / Quick Configuration del conector.
2. Verificar permisos API en la tienda.
3. Importar **categorías**, **impuestos** y **estados de pedido** antes del catálogo masivo.

---

## 3. Flujo recomendado — PrestaShop

Referencia: integración `tiendabellasartesjer.com`, script `scripts/arte_sync_categories_pos_xmlrpc.py`.

### Fase A — Preparación en PrestaShop

1. **Webservice activo** con permisos GET/PUT en productos, categorías, stock, pedidos.
2. Anotar **URL** y **clave API**.
3. En backoffice, anotar cuántos productos activos hay (referencia; luego contrastar con API).

**Conteo vía API** (paginación correcta: `limit=inicio,cantidad`, no `offset`):

```bash
python3 << 'PY'
import requests, time
URL = "https://TU-TIENDA.com"
KEY = "TU_CLAVE"
ids, start, step = set(), 0, 500
while True:
    r = requests.get(f"{URL}/api/products", params={
        "ws_key": KEY, "output_format": "JSON", "display": "[id]",
        "filter[active]": "[1]", "limit": f"{start},{step}",
    }, auth=(KEY, ""), timeout=90)
    prods = r.json().get("products") or []
    if isinstance(prods, dict): prods = [prods]
    if not prods: break
    for p in prods: ids.add(str(p["id"]))
    if len(prods) < step: break
    start += step
    time.sleep(0.05)
print(f"PrestaShop activos (IDs únicos): {len(ids)}")
PY
```

### Fase B — Configurar integración en Odoo

1. **Ventas → Configuración → Integraciones** → crear/editar PrestaShop.
2. URL + API key.
3. Revisar filtro de import (`import_products_filter`, por defecto `{"active": "1"}`).
4. **Check connection** (`action_check_connection`).
5. Importar **categorías públicas** desde PS y comprobar mapping Odoo ↔ externo.

### Fase C — Import de productos (conector)

Orden recomendado:

| Paso | Acción | Qué hace |
|------|--------|----------|
| 1 | Import categorías | `product.public.category` + mappings |
| 2 | `integrationApiImportProducts` | Crea `integration.product.template.external` + auto-match por SKU/ref |
| 3 | Import completo / Link products | Trae nombre, precio, **categorías**, imágenes, variantes |
| 4 | Revisar mapping | Cada producto Odoo debe tener `integration.product.template.mapping` |
| 5 | Sync categorías → TPV | Script Arte (opcional pero recomendado en retail) |

**No hacer:** import masivo por CSV de productos con IDs de PS en `default_code` sin pasar por el conector.

### Fase D — Script categorías + TPV (Arte)

Variables:

```bash
export ODOO_URL=http://localhost:8073   # workers=0 si scripts masivos
export ODOO_DB=arte
export ODOO_USER=admin
export ODOO_PASSWORD=...
```

**Catálogo ya mapeado (flujo normal):**

```bash
cd /workspace && source .venv/bin/activate

python3 scripts/arte_sync_categories_pos_xmlrpc.py \
  --product-scope prestashop \
  --report /tmp/arte_sync_report.json
```

Qué hace:

- Replica `product.public.category` → `product.category` + `pos.category`
- Actualiza productos con mapping: `categ_id`, `public_categ_ids`, `pos_categ_ids`, `available_in_pos`, `sale_ok`, `purchase_ok`

**Dry-run primero:**

```bash
python3 scripts/arte_sync_categories_pos_xmlrpc.py --dry-run
```

### Fase E — Legacy (solo si hay catálogo viejo sin mapping)

Si existen miles de productos **sin** `integration.product.template.mapping` (caso Arte: 14.791):

```bash
python3 scripts/arte_sync_categories_pos_xmlrpc.py \
  --skip-categories \
  --skip-prestashop-sync \
  --legacy-unmapped \
  --no-ps-api-search \
  --min-match-score 55 \
  --progress-every 50 \
  --heartbeat 15 \
  --report /tmp/arte_legacy_match_report.json \
  2>&1 | tee /tmp/arte_legacy_run.log
```

Flags útiles:

| Flag | Uso |
|------|-----|
| `--no-ps-api-search` | Mucho más rápido; solo match local (ID/ref/barcode/nombre) |
| `--legacy-batch-size 500` | Tamaño de lote |
| `--skip-categories` | Si el árbol ya existe |

**Después del legacy:** valorar limpieza de duplicados (no borrado masivo a ciegas).

### Fase F — Verificación PrestaShop (SQL)

```sql
-- Externos vs mapeados vs activos Odoo
SELECT COUNT(*) FROM integration_product_template_external WHERE integration_id = 1;
SELECT COUNT(*) FROM integration_product_template_mapping WHERE integration_id = 1 AND template_id IS NOT NULL;
SELECT COUNT(*) FROM product_template WHERE active;
SELECT COUNT(*) FROM product_template pt
WHERE active AND NOT EXISTS (
  SELECT 1 FROM integration_product_template_mapping m
  WHERE m.template_id = pt.id AND m.integration_id = 1
);

-- Productos en "All" sin categoría pública
SELECT COUNT(*) FROM product_template pt
JOIN product_category pc ON pc.id = pt.categ_id
WHERE pt.active AND pc.complete_name = 'All'
  AND NOT EXISTS (
    SELECT 1 FROM product_public_category_product_template_rel r
    WHERE r.product_template_id = pt.id
  );
```

**Criterios de éxito (Arte):**

- Externos Odoo ≈ productos activos PS (ej. 4.498 ≈ 4.507).
- Casi todos los mapeados con `public_categ_ids`.
- Sin segunda población de ~15k productos fuera del conector.

### Fase G — Diagnóstico barcodes + lote piloto TPV (Arte)

Scripts en `scripts/` para auditar EAN/barcode y preparar productos escaneables en TPV.

**Variables (local `arte`):**

```bash
export ODOO_URL=http://localhost:8069
export ODOO_DB=arte
export ODOO_USER=admin
export ODOO_PASSWORD=...
export INTEGRATION_ID=1
```

**Diagnóstico completo** (métricas, duplicados, informe en `scripts/arte_reports/`):

```bash
python3 scripts/arte_barcode_diagnostic.py --integration-id 1
```

**Candidatos piloto** (10–20 productos: mapeados, EAN único, `tracking=none`):

```bash
python3 scripts/arte_barcode_diagnostic.py \
  --integration-id 1 \
  --pilot-candidates \
  --pilot-limit 20
```

**Marcar piloto en TPV** (`available_in_pos=True` en plantillas seleccionadas):

```bash
# Revisar primero con --dry-run
python3 scripts/arte_barcode_diagnostic.py \
  --integration-id 1 \
  --pilot-candidates \
  --pilot-limit 20 \
  --mark-pilot \
  --dry-run

python3 scripts/arte_barcode_diagnostic.py \
  --integration-id 1 \
  --pilot-candidates \
  --pilot-limit 20 \
  --mark-pilot
```

**Comparar EAN vivo PS vs Odoo** (muestra, requiere API PS):

```bash
python3 scripts/arte_barcode_diagnostic.py \
  --integration-id 1 \
  --fetch-ps \
  --fetch-ps-limit 100
```

**Verificar escaneo** (equivalente a `find_product_by_barcode` del TPV):

```bash
python3 << 'PY'
import os, xmlrpc.client
url=os.environ.get("ODOO_URL","http://localhost:8069")
db=os.environ.get("ODOO_DB","arte")
user=os.environ["ODOO_USER"]; pwd=os.environ["ODOO_PASSWORD"]
common=xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True)
uid=common.authenticate(db,user,pwd,{})
models=xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)
for bc in ["8715046090015", "738797990555"]:
    ids=models.execute_kw(db,uid,pwd,"product.product","search",[[("barcode","=",bc),("available_in_pos","=",True)]])
    print(bc, "->", ids)
PY
```

**Configuración relevante en la integración:**

| Campo | Efecto |
|-------|--------|
| `prestashop_skip_invalid_barcodes=True` | No escribe EAN duplicados/inválidos en Odoo |
| `prestashop_validate_barcode` / `validate_barcode` | Validación estricta al importar |
| Mapeo conector `ean13` → `product.product.barcode` | Origen del barcode en variantes |

**SQL rápido** (también en `scripts/arte_sync_validation.sql`):

```bash
psql -h pgdb -U odoo -d arte -f scripts/arte_sync_validation.sql
```

Métricas clave: `var_mapped_with_odoo_barcode`, `var_pos_scan_ready`, `var_dup_external_barcode_groups`.

### Fase H — Migración de stock PrestaShop → Odoo (Arte)

El conector Ventor incluye el wizard **Import Stock Levels** (destructivo en ubicaciones no mapeadas; solo productos **mapeados** con `tracking=none`). Antes de importar masivo, validar con scripts de reconciliación.

**Requisitos previos:**

1. Configurar **Locations** en la integración (`external.stock.location.line` → ubicación Odoo).
2. Integración en estado operativo (no solo `draft` en producción).
3. Lote piloto validado (Fase G).

**Reconciliación PS vs Odoo** (solo lectura, CSV/JSON en `scripts/arte_reports/`):

```bash
# Muestra de 100 variantes mapeadas
python3 scripts/arte_stock_reconcile.py --integration-id 1 --limit 100

# Solo productos del piloto TPV
python3 scripts/arte_stock_reconcile.py --integration-id 1 --pilot-only --pilot-limit 20
```

El script compara `stock_available` (API PS) con `stock.quant` (Odoo). Si no hay `location_line_ids`, usa todas las ubicaciones `internal` (aviso en consola).

**Flujo operativo recomendado:**

| Paso | Acción |
|------|--------|
| 1 | Ejecutar `arte_stock_reconcile.py` **antes** del import (línea base) |
| 2 | Odoo → Integración PS → **Import Stock Levels** (wizard Ventor; encola `queue.job`) |
| 3 | Esperar jobs `done` en Queue Jobs |
| 4 | Ejecutar `arte_stock_reconcile.py --pilot-only` **después** del import |
| 5 | Revisar filas `status=diff` en el CSV |

**Advertencias:**

- El import de stock del conector **sobrescribe** cantidades en la ubicación mapeada; no ejecutar en producción sin piloto.
- Productos con `tracking` distinto de `none` quedan fuera del import automático.
- En DB local `arte` sin stock en Odoo es normal hasta ejecutar el wizard o cargar inventario manualmente.

---

## 4. Flujo recomendado — WooCommerce

Referencia: script `scripts/airbici_link_products_xmlrpc.py`, proyecto Airbici.

### Fase A — Preparación en WooCommerce

1. **REST API** habilitada (WooCommerce → Ajustes → Avanzado → REST API).
2. Crear clave **Read/Write** (Consumer key + Consumer secret).
3. URL base: `https://tu-tienda.com` (sin barra final).
4. Si hay caché/CDN/WAF: permitir User-Agent del conector y del script.

### Fase B — Configurar integración en Odoo

1. **Ventas → Integraciones** → WooCommerce.
2. URL + consumer key + consumer secret.
3. **Check connection** desde Odoo (la prueba sale **del servidor Odoo**, no de tu PC).
4. Importar categorías, impuestos, métodos de envío/pago según wizard.

### Fase C — Fetch controlado + import (script Airbici)

El script replica el flujo “Link Products” con **rate-limit** explícito.

Variables:

```bash
export ODOO_URL=http://localhost:8069
export ODOO_DB=air
export ODOO_USER=admin
export ODOO_PASSWORD=...
# Opcional: override WC (si no están en sale.integration.api.field)
export WC_URL=https://tu-tienda.com
export WC_KEY=ck_...
export WC_SECRET=cs_...
export INTEGRATION_ID=1
```

**Dry-run completo:**

```bash
cd /workspace && source .venv/bin/activate
python3 scripts/airbici_link_products_xmlrpc.py --dry-run
```

**Ejecución real (recomendado):**

```bash
python3 scripts/airbici_link_products_xmlrpc.py \
  --batch-size 50 \
  --pause 60 \
  --import-mode once \
  --ids-file /tmp/wc_template_ids.txt \
  2>&1 | tee /tmp/airbici_import.log
```

Pasos internos del script:

| Paso | Descripción |
|------|-------------|
| 1 | `action_check_connection` (Odoo → WC) |
| 2 | Fetch REST: productos `simple`, `external`, `variable` (paginado) |
| 3 | Fetch variaciones por lotes de 50 + pausa 60s (anti rate-limit) |
| 4 | `integrationApiImportProducts` con todos los IDs (Odoo encola jobs ~150 ids/bloque) |

Opciones:

| Flag | Descripción |
|------|-------------|
| `--skip-check` | Omitir test de conexión |
| `--skip-fetch` + `--ids-file` | Reimportar solo con IDs ya guardados |
| `--skip-variations` | Solo plantillas, sin variaciones |
| `--skip-import` | Solo fetch WC |
| `--import-mode once` | **Recomendado**: 1 RPC, Odoo parte en jobs block-1,2,3… |
| `--import-mode rpc-batches` | Varias RPC; solo si esperas job entre lotes |

### Fase D — Cola de jobs

Tras el import, revisar en Odoo:

- **Ajustes técnicos → Queue Jobs** (o menú del módulo `queue_job`)
- Jobs `import_external_product_block-{integration_id}-{n}` en estado `done`
- Cron / workers activos (`max_cron_threads`, `queue_job` channels)

### Fase E — Verificación WooCommerce

```sql
SELECT COUNT(*) FROM integration_product_template_external WHERE integration_id = 1;
SELECT COUNT(*) FROM integration_product_template_mapping WHERE integration_id = 1 AND template_id IS NOT NULL;
```

En WooCommerce REST, el total aparece en cabeceras `X-WP-Total` al listar productos.

**Criterios de éxito:**

- Externos Odoo ≈ plantillas WC (simple + variable + external).
- SKUs críticos mapeados; revisar productos “sin SKU” del resumen del script.
- Pedidos de prueba importan con líneas vinculadas a productos mapeados.

---

## 5. Comparativa rápida PS vs WC

| Tema | PrestaShop | WooCommerce |
|------|------------|-------------|
| Auth | URL + webservice key | URL + consumer key/secret |
| ID producto | Numérico (`code` en externo) | ID WordPress |
| Rate limit | Moderado; usar `--ps-delay` | Agresivo; lotes 50 + pausa 60s |
| Script repo | `arte_sync_categories_pos_xmlrpc.py` | `airbici_link_products_xmlrpc.py` |
| Categorías ecommerce | `product.public.category` | Idem (Ventor) |
| TPV | Script Arte → `pos.category` | Configurar aparte si aplica |
| Conteo catálogo | API `limit=start,count` | REST `X-WP-Total` |

---

## 6. Errores frecuentes y solución

| Síntoma | Causa | Solución |
|---------|-------|----------|
| XML-RPC 404 | `server_wide_modules` sin `base` | Añadir `base` o `--workers=0` |
| Connection refused :8069 | Odoo parado / puerto distinto | Arrancar Odoo; comprobar `http_port` en `.conf` |
| Categoría "All" en masivo | Import fuera del conector o sin `public_categ_ids` | Import Ventor + script categorías |
| Odoo >> productos tienda | Duplicados / import legacy | Contar PS/WC; limpiar sin mapping |
| Script “congelado” | API lenta (PS search por nombre) | `--no-ps-api-search`; heartbeats en log |
| `cannot marshal None` | Bug XML-RPC local en check conexión | Ignorar si el job se encoló (script lo tolera) |
| Jobs no avanzan | Cola parada | Revisar `queue_job`, cron, logs Odoo |

---

## 7. Checklist final antes de producción

### PrestaShop

- [ ] Productos activos PS contados (API) y ≈ externos Odoo
- [ ] Categorías importadas y mapeadas
- [ ] >95% productos con `integration.product.template.mapping`
- [ ] `public_categ_ids` y `categ_id` correctos (no masivo en "All")
- [ ] TPV: `available_in_pos` + `pos_categ_ids` si aplica
- [ ] Pedido de prueba importado OK
- [ ] Stock sync probado en un producto piloto

### WooCommerce

- [ ] Check connection OK desde servidor Odoo
- [ ] Fetch + import completados; jobs `done`
- [ ] Variaciones de productos variables presentes
- [ ] SKUs vacíos identificados y corregidos en WC u Odoo
- [ ] Pedido de prueba importado OK
- [ ] Webhooks configurados (opcional, post go-live)

---

## 8. Lección del caso Arte (PrestaShop)

Datos reales del diagnóstico:

| Métrica | Valor |
|---------|-------|
| PrestaShop activos (API) | **4.507** |
| Odoo activos | **19.170** |
| Con mapping Ventor | **~4.486** |
| Sin mapping (import sept 2025) | **14.791** |
| Legacy con nombre duplicado 3–4× | **~14.780** |
| Legacy mismo nombre que producto ya mapeado | **~13.056** |

**Conclusión:** no era que PS tuviera 19k productos; era catálogo Odoo inflado por duplicados. La vía correcta es conector + script de categorías; la limpieza de duplicados es fase posterior y selectiva.

---

## 9. Referencias en el repositorio

| Recurso | Ruta |
|---------|------|
| Script WooCommerce | `scripts/airbici_link_products_xmlrpc.py` |
| Script PrestaShop / categorías / TPV | `scripts/arte_sync_categories_pos_xmlrpc.py` |
| Conector base | `oca/ecommerce-integration/integration/` |
| Conector PS | `oca/ecommerce-integration/integration_prestashop/` |
| Conector WC | `oca/ecommerce-integration/integration_woocommerce/` |
| Conf ejemplo PS | `arte.conf` |
| Conf ejemplo WC | `wow.conf`, `forte.conf`, `odoo.conf` |

---

*Documento generado a partir de la operativa real en Arte (PrestaShop) y Airbici (WooCommerce). Actualizar cifras de conteo en cada nuevo proyecto.*
