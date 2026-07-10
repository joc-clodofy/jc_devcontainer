#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Arte / Bellas Artes — Alinear plantillas y variantes Odoo con PrestaShop.

Detecta huecos entre la caché externa de integración y los registros reales
en Odoo (product.template / product.product), y opcionalmente:

  1. Refresca externals + mappings desde PS (`import_external_product`)
  2. Importa/actualiza productos en Odoo (`import_one_product` o cola queue_job)

Staging (default):
  ODOO_URL=https://stg20260604-odoo17-jer-bellas-artes.n3.clodofy.cloud
  ODOO_DB=odoo_stg20260604_odoo17_jer_bellas_artes

Uso:
  # Solo auditoría (sin escrituras)
  python3 scripts/arte_sync_missing_ps_products.py --audit-only

  # Ver qué haría el import
  python3 scripts/arte_sync_missing_ps_products.py --import-products --dry-run

  # Refrescar externals de plantillas con huecos y luego importar (sync, máx. 50)
  python3 scripts/arte_sync_missing_ps_products.py --refresh-externals --import-products --limit 50

  # Encolar imports masivos (recomendado en producción/staging grande)
  python3 scripts/arte_sync_missing_ps_products.py --import-products --use-queue

  # Plantillas concretas (código PS)
  python3 scripts/arte_sync_missing_ps_products.py --template-codes 5369,7196 --import-products

  # Comparar también con catálogo vivo de PS (API)
  python3 scripts/arte_sync_missing_ps_products.py --audit-only --fetch-ps
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import ssl
import sys
import time
import xmlrpc.client
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Odoo
# ---------------------------------------------------------------------------
ODOO_URL = os.environ.get('ODOO_URL', 'https://stg20260604-odoo17-jer-bellas-artes.n3.clodofy.cloud')
ODOO_DB = os.environ.get('ODOO_DB', 'odoo_stg20260604_odoo17_jer_bellas_artes')
ODOO_USER = os.environ.get('ODOO_USER', 'jose.carrillo@clodofy.com')
ODOO_PASSWORD = os.environ.get('ODOO_PASSWORD', '123')
ODOO_SSL_VERIFY = os.environ.get('ODOO_SSL_VERIFY', '1').lower() not in ('0', 'false', 'no')

INTEGRATION_ID = int(os.environ.get('INTEGRATION_ID', '1'))
LANG = os.environ.get('ODOO_LANG', 'es_ES')
REPORT_DIR = Path(os.environ.get('ARTE_REPORT_DIR', 'scripts/arte_reports'))
EXTERNAL_BLOCK = 150  # límite Odoo integration (414 URI too large)
READ_BATCH = 2000
XMLRPC_RECORD_RETURN_MARKERS = (
    "odoo.api.product.template",
    "KeyError: <class 'odoo.api.product.template'>",
)

if http.client._MAXHEADERS < 1000:
    http.client._MAXHEADERS = 1000

_T0 = time.monotonic()
PS_CODE_RE = re.compile(r'^(\d+)(?:-(\d+))?$')


def log(msg, level='*'):
    elapsed = time.monotonic() - _T0
    ts = datetime.now().strftime('%H:%M:%S')
    print(f'[{ts} +{elapsed:6.0f}s] [{level}] {msg}', flush=True)


def fmt_duration(seconds):
    if seconds < 60:
        return f'{seconds:.0f}s'
    return f'{seconds // 60:.0f}m {seconds % 60:.0f}s'


def preflight_odoo_url():
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
        urllib.request.urlopen(probe, timeout=15, context=ctx)
    except urllib.error.URLError as exc:
        reason = getattr(exc, 'reason', exc)
        raise RuntimeError(f'No hay servidor en {base} ({reason})') from exc


def connect_odoo():
    preflight_odoo_url()
    base = ODOO_URL.rstrip('/')
    ctx = None
    if base.startswith('https://') and not ODOO_SSL_VERIFY:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    common = xmlrpc.client.ServerProxy(
        f'{base}/xmlrpc/2/common', allow_none=True, context=ctx,
    )
    models = xmlrpc.client.ServerProxy(
        f'{base}/xmlrpc/2/object', allow_none=True, context=ctx,
    )
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        raise RuntimeError('Autenticación fallida (ODOO_USER / ODOO_PASSWORD).')
    return uid, models


def execute(models, uid, model, method, args=None, **kwargs):
    kw = dict(kwargs)
    ctx = kw.pop('context', None)
    if ctx:
        kw['context'] = ctx
    return models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, model, method, args or [], kw,
    )


def m2o_id(field_val):
    if not field_val:
        return False
    if isinstance(field_val, (list, tuple)):
        return field_val[0]
    return int(field_val)


def parse_ps_code(code):
    code = str(code or '').strip()
    m = PS_CODE_RE.match(code)
    if not m:
        return code, None
    return m.group(1), m.group(2)


def is_xmlrpc_record_return_fault(exc):
    """Odoo importa bien pero XML-RPC no serializa recordsets devueltos."""
    text = str(exc)
    return any(marker in text for marker in XMLRPC_RECORD_RETURN_MARKERS)


def chunked(ids, size):
    ids = list(ids)
    for i in range(0, len(ids), size):
        yield ids[i:i + size]


def read_integration_api(models, uid, integration_id):
    rows = execute(
        models, uid, 'sale.integration.api.field', 'search_read',
        [[('sia_id', '=', integration_id), ('name', 'in', ['url', 'key'])]],
        fields=['name', 'value'],
    )
    cfg = {r['name']: (r['value'] or '').strip() for r in rows}
    url = cfg.get('url', '').rstrip('/')
    key = cfg.get('key', '')
    if not url or not key:
        raise RuntimeError('Faltan url/key de PrestaShop en sale.integration.api.field')
    return url, key


def fetch_ps_active_template_ids(url, key):
    """IDs de plantillas activas en PS (paginación limit=offset,cantidad)."""
    session = requests.Session()
    ids = set()
    offset = 0
    block = 500
    while True:
        params = {
            'ws_key': key,
            'output_format': 'JSON',
            'display': '[id]',
            'limit': f'{offset},{block}',
            'filter[active]': '1',
        }
        resp = session.get(f'{url}/api/products', params=params, auth=(key, ''), timeout=120)
        resp.raise_for_status()
        data = resp.json()
        batch = []
        if isinstance(data, list):
            root = data
        elif isinstance(data, dict):
            root = data.get('products', data)
        else:
            root = []
        if isinstance(root, dict):
            inner = root.get('product', [])
            batch = [inner] if isinstance(inner, dict) else (inner or [])
        elif isinstance(root, list):
            batch = root
        if not batch:
            break
        for item in batch:
            if not isinstance(item, dict):
                continue
            pid = item.get('id')
            if isinstance(pid, dict):
                pid = pid.get('value')
            if pid:
                ids.add(str(pid))
        offset += block
        time.sleep(0.03)
    return ids


def load_mapping_sets(models, uid, integration_id):
    """Carga sets para auditoría de huecos."""
    map_ids = execute(
        models, uid, 'integration.product.product.mapping', 'search',
        [[('integration_id', '=', integration_id)]],
    )
    ext_ids = execute(
        models, uid, 'integration.product.product.external', 'search',
        [[('integration_id', '=', integration_id)]],
    )
    ext_tpl_ids = execute(
        models, uid, 'integration.product.template.external', 'search',
        [[('integration_id', '=', integration_id)]],
    )
    tpl_map_ids = execute(
        models, uid, 'integration.product.template.mapping', 'search',
        [[('integration_id', '=', integration_id)]],
    )

    mapped_ext_var = set()
    mapped_product_ids = set()
    unlinked_mappings = []

    for batch in chunked(map_ids, READ_BATCH):
        rows = execute(
            models, uid, 'integration.product.product.mapping', 'read',
            [batch], fields=['external_product_id', 'product_id'],
        )
        for row in rows:
            eid = m2o_id(row.get('external_product_id'))
            pid = m2o_id(row.get('product_id'))
            if eid:
                mapped_ext_var.add(eid)
            if pid:
                mapped_product_ids.add(pid)
            elif eid:
                unlinked_mappings.append(eid)

    ext_codes = {}
    ext_tpl_by_code = {}
    ext_tpl_id_by_code = {}

    for batch in chunked(ext_ids, READ_BATCH):
        rows = execute(
            models, uid, 'integration.product.product.external', 'read',
            [batch], fields=['code', 'external_product_template_id'],
        )
        for row in rows:
            ext_codes[row['id']] = row.get('code') or ''

    for batch in chunked(ext_tpl_ids, READ_BATCH):
        rows = execute(
            models, uid, 'integration.product.template.external', 'read',
            [batch], fields=['code', 'name'],
        )
        for row in rows:
            code = str(row.get('code') or '')
            ext_tpl_by_code[code] = row
            ext_tpl_id_by_code[code] = row['id']

    tpl_mapped_codes = set()
    ext_id_to_code = {v: k for k, v in ext_tpl_id_by_code.items()}
    for batch in chunked(tpl_map_ids, READ_BATCH):
        rows = execute(
            models, uid, 'integration.product.template.mapping', 'read',
            [batch], fields=['external_template_id', 'template_id'],
        )
        for row in rows:
            ext_id = m2o_id(row.get('external_template_id'))
            code = ext_id_to_code.get(ext_id)
            if code and m2o_id(row.get('template_id')):
                tpl_mapped_codes.add(code)

    unmapped_ext_var = [eid for eid in ext_ids if eid not in mapped_ext_var]
    unmapped_by_tpl = Counter()
    for eid in unmapped_ext_var:
        tpl_code, _ = parse_ps_code(ext_codes.get(eid, ''))
        if tpl_code:
            unmapped_by_tpl[tpl_code] += 1

    mapped_by_tpl = Counter()
    for eid in mapped_ext_var:
        tpl_code, _ = parse_ps_code(ext_codes.get(eid, ''))
        if tpl_code:
            mapped_by_tpl[tpl_code] += 1

    targets = {}
    for tpl_code in set(mapped_by_tpl) | set(unmapped_by_tpl):
        m = mapped_by_tpl.get(tpl_code, 0)
        u = unmapped_by_tpl.get(tpl_code, 0)
        has_ext = tpl_code in ext_tpl_by_code
        has_odoo_tpl = tpl_code in tpl_mapped_codes
        if not has_ext or not has_odoo_tpl:
            reason = 'sin_plantilla_externa' if not has_ext else 'sin_producto_odoo'
            targets[tpl_code] = {
                'reason': reason,
                'mapped_variants': m,
                'unmapped_variants': u,
                'external_template_id': ext_tpl_id_by_code.get(tpl_code),
            }
        elif u > 0:
            targets[tpl_code] = {
                'reason': 'import_parcial' if m > 0 else 'sin_variantes_odoo',
                'mapped_variants': m,
                'unmapped_variants': u,
                'external_template_id': ext_tpl_id_by_code.get(tpl_code),
            }

    pp_active = execute(models, uid, 'product.product', 'search_count', [[('active', '=', True)]])
    pp_total = execute(
        models, uid, 'product.product', 'search_count', [[('active', 'in', [True, False])]],
    )

    return {
        'external_variants': len(ext_ids),
        'external_templates': len(ext_tpl_ids),
        'variant_mappings': len(map_ids),
        'variant_mappings_with_product': len(mapped_product_ids),
        'unmapped_external_variants': len(unmapped_ext_var),
        'unlinked_variant_mappings': len(unlinked_mappings),
        'odoo_variants_active': pp_active,
        'odoo_variants_total': pp_total,
        'targets': targets,
        'unmapped_by_tpl': unmapped_by_tpl,
        'ext_tpl_by_code': ext_tpl_by_code,
        'ext_tpl_id_by_code': ext_tpl_id_by_code,
    }


def audit(models, uid, integration_id, fetch_ps=False):
    log('Auditoría de huecos Odoo ↔ PrestaShop…')
    data = load_mapping_sets(models, uid, integration_id)
    targets = data['targets']

    by_reason = Counter(t['reason'] for t in targets.values())
    top_partial = sorted(
        targets.items(),
        key=lambda x: x[1]['unmapped_variants'],
        reverse=True,
    )[:15]

    ps_only_missing = []
    if fetch_ps:
        log('Consultando plantillas activas en PrestaShop (API)…')
        url, key = read_integration_api(models, uid, integration_id)
        ps_ids = fetch_ps_active_template_ids(url, key)
        odoo_ext_codes = set(data['ext_tpl_id_by_code'])
        ps_only_missing = sorted(ps_ids - odoo_ext_codes, key=int)
        log(f'Plantillas activas PS: {len(ps_ids)} | sin external en Odoo: {len(ps_only_missing)}')
        for ps_code in ps_only_missing:
            if ps_code not in targets:
                targets[ps_code] = {
                    'reason': 'sin_plantilla_externa',
                    'mapped_variants': 0,
                    'unmapped_variants': 0,
                    'external_template_id': None,
                }

    report = {
        'timestamp': datetime.now().isoformat(timespec='seconds'),
        'odoo_url': ODOO_URL,
        'odoo_db': ODOO_DB,
        'integration_id': integration_id,
        'summary': {
            'external_variants_cache': data['external_variants'],
            'external_templates_cache': data['external_templates'],
            'variant_mapping_rows': data['variant_mappings'],
            'variant_mappings_linked_odoo': data['variant_mappings_with_product'],
            'external_variants_sin_mapping': data['unmapped_external_variants'],
            'variant_mappings_sin_product_id': data['unlinked_variant_mappings'],
            'odoo_product_product_activos': data['odoo_variants_active'],
            'odoo_product_product_total': data['odoo_variants_total'],
            'gap_variantes_vs_cache': (
                data['external_variants'] - data['variant_mappings_with_product']
            ),
            'plantillas_a_revisar': len(targets),
            'por_motivo': dict(by_reason),
            'ps_activas_sin_external_odoo': len(ps_only_missing),
        },
        'top_plantillas_parciales': [
            {
                'ps_code': code,
                **info,
            }
            for code, info in top_partial
        ],
        'ps_templates_missing_external': ps_only_missing[:200],
        'all_targets': {
            code: info for code, info in sorted(
                targets.items(),
                key=lambda x: (-x[1]['unmapped_variants'], x[0]),
            )
        },
    }

    log('--- RESUMEN ---')
    for k, v in report['summary'].items():
        log(f'  {k}: {v}')
    if top_partial:
        log('Top plantillas con variantes sin mapping:')
        for code, info in top_partial[:5]:
            log(
                f'  PS {code}: {info["unmapped_variants"]} sin mapping, '
                f'{info["mapped_variants"]} mapeadas ({info["reason"]})'
            )
    return report


def save_report(report, prefix='arte_ps_gap'):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    path = REPORT_DIR / f'{prefix}_{stamp}.json'
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    log(f'Informe guardado: {path}')
    return path


def select_targets(report, args):
    targets = report['all_targets']
    if args.template_codes:
        wanted = {c.strip() for c in args.template_codes.split(',') if c.strip()}
        targets = {k: v for k, v in targets.items() if k in wanted}
    if args.partial_only:
        targets = {k: v for k, v in targets.items() if v['reason'] == 'import_parcial'}
    if args.missing_only:
        targets = {
            k: v for k, v in targets.items()
            if v['reason'] in ('sin_plantilla_externa', 'sin_producto_odoo', 'sin_variantes_odoo')
        }
    ordered = sorted(
        targets.items(),
        key=lambda x: (-x[1]['unmapped_variants'], x[0]),
    )
    if args.limit:
        ordered = ordered[:args.limit]
    return ordered


def refresh_externals(models, uid, integration_id, ps_codes, dry_run):
    if not ps_codes:
        log('Nada que refrescar en externals.')
        return 0
    log(f'Refresh externals: {len(ps_codes)} plantillas PS')
    done = 0
    for batch in chunked(ps_codes, EXTERNAL_BLOCK):
        if dry_run:
            log(f'[dry-run] import_external_product({len(batch)} ids): {batch[:5]}…')
            done += len(batch)
            continue
        try:
            msg = execute(
                models, uid, 'sale.integration', 'import_external_product',
                [[integration_id], batch],
            )
            done += len(batch)
            if msg and isinstance(msg, str) and 'FAIL' in msg:
                log(f'Batch con avisos: {msg[:300]}…', '!')
        except xmlrpc.client.Fault as exc:
            log(f'Error refresh batch ({batch[:3]}…): {exc}', '!')
        time.sleep(0.2)
    log(f'Refresh externals completado: {done} plantillas')
    return done


def import_templates(models, uid, integration_id, targets, dry_run, use_queue, delay):
    """Importa productos Odoo desde external templates."""
    if not targets:
        log('Nada que importar.')
        return {'ok': 0, 'fail': 0, 'queued': 0}

    ext_tpl_ids = []
    flow_codes = []
    for ps_code, info in targets:
        if info.get('external_template_id'):
            ext_tpl_ids.append(info['external_template_id'])
        else:
            flow_codes.append(ps_code)

    stats = {'ok': 0, 'fail': 0, 'queued': 0, 'flow_ok': 0, 'flow_fail': 0, 'ok_xmlrpc': 0}

    # Plantillas sin external: import_product_flow
    for ps_code in flow_codes:
        if dry_run:
            log(f'[dry-run] import_product_flow PS {ps_code}')
            stats['flow_ok'] += 1
            continue
        try:
            result = execute(
                models, uid, 'sale.integration', 'import_product_flow',
                [[integration_id], ps_code],
            )
            if result:
                stats['flow_ok'] += 1
                log(f'import_product_flow OK PS {ps_code} -> template id {result}')
            else:
                stats['flow_fail'] += 1
                log(f'import_product_flow sin resultado PS {ps_code}', '!')
        except xmlrpc.client.Fault as exc:
            stats['flow_fail'] += 1
            log(f'import_product_flow FAIL PS {ps_code}: {exc}', '!')
        if delay:
            time.sleep(delay)

    if not ext_tpl_ids:
        return stats

    if use_queue:
        if dry_run:
            log(f'[dry-run] run_import_products en cola para {len(ext_tpl_ids)} externals')
            stats['queued'] = len(ext_tpl_ids)
            return stats
        for batch in chunked(ext_tpl_ids, 50):
            try:
                execute(
                    models, uid, 'integration.product.template.external', 'run_import_products',
                    [batch],
                )
                stats['queued'] += len(batch)
                log(f'Encolados {len(batch)} imports (queue_job)')
            except xmlrpc.client.Fault as exc:
                stats['fail'] += len(batch)
                log(f'Error encolando batch: {exc}', '!')
        return stats

    # Import síncrono: sale.integration.import_product evita devolver record
    # en el modelo external (XML-RPC no serializa product.template).
    for ext_id in ext_tpl_ids:
        if dry_run:
            log(f'[dry-run] import_product integration={integration_id} external_id={ext_id}')
            stats['ok'] += 1
            continue
        try:
            execute(
                models, uid, 'sale.integration', 'import_product',
                [[integration_id], ext_id],
            )
            stats['ok'] += 1
            log(f'import_product OK external_id={ext_id}')
        except xmlrpc.client.Fault as exc:
            if is_xmlrpc_record_return_fault(exc):
                stats['ok_xmlrpc'] += 1
                log(
                    f'import_product OK external_id={ext_id} '
                    f'(importado; XML-RPC no devuelve recordset)',
                )
            else:
                stats['fail'] += 1
                log(f'import_product FAIL external_id={ext_id}: {str(exc)[:400]}', '!')
        if delay:
            time.sleep(delay)
    return stats


def main():
    parser = argparse.ArgumentParser(
        description='Alinear plantillas/variantes Odoo con PrestaShop (integración).',
    )
    parser.add_argument('--integration-id', type=int, default=INTEGRATION_ID)
    parser.add_argument('--audit-only', action='store_true', help='Solo informe, sin cambios.')
    parser.add_argument('--dry-run', action='store_true', help='Simula escrituras.')
    parser.add_argument(
        '--refresh-externals',
        action='store_true',
        help='Llama sale.integration.import_external_product para plantillas objetivo.',
    )
    parser.add_argument(
        '--import-products',
        action='store_true',
        help='Importa/actualiza productos en Odoo (import_one_product o cola).',
    )
    parser.add_argument(
        '--use-queue',
        action='store_true',
        help='Encolar imports vía queue_job (recomendado para muchas plantillas).',
    )
    parser.add_argument(
        '--fetch-ps',
        action='store_true',
        help='En auditoría, comparar con plantillas activas vivas en PS.',
    )
    parser.add_argument(
        '--template-codes',
        default='',
        help='Códigos PS separados por coma (ej. 5369,7196).',
    )
    parser.add_argument('--partial-only', action='store_true', help='Solo import_parcial.')
    parser.add_argument(
        '--missing-only',
        action='store_true',
        help='Solo plantillas sin external / sin odoo / sin variantes.',
    )
    parser.add_argument('--limit', type=int, default=0, help='Máx. plantillas a procesar.')
    parser.add_argument(
        '--delay', type=float, default=0.3,
        help='Pausa entre imports síncronos (segundos).',
    )
    parser.add_argument('--no-report', action='store_true', help='No guardar JSON en arte_reports.')
    args = parser.parse_args()

    if not ODOO_PASSWORD:
        log('Define ODOO_PASSWORD.', '!')
        sys.exit(1)

    if not args.audit_only and not args.refresh_externals and not args.import_products:
        args.audit_only = True
    if args.import_products and not args.fetch_ps:
        args.fetch_ps = True

    print('=' * 60, flush=True)
    log(f'PS align | DB={ODOO_DB} | {ODOO_URL}')
    if args.dry_run:
        log('Modo DRY-RUN', 'dry')
    print('=' * 60, flush=True)

    uid, models = connect_odoo()
    log(f'XML-RPC OK uid={uid}')

    integration = execute(
        models, uid, 'sale.integration', 'read',
        [[args.integration_id]], fields=['name', 'type_api'],
    )
    if not integration:
        log(f'No existe sale.integration id={args.integration_id}', '!')
        sys.exit(1)
    log(f'Integración: {integration[0]["name"]} ({integration[0]["type_api"]})')

    report = audit(models, uid, args.integration_id, fetch_ps=args.fetch_ps)
    if not args.no_report:
        save_report(report)

    if args.audit_only and not args.refresh_externals and not args.import_products:
        log(f'Fin auditoría. Tiempo: {fmt_duration(time.monotonic() - _T0)}')
        return

    if args.refresh_externals or args.import_products:
        targets = select_targets(report, args)
        ps_codes = [code for code, _ in targets]
        log(f'Plantillas objetivo: {len(ps_codes)}')

    if args.refresh_externals:
        refresh_externals(models, uid, args.integration_id, ps_codes, args.dry_run)
        if not args.import_products:
            log('Re-ejecuta sin --audit-only para ver métricas actualizadas post-refresh.')

    if args.import_products:
        if args.refresh_externals and not args.dry_run:
            log('Re-auditoría rápida tras refresh…')
            report = audit(models, uid, args.integration_id, fetch_ps=False)
            targets = select_targets(report, args)
        stats = import_templates(
            models, uid, args.integration_id, targets, args.dry_run,
            args.use_queue, args.delay,
        )
        log(f'Import stats: {stats}')

    log(f'TERMINADO. Tiempo total: {fmt_duration(time.monotonic() - _T0)}')


if __name__ == '__main__':
    main()
