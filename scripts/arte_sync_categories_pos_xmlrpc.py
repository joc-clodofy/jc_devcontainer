#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sincroniza categorías públicas (PrestaShop / integración) hacia:
  - product.category (categ_id en product.template)
  - pos.category (pos_categ_ids), con la misma jerarquía
  - available_in_pos, sale_ok y purchase_ok en productos

Referencia de estilo/conexión: scripts/airbici_link_products_xmlrpc.py

Entorno arte (arte.conf, puerto 8069):
  export ODOO_URL=http://localhost:8069
  export ODOO_DB=arte
  export ODOO_USER=admin
  export ODOO_PASSWORD=admin

Uso:
  python3 scripts/arte_sync_categories_pos_xmlrpc.py --dry-run
  python3 scripts/arte_sync_categories_pos_xmlrpc.py
  python3 scripts/arte_sync_categories_pos_xmlrpc.py --product-scope public
  python3 scripts/arte_sync_categories_pos_xmlrpc.py --skip-categories --legacy-unmapped --skip-prestashop-sync
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
import unicodedata
import xmlrpc.client
from collections import Counter, defaultdict
from datetime import datetime

import requests

# ---------------------------------------------------------------------------
# Odoo — arte (local devcontainer)
# ---------------------------------------------------------------------------
ODOO_URL = os.environ.get('ODOO_URL', 'http://localhost:8069')
ODOO_DB = os.environ.get('ODOO_DB', 'arte')
ODOO_USER = os.environ.get('ODOO_USER', 'admin')
ODOO_PASSWORD = os.environ.get('ODOO_PASSWORD', 'admin')
ODOO_SSL_VERIFY = os.environ.get('ODOO_SSL_VERIFY', '1').lower() not in ('0', 'false', 'no')

INTEGRATION_ID = int(os.environ.get('INTEGRATION_ID', '1'))
LANG = os.environ.get('ODOO_LANG', 'es_ES')

if http.client._MAXHEADERS < 1000:
    http.client._MAXHEADERS = 1000

_T0 = time.monotonic()


def log(msg, level='*'):
    elapsed = time.monotonic() - _T0
    ts = datetime.now().strftime('%H:%M:%S')
    print(f'[{ts} +{elapsed:6.0f}s] [{level}] {msg}', flush=True)
    sys.stdout.flush()
    sys.stderr.flush()


class ProgressTracker:
    """Log periódico de progreso + heartbeat para detectar bloqueos."""

    def __init__(self, total, every=50, heartbeat=15, label=''):
        self.total = max(total, 1)
        self.every = every
        self.heartbeat = heartbeat
        self.label = label
        self.start = time.monotonic()
        self.last_summary = self.start
        self.last_heartbeat = self.start
        self.count = 0

    def _stats_line(self, stats):
        if not stats:
            return ''
        parts = [f'ok={stats.get("actualizados", 0)}']
        for key in ('sin_match', 'sin_categoria_mapeada', 'ps_no_existe', 'error_ps', 'ps_api_calls'):
            if stats.get(key):
                parts.append(f'{key}={stats[key]}')
        return ' | ' + ', '.join(parts)

    def tick(self, stats=None, extra=''):
        self.count += 1
        now = time.monotonic()
        if self.count == 1:
            log(f'{self.label}Primer ítem procesado (1/{self.total}){extra}', '…')
        elapsed = now - self.start
        rate = self.count / elapsed if elapsed > 0 else 0.0
        if self.count % self.every == 0 or self.count == self.total:
            eta = (self.total - self.count) / rate if rate > 0 else 0
            log(
                f'{self.label}Progreso {self.count}/{self.total} '
                f'({100 * self.count / self.total:.1f}%) '
                f'{rate:.1f}/s · ETA ~{fmt_duration(eta)}'
                f'{self._stats_line(stats)}{extra}',
            )
            self.last_summary = now
            self.last_heartbeat = now
        elif now - self.last_heartbeat >= self.heartbeat:
            log(
                f'{self.label}Heartbeat {self.count}/{self.total} '
                f'({rate:.1f}/s, {now - self.last_summary:.0f}s sin resumen){extra}',
                '…',
            )
            self.last_heartbeat = now


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
        urllib.request.urlopen(probe, timeout=10, context=ctx)
    except urllib.error.URLError as exc:
        reason = getattr(exc, 'reason', exc)
        raise RuntimeError(
            f'No hay servidor en {base} ({reason}). '
            f'Arranca Odoo: python3 odoo/odoo-bin -c arte.conf'
        ) from exc


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


def odoo_ctx(lang=LANG):
    return {'lang': lang} if lang else {}


def m2m_ids(field_val):
    """IDs de un Many2many leído por XML-RPC (int o [id, display_name])."""
    if not field_val:
        return []
    out = []
    for item in field_val:
        if isinstance(item, (list, tuple)):
            out.append(item[0])
        else:
            out.append(int(item))
    return out


def m2o_id(field_val):
    if not field_val:
        return False
    if isinstance(field_val, (list, tuple)):
        return field_val[0]
    return int(field_val)


def pick_name(name_val, lang=LANG):
    if not name_val:
        return 'Sin nombre'
    if isinstance(name_val, dict):
        for key in (lang, 'es_ES', 'en_US'):
            if name_val.get(key):
                return name_val[key]
        for val in name_val.values():
            if val:
                return val
        return 'Sin nombre'
    if isinstance(name_val, str) and name_val.startswith('{'):
        try:
            return pick_name(json.loads(name_val), lang=lang)
        except json.JSONDecodeError:
            pass
    return str(name_val)


def execute(models, uid, model, method, args=None, **kwargs):
    """Wrapper de execute_kw; ``args`` es la lista de argumentos posicionales de Odoo."""
    kw = dict(kwargs)
    ctx = kw.pop('context', None)
    if ctx:
        kw['context'] = ctx
    return models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, model, method, args or [], kw,
    )


def find_category(models, uid, model, name, parent_id, lang=LANG):
    domain = [('name', '=', name)]
    if parent_id:
        domain.append(('parent_id', '=', parent_id))
    else:
        domain.append(('parent_id', '=', False))
    ids = execute(
        models, uid, model, 'search', [domain],
        limit=2, context=odoo_ctx(lang),
    )
    return ids


def sync_one_category(models, uid, model, name, parent_id, dry_run, stats, lang=LANG):
    """Busca por nombre+padre; crea o corrige parent_id / nombre."""
    existing = find_category(models, uid, model, name, parent_id)
    if len(existing) > 1:
        log(
            f'Duplicados en {model}: "{name}" parent={parent_id} → ids={existing}; uso el primero',
            '!',
        )
        stats['dup'] += 1

    if existing:
        cat_id = existing[0]
        row = execute(
            models, uid, model, 'read', [[cat_id]],
            fields=['name', 'parent_id'], context=odoo_ctx(lang),
        )[0]
        parent_now = m2o_id(row.get('parent_id'))
        name_now = pick_name(row.get('name'), lang)
        write_vals = {}
        if parent_now != (parent_id or False):
            write_vals['parent_id'] = parent_id or False
        if name_now != name:
            write_vals['name'] = name
        if write_vals:
            stats['updated'] += 1
            if not dry_run:
                execute(
                    models, uid, model, 'write', [[cat_id], write_vals],
                    context=odoo_ctx(lang),
                )
        else:
            stats['unchanged'] += 1
        return cat_id

    stats['created'] += 1
    if dry_run:
        return -1
    return execute(
        models, uid, model, 'create',
        [{'name': name, 'parent_id': parent_id or False}],
        context=odoo_ctx(lang),
    )


def load_public_categories(models, uid, lang=LANG):
    ids = execute(
        models, uid, 'product.public.category', 'search',
        [[]], order='parent_path',
    )
    if not ids:
        return []
    return execute(
        models, uid, 'product.public.category', 'read', [ids],
        fields=['id', 'name', 'parent_id', 'parent_path'],
        context=odoo_ctx(lang),
    )


def sync_category_trees(models, uid, public_cats, dry_run, lang=LANG):
    """
    Recorre categorías públicas en orden parent_path y replica árbol en
    product.category y pos.category.
    """
    pub_to_pc = {}
    pub_to_pos = {}
    stats_pc = {'created': 0, 'updated': 0, 'unchanged': 0, 'dup': 0}
    stats_pos = {'created': 0, 'updated': 0, 'unchanged': 0, 'dup': 0}

    log(f'Sincronizando {len(public_cats)} categorías públicas → product.category + pos.category')

    for pub in public_cats:
        pub_id = pub['id']
        name = pick_name(pub.get('name'), lang)
        parent_pub_id = m2o_id(pub.get('parent_id'))
        parent_pc = pub_to_pc.get(parent_pub_id) if parent_pub_id else False
        parent_pos = pub_to_pos.get(parent_pub_id) if parent_pub_id else False

        if parent_pub_id and not parent_pc:
            log(f'Padre público {parent_pub_id} sin mapear antes de {pub_id} ({name})', '!')

        pc_id = sync_one_category(
            models, uid, 'product.category', name, parent_pc, dry_run, stats_pc, lang,
        )
        pos_id = sync_one_category(
            models, uid, 'pos.category', name, parent_pos, dry_run, stats_pos, lang,
        )

        if pc_id and pc_id > 0:
            pub_to_pc[pub_id] = pc_id
        if pos_id and pos_id > 0:
            pub_to_pos[pub_id] = pos_id

    log(
        f'product.category: +{stats_pc["created"]} creadas, '
        f'~{stats_pc["updated"]} actualizadas, {stats_pc["unchanged"]} sin cambios'
    )
    log(
        f'pos.category: +{stats_pos["created"]} creadas, '
        f'~{stats_pos["updated"]} actualizadas, {stats_pos["unchanged"]} sin cambios'
    )
    return pub_to_pc, pub_to_pos


def resolve_product_domain(models, uid, scope, integration_id):
    if scope == 'prestashop':
        mappings = execute(
            models, uid, 'integration.product.template.mapping', 'search_read',
            [[('integration_id', '=', integration_id), ('template_id', '!=', False)]],
            fields=['template_id'],
        )
        tpl_ids = list({m2o_id(m['template_id']) for m in mappings})
        return [('id', 'in', tpl_ids)] if tpl_ids else [('id', '=', 0)]

    if scope == 'public':
        ids = execute(
            models, uid, 'product.template', 'search',
            [[('public_categ_ids', '!=', False)]],
        )
        return [('id', 'in', ids)] if ids else [('id', '=', 0)]

    if scope == 'all':
        return [('active', 'in', (True, False))]

    raise ValueError(f'scope desconocido: {scope}')


def load_products(models, uid, domain, batch_size, lang=LANG):
    ids = execute(models, uid, 'product.template', 'search', [domain], order='id')
    log(f'Productos a procesar: {len(ids)}')
    fields = [
        'id', 'name', 'default_code', 'categ_id',
        'default_public_categ_id', 'public_categ_ids',
        'available_in_pos', 'sale_ok', 'purchase_ok', 'pos_categ_ids',
    ]
    for start in range(0, len(ids), batch_size):
        chunk = ids[start:start + batch_size]
        rows = execute(
            models, uid, 'product.template', 'read', [chunk],
            fields=fields, context=odoo_ctx(lang),
        )
        for row in rows:
            yield row


def map_public_ids_to_targets(public_ids, pub_to_pc, pub_to_pos):
    pc_ids = []
    pos_ids = []
    for pid in public_ids:
        pc = pub_to_pc.get(pid)
        pos = pub_to_pos.get(pid)
        if pc and pc > 0 and pc not in pc_ids:
            pc_ids.append(pc)
        if pos and pos > 0 and pos not in pos_ids:
            pos_ids.append(pos)
    return pc_ids, pos_ids


def sync_products(
    models, uid, domain, pub_to_pc, pub_to_pos, dry_run,
    set_sale_purchase, batch_size, lang=LANG,
):
    stats = {
        'processed': 0,
        'updated': 0,
        'skipped_dry': 0,
        'no_public': 0,
        'errors': 0,
    }

    for product in load_products(models, uid, domain, batch_size, lang):
        stats['processed'] += 1
        pub_ids = m2m_ids(product.get('public_categ_ids'))
        if not pub_ids:
            stats['no_public'] += 1
            if set_sale_purchase:
                write_vals = {
                    'available_in_pos': True,
                    'sale_ok': True,
                    'purchase_ok': True,
                }
            else:
                write_vals = {'available_in_pos': True}
            if dry_run:
                stats['skipped_dry'] += 1
                continue
            try:
                execute(
                    models, uid, 'product.template', 'write',
                    [[product['id']], write_vals],
                )
                stats['updated'] += 1
            except xmlrpc.client.Fault as exc:
                stats['errors'] += 1
                log(f'Error tpl {product["id"]}: {exc.faultString}', '!')
            continue

        pc_ids, pos_ids = map_public_ids_to_targets(pub_ids, pub_to_pc, pub_to_pos)
        default_pub = m2o_id(product.get('default_public_categ_id')) or pub_ids[0]
        main_pc = pub_to_pc.get(default_pub) or (pc_ids[0] if pc_ids else False)

        write_vals = {
            'available_in_pos': True,
            'pos_categ_ids': [(6, 0, pos_ids)],
        }
        if main_pc:
            write_vals['categ_id'] = main_pc
        if set_sale_purchase:
            write_vals['sale_ok'] = True
            write_vals['purchase_ok'] = True

        current_pc = m2o_id(product.get('categ_id'))
        current_pos = sorted(m2m_ids(product.get('pos_categ_ids')))
        needs = (
            product.get('available_in_pos') is not True
            or sorted(pos_ids) != current_pos
            or (main_pc and current_pc != main_pc)
            or (set_sale_purchase and (
                not product.get('sale_ok') or not product.get('purchase_ok')
            ))
        )
        if not needs:
            continue

        if dry_run:
            stats['skipped_dry'] += 1
            if stats['skipped_dry'] <= 5:
                log(
                    f'dry-run tpl {product["id"]} ({product.get("default_code")}): '
                    f'categ_id→{main_pc}, pos→{pos_ids}',
                    'dry',
                )
            continue

        try:
            execute(
                models, uid, 'product.template', 'write',
                [[product['id']], write_vals],
            )
            stats['updated'] += 1
        except xmlrpc.client.Fault as exc:
            stats['errors'] += 1
            log(f'Error tpl {product["id"]}: {exc.faultString}', '!')

        if stats['processed'] % 500 == 0:
            log(f'… {stats["processed"]} productos procesados, {stats["updated"]} escritos')

    log(
        f'Productos: {stats["processed"]} procesados, {stats["updated"]} actualizados, '
        f'{stats["no_public"]} sin categoría pública (solo TPV/venta/compra si aplica), '
        f'{stats["errors"]} errores'
    )
    if dry_run:
        log(f'dry-run: {stats["skipped_dry"]} habrían sido actualizados', 'dry')
    return stats


# ---------------------------------------------------------------------------
# Legacy: productos sin mapping → PrestaShop (ID, ref, barcode, nombre + API)
# ---------------------------------------------------------------------------

PS_ID_RE = re.compile(r'^\d+$')
PS_FETCH_FIELDS = 'id,id_category_default,associations'
PS_TIMEOUT = 45
PS_MAX_RETRIES = 3


def norm_ref(value):
    return (value or '').strip().upper()


def norm_name(value, lang=LANG):
    text = pick_name(value, lang)
    text = unicodedata.normalize('NFKD', text)
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().strip()
    text = re.sub(r'[^\w\s\-]', ' ', text, flags=re.UNICODE)
    return ' '.join(text.split())


def token_set(name):
    return {t for t in norm_name(name).split() if len(t) > 2}


def token_similarity(a, b):
    ta, tb = token_set(a), token_set(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


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


def build_ps_category_to_public_map(models, uid, integration_id):
    ext_rows = execute(
        models, uid, 'integration.product.public.category.external', 'search_read',
        [[('integration_id', '=', integration_id)]],
        fields=['id', 'code'],
    )
    ext_by_id = {r['id']: r for r in ext_rows}
    mappings = execute(
        models, uid, 'integration.product.public.category.mapping', 'search_read',
        [[('integration_id', '=', integration_id), ('public_category_id', '!=', False)]],
        fields=['external_public_category_id', 'public_category_id'],
    )
    ps_to_public = {}
    for row in mappings:
        ext = ext_by_id.get(m2o_id(row['external_public_category_id']))
        pub_id = m2o_id(row['public_category_id'])
        if ext and pub_id:
            ps_to_public[str(ext['code'])] = pub_id
    return ps_to_public


def build_external_indexes(externals, lang=LANG):
    by_code = {}
    by_ref = defaultdict(list)
    by_barcode = defaultdict(list)
    by_name = defaultdict(list)
    for ext in externals:
        code = str(ext['code']).strip()
        by_code[code] = ext
        ref = norm_ref(ext.get('external_reference'))
        if ref:
            by_ref[ref].append(ext)
        bc = (ext.get('external_barcode') or '').strip()
        if bc:
            by_barcode[bc].append(ext)
        name = norm_name(ext.get('name'), lang)
        if name:
            by_name[name].append(ext)
    return {
        'by_code': by_code,
        'by_ref': by_ref,
        'by_barcode': by_barcode,
        'by_name': by_name,
    }


def load_externals(models, uid, integration_id):
    return execute(
        models, uid, 'integration.product.template.external', 'search_read',
        [[('integration_id', '=', integration_id)]],
        fields=['id', 'code', 'name', 'external_reference', 'external_barcode'],
    )


def load_unmapped_product_ids(models, uid, integration_id):
    mapped = execute(
        models, uid, 'integration.product.template.mapping', 'search_read',
        [[('integration_id', '=', integration_id), ('template_id', '!=', False)]],
        fields=['template_id'],
    )
    mapped_ids = {m2o_id(r['template_id']) for r in mapped}
    all_ids = execute(
        models, uid, 'product.template', 'search',
        [[('active', '=', True)]],
    )
    return [pid for pid in all_ids if pid not in mapped_ids]


def load_products_by_ids(models, uid, ids, lang=LANG, batch_size=200, label=''):
    if not ids:
        return []
    fields = [
        'id', 'name', 'default_code', 'barcode', 'categ_id',
        'default_public_categ_id', 'public_categ_ids', 'pos_categ_ids',
        'available_in_pos', 'sale_ok', 'purchase_ok',
    ]
    rows = []
    total = len(ids)
    n_batches = (total + batch_size - 1) // batch_size
    log(f'{label}Cargando {total} productos en {n_batches} lote(s) de {batch_size}…')
    for batch_num, start in enumerate(range(0, total, batch_size), 1):
        chunk = ids[start:start + batch_size]
        t0 = time.monotonic()
        rows.extend(execute(
            models, uid, 'product.template', 'read', [chunk],
            fields=fields, context=odoo_ctx(lang),
        ))
        log(
            f'{label}  lote {batch_num}/{n_batches}: {len(chunk)} leídos '
            f'({len(rows)}/{total} acum.) en {fmt_duration(time.monotonic() - t0)}',
            '…',
        )
    return rows


def score_external_candidate(odoo, ext, odoo_name_counts, ext_name_count, lang=LANG):
    score = 55
    reasons = ['nombre_exacto']
    name = norm_name(odoo.get('name'), lang)
    if odoo_name_counts.get(name, 0) == 1:
        score += 12
        reasons.append('nombre_unico_odoo')
    if ext_name_count == 1:
        score += 10
        reasons.append('nombre_unico_ps')
    ref = norm_ref(odoo.get('default_code'))
    if ref and ref == norm_ref(ext.get('external_reference')):
        score += 18
        reasons.append('referencia_coincide')
    bc = (odoo.get('barcode') or '').strip()
    if bc and bc == (ext.get('external_barcode') or '').strip():
        score += 20
        reasons.append('barcode_coincide')
    return score, '+'.join(reasons)


def match_odoo_to_prestashop(odoo, indexes, odoo_name_counts, min_score, lang=LANG):
    code = (odoo.get('default_code') or '').strip()
    barcode = (odoo.get('barcode') or '').strip()
    name = norm_name(odoo.get('name'), lang)
    numeric_ps_id = code if (code and PS_ID_RE.match(code)) else None

    if numeric_ps_id and numeric_ps_id in indexes['by_code']:
        return numeric_ps_id, 100, 'ps_id_local', f'default_code={numeric_ps_id}'

    if code:
        ref = norm_ref(code)
        cands = indexes['by_ref'].get(ref, [])
        if len(cands) == 1:
            return str(cands[0]['code']), 95, 'ps_ref', f'ref={ref}'
        if len(cands) > 1:
            best = max(
                cands,
                key=lambda e: score_external_candidate(
                    odoo, e, odoo_name_counts, len(cands), lang,
                )[0],
            )
            sc, det = score_external_candidate(odoo, best, odoo_name_counts, len(cands), lang)
            if sc >= min_score:
                return str(best['code']), sc, 'ps_ref_ambiguo', det

    if barcode:
        cands = indexes['by_barcode'].get(barcode, [])
        if len(cands) == 1:
            return str(cands[0]['code']), 88, 'barcode', f'barcode={barcode}'
        if len(cands) > 1:
            best = max(
                cands,
                key=lambda e: score_external_candidate(
                    odoo, e, odoo_name_counts, len(cands), lang,
                )[0],
            )
            sc, det = score_external_candidate(odoo, best, odoo_name_counts, len(cands), lang)
            if sc >= min_score:
                return str(best['code']), sc, 'barcode_ambiguo', det

    cands = indexes['by_name'].get(name, [])
    if cands:
        if len(cands) == 1 and odoo_name_counts.get(name, 0) == 1:
            return str(cands[0]['code']), 85, 'nombre_1a1', name[:60]
        ranked = []
        ext_name_count = len(cands)
        for ext in cands:
            sc, det = score_external_candidate(
                odoo, ext, odoo_name_counts, ext_name_count, lang,
            )
            if ext_name_count > 1:
                sc -= 5
            ranked.append((sc, ext, det))
        ranked.sort(key=lambda x: x[0], reverse=True)
        best_sc, best_ext, best_det = ranked[0]
        if best_sc >= min_score:
            method = 'nombre' if len(cands) == 1 else 'nombre_ambiguo'
            return str(best_ext['code']), best_sc, method, best_det

    return None, 0, 'sin_match', ''


def _prestashop_get(session, url, key, path, params=None, allow_404=False):
    api_url = f'{url}/api/{path.lstrip("/")}'
    req_params = {'ws_key': key, 'output_format': 'JSON'}
    if params:
        req_params.update(params)
    last_err = None
    for attempt in range(PS_MAX_RETRIES + 1):
        try:
            resp = session.get(api_url, params=req_params, auth=(key, ''), timeout=PS_TIMEOUT)
            if resp.status_code == 404 and allow_404:
                return None
            if resp.status_code in (429, 502, 503, 504) and attempt < PS_MAX_RETRIES:
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last_err = exc
            if attempt < PS_MAX_RETRIES:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f'PrestaShop GET {path}: {last_err}')


def _parse_ps_category_ids(product_payload):
    product = product_payload.get('product', product_payload)
    if isinstance(product, list):
        product = product[0] if product else {}
    ids = []
    default = product.get('id_category_default')
    if isinstance(default, dict):
        default = default.get('value') or default.get('id')
    if default and str(default) not in ('0', 'false', 'False', ''):
        ids.append(str(default))
    associations = product.get('associations') or {}
    categories = associations.get('categories') or {}
    items = categories.get('category') or []
    if isinstance(items, dict):
        items = [items]
    for item in items:
        cid = item.get('id') if isinstance(item, dict) else item
        if isinstance(cid, dict):
            cid = cid.get('value') or cid.get('id')
        if cid and str(cid) not in ('0', 'false', ''):
            ids.append(str(cid))
    seen = set()
    out = []
    for cid in ids:
        if cid not in seen and cid != '1':
            seen.add(cid)
            out.append(cid)
    return out


def fetch_ps_categories(session, url, key, ps_product_id, cache, invalid_ids, delay):
    ps_product_id = str(ps_product_id)
    if ps_product_id in cache:
        return cache[ps_product_id]
    if ps_product_id in invalid_ids:
        return []
    if delay:
        time.sleep(delay)
    data = _prestashop_get(
        session, url, key, f'products/{ps_product_id}',
        {'display': '[id,id_category_default,associations]'},
        allow_404=True,
    )
    if data is None:
        invalid_ids.add(ps_product_id)
        cache[ps_product_id] = []
        return []
    cat_ids = _parse_ps_category_ids(data)
    cache[ps_product_id] = cat_ids
    return cat_ids


def search_ps_by_name(session, url, key, name, cache_search, delay):
    key_cache = norm_name(name)
    if key_cache in cache_search:
        return cache_search[key_cache]
    if delay:
        time.sleep(delay)
    try:
        data = _prestashop_get(
            session, url, key, 'products',
            {
                'display': '[id,name,reference,id_category_default,associations]',
                'filter[name]': f'[{name[:128]}]',
                'limit': '15',
            },
        )
        products = data.get('products') or []
        if isinstance(products, dict):
            products = [products]
        cache_search[key_cache] = products
        return products
    except RuntimeError:
        cache_search[key_cache] = []
        return []


def match_via_ps_api(odoo, session, url, key, cache_search, min_score, lang=LANG):
    name_raw = pick_name(odoo.get('name'), lang)
    products = search_ps_by_name(session, url, key, name_raw, cache_search, 0)
    if not products:
        return None, 0, 'ps_api_sin_resultado', ''

    ranked = []
    for prod in products:
        sc = 50
        reasons = ['ps_api_nombre']
        ps_name = prod.get('name') or ''
        if isinstance(ps_name, dict):
            ps_name = ps_name.get('language') or next(iter(ps_name.values()), '')
        sim = token_similarity(name_raw, ps_name)
        sc += int(sim * 30)
        reasons.append(f'tokens={sim:.2f}')
        ref = norm_ref(odoo.get('default_code'))
        ps_ref = norm_ref(prod.get('reference'))
        if ref and ps_ref and ref == ps_ref:
            sc += 25
            reasons.append('ref_api')
        if norm_name(name_raw, lang) == norm_name(ps_name, lang):
            sc += 10
            reasons.append('nombre_norm_igual')
        ranked.append((sc, str(prod['id']), '+'.join(reasons), prod))

    ranked.sort(key=lambda x: x[0], reverse=True)
    best_sc, ps_id, detail, _prod = ranked[0]
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 8:
        detail += '|margen_bajo_vs_2o'
        best_sc -= 5
    if best_sc >= min_score:
        return ps_id, best_sc, 'ps_api_nombre', detail
    return None, 0, 'ps_api_bajo_score', detail


def apply_product_categories(
    models, uid, product, public_ids, pub_to_pc, pub_to_pos, dry_run,
    set_sale_purchase, lang=LANG,
):
    if not public_ids:
        return False
    default_pub = public_ids[0]
    pc_ids, pos_ids = map_public_ids_to_targets(public_ids, pub_to_pc, pub_to_pos)
    main_pc = pub_to_pc.get(default_pub) or (pc_ids[0] if pc_ids else False)
    write_vals = {
        'public_categ_ids': [(6, 0, public_ids)],
        'default_public_categ_id': default_pub,
        'available_in_pos': True,
        'pos_categ_ids': [(6, 0, pos_ids)],
    }
    if main_pc:
        write_vals['categ_id'] = main_pc
    if set_sale_purchase:
        write_vals['sale_ok'] = True
        write_vals['purchase_ok'] = True
    if dry_run:
        return True
    execute(
        models, uid, 'product.template', 'write',
        [[product['id']], write_vals],
        context=odoo_ctx(lang),
    )
    return True


def build_external_to_mapped_public(models, uid, integration_id):
    """Externo PS → public_categ_ids del producto Odoo ya mapeado."""
    mappings = execute(
        models, uid, 'integration.product.template.mapping', 'search_read',
        [[('integration_id', '=', integration_id), ('template_id', '!=', False)]],
        fields=['external_template_id', 'template_id'],
    )
    ext_to_tpl = {m2o_id(m['external_template_id']): m2o_id(m['template_id']) for m in mappings}
    tpl_ids = list(set(ext_to_tpl.values()))
    if not tpl_ids:
        return {}
    tpl_rows = execute(
        models, uid, 'product.template', 'read', [tpl_ids],
        fields=['id', 'public_categ_ids', 'default_public_categ_id'],
    )
    tpl_pub = {}
    for row in tpl_rows:
        pub_ids = m2m_ids(row.get('public_categ_ids'))
        if pub_ids:
            tpl_pub[row['id']] = pub_ids
    ext_to_public = {}
    for ext_id, tpl_id in ext_to_tpl.items():
        if tpl_id in tpl_pub:
            ext_to_public[ext_id] = tpl_pub[tpl_id]
    return ext_to_public


def resolve_public_categories(
    ps_id, ext_row, ps_to_public, ext_to_mapped_public,
    session, url, key, ps_cat_cache, invalid_ps_ids, ps_delay,
    from_api_search=False,
):
    """
    Categorías públicas Odoo para un producto PS.
    1) Copiar del producto Odoo ya mapeado al mismo externo (sin API).
    2) API PrestaShop solo si hay externo local confirmado o match vía búsqueda API.
    """
    if ext_row:
        ext_id = ext_row['id']
        copied = ext_to_mapped_public.get(ext_id)
        if copied:
            return copied, 'copiado_mapeado'

    if not ext_row and not from_api_search:
        return [], 'sin_externo_local'

    ps_cat_codes = fetch_ps_categories(
        session, url, key, ps_id, ps_cat_cache, invalid_ps_ids, ps_delay,
    )
    public_ids = [ps_to_public[c] for c in ps_cat_codes if c in ps_to_public]
    public_ids = list(dict.fromkeys(public_ids))
    if public_ids:
        return public_ids, 'prestashop_api'
    if ps_id in invalid_ps_ids:
        return [], 'ps_no_existe'
    return [], 'sin_categoria_ps'


def sync_legacy_unmapped(
    models, uid, integration_id, pub_to_pc, pub_to_pos, dry_run,
    set_sale_purchase, min_score, ps_delay, use_ps_api, report_path, lang=LANG,
    batch_size=500, progress_every=50, heartbeat=15,
):
    log('=== Fase legacy: productos sin mapping PrestaShop ===')
    url, key = read_integration_api(models, uid, integration_id)
    log(f'PrestaShop API: {url}')

    ps_to_public = build_ps_category_to_public_map(models, uid, integration_id)
    log(f'Mapeo categorías PS→público Odoo: {len(ps_to_public)} entradas')

    log('Cargando externos PS desde Odoo…')
    externals = load_externals(models, uid, integration_id)
    log(f'Indexando {len(externals)} externos (código, ref, barcode, nombre)…')
    indexes = build_external_indexes(externals, lang)
    ext_by_code = indexes['by_code']
    log('Construyendo mapa externo → categorías del producto ya mapeado…')
    ext_to_mapped_public = build_external_to_mapped_public(models, uid, integration_id)
    log(f'Externos PS: {len(externals)} | categorías copiables: {len(ext_to_mapped_public)}')

    log('Buscando IDs Odoo sin mapping de integración…')
    unmapped_ids = load_unmapped_product_ids(models, uid, integration_id)
    total = len(unmapped_ids)
    log(f'Productos sin mapping: {total}')
    if use_ps_api:
        log('Búsqueda API PS por nombre: ACTIVADA (puede ir lento)', '!')
    else:
        log('Búsqueda API PS por nombre: desactivada (--no-ps-api-search)')

    session = requests.Session()
    ps_cat_cache = {}
    ps_search_cache = {}
    invalid_ps_ids = set()
    report = []
    stats = Counter()
    progress = ProgressTracker(total, every=progress_every, heartbeat=heartbeat, label='[legacy] ')

    n_batches = (total + batch_size - 1) // batch_size if total else 0
    global_idx = 0

    for batch_num, start in enumerate(range(0, total, batch_size), 1):
        chunk_ids = unmapped_ids[start:start + batch_size]
        log(f'[legacy] === Lote {batch_num}/{n_batches} ({len(chunk_ids)} productos) ===')
        products = load_products_by_ids(
            models, uid, chunk_ids, lang,
            batch_size=min(200, batch_size), label='[legacy] ',
        )
        log(f'[legacy] Indexando nombres del lote {batch_num}…')
        odoo_name_counts = Counter(norm_name(p.get('name'), lang) for p in products)
        log(f'[legacy] Procesando lote {batch_num}/{n_batches}…')

        for product in products:
            global_idx += 1
            ps_id, score, method, detail = match_odoo_to_prestashop(
                product, indexes, odoo_name_counts, min_score, lang,
            )

            if not ps_id and use_ps_api:
                if stats['ps_api_calls'] < 5:
                    log(
                        f'[legacy] Búsqueda API PS por nombre: odoo={product["id"]} '
                        f'"{pick_name(product.get("name"), lang)[:50]}"',
                        '…',
                    )
                stats['ps_api_calls'] += 1
                ps_id, score, method, detail = match_via_ps_api(
                    product, session, url, key, ps_search_cache, min_score, lang,
                )
                if ps_id:
                    method = 'ps_api_nombre'

            entry = {
                'odoo_id': product['id'],
                'default_code': product.get('default_code'),
                'name': pick_name(product.get('name'), lang),
                'ps_id': ps_id,
                'score': score,
                'method': method,
                'detail': detail,
            }

            if not ps_id:
                stats['sin_match'] += 1
                report.append(entry)
                progress.tick(stats)
                continue

            ext_row = ext_by_code.get(str(ps_id))
            try:
                public_ids, cat_source = resolve_public_categories(
                    ps_id, ext_row, ps_to_public, ext_to_mapped_public,
                    session, url, key, ps_cat_cache, invalid_ps_ids, ps_delay,
                    from_api_search=(method == 'ps_api_nombre'),
                )
            except RuntimeError as exc:
                stats['error_ps'] += 1
                entry['error'] = str(exc)
                report.append(entry)
                if stats['error_ps'] <= 10:
                    log(f'Error PS producto {ps_id} (odoo {product["id"]}): {exc}', '!')
                progress.tick(stats)
                continue

            entry['category_source'] = cat_source
            entry['public_category_ids'] = public_ids

            if cat_source == 'ps_no_existe':
                stats['ps_no_existe'] += 1
                report.append(entry)
                progress.tick(stats)
                continue

            if not public_ids:
                stats['sin_categoria_mapeada'] += 1
                report.append(entry)
                progress.tick(stats)
                continue

            if apply_product_categories(
                models, uid, product, public_ids, pub_to_pc, pub_to_pos, dry_run,
                set_sale_purchase, lang,
            ):
                stats['actualizados'] += 1
                stats[method] += 1
                if cat_source == 'copiado_mapeado':
                    stats['copiado_mapeado'] += 1
            report.append(entry)
            progress.tick(stats)

        log(
            f'[legacy] Lote {batch_num}/{n_batches} listo · '
            f'global {global_idx}/{total} · ok={stats["actualizados"]}',
        )

    log(
        f'Legacy: {stats["actualizados"]} actualizados, {stats["sin_match"]} sin match, '
        f'{stats["sin_categoria_mapeada"]} sin categoría mapeada, '
        f'{stats["ps_no_existe"]} ID PS inexistente, '
        f'{stats["ps_api_calls"]} búsquedas API, {stats["error_ps"]} errores PS'
    )
    for method in (
        'ps_id_local', 'ps_ref', 'barcode', 'nombre_1a1',
        'nombre_ambiguo', 'nombre', 'ps_api_nombre', 'copiado_mapeado',
    ):
        if stats[method]:
            log(f'  · {method}: {stats[method]}')

    if report_path:
        with open(report_path, 'w', encoding='utf-8') as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        log(f'Informe: {report_path}')

    return stats


def main():
    parser = argparse.ArgumentParser(
        description='Arte: sincronizar public categories → product.category + pos.category y TPV.',
    )
    parser.add_argument('--integration-id', type=int, default=INTEGRATION_ID)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--lang', default=LANG, help=f'Idioma para nombres (default {LANG}).')
    parser.add_argument(
        '--product-scope',
        choices=('prestashop', 'public', 'all'),
        default='prestashop',
        help=(
            'prestashop: solo productos con mapping de integración (default). '
            'public: cualquier producto con public_categ_ids. '
            'all: todos los product.template.'
        ),
    )
    parser.add_argument('--batch-size', type=int, default=200)
    parser.add_argument('--skip-categories', action='store_true',
                        help='No recrear árbol; solo actualizar productos (requiere mapeo previo).')
    parser.add_argument('--skip-products', action='store_true',
                        help='Solo sincronizar árbol de categorías.')
    parser.add_argument('--no-sale-purchase', action='store_true',
                        help='No forzar sale_ok/purchase_ok (sí available_in_pos).')
    parser.add_argument(
        '--legacy-unmapped',
        action='store_true',
        help='Fase extra: productos sin mapping → match PS (id/ref/barcode/nombre+API) y categorías.',
    )
    parser.add_argument(
        '--skip-prestashop-sync',
        action='store_true',
        help='No ejecutar sync estándar (--product-scope); solo categorías y/o legacy.',
    )
    parser.add_argument(
        '--min-match-score',
        type=int,
        default=55,
        help='Puntuación mínima para aceptar match legacy (default 55).',
    )
    parser.add_argument(
        '--ps-delay',
        type=float,
        default=0.05,
        help='Pausa entre llamadas GET a PrestaShop (segundos).',
    )
    parser.add_argument(
        '--no-ps-api-search',
        action='store_true',
        help='No buscar en API PS por nombre si falla match local.',
    )
    parser.add_argument(
        '--report',
        default='/tmp/arte_legacy_match_report.json',
        help='Ruta JSON con detalle de matches legacy.',
    )
    parser.add_argument(
        '--progress-every',
        type=int,
        default=50,
        help='Log de progreso cada N productos (default 50).',
    )
    parser.add_argument(
        '--heartbeat',
        type=int,
        default=15,
        help='Log heartbeat si no hay resumen en N segundos (default 15).',
    )
    parser.add_argument(
        '--legacy-batch-size',
        type=int,
        default=500,
        help='Productos por lote en fase legacy (default 500).',
    )
    args = parser.parse_args()
    lang = args.lang

    if not ODOO_PASSWORD:
        log('Define ODOO_PASSWORD.', '!')
        sys.exit(1)

    print('=' * 60, flush=True)
    log(f'Inicio | DB={ODOO_DB} | {ODOO_URL} | scope={args.product_scope}')
    if args.dry_run:
        log('Modo DRY-RUN (sin escrituras)', 'dry')
    print('=' * 60, flush=True)

    try:
        uid, models = connect_odoo()
        log(f'XML-RPC OK uid={uid}')
    except Exception as exc:
        log(str(exc), '!')
        sys.exit(1)

    integration = execute(
        models, uid, 'sale.integration', 'read',
        [[args.integration_id]], fields=['name', 'type_api', 'state'],
    )
    if not integration:
        log(f'No existe sale.integration id={args.integration_id}', '!')
        sys.exit(1)
    log(f'Integración: {integration[0]["name"]} ({integration[0]["type_api"]})')

    pub_to_pc = {}
    pub_to_pos = {}

    if not args.skip_categories:
        public_cats = load_public_categories(models, uid, lang)
        if not public_cats:
            log('No hay product.public.category en Odoo.', '!')
            sys.exit(1)
        pub_to_pc, pub_to_pos = sync_category_trees(
            models, uid, public_cats, args.dry_run, lang,
        )
    else:
        log('Omitiendo árbol de categorías (--skip-categories)', '!')
        log('Reconstruyendo mapeo por nombre+jerarquía desde categorías públicas…')
        public_cats = load_public_categories(models, uid, lang)
        for pub in public_cats:
            name = pick_name(pub.get('name'), lang)
            parent_pub = m2o_id(pub.get('parent_id'))
            parent_pc = pub_to_pc.get(parent_pub) if parent_pub else False
            parent_pos = pub_to_pos.get(parent_pub) if parent_pub else False
            pc_ids = find_category(models, uid, 'product.category', name, parent_pc, lang)
            pos_ids = find_category(models, uid, 'pos.category', name, parent_pos, lang)
            if pc_ids:
                pub_to_pc[pub['id']] = pc_ids[0]
            if pos_ids:
                pub_to_pos[pub['id']] = pos_ids[0]

    if args.skip_products and not args.legacy_unmapped:
        log(f'Fin (--skip-products). Tiempo: {fmt_duration(time.monotonic() - _T0)}')
        return

    if not args.skip_prestashop_sync and not args.legacy_unmapped:
        domain = resolve_product_domain(models, uid, args.product_scope, args.integration_id)
        sync_products(
            models, uid, domain, pub_to_pc, pub_to_pos, args.dry_run,
            set_sale_purchase=not args.no_sale_purchase,
            batch_size=args.batch_size,
            lang=lang,
        )
    elif not args.skip_prestashop_sync and args.legacy_unmapped:
        domain = resolve_product_domain(models, uid, args.product_scope, args.integration_id)
        sync_products(
            models, uid, domain, pub_to_pc, pub_to_pos, args.dry_run,
            set_sale_purchase=not args.no_sale_purchase,
            batch_size=args.batch_size,
            lang=lang,
        )

    if args.legacy_unmapped:
        sync_legacy_unmapped(
            models, uid, args.integration_id, pub_to_pc, pub_to_pos, args.dry_run,
            set_sale_purchase=not args.no_sale_purchase,
            min_score=args.min_match_score,
            ps_delay=args.ps_delay,
            use_ps_api=not args.no_ps_api_search,
            report_path=args.report,
            lang=lang,
            batch_size=args.legacy_batch_size,
            progress_every=args.progress_every,
            heartbeat=args.heartbeat,
        )

    log(f'TERMINADO. Tiempo total: {fmt_duration(time.monotonic() - _T0)}')


if __name__ == '__main__':
    main()
