#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Arte / Bellas Artes — SOLO productos con mapping de integración (Odoo).

Copia de arte_sync_categories_pos_xmlrpc.py SIN la fase legacy (~19k sin mapping
emparejados por nombre / API PrestaShop).

Staging (default):
  ODOO_URL=https://stg20260604-odoo17-jer-bellas-artes.n3.clodofy.cloud
  ODOO_DB=odoo_stg20260604_odoo17_jer_bellas_artes

Uso:
  python3 scripts/arte_sync_categories_pos_odoo_only.py
  python3 scripts/arte_sync_categories_pos_odoo_only.py --dry-run
  python3 scripts/arte_sync_categories_pos_odoo_only.py --skip-categories
  python3 scripts/arte_sync_categories_pos_odoo_only.py --skip-categories --purge-catalog --dry-run
  python3 scripts/arte_sync_categories_pos_odoo_only.py --cleanup-duplicates --consolidate-duplicates --purge-unmapped
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
ODOO_URL = os.environ.get('ODOO_URL', 'https://stg20260604-odoo17-jer-bellas-artes.n3.clodofy.cloud')
ODOO_DB = os.environ.get('ODOO_DB', 'odoo_stg20260604_odoo17_jer_bellas_artes')
ODOO_USER = os.environ.get('ODOO_USER', 'jose.carrillo@clodofy.com')
ODOO_PASSWORD = os.environ.get('ODOO_PASSWORD', '123')
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


def normalize_code(value):
    return (value or '').strip().upper()


def score_mapping_match(default_code, external_code):
    """
    Heurística conservadora para elegir mapping "más lógico":
      3: default_code == external_code
      2: default_code tipo PS-<external>-...
      1: contiene external_code
      0: sin coincidencia
    """
    d = normalize_code(default_code)
    e = normalize_code(external_code)
    if not d or not e:
        return 0
    if d == e:
        return 3
    if d.startswith(f'PS-{e}-') or d == f'PS-{e}':
        return 2
    if e in d:
        return 1
    return 0


def _build_default_code_index(models, uid, model, ids):
    if not ids:
        return {}
    rows = execute(
        models, uid, model, 'read', [list(ids)],
        fields=['default_code'],
    )
    return {row['id']: row.get('default_code') for row in rows}


def _build_external_code_index(models, uid, model, ids):
    if not ids:
        return {}
    rows = execute(
        models, uid, model, 'read', [list(ids)],
        fields=['code'],
    )
    return {row['id']: row.get('code') for row in rows}


def cleanup_duplicate_mappings(models, uid, integration_id, dry_run):
    """
    Limpia duplicados en mappings:
      - integration.product.template.mapping con mismo template_id (>1 externos)
      - integration.product.product.mapping con mismo product_id (>1 externos)
    Conserva 1 mapping por registro Odoo usando heurística de código.
    """
    summary = {
        'template_groups': 0,
        'template_removed': 0,
        'variant_groups': 0,
        'variant_removed': 0,
    }

    # -------------------------
    # Duplicados en plantillas
    # -------------------------
    tpl_map_rows = execute(
        models, uid, 'integration.product.template.mapping', 'search_read',
        [[('integration_id', '=', integration_id), ('template_id', '!=', False)]],
        fields=['id', 'template_id', 'external_template_id'],
        order='id',
    )
    tpl_groups = defaultdict(list)
    tpl_ids = set()
    ext_tpl_ids = set()
    for row in tpl_map_rows:
        tpl_id = m2o_id(row.get('template_id'))
        ext_id = m2o_id(row.get('external_template_id'))
        if not tpl_id or not ext_id:
            continue
        tpl_groups[tpl_id].append(row)
        tpl_ids.add(tpl_id)
        ext_tpl_ids.add(ext_id)

    tpl_code = _build_default_code_index(models, uid, 'product.template', tpl_ids)
    ext_tpl_code = _build_external_code_index(
        models, uid, 'integration.product.template.external', ext_tpl_ids,
    )

    to_unlink_tpl = []
    for tpl_id, rows in tpl_groups.items():
        if len(rows) <= 1:
            continue
        summary['template_groups'] += 1
        ranked = []
        for row in rows:
            ext_id = m2o_id(row.get('external_template_id'))
            score = score_mapping_match(tpl_code.get(tpl_id), ext_tpl_code.get(ext_id))
            ranked.append((score, row['id'], row))
        ranked.sort(key=lambda x: (-x[0], x[1]))  # mejor score, luego id menor
        keep_id = ranked[0][1]
        drop_ids = [x[1] for x in ranked[1:]]
        to_unlink_tpl.extend(drop_ids)
        log(
            f'[dedupe tpl] tpl {tpl_id}: keep={keep_id} drop={drop_ids} '
            f'(scores={[x[0] for x in ranked]})',
            '!',
        )

    if to_unlink_tpl:
        summary['template_removed'] = len(to_unlink_tpl)
        if dry_run:
            log(f'[dedupe tpl] dry-run: se eliminarían {len(to_unlink_tpl)} mappings', 'dry')
        else:
            execute(
                models, uid, 'integration.product.template.mapping', 'unlink',
                [to_unlink_tpl],
            )
            log(f'[dedupe tpl] eliminados {len(to_unlink_tpl)} mappings')

    # ------------------------
    # Duplicados en variantes
    # ------------------------
    var_map_rows = execute(
        models, uid, 'integration.product.product.mapping', 'search_read',
        [[('integration_id', '=', integration_id), ('product_id', '!=', False)]],
        fields=['id', 'product_id', 'external_product_id'],
        order='id',
    )
    var_groups = defaultdict(list)
    product_ids = set()
    ext_var_ids = set()
    for row in var_map_rows:
        product_id = m2o_id(row.get('product_id'))
        ext_id = m2o_id(row.get('external_product_id'))
        if not product_id or not ext_id:
            continue
        var_groups[product_id].append(row)
        product_ids.add(product_id)
        ext_var_ids.add(ext_id)

    product_code = _build_default_code_index(models, uid, 'product.product', product_ids)
    ext_var_code = _build_external_code_index(
        models, uid, 'integration.product.product.external', ext_var_ids,
    )

    to_unlink_var = []
    for product_id, rows in var_groups.items():
        if len(rows) <= 1:
            continue
        summary['variant_groups'] += 1
        ranked = []
        for row in rows:
            ext_id = m2o_id(row.get('external_product_id'))
            score = score_mapping_match(product_code.get(product_id), ext_var_code.get(ext_id))
            ranked.append((score, row['id'], row))
        ranked.sort(key=lambda x: (-x[0], x[1]))
        keep_id = ranked[0][1]
        drop_ids = [x[1] for x in ranked[1:]]
        to_unlink_var.extend(drop_ids)
        log(
            f'[dedupe var] var {product_id}: keep={keep_id} drop={drop_ids} '
            f'(scores={[x[0] for x in ranked]})',
            '!',
        )

    if to_unlink_var:
        summary['variant_removed'] = len(to_unlink_var)
        if dry_run:
            log(f'[dedupe var] dry-run: se eliminarían {len(to_unlink_var)} mappings', 'dry')
        else:
            execute(
                models, uid, 'integration.product.product.mapping', 'unlink',
                [to_unlink_var],
            )
            log(f'[dedupe var] eliminados {len(to_unlink_var)} mappings')

    log(
        '[dedupe] grupos tpl=%s, elim tpl=%s, grupos var=%s, elim var=%s'
        % (
            summary['template_groups'],
            summary['template_removed'],
            summary['variant_groups'],
            summary['variant_removed'],
        )
    )
    return summary


PROTECTED_CODE_PREFIXES = ('DUA', 'EXP_', 'DELIVERY', 'DISCOUNT')


def norm_name(value, lang=LANG):
    text = pick_name(value, lang).lower().strip()
    text = unicodedata.normalize('NFKD', text)
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r'[^\w\s\-]', ' ', text, flags=re.UNICODE)
    return ' '.join(text.split())


def is_protected_template(template):
    code = normalize_code(template.get('default_code'))
    if any(code.startswith(prefix) for prefix in PROTECTED_CODE_PREFIXES):
        return True
    return template.get('type') == 'service'


def load_mapped_template_ids(models, uid, integration_id):
    rows = execute(
        models, uid, 'integration.product.template.mapping', 'search_read',
        [[('integration_id', '=', integration_id), ('template_id', '!=', False)]],
        fields=['template_id'],
    )
    return {m2o_id(row['template_id']) for row in rows if m2o_id(row.get('template_id'))}


def load_all_templates(models, uid, lang=LANG, batch_size=500):
    ids = execute(models, uid, 'product.template', 'search', [[('active', 'in', (True, False))]])
    fields = ['id', 'default_code', 'name', 'active', 'type']
    templates = []
    for start in range(0, len(ids), batch_size):
        chunk = ids[start:start + batch_size]
        templates.extend(
            execute(
                models, uid, 'product.template', 'read', [chunk],
                fields=fields, context=odoo_ctx(lang),
            )
        )
    return templates


def load_templates_with_stock(models, uid):
    groups = execute(
        models, uid, 'stock.quant', 'read_group',
        [[('quantity', '>', 0)]],
        fields=['quantity'],
        groupby=['product_id'],
    )
    if not groups:
        return set()
    product_ids = [m2o_id(g['product_id']) for g in groups if g.get('product_id')]
    if not product_ids:
        return set()
    tpl_ids = set()
    for start in range(0, len(product_ids), 500):
        chunk = product_ids[start:start + 500]
        rows = execute(
            models, uid, 'product.product', 'read', [chunk],
            fields=['product_tmpl_id'],
        )
        tpl_ids.update(m2o_id(r['product_tmpl_id']) for r in rows if m2o_id(r.get('product_tmpl_id')))
    return tpl_ids


def _chunked(ids, size=100):
    ids = list(ids)
    for i in range(0, len(ids), size):
        yield ids[i:i + size]


def _is_pos_session_block(exc):
    msg = (exc.faultString or '').lower()
    return 'punto de venta' in msg or 'point of sale' in msg or 'pos session' in msg


def _remove_templates(models, uid, template_ids, dry_run, purge_mode):
    if not template_ids:
        return 0
    if dry_run:
        log(f'[purge] dry-run: afectaría {len(template_ids)} plantillas (mode={purge_mode})', 'dry')
        return len(template_ids)
    removed = 0
    archived_fallback = 0
    for chunk in _chunked(template_ids, 100):
        try:
            if purge_mode == 'archive':
                execute(models, uid, 'product.template', 'write', [chunk, {'active': False}])
            else:
                execute(models, uid, 'product.template', 'unlink', [chunk])
            removed += len(chunk)
        except xmlrpc.client.Fault as exc:
            if purge_mode == 'unlink' and _is_pos_session_block(exc):
                try:
                    execute(
                        models, uid, 'product.template', 'write',
                        [chunk, {'active': False, 'available_in_pos': False}],
                    )
                    archived_fallback += len(chunk)
                    removed += len(chunk)
                    log(
                        f'[purge] POS abierto: archivados {len(chunk)} en lugar de borrar',
                        '!',
                    )
                    continue
                except xmlrpc.client.Fault as exc2:
                    log(f'[purge] fallback archive falló ({chunk[:5]}…): {exc2.faultString}', '!')
            else:
                log(f'[purge] error en chunk ({chunk[:5]}…): {exc.faultString}', '!')
    if archived_fallback:
        log(f'[purge] archivados por POS={archived_fallback}')
    return removed


def consolidate_duplicate_templates(
    models, uid, integration_id, dry_run, purge_mode,
    skip_stock, consolidate_by_name, lang=LANG,
):
    """
    Agrupa duplicados por referencia (y opcionalmente nombre).
    Si alguno del grupo tiene mapping PS, conserva ese y elimina el resto.
    """
    mapped_ids = load_mapped_template_ids(models, uid, integration_id)
    stock_tpl_ids = load_templates_with_stock(models, uid) if skip_stock else set()
    templates = load_all_templates(models, uid, lang=lang)

    by_code = defaultdict(list)
    by_name = defaultdict(list)
    for tpl in templates:
        if is_protected_template(tpl):
            continue
        code = normalize_code(tpl.get('default_code'))
        if code:
            by_code[code].append(tpl)
        elif consolidate_by_name:
            name = norm_name(tpl.get('name'), lang)
            if name and name != 'sin nombre':
                by_name[name].append(tpl)

    groups = []
    for key, items in by_code.items():
        if len(items) > 1:
            groups.append(('code', key, items))
    if consolidate_by_name:
        for key, items in by_name.items():
            if len(items) > 1:
                groups.append(('name', key, items))

    summary = {
        'groups': 0,
        'keepers': 0,
        'removed': 0,
        'skipped_stock': 0,
        'skipped_no_mapped': 0,
    }
    to_remove = []

    for kind, key, items in groups:
        mapped_in_group = [t for t in items if t['id'] in mapped_ids]
        if not mapped_in_group:
            summary['skipped_no_mapped'] += 1
            continue
        mapped_in_group.sort(key=lambda t: t['id'])
        keeper = mapped_in_group[0]
        drops = [t for t in items if t['id'] != keeper['id']]
        if not drops:
            continue
        summary['groups'] += 1
        summary['keepers'] += 1
        drop_ids = []
        for tpl in drops:
            if skip_stock and tpl['id'] in stock_tpl_ids:
                summary['skipped_stock'] += 1
                continue
            drop_ids.append(tpl['id'])
        if drop_ids:
            to_remove.extend(drop_ids)
            log(
                f'[consolidate {kind}] {key}: keep tpl {keeper["id"]} '
                f'({keeper.get("default_code")}) drop={drop_ids}',
                '!',
            )

    if to_remove:
        summary['removed'] = _remove_templates(models, uid, to_remove, dry_run, purge_mode)

    log(
        '[consolidate] grupos=%s, keepers=%s, eliminados=%s, '
        'omitidos_sin_mapping=%s, omitidos_stock=%s'
        % (
            summary['groups'], summary['keepers'], summary['removed'],
            summary['skipped_no_mapped'], summary['skipped_stock'],
        )
    )
    return summary


def purge_unmapped_templates(
    models, uid, integration_id, dry_run, purge_mode, skip_stock, lang=LANG,
):
    """Elimina/archiva plantillas sin mapping PS (dejando solo catálogo enlazado)."""
    mapped_ids = load_mapped_template_ids(models, uid, integration_id)
    stock_tpl_ids = load_templates_with_stock(models, uid) if skip_stock else set()
    templates = load_all_templates(models, uid, lang=lang)

    to_remove = []
    summary = {
        'candidates': 0,
        'removed': 0,
        'skipped_protected': 0,
        'skipped_stock': 0,
        'skipped_mapped': 0,
    }

    for tpl in templates:
        tpl_id = tpl['id']
        if tpl_id in mapped_ids:
            summary['skipped_mapped'] += 1
            continue
        if is_protected_template(tpl):
            summary['skipped_protected'] += 1
            continue
        if skip_stock and tpl_id in stock_tpl_ids:
            summary['skipped_stock'] += 1
            continue
        summary['candidates'] += 1
        to_remove.append(tpl_id)

    log(f'[purge-unmapped] candidatos={summary["candidates"]} (mapped conservados={len(mapped_ids)})')
    if to_remove:
        summary['removed'] = _remove_templates(models, uid, to_remove, dry_run, purge_mode)

    log(
        '[purge-unmapped] eliminados=%s, protegidos=%s, stock_omitido=%s'
        % (summary['removed'], summary['skipped_protected'], summary['skipped_stock'])
    )
    return summary


def main():
    parser = argparse.ArgumentParser(
        description='Arte (solo mapping Odoo): categorías públicas → product/pos + TPV.',
    )
    parser.add_argument('--integration-id', type=int, default=INTEGRATION_ID)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--lang', default=LANG, help=f'Idioma para nombres (default {LANG}).')
    parser.add_argument(
        '--product-scope',
        choices=('prestashop', 'public', 'all'),
        default='prestashop',
        help='prestashop (default): solo productos con mapping de integración.',
    )
    parser.add_argument('--batch-size', type=int, default=200)
    parser.add_argument('--skip-categories', action='store_true',
                        help='No recrear árbol; solo actualizar productos.')
    parser.add_argument('--skip-products', action='store_true',
                        help='Solo sincronizar árbol de categorías.')
    parser.add_argument('--no-sale-purchase', action='store_true',
                        help='No forzar sale_ok/purchase_ok (sí available_in_pos).')
    parser.add_argument(
        '--cleanup-duplicates',
        action='store_true',
        help='Limpia mappings duplicados (template/variant) conservando el mejor match por código.',
    )
    parser.add_argument(
        '--consolidate-duplicates',
        action='store_true',
        help='Agrupa duplicados por referencia/nombre; si hay mapping PS, borra los demás.',
    )
    parser.add_argument(
        '--purge-unmapped',
        action='store_true',
        help='Elimina/archiva plantillas sin mapping PrestaShop.',
    )
    parser.add_argument(
        '--purge-catalog',
        action='store_true',
        help='Atajo: --cleanup-duplicates + --consolidate-duplicates + --purge-unmapped.',
    )
    parser.add_argument(
        '--purge-mode',
        choices=('archive', 'unlink'),
        default='unlink',
        help='archive: active=False; unlink: borrar registro (default unlink).',
    )
    parser.add_argument(
        '--skip-stock',
        action='store_true',
        default=True,
        help='No tocar plantillas con stock > 0 (default: sí omitir).',
    )
    parser.add_argument(
        '--no-skip-stock',
        action='store_false',
        dest='skip_stock',
        help='Permite purgar aunque tengan stock.',
    )
    parser.add_argument(
        '--consolidate-by-name',
        action='store_true',
        help='Además de default_code, consolidar duplicados por nombre normalizado.',
    )
    args = parser.parse_args()
    if args.purge_catalog:
        args.cleanup_duplicates = True
        args.consolidate_duplicates = True
        args.purge_unmapped = True
    lang = args.lang

    if not ODOO_PASSWORD:
        log('Define ODOO_PASSWORD.', '!')
        sys.exit(1)

    print('=' * 60, flush=True)
    log(f'Inicio (solo Odoo/mapping) | DB={ODOO_DB} | {ODOO_URL} | scope={args.product_scope}')
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

    cleanup_requested = (
        args.cleanup_duplicates or args.consolidate_duplicates or args.purge_unmapped
    )

    if not args.skip_products:
        domain = resolve_product_domain(models, uid, args.product_scope, args.integration_id)
        sync_products(
            models, uid, domain, pub_to_pc, pub_to_pos, args.dry_run,
            set_sale_purchase=not args.no_sale_purchase,
            batch_size=args.batch_size,
            lang=lang,
        )
    elif not cleanup_requested:
        log(f'Fin (--skip-products). Tiempo: {fmt_duration(time.monotonic() - _T0)}')
        return

    if args.cleanup_duplicates:
        cleanup_duplicate_mappings(
            models, uid, args.integration_id, args.dry_run,
        )
    if args.consolidate_duplicates:
        consolidate_duplicate_templates(
            models, uid, args.integration_id, args.dry_run, args.purge_mode,
            args.skip_stock, args.consolidate_by_name, lang,
        )
    if args.purge_unmapped:
        purge_unmapped_templates(
            models, uid, args.integration_id, args.dry_run, args.purge_mode,
            args.skip_stock, lang,
        )

    log(f'TERMINADO. Tiempo total: {fmt_duration(time.monotonic() - _T0)}')


if __name__ == '__main__':
    main()
