# Dev Container Guide (Linux/macOS)

Esta guia sirve para ti y para cualquier IA que necesite montar este entorno desde cero en otro equipo (incluyendo MacBook).

## 1) Requisitos

- Docker Desktop instalado y corriendo.
- Extension Dev Containers en VS Code o Cursor.
- Git clonado en una ruta local.
- (Opcional) `uv` instalado localmente si quieres ejecutar comandos fuera del contenedor.

## 2) Variables locales (`.env`)

Este proyecto usa `.devcontainer/.env` para rutas locales del host.  
No se versiona el archivo real; se versiona solo el ejemplo.

Pasos:

1. Copiar:
   - `cp .devcontainer/.env.example .devcontainer/.env`
2. Editar `ODOO_SERVER` con ruta absoluta local:
   - Linux ejemplo: `/home/usuario/Documents/odoo_versions/17.0`
   - macOS ejemplo: `/Users/usuario/Documents/odoo_versions/17.0`

## 3) Red Docker compartida (`shared-net`)

En este repo la red `shared-net` es **requisito** y se mantiene como externa en `docker-compose`.

Crear una sola vez por maquina:

```bash
docker network create shared-net
```

Verificar:

```bash
docker network ls | rg shared-net
```

## 4) HTTPS local con nginx

La configuracion de nginx esta en:

- `.devcontainer/nginx/nginx.conf`

Certificados autofirmados:

- Script: `.devcontainer/nginx/generate-ssl.sh`
- Salida: `.devcontainer/nginx/ssl/cert.pem` y `.devcontainer/nginx/ssl/key.pem`

Si falta SSL o quieres regenerar:

```bash
cd .devcontainer/nginx
./generate-ssl.sh
```

## 5) Arranque del dev container

### Clonado rapido de repos OCA (sin repos Clodofy)

Antes de abrir el contenedor, puedes clonar/actualizar automaticamente los repos OCA con:

```bash
./default_instalation
```

Opcionalmente puedes cambiar la rama objetivo:

```bash
OCA_BRANCH=17.0 ./default_instalation
```

El script:

- Lee `oca/repositories.txt`.
- Ignora entradas comentadas o invalidas.
- Ignora repos con `clodofy` en el nombre.
- Clona desde `https://github.com/OCA/<repo>.git`.
- Si el repo ya existe, hace `fetch + checkout + pull` en la rama indicada.

1. Abrir el repo en Cursor/VS Code.
2. Ejecutar "Reopen in Container".
3. Esperar a `postCreateCommand` (instala dependencias con `uv`).
4. Dentro del contenedor, iniciar Odoo con tu comando habitual (ej. `python /workspace/odoo/odoo-bin -c /workspace/odoo.conf`).

## 6) Puertos y servicios

- `https://localhost` -> Odoo por nginx (443)
- `http://localhost` -> redirige a HTTPS (80)
- `http://localhost:8085` -> pgAdmin
- Odoo expone internamente 8069/8071/8072 hacia nginx

## 7) Checklist rapido antes de subir a GitHub

- Confirmar que `.devcontainer/.env` no se versiona.
- Confirmar que claves/certs locales no se versionan.
- Confirmar que existe `.devcontainer/nginx/nginx.conf`.
- Confirmar que `shared-net` esta documentada para onboarding.

## 8) Inventario actual de scripts (`scripts/`)

- `scripts/delete_queue_jobs.py`
- `scripts/delete_queue_jobs.sh`
- `scripts/fix_missing_filestore_attachment.py`
- `scripts/switch_oca_versions.py`
- `scripts/update_account_move_comment_to_reference.py`
- `scripts/update_modules.py`
- `scripts/update_modules.sh`
- `scripts/update_odoo_notifications.py`
- `scripts/update_odoo_notifications_report_20260306_021819.md`
- `scripts/update_odoo_notifications_report_20260306_021949.md`

## 9) Inventario actual de `custom_scripts/`

- `custom_scripts/AJUSTE_COLFISIO.py`
- `custom_scripts/check_mongo.py`
- `custom_scripts/cleanup_mongo_duplicates.py`
- `custom_scripts/corregir_n_factura_inscripciones_xmlrpc.py`
- `custom_scripts/delete_mongo.py`
- `custom_scripts/delete_orphan_invoices.py`
- `custom_scripts/fix_incoherent_refund_invoices.py`
- `custom_scripts/fix_incoherent_refund_invoices_jsonrpc.py`
- `custom_scripts/segu_clients.py`
- `custom_scripts/validar_codigos_colegiado_efectos_xmlrpc.py`
- `custom_scripts/validar_cuentas_bancarias_xmlrpc.py`
- `custom_scripts/md/auditoria_borrados_incorrectos_20260227_084643.md`
- `custom_scripts/md/auditoria_borrados_incorrectos_20260227_084756.md`
- `custom_scripts/md/auditoria_borrados_incorrectos_20260227_085107.md`
- `custom_scripts/md/auditoria_borrados_incorrectos_20260227_085427.md`
- `custom_scripts/md/cleanup_mongo_report_20260204_082421.md`
- `custom_scripts/md/cleanup_mongo_report_20260204_182539.md`
- `custom_scripts/md/cleanup_mongo_report_20260217_162323.md`
- `custom_scripts/md/cleanup_mongo_summary_20260204_182538.json`
- `custom_scripts/md/cleanup_mongo_summary_20260217_162323.json`
- `custom_scripts/md/duplicated_external_references_20260219_114701.md`
- `custom_scripts/md/efectos_marked_delete_20260227_082302.md`
- `custom_scripts/md/efectos_marked_delete_20260227_083909.md`
- `custom_scripts/md/efectos_sync_summary_20260227_080302.json`
- `custom_scripts/md/efectos_sync_summary_20260227_080411.json`
- `custom_scripts/md/efectos_sync_summary_20260227_081306.json`
- `custom_scripts/md/efectos_sync_summary_20260227_082302.json`
- `custom_scripts/md/efectos_sync_summary_20260227_083909.json`
- `custom_scripts/md/fix_incoherent_refunds_report_20260223_133741.md`
- `custom_scripts/md/fix_incoherent_refunds_report_20260224_045223.md`
- `custom_scripts/md/fix_incoherent_refunds_report_20260224_115216.md`
- `custom_scripts/md/fix_incoherent_refunds_report_20260224_121519.md`
- `custom_scripts/md/fix_incoherent_refunds_report_20260224_122713.md`
- `custom_scripts/md/fix_incoherent_refunds_summary_20260218_135840.json`
- `custom_scripts/md/fix_incoherent_refunds_summary_20260223_133740.json`
- `custom_scripts/md/fix_incoherent_refunds_summary_20260224_045223.json`
- `custom_scripts/md/fix_incoherent_refunds_summary_20260224_115216.json`
- `custom_scripts/md/fix_incoherent_refunds_summary_20260224_121519.json`
- `custom_scripts/md/fix_incoherent_refunds_summary_20260224_122713.json`

## 10) Comandos de diagnostico utiles

```bash
docker compose -f .devcontainer/docker-compose.yml --env-file .devcontainer/.env config
docker ps
docker logs nginx-proxy
docker logs odoo-dev
docker logs pgdb
```
