# Diccionario de datos — Integración App QC ↔ Odoo

## 1. Resumen

Este documento describe la estructura de datos (diccionario de campos) de todos
los mensajes que se intercambian entre la **App de Control de Calidad** y **Odoo**.

La integración es **bidireccional** y se realiza mediante **webhooks HTTP POST con
cuerpo JSON**. Hay dos canales:

| Canal | Dirección | Quién llama | Endpoint que recibe |
|-------|-----------|-------------|---------------------|
| **Entrante** | Odoo → App QC | Odoo | Endpoint de la App QC (ver §2) |
| **Saliente** | App QC → Odoo | App QC | Endpoint que Odoo debe facilitar |

Todos los mensajes comparten una estructura común:

```jsonc
{
  "event": "<tipo_de_evento>",   // string, obligatorio: identifica el mensaje
  ...                            // campos propios de cada evento (ver abajo)
}
```

El campo `event` es **siempre obligatorio** y determina cómo se interpreta el resto
del payload.

> **Nota sobre el estado de este documento:**
> El canal **entrante (Odoo → App QC)** está implementado y su contrato es firme.
> El canal **saliente (App QC → Odoo)** tiene la infraestructura de envío y reintentos
> lista; la estructura de campos que se describe aquí es la **definitiva por nuestra
> parte** y queda a falta de que nos confirméis encaje con vuestros modelos. Los únicos
> puntos pendientes son los marcados con 🟡 (principalmente el endpoint/token que debéis
> facilitarnos, §2.2).

---

## 2. Transporte, autenticación y entrega

### 2.1 Endpoint entrante (lo expone la App QC, lo llama Odoo)

```
URL:    https://gvmnackvgwfxuoeevgxq.supabase.co/functions/v1/odoo-webhook
Método: POST
Headers:
  Content-Type:  application/json
  Authorization: Bearer <ODOO_WEBHOOK_SECRET>
```

El token `ODOO_WEBHOOK_SECRET` se facilita por canal seguro aparte (no se incluye en
este documento). Toda petición sin ese Bearer correcto se rechaza con `401`.

### 2.2 Endpoint saliente (lo debe exponer Odoo, lo llama la App QC) 🟡

Necesitamos que nos facilitéis:

- **URL** del endpoint que recibirá nuestros eventos.
- **Token/secreto** (`Bearer`) que debemos incluir en la cabecera `Authorization`.

Nuestra política de entrega es: **3 intentos** con reintento en `0s → +5 min → +15 min`.
Si los 3 fallan, el evento queda registrado en nuestro log (`odoo_webhook_log`) con
estado `failed` para reenvío/revisión manual. Consideramos entrega correcta cualquier
respuesta HTTP `2xx`.

### 2.3 Códigos de respuesta esperados

| Código | Significado |
|--------|-------------|
| `200`  | Procesado correctamente |
| `400`  | JSON inválido o falta el campo `event` |
| `401`  | Token de autenticación incorrecto |
| `422`  | Faltan campos obligatorios o un código no existe (ver detalle por evento) |
| `5xx`  | Error interno del receptor (provoca reintento en el canal saliente) |

---

## 3. Canal ENTRANTE — Odoo → App QC (implementado)

### 3.1 `offer.confirmed` — Odoo confirma una oferta/pedido

Odoo nos comunica que una oferta se ha confirmado y debemos prepararla. Al recibirlo,
la App crea la oferta internamente y **asigna automáticamente** los equipos disponibles
en almacén que correspondan a esa variante.

| Campo | Tipo | Obligatorio | Descripción |
|-------|------|:-----------:|-------------|
| `event` | string | ✅ | Valor fijo: `"offer.confirmed"` |
| `offer_code` | string | ✅ | Identificador de la oferta/pedido en Odoo (ej. `OFE-2026-0042`). Es la clave de correlación entre ambos sistemas. |
| `variant_code` | string | ✅ | Código de variante del producto (MPN). Ver §5. Ej. `DOBACK_V2_MO_F1.7.3` |
| `requested_quantity` | integer | ✅ | Nº de unidades solicitadas (> 0) |
| `project_code` | string | ❌ | Código del proyecto en la App QC. Si se omite o es null, se trata como cliente sin proyecto formal. Si se envía y no existe → `422`. |

**Ejemplo:**
```json
{
  "event": "offer.confirmed",
  "offer_code": "OFE-2026-0042",
  "variant_code": "DOBACK_V2_MO_F1.7.3",
  "requested_quantity": 10,
  "project_code": "PROY-IVECO-2026"
}
```

**Respuesta de la App QC:**
```json
{
  "success": true,
  "offer_id": "9f1c…",
  "variant": "DOBACK_V2_MO_F1.7.3",
  "requested": 10,
  "assigned": 7,
  "pending": 3,
  "sop_found": true
}
```
- `assigned`: unidades que se han podido asignar de stock inmediatamente.
- `pending`: unidades que quedan pendientes de stock (se asignarán cuando lleguen lotes).
- `sop_found`: si existe un procedimiento de inspección (SOP) configurado para esa variante.

---

### 3.2 `shipment.registered` — Odoo registra el envío físico

Odoo nos informa de que el envío de una oferta se ha registrado/expedido. El envío
físico lo gestiona Odoo; la App solo registra el evento.

| Campo | Tipo | Obligatorio | Descripción |
|-------|------|:-----------:|-------------|
| `event` | string | ✅ | Valor fijo: `"shipment.registered"` |
| `offer_code` | string | ✅ | Oferta/pedido cuyo envío se registra. Si no existe → `422`. |

**Ejemplo:**
```json
{
  "event": "shipment.registered",
  "offer_code": "OFE-2026-0042"
}
```

---

### 3.3 `catalog.updated` — Odoo notifica cambios en el catálogo

Odoo nos comunica que el catálogo de productos/variantes ha cambiado. La App QC
**procesa el cambio automáticamente** (alta/actualización de variantes — *upsert*),
usando `variant_code` como clave. No requiere intervención manual.

| Campo | Tipo | Obligatorio | Descripción |
|-------|------|:-----------:|-------------|
| `event` | string | ✅ | Valor fijo: `"catalog.updated"` |
| `variants` | array<object> | ✅ | Lista de variantes a dar de alta o actualizar (ver subtabla) |

Cada objeto del array `variants`:

| Campo | Tipo | Obligatorio | Descripción |
|-------|------|:-----------:|-------------|
| `variant_code` | string | ✅ | Código de variante (MPN). Clave de *upsert*: si existe se actualiza, si no se crea. Ver §5. |
| `product_family` | string (enum) | ✅ | Familia de producto: `"DOBACK"`, `"HUMS"` o `"DBELITE"` |
| `description` | string | ❌ | Descripción legible de la variante |
| `is_active` | boolean | ❌ | Si la variante sigue activa en catálogo. `false` la desactiva (no se borra, para no romper histórico). Por defecto `true`. |

**Ejemplo:**
```json
{
  "event": "catalog.updated",
  "variants": [
    {
      "variant_code": "DOBACK_V2_MO_F1.7.3",
      "product_family": "DOBACK",
      "description": "DOBACK V2 Militar, pantalla+altavoz+chasis, FW 1.7.3",
      "is_active": true
    },
    {
      "variant_code": "HUMS_V3_F1.7.3.1",
      "product_family": "HUMS",
      "description": "HUMS V3, RS485+J1939, eSIM+GPS, cable rotativos",
      "is_active": true
    }
  ]
}
```

**Respuesta de la App QC:**
```json
{ "success": true, "processed": 2, "created": 1, "updated": 1 }
```

---

## 4. Canal SALIENTE — App QC → Odoo

Esta es la parte que nos habéis pedido: el mensaje que **la App QC enviará a Odoo**.

Tras vuestra indicación, el canal saliente es **un único evento de resumen agregado**
(`daily.status_summary`), no notificaciones unidad a unidad. No obstante, dado que
necesitáis el **identificador interno por equipo** (el ID que asigna la App y que ve
el cliente final), el resumen **incluye, dentro de cada oferta, el detalle de las
unidades conformes** con su número de serie e ID interno. Así se envía un solo mensaje
consolidado que sigue trayendo los identificadores individuales.

> **Contexto de negocio relevante:** la gestión de fallos y sustituciones de equipos
> ocurre **dentro de la App QC, antes de que Odoo intervenga**. Si un equipo falla la
> inspección, el sistema toma automáticamente otro de almacén. Por tanto Odoo **no
> gestiona reemplazos**: recibe únicamente el resultado final consolidado.

### 4.1 `daily.status_summary` — resumen diario de estado por oferta

Resumen agregado (una vez al día) del estado de cada oferta activa: cuántas unidades
están conformes, en reparación y pendientes, más el detalle de las unidades ya conformes.
Es el "pedido X: N conformes, Y a reparación" acordado en la lógica de negocio.

| Campo | Tipo | Obligatorio | Descripción |
|-------|------|:-----------:|-------------|
| `event` | string | ✅ | Valor fijo: `"daily.status_summary"` |
| `timestamp` | string (ISO-8601) | ✅ | Momento del envío |
| `date` | string (YYYY-MM-DD) | ✅ | Día al que corresponde el resumen |
| `offers` | array<object> | ✅ | Lista de ofertas con su desglose (ver subtabla) |

Cada objeto del array `offers`:

| Campo | Tipo | Obligatorio | Descripción |
|-------|------|:-----------:|-------------|
| `offer_code` | string | ✅ | Oferta/pedido de Odoo. **Clave de correlación** entre ambos sistemas. |
| `variant_code` | string | ✅ | Variante del producto |
| `project_code` | string | ❌ | Proyecto asociado (si la oferta lo tiene) |
| `requested_quantity` | integer | ✅ | Unidades solicitadas en la oferta |
| `ready_to_ship` | integer | ✅ | Nº de unidades conformes, listas para envío |
| `in_repair` | integer | ✅ | Nº de unidades que fallaron QC y están en reparación |
| `pending_qc` | integer | ✅ | Nº de unidades aún pendientes de inspeccionar |
| `pending_stock` | integer | ✅ | Nº de unidades pendientes de asignar por falta de stock |
| `units` | array<object> | ✅ | Detalle de las unidades **conformes** (listas para envío). Lista vacía si aún no hay ninguna. |

Cada objeto del array `units` (una unidad conforme):

| Campo | Tipo | Obligatorio | Descripción |
|-------|------|:-----------:|-------------|
| `manufacturer_sn` | string | ✅ | Nº de serie físico del fabricante (clave única del equipo) |
| `internal_id` | string | ✅ | Identificador interno que asigna la App QC; es el ID que ve el cliente final |
| `qc_passed_at` | string (ISO-8601) | ✅ | Fecha/hora en que la unidad superó la inspección |

**Ejemplo:**
```json
{
  "event": "daily.status_summary",
  "timestamp": "2026-06-29T23:00:00Z",
  "date": "2026-06-29",
  "offers": [
    {
      "offer_code": "OFE-2026-0042",
      "variant_code": "DOBACK_V2_MO_F1.7.3",
      "project_code": "PROY-IVECO-2026",
      "requested_quantity": 10,
      "ready_to_ship": 2,
      "in_repair": 1,
      "pending_qc": 4,
      "pending_stock": 3,
      "units": [
        {
          "manufacturer_sn": "SN-AB12345",
          "internal_id": "PAR-2026-0107",
          "qc_passed_at": "2026-06-29T10:14:58Z"
        },
        {
          "manufacturer_sn": "SN-AB12346",
          "internal_id": "PAR-2026-0108",
          "qc_passed_at": "2026-06-29T16:32:11Z"
        }
      ]
    }
  ]
}
```

> **Nota de diseño:** el detalle de `units` enumera las unidades **conformes acumuladas**
> de la oferta (no solo las del día), para que Odoo siempre tenga la lista completa de
> seriales/IDs internos listos para esa oferta. Si preferís que `units` contenga solo
> las del día, lo ajustamos.