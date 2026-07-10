# Productos archivados en producción — Happy Japan

**Fecha:** 2026-07-08 05:13 UTC
**URL:** https://stg20260707-odoo17-happy-japan.n3.clodofy.cloud
**Base de datos:** `odoo_stg20260707_odoo17_happy_japan`
**Acción:** ARCHIVADO (active=False)
**Total productos:** 0

## Cómo revertir

Para reactivar un producto archivado, en Odoo ir a Inventario → Productos,
activar el filtro "Archivados", buscar por ID o referencia, y pulsar "Desarchivar".

Vía XML-RPC (ejemplo para un ID):

```python
models.execute_kw(db, uid, password, 'product.template', 'write',
    [[PRODUCT_ID], {'active': True}])
```

## Listado completo

| # | ID archivado | Referencia | Nombre archivado | ID conservado | Motivo |
|---|-------------|------------|------------------|---------------|--------|

## IDs archivados (lista plana)

```

```
