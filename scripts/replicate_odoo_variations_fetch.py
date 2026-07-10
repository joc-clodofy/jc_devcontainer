#!/usr/bin/env python3
"""
Replica el flujo de Link Products -> get_templates_and_products_for_validation_test
para WooCommerce (sale.integration id=1 / Airbici).

Uso:
  python3 replicate_odoo_variations_fetch.py
  python3 replicate_odoo_variations_fetch.py --sync   # use_async=False
  python3 replicate_odoo_variations_fetch.py --threads 4
"""
from __future__ import annotations

import argparse
import asyncio
import http.client
import json
import sys
import time

# Mismo parche que integration_woocommerce/client/wordpress_api.py
if http.client._MAXHEADERS < 1000:
    http.client._MAXHEADERS = 1000
from urllib.parse import urlencode, urlparse, parse_qs

import aiohttp
from aiohttp import BasicAuth
import requests
from requests.auth import HTTPBasicAuth

# --- mismos valores que Odoo / air DB ---
BASE_URL = 'https://airbici.es'
WC_VERSION = 'wc/v3'
CONSUMER_KEY = 'ck_0e938777edc3e8e6b78fe84ea835a92a6b4d05cd'
CONSUMER_SECRET = 'cs_340ae9e6802ca92a60f7be6dbcfd428981c5db47'
LANG = 'es'
IMPORT_TYPES = ['simple', 'external', 'variable']
PRODUCT_FIELDS = 'id,name,type,sku,barcode'
VARIANT_FIELDS = 'id,sku,barcode'
MAX_PAGE_SIZE = 100
USER_AGENT = 'Odoo-Integration-Woocommerce/1.0'
TIMEOUT = 30


def gather_url(endpoint: str) -> str:
    return f'{BASE_URL.rstrip("/")}/wp-json/{WC_VERSION}/{endpoint.lstrip("/")}'


def default_headers():
    return {'Accept': 'application/json', 'User-Agent': USER_AGENT}


def prepare_params(extra: dict | None = None) -> dict:
    params = dict(extra or {})
    params.setdefault('per_page', MAX_PAGE_SIZE)
    params.setdefault('orderby', 'id')
    return params


def parse_link_header(response) -> str | None:
    link = response.headers.get('Link') or response.headers.get('link')
    if not link:
        return None
    for part in link.split(','):
        if 'rel="next"' in part:
            return part.split(';')[0].strip(' <>')
    return None


def sync_get_all(endpoint: str, params: dict) -> list:
    """Replica _request_with_paging de WordPressAPI."""
    url = gather_url(endpoint)
    auth = HTTPBasicAuth(CONSUMER_KEY, CONSUMER_SECRET)
    headers = default_headers()
    result = []

    while url:
        resp = requests.get(
            url, params=params if url == gather_url(endpoint) else None,
            auth=auth, headers=headers, timeout=TIMEOUT,
        )
        if resp.status_code not in (200, 201):
            return _fail_sync(url, resp)
        data = resp.json()
        if isinstance(data, dict):
            data = [data]
        result.extend(data)
        url = parse_link_header(resp)
        params = None  # next URL ya trae query string

    return result


def _fail_sync(url, resp):
    body = (resp.text or '')[:1200]
    print(f'\n[FALLO SYNC] HTTP {resp.status_code}\nURL: {url}\nContent-Type: {resp.headers.get("Content-Type")}\n{body}\n')
    sys.exit(1)


def _audit_write(path: str | None, line: str):
    if not path:
        return
    with open(path, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


async def async_get_one(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    endpoint: str,
    params: dict,
    audit_path: str | None = None,
) -> tuple[str, list, int | None]:
    """Replica _async_get (with_paging=False en fetch_urls de validación)."""
    url = gather_url(endpoint)
    async with semaphore:
        async with session.get(
            url,
            params=params,
            auth=BasicAuth(CONSUMER_KEY, CONSUMER_SECRET),
            headers=default_headers(),
            timeout=aiohttp.ClientTimeout(total=TIMEOUT),
        ) as response:
            body = await response.text()
            status = response.status
            if status not in (200, 201):
                preview = (body or '').strip()[:1200]
                _audit_write(audit_path, f'FAIL {status} {endpoint} {url}')
                _audit_write(audit_path, preview[:500])
                raise RuntimeError(
                    f'HTTP {status} para {url}\nContent-Type: {response.headers.get("Content-Type")}\n{preview}'
                )
            _audit_write(audit_path, f'OK {status} {endpoint}')
            if not body.strip():
                return endpoint, [], status
            if not (
                'application/json' in (response.headers.get('Content-Type') or '').lower()
                or body.lstrip().startswith(('{', '['))
            ):
                raise RuntimeError(f'No JSON para {url}: {body[:500]}')
            return endpoint, json.loads(body), status


async def async_fetch_urls(
    endpoints: list[str], params: dict, threads: int, audit_path: str | None = None,
) -> dict:
    """Replica fetch_urls + _prepare_tasks (with_paging=False)."""
    semaphore = asyncio.Semaphore(threads)
    params_ = prepare_params(params)

    async with aiohttp.ClientSession() as session:
        # Odoo hace await self._aget(...) en bucle y luego gather — mismo orden de creación
        coros = [
            async_get_one(session, semaphore, ep, dict(params_), audit_path)
            for ep in endpoints
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)

    out = {}
    errors = []
    for item in results:
        if isinstance(item, Exception):
            errors.append(item)
            continue
        endpoint, data, _status = item
        if isinstance(data, dict):
            data = [data]
        out[endpoint] = data
    return out, errors


def sync_fetch_urls(endpoints: list[str], params: dict, audit_path: str | None = None) -> dict:
    """Replica fetch_urls con use_async=False."""
    out = {}
    for i, ep in enumerate(endpoints, 1):
        url = gather_url(ep)
        auth = HTTPBasicAuth(CONSUMER_KEY, CONSUMER_SECRET)
        resp = requests.get(
            url, params=prepare_params(params), auth=auth,
            headers=default_headers(), timeout=TIMEOUT,
        )
        if resp.status_code not in (200, 201):
            _audit_write(audit_path, f'FAIL {resp.status_code} [{i}/{len(endpoints)}] {ep}')
            _fail_sync(url, resp)
        _audit_write(audit_path, f'OK {resp.status_code} [{i}/{len(endpoints)}] {ep}')
        data = resp.json()
        out[ep] = data if isinstance(data, list) else [data]
    return out


def fetch_products_like_odoo() -> list[dict]:
    """Replica _get_products con filtro de import (3 tipos, paginado)."""
    products = []
    base = {'lang': LANG, '_fields': PRODUCT_FIELDS}

    for p_type in IMPORT_TYPES:
        params = {**base, 'type': p_type}
        batch = sync_get_all('products', params)
        print(f'  tipo {p_type}: {len(batch)} productos')
        products.extend(batch)
    return products


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sync', action='store_true', help='Modo secuencial (use_async=False)')
    parser.add_argument('--threads', type=int, default=4, help='async_threads (default 4)')
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Guarda cada petición en /tmp/woocommerce_fetch_audit.log',
    )
    args = parser.parse_args()
    audit_path = '/tmp/woocommerce_fetch_audit.log'

    print('=== Paso 1: listar productos (como _get_products) ===')
    t0 = time.time()
    products = fetch_products_like_odoo()
    variable = [p for p in products if p.get('type') == 'variable']
    print(f'Total productos: {len(products)}, variables: {len(variable)}')

    endpoints = [f'products/{p["id"]}/variations' for p in variable]
    var_params = {
        '_fields': VARIANT_FIELDS,
        'lang': LANG,
    }

    print(f'\n=== Paso 2: {len(endpoints)} peticiones a /variations ===')
    print(f'Modo: {"SYNC (use_async=False)" if args.sync else f"ASYNC ({args.threads} hilos)"}')
    audit = audit_path if args.verbose else None
    if audit:
        open(audit, 'w').close()
        print(f'Log detallado: {audit}')

    t1 = time.time()
    if args.sync:
        try:
            variations_data = sync_fetch_urls(endpoints, var_params, audit)
            errors = []
        except SystemExit:
            return
    else:
        variations_data, errors = asyncio.run(
            async_fetch_urls(endpoints, var_params, args.threads, audit)
        )

    elapsed = time.time() - t0
    ok = len(variations_data)
    fail = len(errors)

    print(f'\n=== Resultado ===')
    print(f'OK: {ok}/{len(endpoints)} endpoints')
    print(f'Errores: {fail}')
    print(f'Tiempo total: {elapsed:.1f}s (variations: {time.time()-t1:.1f}s)')

    if errors:
        print('\n--- Primeros errores ---')
        for i, err in enumerate(errors[:5]):
            print(f'\n[{i+1}] {err}')
        if fail > 5:
            print(f'\n... y {fail - 5} más')
        sys.exit(1)

    print('\nÉxito: todas las variaciones respondieron 200 JSON (como debería Link Products).')


if __name__ == '__main__':
    main()
