#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Script XML-RPC — Airbici (producción) + fetch WooCommerce controlado

Flujo:
  1. Conecta al Odoo de producción por XML-RPC.
  2. action_check_connection (la tienda se prueba desde el servidor Odoo).
  3. Fetch catálogo Airbici como Link Products / get_templates_and_products…
     pero en lotes de 50 variaciones + pausa de 60 s entre lotes (anti rate-limit).
  4. integrationApiImportProducts por lotes de 50 + 60 s entre lotes Odoo.

Sin modificar ecommerce-integration.

Local (Odoo con air.conf, puerto 8069 HTTP):
  export ODOO_URL=http://localhost:8069
  export ODOO_DB=air
  export ODOO_USER=admin
  export ODOO_PASSWORD=admin

Local vía nginx HTTPS (puerto 443, cert autofirmado):
  export ODOO_URL=https://localhost
  export ODOO_SSL_VERIFY=0

Producción:
  export ODOO_URL=https://odoo17-airbici.n1.clodofy.cloud
  export ODOO_PASSWORD='...'

Uso:
  python3 airbici_link_products_xmlrpc.py --dry-run
  python3 airbici_link_products_xmlrpc.py
"""

import argparse
import http.client
import os
import ssl
import sys
import time
import xmlrpc.client
from datetime import datetime

import requests
from requests.auth import HTTPBasicAuth

# ---------------------------------------------------------------------------
# Odoo — por defecto LOCAL (air.conf → http_port=8069)
# Producción: export ODOO_URL=https://odoo17-airbici.n1.clodofy.cloud
# HTTPS local (nginx devcontainer): export ODOO_URL=https://localhost ODOO_SSL_VERIFY=0
# Nota: no hay Odoo en :8080; 8069 = HTTP directo | 443 = nginx → Odoo
# ---------------------------------------------------------------------------
# Local devcontainer: http://localhost:8069 | HTTPS nginx: https://localhost + ODOO_SSL_VERIFY=0
# ODOO_URL = os.environ.get('ODOO_URL', 'http://localhost:8069')
ODOO_URL = os.environ.get('ODOO_URL', 'https://airbici.v17.clodofy.cloud')
ODOO_DB = os.environ.get('ODOO_DB', 'odoo_odoo17_airbici')
ODOO_USER = os.environ.get('ODOO_USER', 'jose.carrillo@clodofy.com')
ODOO_PASSWORD = os.environ.get('ODOO_PASSWORD', '123')
ODOO_SSL_VERIFY = os.environ.get('ODOO_SSL_VERIFY', '1').lower() not in ('0', 'false', 'no')

WC_URL = os.environ.get('WC_URL', '')
WC_KEY = os.environ.get('WC_KEY', '')
WC_SECRET = os.environ.get('WC_SECRET', '')

INTEGRATION_ID = int(os.environ.get('INTEGRATION_ID', '1'))

if http.client._MAXHEADERS < 1000:
    http.client._MAXHEADERS = 1000

USER_AGENT = 'Odoo-Integration-Woocommerce/1.0'
WC_TIMEOUT = 45
WC_MAX_RETRIES = 5
WC_RETRY_DELAY = 15

# Mismos campos que get_templates_and_products_for_validation_test
PRODUCT_FIELDS = 'id,name,type,sku,barcode'
VARIANT_FIELDS = 'id,sku,barcode'

_T0 = time.monotonic()


def log(msg, level='*'):
    """Print con marca de tiempo y segundos desde el inicio."""
    elapsed = time.monotonic() - _T0
    ts = datetime.now().strftime('%H:%M:%S')
    print(f'[{ts} +{elapsed:6.0f}s] [{level}] {msg}', flush=True)


def fmt_duration(seconds):
    if seconds < 60:
        return f'{seconds:.0f}s'
    return f'{seconds // 60:.0f}m {seconds % 60:.0f}s'


def preflight_odoo_url():
    """Comprueba que el puerto responde antes de XML-RPC (evita 'Connection refused' críptico)."""
    import urllib.error
    import urllib.request

    base = ODOO_URL.rstrip('/')
    probe = f'{base}/web/login'
    ctx = None
    if base.startswith('https://') and not ODOO_SSL_VERIFY:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        urllib.request.urlopen(probe, timeout=5, context=ctx)
    except urllib.error.URLError as exc:
        reason = getattr(exc, 'reason', exc)
        hint = (
            '¿Odoo en marcha? En el devcontainer: cd /workspace && python3 odoo/odoo-bin -c air.conf\n'
            '¿Docker arriba? docker ps → odoo-dev debe mapear 8069.\n'
            'Local suele ser: export ODOO_URL=http://localhost:8069'
        )
        raise RuntimeError(f'No hay servidor en {base} ({reason}).\n{hint}') from exc


def connect_odoo():
    preflight_odoo_url()
    base = ODOO_URL.rstrip('/')
    ctx = None
    if base.startswith('https://') and not ODOO_SSL_VERIFY:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        common = xmlrpc.client.ServerProxy(
            f'{base}/xmlrpc/2/common', allow_none=True, context=ctx,
        )
        models = xmlrpc.client.ServerProxy(
            f'{base}/xmlrpc/2/object', allow_none=True, context=ctx,
        )
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    except (ConnectionRefusedError, OSError) as exc:
        raise RuntimeError(
            f'Connection refused en {base}/xmlrpc — Odoo no escucha o URL incorrecta. '
            f'Prueba: export ODOO_URL=http://localhost:8069'
        ) from exc
    except xmlrpc.client.ProtocolError as exc:
        if exc.errcode == 404:
            raise RuntimeError(
                f'XML-RPC 404 en {base}. En air.conf falta base en server_wide_modules '
                f'o hay que reiniciar Odoo tras el cambio.'
            ) from exc
        raise
    if not uid:
        raise RuntimeError('Autenticación fallida (ODOO_USER / ODOO_PASSWORD).')
    return uid, models


def odoo_check_connection(models, uid, integration_id, dry_run):
    """Ejecuta action_check_connection en el servidor Odoo (no en tu PC)."""
    log('Paso 1: check conexión (Odoo → WooCommerce, desde el servidor Odoo)')
    if dry_run:
        log('dry-run: se omitiría action_check_connection', 'dry')
        return
    t0 = time.monotonic()
    try:
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.integration', 'action_check_connection',
            [[integration_id]],
            {'raise_success': False},
        )
        log(f'Conexión OK ({fmt_duration(time.monotonic() - t0)})')
    except xmlrpc.client.Fault as fault:
        msg = fault.faultString or ''
        # Odoo local: el método devuelve None y el servidor XML-RPC no puede serializarlo.
        if 'cannot marshal None' in msg or 'allow_none' in msg:
            log(
                'check conexión ejecutado en Odoo pero respuesta None (bug XML-RPC local); se continúa',
                '!',
            )
            return
        raise RuntimeError(f'Check conexión falló: {fault.faultString}') from fault


def read_integration_wc_settings(models, uid, integration_id):
    rows = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'sale.integration.api.field', 'search_read',
        [[('sia_id', '=', integration_id), ('name', 'in', ['url', 'consumer_key', 'consumer_secret'])]],
        {'fields': ['name', 'value']},
    )
    cfg = {row['name']: (row['value'] or '').strip() for row in rows}
    url = (WC_URL or cfg.get('url', '')).rstrip('/')
    key = WC_KEY or cfg.get('consumer_key', '')
    secret = WC_SECRET or cfg.get('consumer_secret', '')
    if not all([url, key, secret]):
        raise RuntimeError('Faltan url / consumer_key / consumer_secret en la integración.')
    return url, key, secret


def wc_get(base_url, key, secret, path, params, label=''):
    url = f'{base_url}/wp-json/wc/v3/{path.lstrip("/")}'
    auth = HTTPBasicAuth(key, secret)
    headers = {'Accept': 'application/json', 'User-Agent': USER_AGENT}
    last_error = None
    for attempt in range(WC_MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url, params=params, auth=auth, headers=headers, timeout=WC_TIMEOUT,
            )
            if resp.status_code in (403, 429, 502, 503, 504) and attempt < WC_MAX_RETRIES:
                wait = WC_RETRY_DELAY * (2 ** attempt)
                log(f'{label} HTTP {resp.status_code} → espera {wait}s (intento {attempt + 1})', '!')
                time.sleep(wait)
                continue
            if resp.status_code not in (200, 201):
                raise RuntimeError(f'{label} HTTP {resp.status_code}: {(resp.text or "")[:500]}')
            return resp
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as exc:
            last_error = exc
            if attempt >= WC_MAX_RETRIES:
                raise
            wait = WC_RETRY_DELAY * (2 ** attempt)
            log(f'{label} {type(exc).__name__} → espera {wait}s (intento {attempt + 1})', '!')
            time.sleep(wait)
    if last_error:
        raise last_error
    raise RuntimeError(f'Fallo WC: {label}')


def pause_between_batches(pause_seconds, label):
    if pause_seconds <= 0:
        return
    log(f'Pausa {pause_seconds}s ({label})…')
    time.sleep(pause_seconds)


def fetch_airbici_catalog(wc_url, wc_key, wc_secret, batch_size, pause_seconds, skip_variations):
    """
    Replica el fetch de Link Products (productos + variaciones) en lotes de N.
    """
    log('Paso 2: fetch catálogo WooCommerce (REST, desde este script)')
    products = []
    variable_ids = []
    t_fetch = time.monotonic()

    for p_type in ('simple', 'external', 'variable'):
        page = 1
        type_count_before = len(products)
        while True:
            resp = wc_get(
                wc_url, wc_key, wc_secret, 'products',
                {
                    'type': p_type,
                    'lang': 'es',
                    'per_page': 100,
                    'orderby': 'id',
                    'page': page,
                    '_fields': PRODUCT_FIELDS,
                },
                label=f'products/{p_type} p{page}',
            )
            batch = resp.json()
            if not batch:
                break
            products.extend(batch)
            for item in batch:
                if item.get('type') == 'variable':
                    variable_ids.append(item['id'])
            total_pages = int(resp.headers.get('X-WP-TotalPages') or 1)
            total_items = resp.headers.get('X-WP-Total', '?')
            log(
                f'WC {p_type} p{page}/{total_pages}: +{len(batch)} '
                f'(acum. tipo {len(products) - type_count_before}, total WP={total_items})',
            )
            if page >= total_pages:
                break
            page += 1
        added = len(products) - type_count_before
        log(f'WC {p_type} listo → {added} plantillas')

    template_ids = [str(p['id']) for p in products]
    by_type = {}
    for p in products:
        by_type[p.get('type', '?')] = by_type.get(p.get('type', '?'), 0) + 1
    log(
        f'Plantillas totales: {len(template_ids)} | variables: {len(variable_ids)} | '
        f'por tipo: {by_type} | fetch listado: {fmt_duration(time.monotonic() - t_fetch)}',
    )

    if skip_variations or not variable_ids:
        if skip_variations:
            log('Variaciones omitidas (--skip-variations)', 'dry')
        return template_ids, products

    total_batches = (len(variable_ids) + batch_size - 1) // batch_size
    est_pause = pause_seconds * max(0, total_batches - 1)
    log(
        f'Variaciones: {len(variable_ids)} productos → {total_batches} lotes de {batch_size} '
        f'(pausa ~{est_pause}s entre lotes si pause={pause_seconds})',
    )

    for batch_num, start in enumerate(range(0, len(variable_ids), batch_size), 1):
        chunk = variable_ids[start:start + batch_size]
        t_batch = time.monotonic()
        log(f'Lote WC {batch_num}/{total_batches} — ids {chunk[0]}…{chunk[-1]} ({len(chunk)} variables)')
        ok = 0
        for i, pid in enumerate(chunk, 1):
            wc_get(
                wc_url, wc_key, wc_secret,
                f'products/{pid}/variations',
                {'lang': 'es', 'per_page': 100, 'orderby': 'id', '_fields': VARIANT_FIELDS},
                label=f'variations/{pid}',
            )
            ok += 1
            if i % 10 == 0 or i == len(chunk):
                log(f'  variaciones {i}/{len(chunk)} en lote {batch_num}')
        log(f'Lote WC {batch_num} OK ({ok}/{len(chunk)}) en {fmt_duration(time.monotonic() - t_batch)}')
        if batch_num < total_batches:
            pause_between_batches(pause_seconds, 'entre lotes WooCommerce')

    log(f'Fetch completo (listado + variaciones): {fmt_duration(time.monotonic() - t_fetch)}')
    return template_ids, products


def summarize_catalog(products):
    """Resumen rápido tipo validación (SKU vacíos)."""
    empty_tpl_sku = sum(1 for p in products if not (p.get('sku') or '').strip())
    with_sku = len(products) - empty_tpl_sku
    log(f'Resumen catálogo: {with_sku} con SKU, {empty_tpl_sku} sin SKU en WC')


def _xmlrpc_none_ok(exc):
    """Odoo local devuelve None y el servidor XML-RPC no lo serializa; el job sí se encola."""
    if not isinstance(exc, xmlrpc.client.Fault):
        return False
    msg = exc.faultString or ''
    return 'cannot marshal None' in msg or 'allow_none' in msg


ODOO_IMPORT_BLOCK_SIZE = 150  # sale.integration.IMPORT_EXTERNAL_BLOCK


def _execute_import_products(models, uid, integration_id, ext_ids, dry_run):
    if dry_run:
        return
    try:
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.integration', 'integrationApiImportProducts',
            [[integration_id]],
            {'external_ids': ext_ids},
        )
    except xmlrpc.client.Fault as fault:
        if not _xmlrpc_none_ok(fault):
            raise
        log('import OK en Odoo (respuesta None por XML-RPC local)', '!')


def wait_queue_job_by_identity(models, uid, identity_key, poll_seconds=15, timeout=7200):
    """Espera a que no queden jobs activos con esta identity_key."""
    active_states = ['pending', 'enqueued', 'started', 'wait_dependencies']
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'queue.job', 'search_read',
            [[('identity_key', '=', identity_key), ('state', 'in', active_states)]],
            {'fields': ['id', 'state', 'name'], 'limit': 5},
        )
        if not jobs:
            return
        states = ', '.join(f'{j["id"]}:{j["state"]}' for j in jobs)
        log(f'  job activo {identity_key} → {states} (espera {poll_seconds}s)')
        time.sleep(poll_seconds)
    raise RuntimeError(f'Timeout esperando job {identity_key}')


def odoo_import_all_once(models, uid, integration_id, template_ids, dry_run):
    """
    Una sola llamada RPC: Odoo parte en bloques 1,2,3… (150 ids) con identity_key distinta.
    """
    ext_ids = [str(i) for i in template_ids]
    n_blocks = (len(ext_ids) + ODOO_IMPORT_BLOCK_SIZE - 1) // ODOO_IMPORT_BLOCK_SIZE
    log(
        f'integrationApiImportProducts → {len(ext_ids)} ids en 1 RPC '
        f'(Odoo encolará ~{n_blocks} jobs: block-1…{n_blocks})',
    )
    if dry_run:
        log(f'dry-run: {len(ext_ids)} ids, ~{n_blocks} jobs internos', 'dry')
        return
    t0 = time.monotonic()
    _execute_import_products(models, uid, integration_id, ext_ids, dry_run)
    log(f'RPC import terminada en {fmt_duration(time.monotonic() - t0)}')


def odoo_import_rpc_batches(
    models, uid, integration_id, template_ids, batch_size, pause_seconds, dry_run,
):
    """
    Varias llamadas RPC (legacy). PROBLEMA: cada llamada reinicia block=1 → misma
    identity_key. Solo funciona si esperas a que termine el job block-1 antes del siguiente.
    """
    batches = list(chunks(template_ids, batch_size))
    identity_block_1 = f'import_external_product_block-{integration_id}-1'
    log(
        f'Modo RPC por lotes ({len(batches)} llamadas). '
        f'identity_key fija: {identity_block_1} — se espera fin de job entre lotes',
        '!',
    )
    for idx, batch in enumerate(batches, 1):
        ext_ids = [str(i) for i in batch]
        id_range = f'{ext_ids[0]}…{ext_ids[-1]}' if len(ext_ids) > 1 else ext_ids[0]
        log(f'Lote RPC {idx}/{len(batches)} → {len(ext_ids)} ids ({id_range})')
        if dry_run:
            log(f'dry-run integrationApiImportProducts ({id_range})', 'dry')
        else:
            if idx > 1:
                wait_queue_job_by_identity(models, uid, identity_block_1)
            _execute_import_products(models, uid, integration_id, ext_ids, dry_run)
            log(f'Job encolado ({id_range})')
            if idx < len(batches) and pause_seconds > 0:
                pause_between_batches(pause_seconds, 'entre lotes RPC Odoo')


def chunks(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def main():
    parser = argparse.ArgumentParser(
        description='Airbici producción: check conexión + fetch WC por lotes + import Odoo XML-RPC.',
    )
    parser.add_argument('--integration-id', type=int, default=INTEGRATION_ID)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--batch-size', type=int, default=50,
                        help='Productos por lote (default 50).')
    parser.add_argument('--pause', type=int, default=60,
                        help='Segundos entre lotes (default 60 = 1 min).')
    parser.add_argument('--skip-check', action='store_true',
                        help='No llamar action_check_connection.')
    parser.add_argument('--skip-fetch', action='store_true',
                        help='No llamar WooCommerce; usar --ids-file.')
    parser.add_argument('--skip-variations', action='store_true',
                        help='Solo listar plantillas, sin /variations.')
    parser.add_argument('--skip-import', action='store_true',
                        help='Solo fetch WC, no encolar jobs en Odoo.')
    parser.add_argument('--ids-file', type=str, default='',
                        help='Guardar/cargar IDs (una línea por ID).')
    parser.add_argument(
        '--import-mode',
        choices=('once', 'rpc-batches'),
        default='once',
        help=(
            'once (default): 1 RPC con todos los IDs; Odoo crea jobs block-1,2,3… '
            'rpc-batches: varias RPC (solo si esperas job entre lotes; ver doc)'
        ),
    )
    args = parser.parse_args()

    if not ODOO_PASSWORD:
        log('Define ODOO_PASSWORD en el entorno.', '!')
        sys.exit(1)

    flags = []
    if args.dry_run:
        flags.append('DRY-RUN')
    if args.skip_check:
        flags.append('skip-check')
    if args.skip_fetch:
        flags.append('skip-fetch')
    if args.skip_variations:
        flags.append('skip-variations')
    if args.skip_import:
        flags.append('skip-import')

    print('=' * 60, flush=True)
    log(f'Inicio | modo: {", ".join(flags) or "REAL"}')
    log(f'Odoo: {ODOO_URL} | DB: {ODOO_DB} | user: {ODOO_USER}')
    log(f'Integración id={args.integration_id} | lote={args.batch_size} | pausa={args.pause}s')
    if args.ids_file:
        log(f'Archivo IDs: {args.ids_file}')
    print('=' * 60, flush=True)

    try:
        t0 = time.monotonic()
        uid, models = connect_odoo()
        log(f'XML-RPC OK → uid={uid} ({fmt_duration(time.monotonic() - t0)})')
    except Exception as e:
        log(f'Odoo: {e}', '!')
        sys.exit(1)

    integration = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'sale.integration', 'read',
        [[args.integration_id]],
        {'fields': ['name', 'type_api', 'state']},
    )
    if not integration:
        print(f'[!] No existe sale.integration id={args.integration_id}')
        sys.exit(1)
    integration = integration[0]
    log(f'Tienda: {integration["name"]} | {integration["type_api"]} | state={integration["state"]}')

    if not args.skip_check:
        try:
            odoo_check_connection(models, uid, args.integration_id, args.dry_run)
        except Exception as e:
            log(str(e), '!')
            sys.exit(1)

    template_ids = []
    products = []

    if args.ids_file and os.path.isfile(args.ids_file) and args.skip_fetch:
        with open(args.ids_file, encoding='utf-8') as f:
            template_ids = [ln.strip() for ln in f if ln.strip()]
        log(f'IDs cargados desde {args.ids_file}: {len(template_ids)}')
    elif not args.skip_fetch:
        try:
            wc_url, wc_key, wc_secret = read_integration_wc_settings(
                models, uid, args.integration_id,
            )
        except Exception as e:
            log(str(e), '!')
            sys.exit(1)
        log(f'WooCommerce: {wc_url} | key/secret: {len(wc_key)}+{len(wc_secret)} chars')
        try:
            template_ids, products = fetch_airbici_catalog(
                wc_url, wc_key, wc_secret,
                args.batch_size, args.pause, args.skip_variations,
            )
            summarize_catalog(products)
        except Exception as e:
            log(f'Fetch Airbici: {e}', '!')
            sys.exit(1)
        if args.ids_file:
            with open(args.ids_file, 'w', encoding='utf-8') as f:
                f.write('\n'.join(template_ids) + '\n')
            log(f'IDs guardados en {args.ids_file} ({len(template_ids)} líneas)')

    if args.skip_import:
        log(f'Fin (--skip-import). Tiempo total: {fmt_duration(time.monotonic() - _T0)}')
        return

    if not template_ids:
        log('Sin IDs de plantilla.', '!')
        sys.exit(1)

    ids_cache = args.ids_file or 'airbici_template_ids.txt'
    try:
        with open(ids_cache, 'w', encoding='utf-8') as f:
            f.write('\n'.join(template_ids) + '\n')
        log(f'IDs guardados en {ids_cache} (reanudar: --skip-fetch --skip-check --ids-file {ids_cache})')
    except OSError as exc:
        log(f'No se pudo guardar {ids_cache}: {exc}', '!')

    log(f'Paso 3: import Odoo | modo={args.import_mode} | {len(template_ids)} plantillas')
    log('Método: sale.integration.integrationApiImportProducts')

    t_import = time.monotonic()
    try:
        if args.import_mode == 'once':
            odoo_import_all_once(
                models, uid, args.integration_id, template_ids, args.dry_run,
            )
        else:
            odoo_import_rpc_batches(
                models, uid, args.integration_id, template_ids,
                args.batch_size, args.pause, args.dry_run,
            )
    except Exception as e:
        log(f'Error import Odoo: {e}', '!')
        sys.exit(1)

    total = fmt_duration(time.monotonic() - _T0)
    if args.dry_run:
        log(f'DRY-RUN terminado (sin cambios en Odoo). Tiempo total: {total}')
    else:
        log(f'TERMINADO. Tiempo total: {total}')
        log('Revisa: Ajustes → Técnicas → Trabajos en cola (queue_job)')
        log('Luego: Integración Airbici → Mappings → Products')


if __name__ == '__main__':
    main()
