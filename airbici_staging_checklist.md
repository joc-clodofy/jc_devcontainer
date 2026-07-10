# Airbici - Checklist de Staging (VAT, Impuestos, Reembolsos)

## 1) Código a desplegar

- Subir y actualizar el módulo custom: `integration_airbici_tools` (v `17.0.1.0.2`).
- Reiniciar Odoo en staging.
- Actualizar módulo:
  - UI: Apps -> Update Apps List -> Upgrade `integration_airbici_tools`
  - o CLI: `odoo-bin -d <db_staging> -u integration_airbici_tools --stop-after-init`

## 2) Ajustes de configuración (obligatorios)

### 2.1 VAT / NIF

En la integración Airbici (WooCommerce):

- `customer_company_vat_field` = `vat` (modelo `res.partner`).
- `woocommerce_vat_metafield_field_name` = `_billing_eu_vat_number`.
- `ignore_vat_validation` según política (en producción estaba `true`).

Nota:
- Con el fallback custom, si no llega VAT por meta y `billing.company` parece NIF/CIF/NIE, se usará como VAT.

### 2.2 Importación de pedidos por estado

Revisar `receive_orders_filter` (en producción estaba solo `processing`).

Recomendado para producción:

```json
{"status": "pending,processing,completed,refunded"}
```

Con `integration_airbici_tools` v `17.0.1.0.6+`, si el pedido **ya está importado** y WooCommerce lo devuelve en el cron (p. ej. pasó a `refunded`), se actualiza el `raw_data`, se lanza el pipeline y se procesan reembolsos (NC). Sin `refunded` en el filtro, pedidos como el `41176` no entran en el fetch del cron.

### 2.3 Auto-workflow / diarios de factura

En `Auto-Workflow -> Order Statuses`, para subestados de WooCommerce (`processing`, `completed`, `refunded`):

- Definir `Invoice Journal` (obligatorio).
- Confirmar qué tareas deben correr por estado:
  - `create_invoice`
  - `validate_invoice`
  - (opcionales) `validate_order`, `validate_picking`, `register_payment`

Sin `Invoice Journal`, falla la creación de factura y por ende el flujo de reembolso.

## 3) Impuestos (mapeo + productos existentes)

### 3.1 Mapeo principal de WooCommerce

- El tax ID que más llega en líneas de producto es **WC `14`** (IVA 21% ES).
- Verificar en mapeo:
  - Externo `14` -> impuesto Odoo de 21% decidido por negocio (`IVA21% (Bienes)` o `IVA (ES 21.0000%)`).

### 3.2 Productos ya importados (histórico)

Los productos existentes no se corrigen solos al cambiar mapeo. Sin actualizar `taxes_id` en producto, facturas creadas directamente en Odoo pueden usar impuestos distintos a los pedidos de integración.

Tras corregir mapeo y Tax Group (`standard` → tax externo por defecto, p. ej. WC `14`):

1. **Recomendado:** acción de servidor en lista de productos (o sin selección = todos los mapeados):
   - **Airbici: Sync taxes from integration mapping**
   - Solo consulta BD: Tax Group `standard` → tax externo por defecto (p. ej. WC `14`) → **Mappings → Taxes** → `taxes_id`.
   - No llama a WooCommerce (evita el error de `system_status` / `active_plugins`).
2. **Alternativa:** `Refresh from Store` cuando la API de WooCommerce funcione.

La acción solo aplica a productos con mapeo de integración activo. Productos con otra `tax_class` en WooCommerce (reducido, cero, etc.) requieren otro Tax Group configurado o ajuste manual.

## 4) VAT histórico (datos ya creados)

Para partners ya mal creados (NIF en nombre de empresa):

Ejecutar acción de servidor en contactos:

- **Airbici: Fix VAT from company name**

Resultado esperado:
- mueve NIF/CIF/NIE a `vat`,
- limpia casos de empresa ficticia con NIF como nombre.

## 5) Reembolsos - comportamiento esperado

Con el custom:

- Si el pedido entra `refunded` (total):
  - factura cliente + nota de crédito total.
- Si el pedido tiene reembolso parcial (`refunds[]`, ej. `40996`):
  - crea nota de crédito parcial por líneas reembolsadas.
- Si luego WooCommerce cambia a `refunded`:
  - se refresca pedido desde API, se actualiza estado y se procesan reembolsos.
- Evita duplicados con `airbici_processed_refund_ids`.

## 6) Casos de prueba mínimos en staging

### Caso A: VAT fallback

1. Crear pedido en WooCommerce con NIF en `billing.company` y meta VAT vacío.
2. Importar pedido.
3. Verificar en Odoo:
   - partner con `vat` informado,
   - sin empresa ficticia con NIF como `name`.

### Caso B: Reembolso parcial (tipo 40996)

1. Importar pedido con reembolso parcial.
2. Ejecutar workflow.
3. Verificar:
   - factura normal por total pedido,
   - nota de crédito parcial por la línea reembolsada,
   - `airbici_processed_refund_ids` actualizado.

### Caso C: Reembolso total (`refunded`)

1. Importar/actualizar pedido con estado `refunded`.
2. Verificar:
   - subestado actualizado,
   - nota de crédito total creada.

## 7) Validaciones post-despliegue

- Revisar queue jobs de integración (sin errores de journal ni de mapeo de impuestos).
- Confirmar que no aparece el error de mezcla `Included in Price`.
- Confirmar que no se duplican notas de crédito al reprocesar webhooks.

## 8) Rollback rápido (si algo falla)

- Desactivar tareas automáticas de facturación en subestados `completed/refunded`.
- Mantener solo importación + actualización de estado.
- Corregir configuración de journal y reintentar.
