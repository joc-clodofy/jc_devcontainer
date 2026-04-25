#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Reparacion de rectificativas incoherentes entre Odoo y MongoDB.
VERSION JSON-RPC: usa JSON-RPC en lugar de XML-RPC para evitar el error
"cannot marshal None" en Python 3.12+ con metodos que retornan None (ej. unlink).

Objetivo:
    1) Detectar rectificativas (out_refund) que apuntan a una factura original
       de otro colegiado (partner distinto).
    2) Borrar esas rectificativas incoherentes en Odoo.
    3) Borrar sus efectos relacionados en Mongo (coleccion efectos).
    4) Para cada colegiado afectado, localizar su rectificativa coherente mas
       reciente y crear una nueva rectificativa basada en esa peticion.
    5) Insertar tambien el efecto asociado en Mongo para la nueva rectificativa.

Notas:
    - Odoo y Mongo se modifican SOLO si DRY_RUN=False.
    - El script genera resumen JSON y Markdown para auditoria.
    - Usa JSON-RPC: no requiere modificar Odoo (evita error XML-RPC con None).
"""

import datetime
import json
import logging
import os
import re
import sys
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database


# ---------------------------------------------------------------------------
# CONFIGURACION
# ---------------------------------------------------------------------------

ODOO_URL = os.environ.get("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.environ.get("ODOO_DB", "colfisio")
ODOO_USER = os.environ.get("ODOO_USER", "admin")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "123")

MONGO_URI_FALLBACK = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME_FALLBACK = os.environ.get("MONGO_DB_NAME", "colfisio_mongo")
COL_EFECTOS = os.environ.get("MONGO_COL_EFECTOS", "efectos")

DRY_RUN = False
DEFER_ODOO_DELETE = os.environ.get(
    "INCOHERENT_REFUND_DEFER_ODOO_DELETE", "1"
).lower() in ("1", "true", "yes")
SUMMARY_DIR = os.environ.get("INCOHERENT_REFUND_SUMMARY_DIR", ".")
MAX_REFUNDS_TO_SCAN = int(os.environ.get("INCOHERENT_REFUND_SCAN_LIMIT", "0"))
MAX_ORIGINAL_CANDIDATES = int(
    os.environ.get("INCOHERENT_REFUND_ORIGINAL_CANDIDATES", "50")
)
# Tolerancia para considerar mismo monto al agrupar duplicadas (solo rectificativas).
AMOUNT_TOLERANCE_DUPLICATE = float(os.environ.get("INCOHERENT_REFUND_AMOUNT_TOLERANCE", "0.01"))
# Coherencia temporal: no asociar factura de más de N días antes que la rectificativa/efecto (alarma).
MAX_DAYS_INVOICE_BEFORE_REFUND = int(os.environ.get("INCOHERENT_REFUND_MAX_DAYS_ANTIGUEDAD", "365"))
_filter_year = os.environ.get("INCOHERENT_REFUND_FILTER_YEAR", "2026")
FILTER_YEAR = int(_filter_year) if _filter_year else 0
# Límite por colegiados (0 = todos). Por cada colegiado se procesan todas sus rectificativas.
_max_process = os.environ.get("INCOHERENT_REFUND_MAX_PROCESS", "0")
MAX_TO_PROCESS = int(_max_process) if _max_process else 0

# Por defecto (pruebas) solo estos colegiados; vacío = todos.
_codigos_env = os.environ.get("INCOHERENT_REFUND_CODIGOS_COLEGIADO", "9769,9339")
FILTER_CODIGOS_COLEGIADO = (
    [int(x.strip()) for x in _codigos_env.split(",") if x.strip()]
    if _codigos_env and str(_codigos_env).lower() not in ("", "all")
    else []
)


# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

logger = logging.getLogger("fix_incoherent_refund_invoices_jsonrpc")
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(
    logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s", "%Y-%m-%d %H:%M:%S")
)
logger.addHandler(console_handler)


# ---------------------------------------------------------------------------
# JSON-RPC CLIENT ODOO
# ---------------------------------------------------------------------------


def _jsonrpc_call(service: str, method: str, args: List[Any]) -> Any:
    """Llama al endpoint JSON-RPC de Odoo."""
    url = f"{ODOO_URL.rstrip('/')}/jsonrpc"
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {"service": service, "method": method, "args": args},
        "id": 1,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read().decode())
    if "error" in result:
        err = result["error"]
        msg = err.get("data", {}).get("message") or err.get("message", str(err))
        raise RuntimeError(f"Odoo JSON-RPC error: {msg}")
    return result.get("result")


def get_odoo_jsonrpc() -> Tuple[int, Callable[..., Any]]:
    """Autentica y devuelve (uid, execute_kw)."""
    uid = _jsonrpc_call("common", "authenticate", [ODOO_DB, ODOO_USER, ODOO_PASSWORD, {}])
    if not uid:
        raise RuntimeError("No se pudo autenticar en Odoo. Revisa credenciales.")

    def execute_kw(model: str, method: str, args: List[Any], kwargs: Optional[Dict] = None) -> Any:
        # Odoo execute_kw espera: (db, uid, pwd, model, method, args, kw)
        # args debe ser la lista de argumentos posicionales (ej. [domain] para search_read)
        call_args = [ODOO_DB, uid, ODOO_PASSWORD, model, method, args]
        if kwargs:
            call_args.append(kwargs)
        return _jsonrpc_call("object", "execute_kw", call_args)

    return uid, execute_kw


def get_mongo_config_from_parametrizacion(execute_kw: Callable) -> Tuple[str, str]:
    try:
        res = execute_kw(
            "parametrizacion",
            "search_read",
            [[]],
            {"fields": ["mongo_db_url", "mongo_db"], "limit": 1},
        )
    except Exception as exc:
        logger.warning("No se pudo leer parametrizacion de Mongo en Odoo: %s", exc)
        return MONGO_URI_FALLBACK, MONGO_DB_NAME_FALLBACK
    if not res:
        return MONGO_URI_FALLBACK, MONGO_DB_NAME_FALLBACK
    row = res[0]
    return (
        row.get("mongo_db_url") or MONGO_URI_FALLBACK,
        row.get("mongo_db") or MONGO_DB_NAME_FALLBACK,
    )


# ---------------------------------------------------------------------------
# HELPERS ODOO (JSON-RPC)
# ---------------------------------------------------------------------------


def odoo_search_read(execute_kw: Callable, model: str, domain: List, fields: List[str], **kwargs) -> List[Dict]:
    try:
        return execute_kw(model, "search_read", [domain], {"fields": fields, **kwargs})
    except RuntimeError as e:
        print("\n" + "!" * 80)
        print("ERROR CRITICO DESDE EL SERVIDOR ODOO (JSON-RPC)")
        print("-" * 80)
        print(f"MENSAJE: {e}")
        print("!" * 80 + "\n")
        raise


def odoo_read(execute_kw: Callable, model: str, ids: List[int], fields: List[str]) -> List[Dict]:
    if not ids:
        return []
    return execute_kw(model, "read", [ids], {"fields": fields})


def _m2o_id(value: Any) -> Optional[int]:
    if isinstance(value, (list, tuple)) and value:
        return int(value[0])
    if isinstance(value, int):
        return value
    return None


def _m2o_name(value: Any) -> str:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return str(value[1] or "")
    return ""


def _invoice_is_coherent(refund: Dict, original: Optional[Dict]) -> bool:
    if not original:
        return False
    return _m2o_id(refund.get("partner_id")) == _m2o_id(original.get("partner_id"))


def _safe_str(value: Any) -> str:
    return str(value) if value is not None else ""


def _now() -> datetime.datetime:
    return datetime.datetime.now().replace(microsecond=0)


def _normalize_codigo(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def find_incoherent_refunds(execute_kw: Callable) -> Tuple[List[Dict], int]:
    fields = [
        "id", "name", "partner_id", "partner_codigo_colegiado", "move_type", "state",
        "invoice_date", "amount_total", "currency_id", "reversed_entry_id",
        "invoice_origin", "journal_id", "payment_reference", "ref", "display_name",
    ]
    limit = MAX_REFUNDS_TO_SCAN if MAX_REFUNDS_TO_SCAN > 0 else 0
    domain = [("move_type", "=", "out_refund"), ("state", "!=", "cancel")]
    if FILTER_YEAR:
        domain.extend([
            ("invoice_date", ">=", f"{FILTER_YEAR}-01-01"),
            ("invoice_date", "<=", f"{FILTER_YEAR}-12-31"),
        ])
    refunds = odoo_search_read(
        execute_kw, "account.move", domain, fields,
        limit=limit, order="invoice_date desc, id desc",
    )

    move_cache: Dict[int, Dict] = {}
    move_fields = [
        "id", "name", "partner_id", "move_type", "state", "invoice_date",
        "amount_total", "invoice_line_ids", "journal_id", "reversed_entry_id",
        "invoice_origin", "payment_reference", "ref", "display_name",
    ]

    def get_move(move_id: int) -> Optional[Dict]:
        if not move_id:
            return None
        if move_id in move_cache:
            return move_cache[move_id]
        rows = odoo_read(execute_kw, "account.move", [move_id], move_fields)
        move_cache[move_id] = rows[0] if rows else None
        return move_cache[move_id]

    invoice_name_cache: Dict[str, Optional[Dict]] = {}

    def find_invoice_by_name(name: str, partner_id: Optional[int], any_partner: bool = False) -> Optional[Dict]:
        key = f"{name}|{partner_id or 0}|{int(any_partner)}"
        if key in invoice_name_cache:
            return invoice_name_cache[key]
        dom = [("name", "=", name), ("move_type", "=", "out_invoice"), ("state", "=", "posted")]
        if partner_id and not any_partner:
            dom.append(("partner_id", "=", partner_id))
        rows = odoo_search_read(execute_kw, "account.move", dom, move_fields, limit=1)
        invoice_name_cache[key] = rows[0] if rows else None
        return invoice_name_cache[key]

    def extract_invoice_names(*texts: Optional[str]) -> List[str]:
        names, seen = [], set()
        pattern = re.compile(r"\bINV/\d{4}/\d+\b", re.IGNORECASE)
        for text in texts:
            if not text:
                continue
            for match in pattern.findall(text):
                name = match.upper()
                if name not in seen:
                    seen.add(name)
                    names.append(name)
        return names

    def resolve_root_original(refund: Dict) -> Tuple[Optional[Dict], List[int], str]:
        chain_ids = [refund["id"]]
        seen = set(chain_ids)
        current = refund
        last_reason = "sin_resolver"
        while True:
            reversed_id = _m2o_id(current.get("reversed_entry_id"))
            if not reversed_id:
                break
            next_move = get_move(reversed_id)
            if not next_move:
                last_reason = "reversed_entry_no_encontrada"
                break
            if next_move["id"] in seen:
                last_reason = "bucle_reversed_entry"
                break
            chain_ids.append(next_move["id"])
            seen.add(next_move["id"])
            if next_move.get("move_type") == "out_invoice":
                return next_move, chain_ids, "cadena_reversed_entry"
            if next_move.get("move_type") != "out_refund":
                last_reason = f"tipo_invalido_en_cadena:{next_move.get('move_type')}"
                break
            current = next_move

        partner_id = _m2o_id(refund.get("partner_id"))
        invoice_origin = _safe_str(refund.get("invoice_origin")).strip()
        payment_reference = _safe_str(refund.get("payment_reference")).strip()
        ref_text = _safe_str(refund.get("ref")).strip()
        display_name = _safe_str(refund.get("display_name")).strip()
        candidate_names = []
        if invoice_origin:
            candidate_names.append(invoice_origin.upper())
        if payment_reference:
            candidate_names.append(payment_reference.upper())
        for inv_name in extract_invoice_names(invoice_origin, payment_reference, ref_text, display_name):
            if inv_name not in candidate_names:
                candidate_names.append(inv_name)

        for inv_name in candidate_names:
            by_same = find_invoice_by_name(inv_name, partner_id, any_partner=False)
            if by_same:
                return by_same, chain_ids, f"texto_{inv_name}_mismo_partner"
        for inv_name in candidate_names:
            by_any = find_invoice_by_name(inv_name, partner_id, any_partner=True)
            if by_any:
                return by_any, chain_ids, f"texto_{inv_name}_otro_partner"
        return None, chain_ids, last_reason

    incoherent = []
    for refund in refunds:
        original, chain_ids, origin_source = resolve_root_original(refund)
        if original and not _invoice_is_coherent(refund, original):
            incoherent.append({
                "refund": refund, "original": original,
                "refund_chain_ids": chain_ids, "origin_source": origin_source,
                "reason": "partner_distinto_o_original_no_encontrada",
            })
    return incoherent, len(refunds)


def delete_odoo_refund(execute_kw: Callable, refund_id: int) -> Tuple[bool, str]:
    move_data = odoo_read(execute_kw, "account.move", [refund_id], ["id", "name", "move_type", "state"])
    if not move_data:
        return False, "error_borrado_odoo: refund_no_encontrada"
    if move_data[0].get("move_type") != "out_refund":
        return False, f"bloqueado_no_out_refund:{move_data[0].get('move_type')}"
    if DRY_RUN:
        return True, "borrado_dry_run"

    for step_group in [("button_draft", "unlink"), ("button_cancel", "button_draft", "unlink")]:
        try:
            for method in step_group:
                execute_kw("account.move", method, [[refund_id]])
            return True, "borrado"
        except Exception as exc:
            last_error = str(exc)
    return False, f"error_borrado_odoo: {last_error}"


def _refund_duplicate_key(item: Dict) -> Tuple[int, float]:
    """Clave para agrupar rectificativas duplicadas: mismo partner y mismo monto (solo out_refund)."""
    r = item.get("refund") or {}
    pid = _m2o_id(r.get("partner_id")) or 0
    amount = abs(float(r.get("amount_total") or 0.0))
    # Redondear para agrupar con tolerancia
    amount_key = round(amount / AMOUNT_TOLERANCE_DUPLICATE) * AMOUNT_TOLERANCE_DUPLICATE
    return (pid, amount_key)


def group_refunds_by_duplicate(to_fix: List[Dict]) -> List[List[Dict]]:
    """Agrupa rectificativas incoherentes por duplicado: mismo partner_id y amount_total (solo rectificativas).
    Retorna lista de grupos; cada grupo es una lista de items a procesar juntos (una sola creación, N borrados).
    """
    from collections import OrderedDict
    groups_map = OrderedDict()
    for item in to_fix:
        key = _refund_duplicate_key(item)
        if key not in groups_map:
            groups_map[key] = []
        groups_map[key].append(item)
    return list(groups_map.values())


def _detail_from_item(
    item: Dict,
    refund: Dict,
    real_original: Optional[Dict],
    origin_reason: Optional[str],
    accion_creacion_odoo: str,
    new_refund: Optional[Dict],
    new_refund_name: Optional[str],
) -> Dict:
    """Construye el diccionario de detalle para un item (una rectificativa) en el informe."""
    original = item.get("original")
    return {
        "refund_id": refund.get("id"),
        "refund_name": refund.get("name"),
        "refund_partner_id": _m2o_id(refund.get("partner_id")),
        "refund_partner_name": _m2o_name(refund.get("partner_id")),
        "refund_codigo_colegiado": refund.get("partner_codigo_colegiado"),
        "original_id": original.get("id") if original else None,
        "original_name": original.get("name") if original else None,
        "original_partner_id": _m2o_id(original.get("partner_id")) if original else None,
        "original_partner_name": _m2o_name(original.get("partner_id")) if original else None,
        "razon_inconsistencia": item.get("reason"),
        "origen_resolucion_original": item.get("origin_source"),
        "cadena_refund_ids": item.get("refund_chain_ids", []),
        "accion_borrado_odoo": None,
        "accion_borrado_mongo": None,
        "original_real_id": real_original.get("id") if real_original else None,
        "original_real_name": real_original.get("name") if real_original else None,
        "origen_seleccion_original_real": origin_reason,
        "new_refund_id": new_refund.get("id") if new_refund else None,
        "new_refund_name": new_refund_name,
        "accion_creacion_odoo": accion_creacion_odoo,
        "accion_insert_mongo": None,
    }


def find_real_original_invoice_for_refund(execute_kw: Callable, refund: Dict) -> Tuple[Optional[Dict], str]:
    """Devuelve la factura original para crear la rectificativa. Prioridad: última factura del cliente
    (más reciente, con coherencia temporal: no usar factura muy antigua vs fecha de la rectificativa/efecto)."""
    partner_id = _m2o_id(refund.get("partner_id"))
    if not partner_id:
        return None, "sin_partner_en_refund"
    target_amount = abs(float(refund.get("amount_total") or 0.0))
    invoice_origin = _safe_str(refund.get("invoice_origin")).strip()
    payment_reference = _safe_str(refund.get("payment_reference")).strip()
    reversed_entry_id = _m2o_id(refund.get("reversed_entry_id"))
    original_fields = ["id", "name", "partner_id", "move_type", "state", "invoice_date", "amount_total", "invoice_line_ids", "journal_id", "company_id"]

    # 1) Prioridad: última factura más reciente del cliente (coherencia temporal: no asociar 2023 a efecto 2025).
    refund_date_str = refund.get("invoice_date")
    domain_latest = [
        ("partner_id", "=", partner_id),
        ("move_type", "=", "out_invoice"),
        ("state", "=", "posted"),
    ]
    if refund_date_str and MAX_DAYS_INVOICE_BEFORE_REFUND > 0:
        try:
            refund_dt = datetime.datetime.strptime(str(refund_date_str)[:10], "%Y-%m-%d").date()
            limit_date = refund_dt - datetime.timedelta(days=MAX_DAYS_INVOICE_BEFORE_REFUND)
            domain_latest.append(("invoice_date", ">=", limit_date.isoformat()))
        except (ValueError, TypeError):
            pass
    candidates_latest = odoo_search_read(
        execute_kw, "account.move", domain_latest,
        original_fields, limit=1, order="invoice_date desc, id desc"
    )
    if candidates_latest:
        return candidates_latest[0], "ultima_factura_cliente"

    # 2) Cadena reversed_entry_id sin límite de profundidad hasta la factura original.
    if reversed_entry_id:
        seen_ids = set()
        current_id = reversed_entry_id
        while current_id and current_id not in seen_ids:
            seen_ids.add(current_id)
            row = odoo_read(execute_kw, "account.move", [current_id], original_fields + ["reversed_entry_id"])
            if not row:
                break
            current = row[0]
            if current.get("move_type") == "out_invoice":
                if _m2o_id(current.get("partner_id")) == partner_id:
                    return current, "cadena_reversed_entry"
                break
            if current.get("move_type") != "out_refund":
                break
            current_id = _m2o_id(current.get("reversed_entry_id"))

    if invoice_origin:
        by_origin = odoo_search_read(execute_kw, "account.move", [
            ("name", "=", invoice_origin), ("partner_id", "=", partner_id),
            ("move_type", "=", "out_invoice"), ("state", "=", "posted"),
        ], original_fields, limit=1)
        if by_origin:
            return by_origin[0], "por_invoice_origin"

    if payment_reference:
        by_ref = odoo_search_read(execute_kw, "account.move", [
            ("name", "=", payment_reference), ("partner_id", "=", partner_id),
            ("move_type", "=", "out_invoice"), ("state", "=", "posted"),
        ], original_fields, limit=1)
        if by_ref:
            return by_ref[0], "por_payment_reference"

    if reversed_entry_id:
        reversed_row = odoo_read(execute_kw, "account.move", [reversed_entry_id], ["id", "name", "move_type"])
        if reversed_row and reversed_row[0].get("name"):
            wrong_name = reversed_row[0]["name"]
            by_wrong = odoo_search_read(execute_kw, "account.move", [
                ("name", "=", wrong_name), ("partner_id", "=", partner_id),
                ("move_type", "=", "out_invoice"), ("state", "=", "posted"),
            ], original_fields, limit=1)
            if by_wrong:
                return by_wrong[0], "por_nombre_reversed_entry"

    candidates = odoo_search_read(execute_kw, "account.move", [
        ("partner_id", "=", partner_id), ("move_type", "=", "out_invoice"), ("state", "=", "posted"),
    ], original_fields, limit=MAX_ORIGINAL_CANDIDATES, order="invoice_date desc, id desc")
    if not candidates:
        return None, "sin_facturas_out_invoice_partner"
    with_same = [c for c in candidates if abs(abs(float(c.get("amount_total") or 0.0)) - target_amount) <= 0.01]
    return (with_same[0], "fallback_match_importe") if with_same else (candidates[0], "fallback_mas_reciente")


def _get_move_lines_vals_from_original(execute_kw: Callable, original_move: Dict) -> List[Tuple[int, int, Dict]]:
    line_ids = original_move.get("invoice_line_ids") or []
    if not line_ids:
        return []
    lines = odoo_read(execute_kw, "account.move.line", line_ids, ["product_id", "name", "quantity", "price_unit", "discount", "tax_ids"])
    vals = []
    for line in lines:
        product_id = _m2o_id(line.get("product_id"))
        if not product_id:
            continue
        raw_taxes = line.get("tax_ids") or []
        tax_ids = []
        for t in raw_taxes:
            if isinstance(t, int):
                tax_ids.append(t)
            elif isinstance(t, (list, tuple)) and t:
                tax_ids.append(int(t[0]))
        vals.append((0, 0, {
            "product_id": product_id, "name": line.get("name") or "",
            "quantity": float(line.get("quantity") or 1.0), "price_unit": float(line.get("price_unit") or 0.0),
            "discount": float(line.get("discount") or 0.0), "tax_ids": [(6, 0, tax_ids)],
        }))
    return vals


def create_refund_from_original(
    execute_kw: Callable,
    incoherent_refund: Dict,
    original_invoice: Dict,
    deleted_refund_name: str,
) -> Tuple[Optional[Dict], str]:
    partner_id = _m2o_id(incoherent_refund.get("partner_id"))
    if not partner_id:
        return None, "error_sin_partner_plantilla"

    original_name = _safe_str(original_invoice.get("name"))
    original_id = original_invoice.get("id")
    if not original_id:
        return None, "error_sin_id_factura_original"

    journal_id = _m2o_id(incoherent_refund.get("journal_id")) or _m2o_id(original_invoice.get("journal_id"))
    if not journal_id:
        return None, "error_sin_journal"

    if DRY_RUN:
        return {"id": 0, "name": f"DRY_RUN_REFUND_{deleted_refund_name}"}, "creacion_dry_run"

    ref_text = f"Reversión de {original_name}, Devolución" if original_name else f"AUTO_FIX_{deleted_refund_name}"

    try:
        # Llamar al método server-side (rpc_create_refund en account.move).
        # Ejecuta create + action_post con ORM completo dentro de Odoo,
        # igual que traspaso_cuotas_mensuales. Computes, constraints y
        # payment_term lines se generan correctamente.
        result = execute_kw(
            "account.move", "rpc_create_refund",
            [original_id, partner_id, journal_id, ref_text],
        )
        if not isinstance(result, dict):
            return None, f"error_respuesta_inesperada: {result}"
        if result.get("error"):
            diag = result.get("diag")
            if diag:
                print(f"  [DIAG] Factura original: {diag.get('original_name')} state={diag.get('original_state')} "
                      f"date={diag.get('original_invoice_date')} date_due={diag.get('original_invoice_date_due')}")
                print(f"  [DIAG] Payment term original: {diag.get('original_payment_term')}")
                print(f"  [DIAG] Partner payment term: {diag.get('partner_property_payment_term')}")
                print(f"  [DIAG] Partner receivable account: {diag.get('partner_receivable_account')}")
                for ln in diag.get("lines_from_original", []):
                    print(f"  [DIAG]   line {ln.get('line_id')}: display_type={ln.get('display_type')} "
                          f"account={ln.get('account')} product={ln.get('product', '-')} skipped={ln.get('skipped')}")
            post_lines = result.get("post_create_lines")
            if post_lines:
                print(f"  [DIAG] Líneas del refund post-create:")
                for ln in post_lines:
                    print(f"  [DIAG]   line {ln.get('id')}: display_type={ln.get('display_type')} "
                          f"account={ln.get('account_code')}({ln.get('account_type')}) date_maturity={ln.get('date_maturity')}")
            return None, f"error_server_side: {result['error']}"
        status = result.get("status", "creada")
        return {"id": result["id"], "name": result["name"]}, status
    except Exception as exc:
        return None, f"error_creando_rectificativa: {exc}"


def get_partner_codigo_colegiado(execute_kw: Callable, partner_id: int) -> Optional[Union[int, str]]:
    rows = odoo_read(execute_kw, "res.partner", [partner_id], ["codigo_colegiado"])
    return rows[0].get("codigo_colegiado") if rows else None


# ---------------------------------------------------------------------------
# HELPERS MONGO (igual que XML-RPC)
# ---------------------------------------------------------------------------

def delete_refund_effects_in_mongo(col_efectos: Collection, refund_name: str) -> Tuple[int, str]:
    query = {"$or": [{"n_factura": refund_name}, {"n_efecto": refund_name}]}
    if DRY_RUN:
        return col_efectos.count_documents(query), "borrado_dry_run"
    return col_efectos.delete_many(query).deleted_count, "borrado"


def get_refund_effects_in_mongo(col_efectos: Collection, refund_name: str) -> List[Dict]:
    return list(col_efectos.find({"$or": [{"n_factura": refund_name}, {"n_efecto": refund_name}]}))


def mongo_effects_are_inconsistent(effects: List[Dict], expected_codigo_colegiado: Optional[Union[int, str]]) -> bool:
    expected = _normalize_codigo(expected_codigo_colegiado)
    if not effects:
        return True
    if not expected:
        return False
    for effect in effects:
        if _normalize_codigo(effect.get("codigo_colegiado")) and _normalize_codigo(effect.get("codigo_colegiado")) != expected:
            return True
    return False


def _effect_year_from_doc(doc: Dict) -> Optional[int]:
    """Extrae año de un efecto en Mongo (created_at, fecha_efecto, changed_at)."""
    for key in ("created_at", "fecha_efecto", "changed_at", "fecha_vencimiento"):
        val = doc.get(key)
        if not val:
            continue
        if hasattr(val, "year"):
            return val.year
        try:
            s = str(val)[:10]
            if len(s) >= 4:
                return int(s[:4])
        except (ValueError, TypeError):
            continue
    return None


def mongo_effects_temporal_alarm(effects: List[Dict], invoice_date_str: Optional[str]) -> bool:
    """Alarma: efecto en Mongo de un año no debería estar asociado a factura de año muy anterior (ej. efecto 2025 vs factura 2023)."""
    if not effects or not invoice_date_str:
        return False
    try:
        invoice_year = int(str(invoice_date_str)[:4])
    except (ValueError, TypeError):
        return False
    for effect in effects:
        effect_year = _effect_year_from_doc(effect)
        if effect_year is not None and invoice_year < effect_year - 1:
            return True
    return False


def _build_effect_from_template(template_effect: Optional[Dict], new_refund: Dict, codigo_colegiado: Optional[Union[int, str]]) -> Dict:
    now = _now()
    base = dict(template_effect) if template_effect else {
        "descripcion": "Devolucion Cuota", "importe": 0.0, "descuento": 0.0, "total": 0.0,
        "situacion": "pendiente", "comision_impagado": 0, "fecha_vencimiento": now,
        "referencia_curso": None, "tipo_efecto": "C", "reader_at": "",
    }
    if template_effect:
        base.pop("_id", None)
    base.update({
        "codigo_colegiado": codigo_colegiado, "n_factura": new_refund.get("name"), "n_efecto": new_refund.get("name"),
        "fecha_efecto": now, "fecha_hora_registro": now, "created_at": now, "changed_at": now, "id_pdf": new_refund.get("id"),
    })
    return base


def insert_new_effect_in_mongo(col_efectos: Collection, source_refund_name: str, new_refund: Dict, codigo_colegiado: Optional[Union[int, str]]) -> Tuple[bool, str]:
    template = col_efectos.find_one(
        {"$or": [{"n_factura": source_refund_name}, {"n_efecto": source_refund_name}]},
        sort=[("fecha_hora_registro", -1), ("created_at", -1), ("_id", -1)],
    )
    effect_doc = _build_effect_from_template(template, new_refund, codigo_colegiado)
    if DRY_RUN:
        return True, "insert_dry_run"
    try:
        col_efectos.insert_one(effect_doc)
        return True, "insertado"
    except Exception as exc:
        return False, f"error_insert_mongo: {exc}"


# ---------------------------------------------------------------------------
# PROCESO PRINCIPAL
# ---------------------------------------------------------------------------

def process() -> Dict[str, Any]:
    print("[PROCESO] Conectando a Odoo (JSON-RPC)...")
    uid, execute_kw = get_odoo_jsonrpc()
    print("[PROCESO] Conectando a Mongo...")
    mongo_uri, mongo_db_name = get_mongo_config_from_parametrizacion(execute_kw)
    client = MongoClient(mongo_uri)
    col_efectos = client[mongo_db_name][COL_EFECTOS]
    print(f"[PROCESO] Mongo conectado: {mongo_db_name}.{COL_EFECTOS}")

    summary = {
        "dry_run": DRY_RUN, "defer_odoo_delete": DEFER_ODOO_DELETE,
        "filter_year": FILTER_YEAR if FILTER_YEAR else None,
        "filter_codigos_colegiado": FILTER_CODIGOS_COLEGIADO if FILTER_CODIGOS_COLEGIADO else None,
        "max_dias_factura_antes_refund": MAX_DAYS_INVOICE_BEFORE_REFUND,
        "timestamp": _now().isoformat(),
        "mongo_db": mongo_db_name, "mongo_collection_efectos": COL_EFECTOS,
        "refunds_analizadas": 0, "refunds_incoherentes": 0, "refunds_incoherentes_por_odoo": 0,
        "refunds_incoherentes_por_mongo": 0, "refunds_incoherentes_borradas_odoo": 0,
        "refunds_incoherentes_error_borrado_odoo": 0, "efectos_borrados_mongo": 0,
        "rectificativas_recreadas_odoo": 0, "rectificativas_recreadas_ya_existian": 0,
        "rectificativas_recreadas_error_odoo": 0, "efectos_insertados_mongo": 0,
        "efectos_error_insert_mongo": 0, "grupos_duplicados": 0, "detalles": [], "errores": [],
    }

    if FILTER_YEAR:
        print(f"[PROCESO] Filtro activo: solo rectificativas del ano {FILTER_YEAR}")
    if FILTER_CODIGOS_COLEGIADO:
        print(f"[PROCESO] Filtro colegiados (pruebas): solo codigos {FILTER_CODIGOS_COLEGIADO}")
    print("[PROCESO] Buscando rectificativas incoherentes en Odoo...")
    incoherent_by_odoo, total_refunds = find_incoherent_refunds(execute_kw)
    summary["refunds_analizadas"] = total_refunds
    print(f"[PROCESO] Analizadas {total_refunds} rectificativas, {len(incoherent_by_odoo)} incoherentes por Odoo")

    if DRY_RUN:
        logger.warning("MODO DRY_RUN ACTIVO: no se ejecutaran cambios en Odoo ni Mongo.")
    if DEFER_ODOO_DELETE:
        logger.warning("MODO DEFER_ODOO_DELETE: se crean rectificativas primero, borrado en Odoo al final (para debug).")

    to_fix = []
    seen_refund_ids = set()
    for item in incoherent_by_odoo:
        to_fix.append(item)
        seen_refund_ids.add(item["refund"]["id"])
    summary["refunds_incoherentes_por_odoo"] = len(incoherent_by_odoo)

    if summary["refunds_analizadas"] > 0:
        print("[PROCESO] Comprobando incoherencias por Mongo...")
        fields = ["id", "name", "partner_id", "partner_codigo_colegiado", "state", "move_type", "amount_total", "journal_id", "reversed_entry_id", "invoice_origin", "payment_reference", "invoice_date"]
        limit = MAX_REFUNDS_TO_SCAN if MAX_REFUNDS_TO_SCAN > 0 else 0
        mongo_domain = [("move_type", "=", "out_refund"), ("state", "!=", "cancel")]
        if FILTER_YEAR:
            mongo_domain.extend([("invoice_date", ">=", f"{FILTER_YEAR}-01-01"), ("invoice_date", "<=", f"{FILTER_YEAR}-12-31")])
        all_refunds = odoo_search_read(execute_kw, "account.move", mongo_domain, fields, limit=limit, order="invoice_date desc, id desc")
        for refund in all_refunds:
            if refund["id"] in seen_refund_ids:
                continue
            effects = get_refund_effects_in_mongo(col_efectos, refund.get("name"))
            codigo_inconsistent = mongo_effects_are_inconsistent(effects, refund.get("partner_codigo_colegiado"))
            # Alarma temporal: efecto 2025 no debe estar asociado a factura 2023 (buscar otra factura).
            rev_id = _m2o_id(refund.get("reversed_entry_id"))
            invoice_date_for_alarm = None
            if rev_id and effects:
                row = odoo_read(execute_kw, "account.move", [rev_id], ["invoice_date", "move_type"])
                if row and row[0].get("move_type") == "out_invoice":
                    invoice_date_for_alarm = row[0].get("invoice_date")
            temporal_alarm = bool(invoice_date_for_alarm and mongo_effects_temporal_alarm(effects, invoice_date_for_alarm))
            if not codigo_inconsistent and not temporal_alarm:
                continue
            real_original, origin_reason = find_real_original_invoice_for_refund(execute_kw, refund)
            reason = "incoherencia_temporal_mongo" if (temporal_alarm and not codigo_inconsistent) else "inconsistencia_mongo"
            to_fix.append({"refund": refund, "original": real_original, "refund_chain_ids": [refund["id"]], "origin_source": origin_reason, "reason": reason})
            seen_refund_ids.add(refund["id"])
            summary["refunds_incoherentes_por_mongo"] += 1

    if FILTER_CODIGOS_COLEGIADO:
        codigos_set = set(FILTER_CODIGOS_COLEGIADO)
        to_fix = [it for it in to_fix if (it["refund"].get("partner_codigo_colegiado") or 0) in codigos_set]
        # Prioridad: 9769, 9339 primero (orden de FILTER_CODIGOS_COLEGIADO)
        def _priority_key(item):
            cc = item["refund"].get("partner_codigo_colegiado") or 0
            try:
                return FILTER_CODIGOS_COLEGIADO.index(cc)
            except ValueError:
                return len(FILTER_CODIGOS_COLEGIADO)
        to_fix.sort(key=_priority_key)
        print(f"[PROCESO] Filtro colegiados activo: solo {FILTER_CODIGOS_COLEGIADO} ({len(to_fix)} incoherentes)")

    if MAX_TO_PROCESS > 0:
        # Límite por colegiados: procesar como máximo N colegiados distintos, pero TODAS las
        # rectificativas de cada uno (la cadena de borrados no tiene límite por colegiado).
        allowed_cc = set()
        limited = []
        for item in to_fix:
            cc = item["refund"].get("partner_codigo_colegiado") or 0
            if len(allowed_cc) < MAX_TO_PROCESS:
                allowed_cc.add(cc)
            if cc in allowed_cc:
                limited.append(item)
        to_fix = limited
        print(f"[PROCESO] Limite por colegiados: {MAX_TO_PROCESS} -> {len(allowed_cc)} colegiados, {len(to_fix)} rectificativas a procesar")

    summary["refunds_incoherentes"] = len(to_fix)
    if not to_fix:
        print("[PROCESO] No hay rectificativas incoherentes que procesar.")
        return summary

    groups = group_refunds_by_duplicate(to_fix)
    print(f"\n[PROCESO] Rectificativas agrupadas por duplicado (partner+monto): {len(to_fix)} incoherentes en {len(groups)} grupos")
    print(f"[PROCESO] Iniciando procesamiento de {len(groups)} grupos (1 creacion por grupo, N borrados por grupo)...")
    deferred_odoo_delete_ids = []
    summary["grupos_duplicados"] = len(groups)

    for group_idx, group in enumerate(groups, 1):
        first_item = group[0]
        refund = first_item["refund"]
        refund_id = refund["id"]
        refund_name = refund.get("name")
        partner_id = _m2o_id(refund.get("partner_id"))
        partner_name = _m2o_name(refund.get("partner_id"))

        print(f"\n[GRUPO {group_idx}/{len(groups)}] {len(group)} duplicada(s) - Partner: {partner_name} | Monto: {refund.get('amount_total')} | Primera: {refund_name} (ID {refund_id})")

        if not partner_id:
            for item in group:
                r = item["refund"]
                detail = _detail_from_item(item, r, None, None, "saltada_sin_partner", None, None)
                summary["rectificativas_recreadas_error_odoo"] += 1
                summary["errores"].append({"etapa": "resolver_factura_real", "refund_id": r["id"], "refund_name": r.get("name"), "razon_inconsistencia": item.get("reason"), "error": "saltada_sin_partner"})
                summary["detalles"].append(detail)
            continue

        real_original, origin_reason = find_real_original_invoice_for_refund(execute_kw, refund)
        if not real_original:
            print(f"  [PASO 3] Sin factura original (origen: {origin_reason}) - se omite grupo")
            for item in group:
                r = item["refund"]
                detail = _detail_from_item(item, r, None, None, f"saltada_sin_factura_real:{origin_reason}", None, None)
                summary["rectificativas_recreadas_error_odoo"] += 1
                summary["errores"].append({"etapa": "resolver_factura_real", "refund_id": r["id"], "refund_name": r.get("name"), "razon_inconsistencia": item.get("reason"), "error": f"saltada_sin_factura_real:{origin_reason}"})
                summary["detalles"].append(detail)
            continue

        print(f"  [PASO 3] OK - Factura original: {real_original.get('name')} (origen: {origin_reason})")
        print(f"  [PASO 4] Creando UNA nueva rectificativa basada en {real_original.get('name')} para todo el grupo...")
        new_refund, create_msg = create_refund_from_original(execute_kw, refund, real_original, deleted_refund_name=refund_name)
        if create_msg not in ("creada", "creacion_dry_run", "ya_existia"):
            print(f"  [ERROR] Creacion fallida: {create_msg}")
            for item in group:
                r = item["refund"]
                detail = _detail_from_item(item, r, real_original, origin_reason, create_msg, None, None)
                summary["rectificativas_recreadas_error_odoo"] += 1
                summary["errores"].append({"etapa": "crear_refund_odoo", "refund_id": r["id"], "refund_name": r.get("name"), "razon_inconsistencia": item.get("reason"), "error": create_msg})
                summary["detalles"].append(detail)
            continue

        if create_msg in ("creada", "creacion_dry_run"):
            summary["rectificativas_recreadas_odoo"] += 1
        summary["rectificativas_recreadas_ya_existian"] += len(group) - 1 if create_msg in ("creada", "creacion_dry_run") else len(group)
        print(f"  [PASO 4] OK - Rectificativa: {create_msg} -> {new_refund.get('name') if new_refund else '?'}")

        partner_cc = get_partner_codigo_colegiado(execute_kw, partner_id)
        for item_idx, item in enumerate(group):
            r = item["refund"]
            rid = r["id"]
            rname = r.get("name")
            is_first = item_idx == 0
            accion_creacion = create_msg if is_first else "ya_existia"
            detail = _detail_from_item(item, r, real_original, origin_reason, accion_creacion, new_refund, new_refund.get("name") if new_refund else None)
            detail["original_real_id"] = real_original.get("id")
            detail["original_real_name"] = real_original.get("name")
            detail["origen_seleccion_original_real"] = origin_reason
            detail["new_refund_id"] = new_refund.get("id") if new_refund else None
            detail["new_refund_name"] = new_refund.get("name") if new_refund else None

            if not DEFER_ODOO_DELETE:
                ok_delete_odoo, delete_msg = delete_odoo_refund(execute_kw, rid)
                detail["accion_borrado_odoo"] = delete_msg
                if ok_delete_odoo:
                    summary["refunds_incoherentes_borradas_odoo"] += 1
                else:
                    summary["refunds_incoherentes_error_borrado_odoo"] += 1
                    logger.error("Error borrando refund en Odoo | Refund: %s (ID %s) | Razon: %s", rname, rid, delete_msg)
                    summary["errores"].append({"etapa": "borrar_refund_odoo", "refund_id": rid, "refund_name": rname, "razon_inconsistencia": item.get("reason"), "error": delete_msg})
                deleted_count, mongo_delete_msg = delete_refund_effects_in_mongo(col_efectos, rname)
                summary["efectos_borrados_mongo"] += deleted_count
                detail["accion_borrado_mongo"] = {"estado": mongo_delete_msg, "cantidad": deleted_count}

            ok_insert, insert_msg = insert_new_effect_in_mongo(col_efectos, rname, new_refund or {}, partner_cc)
            detail["accion_insert_mongo"] = insert_msg
            if ok_insert:
                summary["efectos_insertados_mongo"] += 1
            else:
                summary["efectos_error_insert_mongo"] += 1
                summary["errores"].append({"etapa": "insertar_efecto_mongo", "refund_id": rid, "refund_name": rname, "razon_inconsistencia": item.get("reason"), "error": insert_msg})

            if DEFER_ODOO_DELETE:
                deleted_count, mongo_delete_msg = delete_refund_effects_in_mongo(col_efectos, rname)
                summary["efectos_borrados_mongo"] += deleted_count
                detail["accion_borrado_mongo"] = {"estado": mongo_delete_msg, "cantidad": deleted_count}
                deferred_odoo_delete_ids.append(rid)

            summary["detalles"].append(detail)
        print(f"  [OK] Grupo procesado: 1 rectificativa nueva, {len(group)} rectificativas antiguas borradas/colas")

    if DEFER_ODOO_DELETE and deferred_odoo_delete_ids:
        print(f"\n[PROCESO] Fase final: borrando {len(deferred_odoo_delete_ids)} rectificativas incoherentes en Odoo...")
        for refund_id in deferred_odoo_delete_ids:
            print(f"  [BORRADO ODOO] Borrando refund ID {refund_id}...")
            ok_delete_odoo, delete_msg = delete_odoo_refund(execute_kw, refund_id)
            detail_for_id = next((d for d in summary["detalles"] if d.get("refund_id") == refund_id), None)
            if detail_for_id:
                detail_for_id["accion_borrado_odoo"] = delete_msg
            if ok_delete_odoo:
                summary["refunds_incoherentes_borradas_odoo"] += 1
                print(f"  [BORRADO ODOO] ID {refund_id}: OK")
            else:
                summary["refunds_incoherentes_error_borrado_odoo"] += 1
                print(f"  [BORRADO ODOO] ID {refund_id}: ERROR - {delete_msg}")
                summary["errores"].append({"etapa": "borrar_refund_odoo_diferido", "refund_id": refund_id, "refund_name": detail_for_id.get("refund_name", "?") if detail_for_id else "?", "razon_inconsistencia": detail_for_id.get("razon_inconsistencia") if detail_for_id else None, "error": delete_msg})

    print("\n[PROCESO] Proceso completado.")
    return summary


# ---------------------------------------------------------------------------
# SALIDAS
# ---------------------------------------------------------------------------

def save_summary_json(summary: Dict) -> str:
    os.makedirs(SUMMARY_DIR, exist_ok=True)
    path = os.path.join(SUMMARY_DIR, f"fix_incoherent_refunds_summary_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    return path


def save_summary_md(summary: Dict) -> str:
    os.makedirs(SUMMARY_DIR, exist_ok=True)
    path = os.path.join(SUMMARY_DIR, f"fix_incoherent_refunds_report_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Reparacion de Rectificativas Incoherentes (JSON-RPC)\n\n")
        f.write(f"- Fecha: {summary.get('timestamp')}\n")
        f.write(f"- Dry run: {summary.get('dry_run')}\n")
        f.write(f"- Defer Odoo delete: {summary.get('defer_odoo_delete')}\n")
        f.write(f"- Filtro ano: {summary.get('filter_year') or 'ninguno'}\n")
        f.write(f"- Mongo DB: {summary.get('mongo_db')}\n")
        f.write(f"- Coleccion efectos: {summary.get('mongo_collection_efectos')}\n\n")
        f.write("## Totales\n\n")
        for k, v in summary.items():
            if k not in ("detalles", "errores", "timestamp") and isinstance(v, (int, float, str, bool, type(None))):
                f.write(f"- {k}: {v}\n")
        if summary.get("errores"):
            f.write("\n## Errores\n\n")
            for err in summary["errores"]:
                f.write(f"- Refund `{err.get('refund_name')}` (ID {err.get('refund_id')}), etapa `{err.get('etapa')}`: {err.get('error')}\n")
        if summary.get("detalles"):
            f.write("\n## Detalles\n\n")
            for d in summary["detalles"]:
                f.write(f"### Refund `{d.get('refund_name')}` (ID {d.get('refund_id')})\n")
                f.write(f"- Borrado Odoo: `{_safe_str(d.get('accion_borrado_odoo'))}`\n")
                f.write(f"- Creacion Odoo: `{_safe_str(d.get('accion_creacion_odoo'))}`\n")
                f.write(f"- Insercion Mongo: `{_safe_str(d.get('accion_insert_mongo'))}`\n\n")
    return path


def main() -> None:
    logger.info("=" * 80)
    logger.info("REPARACION DE RECTIFICATIVAS INCOHERENTES (JSON-RPC)")
    logger.info("=" * 80)
    if DRY_RUN:
        logger.warning("DRY_RUN=True -> solo simulacion.")
    else:
        logger.warning("DRY_RUN=False -> se aplicaran cambios reales.")
    if DEFER_ODOO_DELETE:
        logger.warning("DEFER_ODOO_DELETE=True -> crear primero, borrar en Odoo al final.")

    summary = process()
    json_path = save_summary_json(summary)
    md_path = save_summary_md(summary)

    logger.info("=" * 80)
    logger.info("RESUMEN")
    logger.info("=" * 80)
    logger.info("Rectificativas analizadas:         %s", summary["refunds_analizadas"])
    logger.info("Incoherentes detectadas:           %s", summary["refunds_incoherentes"])
    logger.info("Incoherentes borradas Odoo:        %s", summary["refunds_incoherentes_borradas_odoo"])
    logger.info("Errores borrado Odoo:              %s", summary["refunds_incoherentes_error_borrado_odoo"])
    logger.info("Rectificativas recreadas Odoo:     %s", summary["rectificativas_recreadas_odoo"])
    logger.info("Efectos insertados Mongo:          %s", summary["efectos_insertados_mongo"])
    logger.info("JSON: %s", json_path)
    logger.info("Markdown: %s", md_path)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Fix incoherent refund invoices (JSON-RPC)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max colegiados a procesar (0 = todos). Por cada colegiado se procesan todas sus rectificativas.")
    parser.add_argument("--codigos", type=str, default=None,
                        help="Solo procesar estos codigos colegiado (ej: 9769,9339). Por defecto: 9769,9339")
    parser.add_argument("--all-colegiados", action="store_true",
                        help="Procesar todos los colegiados (ignorar filtro por codigo)")
    args = parser.parse_args()
    if args.limit > 0:
        MAX_TO_PROCESS = args.limit
    if args.all_colegiados:
        FILTER_CODIGOS_COLEGIADO.clear()
    elif args.codigos is not None:
        FILTER_CODIGOS_COLEGIADO[:] = [int(x.strip()) for x in args.codigos.split(",") if x.strip()]
    try:
        main()
    except Exception:
        logger.error("El script ha fallado por una excepcion no controlada:", exc_info=True)
        sys.exit(1)