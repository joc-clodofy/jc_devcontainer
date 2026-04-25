#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Reparacion de rectificativas incoherentes entre Odoo y MongoDB.

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
"""

import datetime
import json
import logging
import os
import re
import sys
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import xmlrpc.client
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
# Para debug: crear rectificativas primero y borrar en Odoo al final.
# INCOHERENT_REFUND_DEFER_ODOO_DELETE=0 para flujo normal (borrar antes de crear).
DEFER_ODOO_DELETE = os.environ.get(
    "INCOHERENT_REFUND_DEFER_ODOO_DELETE", "1"
).lower() in ("1", "true", "yes")
SUMMARY_DIR = os.environ.get("INCOHERENT_REFUND_SUMMARY_DIR", ".")
MAX_REFUNDS_TO_SCAN = int(os.environ.get("INCOHERENT_REFUND_SCAN_LIMIT", "0"))
MAX_ORIGINAL_CANDIDATES = int(
    os.environ.get("INCOHERENT_REFUND_ORIGINAL_CANDIDATES", "50")
)
# Solo procesar rectificativas de este ano. 0 = todas.
_filter_year = os.environ.get("INCOHERENT_REFUND_FILTER_YEAR", "2026")
FILTER_YEAR = int(_filter_year) if _filter_year else 0


# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

logger = logging.getLogger("fix_incoherent_refund_invoices")
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(
    logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s", "%Y-%m-%d %H:%M:%S")
)
logger.addHandler(console_handler)


# ---------------------------------------------------------------------------
# CONEXIONES
# ---------------------------------------------------------------------------


def get_odoo_models_proxy() -> Tuple[xmlrpc.client.ServerProxy, int]:
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        raise RuntimeError("No se pudo autenticar en Odoo. Revisa credenciales.")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return models, uid


def get_mongo_config_from_parametrizacion(
    models: xmlrpc.client.ServerProxy,
    uid: int,
) -> Tuple[str, str]:
    try:
        res = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
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
# HELPERS ODOO
# ---------------------------------------------------------------------------


def odoo_search_read(models, uid, model, domain, fields, **kwargs):
    """
    Wrapper para search_read que captura errores del servidor Odoo
    y acepta argumentos variables como 'limit' o 'order'.
    """
    try:
        # Extraemos parámetros de conexión globales (asegúrate de que existan)
        # Si estas variables no son globales, pásalas como argumentos.
        return models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, model, 'search_read',
            [domain], 
            {'fields': fields, **kwargs}
        )
    except xmlrpc.client.Fault as e:
        print("\n" + "!"*80)
        print("❌ ERROR CRÍTICO DESDE EL SERVIDOR ODOO (XML-RPC)")
        print("-" * 80)
        print(f"MENSAJE: {e.faultString}")
        print("-" * 80)
        print("CONSEJO: Probablemente necesites actualizar el módulo l10n_es_edi_verifactu")
        print("o el módulo 'account' en tu instancia de Odoo.")
        print("!"*80 + "\n")
        raise

def odoo_read(
    models: xmlrpc.client.ServerProxy,
    uid: int,
    model: str,
    ids: List[int],
    fields: List[str],
) -> List[Dict[str, Any]]:
    if not ids:
        return []
    return models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        model,
        "read",
        [ids],
        {"fields": fields},
    )


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


def _invoice_is_coherent(
    refund: Dict[str, Any], original: Optional[Dict[str, Any]]
) -> bool:
    if not original:
        return False
    return _m2o_id(refund.get("partner_id")) == _m2o_id(original.get("partner_id"))


def _safe_str(value: Any) -> str:
    return str(value) if value is not None else ""


def _now() -> datetime.datetime:
    return datetime.datetime.now().replace(microsecond=0)


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _normalize_codigo(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def find_incoherent_refunds(
    models: xmlrpc.client.ServerProxy,
    uid: int,
) -> Tuple[List[Dict[str, Any]], int]:
    fields = [
        "id",
        "name",
        "partner_id",
        "partner_codigo_colegiado",
        "move_type",
        "state",
        "invoice_date",
        "amount_total",
        "currency_id",
        "reversed_entry_id",
        "invoice_origin",
        "journal_id",
        "payment_reference",
        "ref",
        "display_name",
    ]
    limit = MAX_REFUNDS_TO_SCAN if MAX_REFUNDS_TO_SCAN > 0 else 0
    domain = [
        ("move_type", "=", "out_refund"),
        ("state", "!=", "cancel"),
    ]
    if FILTER_YEAR:
        domain.extend([
            ("invoice_date", ">=", f"{FILTER_YEAR}-01-01"),
            ("invoice_date", "<=", f"{FILTER_YEAR}-12-31"),
        ])
    refunds = odoo_search_read(
        models,
        uid,
        "account.move",
        domain,
        fields,
        limit=limit,
        order="invoice_date desc, id desc",
    )

    # Cache para no repetir lecturas de account.move en trazado de cadenas.
    move_cache: Dict[int, Dict[str, Any]] = {}
    move_fields = [
        "id",
        "name",
        "partner_id",
        "move_type",
        "state",
        "invoice_date",
        "amount_total",
        "invoice_line_ids",
        "journal_id",
        "reversed_entry_id",
        "invoice_origin",
        "payment_reference",
        "ref",
        "display_name",
    ]

    def get_move(move_id: int) -> Optional[Dict[str, Any]]:
        if not move_id:
            return None
        if move_id in move_cache:
            return move_cache[move_id]
        rows = odoo_read(models, uid, "account.move", [move_id], move_fields)
        move_cache[move_id] = rows[0] if rows else None
        return move_cache[move_id]

    # Cache de busqueda por nombre para localizar la original por invoice_origin/payment_reference.
    invoice_name_cache: Dict[str, Optional[Dict[str, Any]]] = {}

    def find_invoice_by_name(
        name: str,
        partner_id: Optional[int],
        any_partner: bool = False,
    ) -> Optional[Dict[str, Any]]:
        key = f"{name}|{partner_id or 0}|{int(any_partner)}"
        if key in invoice_name_cache:
            return invoice_name_cache[key]
        domain: List = [
            ("name", "=", name),
            ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"),
        ]
        if partner_id and not any_partner:
            domain.append(("partner_id", "=", partner_id))
        rows = odoo_search_read(
            models, uid, "account.move", domain, move_fields, limit=1
        )
        invoice_name_cache[key] = rows[0] if rows else None
        return invoice_name_cache[key]

    def extract_invoice_names(*texts: Optional[str]) -> List[str]:
        names: List[str] = []
        seen: Set[str] = set()
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

    def resolve_root_original(
        refund: Dict[str, Any],
    ) -> Tuple[Optional[Dict[str, Any]], List[int], str]:
        """
        Traza la cadena de rectificativas hasta la original:
          refund -> refund -> ... -> out_invoice
        """
        chain_ids: List[int] = [refund["id"]]
        seen: Set[int] = set(chain_ids)
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

        # Si no resolvio por cadena, intentar por referencia textual.
        partner_id = _m2o_id(refund.get("partner_id"))
        invoice_origin = _safe_str(refund.get("invoice_origin")).strip()
        payment_reference = _safe_str(refund.get("payment_reference")).strip()
        ref_text = _safe_str(refund.get("ref")).strip()
        display_name = _safe_str(refund.get("display_name")).strip()

        candidate_names: List[str] = []
        if invoice_origin:
            candidate_names.append(invoice_origin.upper())
        if payment_reference:
            candidate_names.append(payment_reference.upper())
        for inv_name in extract_invoice_names(
            invoice_origin, payment_reference, ref_text, display_name
        ):
            if inv_name not in candidate_names:
                candidate_names.append(inv_name)

        # 1) Buscar original del MISMO partner (caso coherente).
        for inv_name in candidate_names:
            by_same_partner = find_invoice_by_name(
                inv_name, partner_id, any_partner=False
            )
            if by_same_partner:
                return by_same_partner, chain_ids, f"texto_{inv_name}_mismo_partner"

        # 2) Si no existe para el mismo partner, buscar en cualquier partner
        #    para detectar mismatch (caso incoherente).
        for inv_name in candidate_names:
            by_any_partner = find_invoice_by_name(
                inv_name, partner_id, any_partner=True
            )
            if by_any_partner:
                return by_any_partner, chain_ids, f"texto_{inv_name}_otro_partner"

        return None, chain_ids, last_reason

    incoherent: List[Dict[str, Any]] = []
    for refund in refunds:
        original, chain_ids, origin_source = resolve_root_original(refund)

        # Regla conservadora: solo incoherencia automatica si la original esta
        # resuelta y el partner no coincide. Si no se resuelve original, no se
        # elimina automaticamente para evitar falsos positivos masivos.
        if original and not _invoice_is_coherent(refund, original):
            incoherent.append(
                {
                    "refund": refund,
                    "original": original,
                    "refund_chain_ids": chain_ids,
                    "origin_source": origin_source,
                    "reason": "partner_distinto_o_original_no_encontrada",
                }
            )
    return incoherent, len(refunds)


def delete_odoo_refund(
    models: xmlrpc.client.ServerProxy,
    uid: int,
    refund_id: int,
) -> Tuple[bool, str]:
    # Blindaje explicito: este flujo nunca debe borrar facturas originales.
    move_data = odoo_read(
        models,
        uid,
        "account.move",
        [refund_id],
        ["id", "name", "move_type", "state"],
    )
    if not move_data:
        return False, "error_borrado_odoo: refund_no_encontrada"

    move_type = move_data[0].get("move_type")
    if move_type != "out_refund":
        return False, f"bloqueado_no_out_refund:{move_type}"

    if DRY_RUN:
        return True, "borrado_dry_run"

    # En account.move normalmente hay que pasar a borrador antes de unlink.
    attempts = [
        ("button_draft", "unlink"),
        ("button_cancel", "button_draft", "unlink"),
    ]
    last_error = ""
    for step_group in attempts:
        try:
            for method in step_group:
                models.execute_kw(
                    ODOO_DB,
                    uid,
                    ODOO_PASSWORD,
                    "account.move",
                    method,
                    [[refund_id]],
                )
            return True, "borrado"
        except Exception as exc:
            last_error = str(exc)

    return False, f"error_borrado_odoo: {last_error}"


def find_real_original_invoice_for_refund(
    models: xmlrpc.client.ServerProxy,
    uid: int,
    refund: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], str]:
    partner_id = _m2o_id(refund.get("partner_id"))
    if not partner_id:
        return None, "sin_partner_en_refund"

    target_amount = abs(float(refund.get("amount_total") or 0.0))
    invoice_origin = _safe_str(refund.get("invoice_origin")).strip()
    payment_reference = _safe_str(refund.get("payment_reference")).strip()
    reversed_entry_id = _m2o_id(refund.get("reversed_entry_id"))

    original_fields = [
        "id",
        "name",
        "partner_id",
        "move_type",
        "state",
        "invoice_date",
        "amount_total",
        "invoice_line_ids",
        "journal_id",
        "company_id",
    ]

    # 0) Seguir cadena reversed_entry_id hasta llegar a out_invoice.
    #    Esto cubre casos de rectificativa de rectificativa.
    reversed_entry_id = _m2o_id(refund.get("reversed_entry_id"))
    if reversed_entry_id:
        seen_ids: Set[int] = set()
        current_id = reversed_entry_id
        while current_id and current_id not in seen_ids:
            seen_ids.add(current_id)
            row = odoo_read(
                models,
                uid,
                "account.move",
                [current_id],
                original_fields + ["reversed_entry_id"],
            )
            if not row:
                break
            current = row[0]
            move_type = current.get("move_type")
            if move_type == "out_invoice":
                if _m2o_id(current.get("partner_id")) == partner_id:
                    return current, "cadena_reversed_entry"
                break
            if move_type != "out_refund":
                break
            current_id = _m2o_id(current.get("reversed_entry_id"))

    # 1) Si invoice_origin existe, es la mejor pista textual.
    if invoice_origin:
        by_origin = odoo_search_read(
            models,
            uid,
            "account.move",
            [
                ("name", "=", invoice_origin),
                ("partner_id", "=", partner_id),
                ("move_type", "=", "out_invoice"),
                ("state", "=", "posted"),
            ],
            original_fields,
            limit=1,
        )
        if by_origin:
            return by_origin[0], "por_invoice_origin"

    # 2) payment_reference tambien suele apuntar a la factura real.
    if payment_reference:
        by_reference = odoo_search_read(
            models,
            uid,
            "account.move",
            [
                ("name", "=", payment_reference),
                ("partner_id", "=", partner_id),
                ("move_type", "=", "out_invoice"),
                ("state", "=", "posted"),
            ],
            original_fields,
            limit=1,
        )
        if by_reference:
            return by_reference[0], "por_payment_reference"

    # 3) Si apuntaba a otra factura por id, intentar recuperar una factura con
    #    el mismo nombre pero del partner correcto.
    if reversed_entry_id:
        reversed_row = odoo_read(
            models,
            uid,
            "account.move",
            [reversed_entry_id],
            ["id", "name", "move_type"],
        )
        if reversed_row and reversed_row[0].get("name"):
            wrong_name = reversed_row[0]["name"]
            by_wrong_name_same_partner = odoo_search_read(
                models,
                uid,
                "account.move",
                [
                    ("name", "=", wrong_name),
                    ("partner_id", "=", partner_id),
                    ("move_type", "=", "out_invoice"),
                    ("state", "=", "posted"),
                ],
                original_fields,
                limit=1,
            )
            if by_wrong_name_same_partner:
                return by_wrong_name_same_partner[0], "por_nombre_reversed_entry"

    # 4) Fallback: factura posted mas reciente del partner y mejor match por importe.
    fields = [
        "id",
        "name",
        "partner_id",
        "move_type",
        "state",
        "invoice_date",
        "amount_total",
        "invoice_line_ids",
        "journal_id",
    ]
    domain: List = [
        ("partner_id", "=", partner_id),
        ("move_type", "=", "out_invoice"),
        ("state", "=", "posted"),
    ]

    candidates = odoo_search_read(
        models,
        uid,
        "account.move",
        domain,
        fields,
        limit=MAX_ORIGINAL_CANDIDATES,
        order="invoice_date desc, id desc",
    )
    if not candidates:
        return None, "sin_facturas_out_invoice_partner"

    # Priorizar por match de importe; si no, la mas reciente.
    with_same_amount = [
        c
        for c in candidates
        if abs(abs(float(c.get("amount_total") or 0.0)) - target_amount) <= 0.01
    ]
    if with_same_amount:
        return with_same_amount[0], "fallback_match_importe"
    return candidates[0], "fallback_mas_reciente"


def _get_move_lines_vals_from_original(
    models: xmlrpc.client.ServerProxy,
    uid: int,
    original_move: Dict[str, Any],
) -> List[Tuple[int, int, Dict[str, Any]]]:
    """Obtiene line_vals desde la factura ORIGINAL (out_invoice), como traspaso_cuotas_mensuales."""
    line_ids = original_move.get("invoice_line_ids") or []
    if not line_ids:
        return []

    lines = odoo_read(
        models,
        uid,
        "account.move.line",
        line_ids,
        ["product_id", "name", "quantity", "price_unit", "discount", "tax_ids"],
    )
    vals: List[Tuple[int, int, Dict[str, Any]]] = []
    for line in lines:
        product_id = _m2o_id(line.get("product_id"))
        if not product_id:
            continue  # Saltar lineas sin producto (como traspaso)
        raw_taxes = line.get("tax_ids") or []
        tax_ids = []
        for t in raw_taxes:
            if isinstance(t, int):
                tax_ids.append(t)
            elif isinstance(t, (list, tuple)) and t:
                tax_ids.append(int(t[0]))
        line_vals = {
            "product_id": product_id,
            "name": line.get("name") or "",
            "quantity": float(line.get("quantity") or 1.0),
            "price_unit": float(line.get("price_unit") or 0.0),
            "discount": float(line.get("discount") or 0.0),
            "tax_ids": [(6, 0, tax_ids)],
        }
        vals.append((0, 0, line_vals))
    return vals


def create_refund_from_original(
    models: xmlrpc.client.ServerProxy,
    uid: int,
    incoherent_refund: Dict[str, Any],
    original_invoice: Dict[str, Any],
    deleted_refund_name: str,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Crea rectificativa basada en la factura ORIGINAL (out_invoice), como traspaso_cuotas_mensuales.
    La referencia es siempre la factura original, nunca la rectificativa incoherente.
    """
    partner_id = _m2o_id(incoherent_refund.get("partner_id"))
    if not partner_id:
        return None, "error_sin_partner_plantilla"

    original_name = _safe_str(original_invoice.get("name"))
    original_id = original_invoice.get("id")
    if not original_id:
        return None, "error_sin_id_factura_original"

    # Idempotencia como traspaso_cuotas_mensuales: buscar por reversed_entry_id
    existing = odoo_search_read(
        models,
        uid,
        "account.move",
        [
            ("reversed_entry_id", "=", original_id),
            ("partner_id", "=", partner_id),
            ("move_type", "=", "out_refund"),
            ("state", "!=", "cancel"),
        ],
        ["id", "name"],
        limit=1,
    )
    if existing:
        return {"id": existing[0]["id"], "name": existing[0]["name"]}, "ya_existia"

    journal_id = _m2o_id(incoherent_refund.get("journal_id")) or _m2o_id(
        original_invoice.get("journal_id")
    )
    if not journal_id:
        return None, "error_sin_journal"

    line_vals = _get_move_lines_vals_from_original(models, uid, original_invoice)
    if not line_vals:
        return None, "error_sin_lineas_original"

    if DRY_RUN:
        return {
            "id": 0,
            "name": f"DRY_RUN_REFUND_{deleted_refund_name}",
        }, "creacion_dry_run"

    invoice_date_str = datetime.date.today().isoformat()
    ref_text = f"Reversión de {original_name}, Devolución" if original_name else f"AUTO_FIX_{deleted_refund_name}"

    try:
        # Paso 1: Crear move header sin líneas
        vals = {
            "move_type": "out_refund",
            "partner_id": partner_id,
            "journal_id": journal_id,
            "invoice_date": invoice_date_str,
            "invoice_date_due": invoice_date_str,
            "invoice_origin": original_name or f"INV/{original_id}",
            "payment_reference": original_name or f"INV/{original_id}",
            "ref": ref_text,
        }
        new_id = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "account.move", "create", [vals],
        )
        # Paso 2: Añadir líneas
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "account.move", "write", [[new_id], {"invoice_line_ids": line_vals}],
        )
        # Paso 3: reversed_entry_id
        try:
            models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "account.move", "write", [[new_id], {"reversed_entry_id": original_id}],
            )
        except Exception:
            pass
        # Paso 4: Corregir líneas payment_term sin date_maturity
        move_data = odoo_read(models, uid, "account.move", [new_id], ["line_ids"])
        all_line_ids = (move_data[0].get("line_ids") or []) if move_data else []
        if all_line_ids:
            pt_lines = odoo_search_read(
                models, uid, "account.move.line",
                [("id", "in", all_line_ids), ("display_type", "=", "payment_term")],
                ["id", "date_maturity"],
            )
            to_fix = [ln["id"] for ln in pt_lines if not ln.get("date_maturity")]
            for line_id in to_fix:
                models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    "account.move.line", "write", [[line_id], {"date_maturity": invoice_date_str}],
                )
        # Paso 5: Validar
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "account.move", "action_post", [[new_id]],
        )
        new_rows = odoo_search_read(
            models, uid, "account.move",
            [("id", "=", new_id)], ["id", "name"], limit=1,
        )
        if new_rows:
            return {"id": new_rows[0]["id"], "name": new_rows[0]["name"]}, "creada"
        return {"id": new_id, "name": f"RINV/{new_id}"}, "creada"
    except Exception as exc:
        return None, f"error_creando_rectificativa: {exc}"


def get_partner_codigo_colegiado(
    models: xmlrpc.client.ServerProxy,
    uid: int,
    partner_id: int,
) -> Optional[Union[int, str]]:
    rows = odoo_read(models, uid, "res.partner", [partner_id], ["codigo_colegiado"])
    if rows:
        return rows[0].get("codigo_colegiado")
    return None


# ---------------------------------------------------------------------------
# HELPERS MONGO
# ---------------------------------------------------------------------------


def delete_refund_effects_in_mongo(
    col_efectos: Collection, refund_name: str
) -> Tuple[int, str]:
    query = {"$or": [{"n_factura": refund_name}, {"n_efecto": refund_name}]}
    if DRY_RUN:
        count = col_efectos.count_documents(query)
        return count, "borrado_dry_run"
    result = col_efectos.delete_many(query)
    return result.deleted_count, "borrado"


def get_refund_effects_in_mongo(
    col_efectos: Collection, refund_name: str
) -> List[Dict[str, Any]]:
    query = {"$or": [{"n_factura": refund_name}, {"n_efecto": refund_name}]}
    return list(col_efectos.find(query))


def mongo_effects_are_inconsistent(
    effects: List[Dict[str, Any]],
    expected_codigo_colegiado: Optional[Union[int, str]],
) -> bool:
    expected = _normalize_codigo(expected_codigo_colegiado)
    if not effects:
        # Sin efecto en Mongo se considera inconsistencia de sincronizacion.
        return True
    if not expected:
        return False
    for effect in effects:
        current = _normalize_codigo(effect.get("codigo_colegiado"))
        if current and current != expected:
            return True
    return False


def _build_effect_from_template(
    template_effect: Optional[Dict[str, Any]],
    new_refund: Dict[str, Any],
    codigo_colegiado: Optional[Union[int, str]],
) -> Dict[str, Any]:
    now = _now()
    if template_effect:
        base = dict(template_effect)
        base.pop("_id", None)
    else:
        base = {
            "descripcion": "Devolucion Cuota",
            "importe": 0.0,
            "descuento": 0.0,
            "total": 0.0,
            "situacion": "pendiente",
            "comision_impagado": 0,
            "fecha_vencimiento": now,
            "referencia_curso": None,
            "tipo_efecto": "C",
            "reader_at": "",
        }

    base.update(
        {
            "codigo_colegiado": codigo_colegiado,
            "n_factura": new_refund.get("name"),
            "n_efecto": new_refund.get("name"),
            "fecha_efecto": now,
            "fecha_hora_registro": now,
            "created_at": now,
            "changed_at": now,
            "id_pdf": new_refund.get("id"),
        }
    )
    return base


def insert_new_effect_in_mongo(
    col_efectos: Collection,
    source_refund_name: str,
    new_refund: Dict[str, Any],
    codigo_colegiado: Optional[Union[int, str]],
) -> Tuple[bool, str]:
    template_effect = col_efectos.find_one(
        {"$or": [{"n_factura": source_refund_name}, {"n_efecto": source_refund_name}]},
        sort=[("fecha_hora_registro", -1), ("created_at", -1), ("_id", -1)],
    )
    effect_doc = _build_effect_from_template(
        template_effect, new_refund, codigo_colegiado
    )

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
    print("[PROCESO] Conectando a Odoo...")
    models, uid = get_odoo_models_proxy()
    print("[PROCESO] Conectando a Mongo...")
    mongo_uri, mongo_db_name = get_mongo_config_from_parametrizacion(models, uid)
    client = MongoClient(mongo_uri)
    db: Database = client[mongo_db_name]
    col_efectos: Collection = db[COL_EFECTOS]
    print(f"[PROCESO] Mongo conectado: {mongo_db_name}.{COL_EFECTOS}")

    summary: Dict[str, Any] = {
        "dry_run": DRY_RUN,
        "defer_odoo_delete": DEFER_ODOO_DELETE,
        "filter_year": FILTER_YEAR if FILTER_YEAR else None,
        "timestamp": _now().isoformat(),
        "mongo_db": mongo_db_name,
        "mongo_collection_efectos": COL_EFECTOS,
        "refunds_analizadas": 0,
        "refunds_incoherentes": 0,
        "refunds_incoherentes_por_odoo": 0,
        "refunds_incoherentes_por_mongo": 0,
        "refunds_incoherentes_borradas_odoo": 0,
        "refunds_incoherentes_error_borrado_odoo": 0,
        "efectos_borrados_mongo": 0,
        "rectificativas_recreadas_odoo": 0,
        "rectificativas_recreadas_ya_existian": 0,
        "rectificativas_recreadas_error_odoo": 0,
        "efectos_insertados_mongo": 0,
        "efectos_error_insert_mongo": 0,
        "detalles": [],
        "errores": [],
    }

    if FILTER_YEAR:
        print(f"[PROCESO] Filtro activo: solo rectificativas del ano {FILTER_YEAR}")
    print("[PROCESO] Buscando rectificativas incoherentes en Odoo...")
    incoherent_by_odoo, total_refunds = find_incoherent_refunds(models, uid)
    summary["refunds_analizadas"] = total_refunds
    print(f"[PROCESO] Analizadas {total_refunds} rectificativas, {len(incoherent_by_odoo)} incoherentes por Odoo")

    if DRY_RUN:
        logger.warning(
            "MODO DRY_RUN ACTIVO: no se ejecutaran cambios en Odoo ni Mongo."
        )
    if DEFER_ODOO_DELETE:
        logger.warning(
            "MODO DEFER_ODOO_DELETE: se crean rectificativas primero, "
            "borrado en Odoo al final (para debug)."
        )
    logger.info("Rectificativas analizadas: %d", summary["refunds_analizadas"])
    logger.info("Rectificativas incoherentes por Odoo: %d", len(incoherent_by_odoo))

    # Ademas de incoherencia por Odoo, detectamos incoherencia por Mongo.
    to_fix: List[Dict[str, Any]] = []
    seen_refund_ids: Set[int] = set()

    for item in incoherent_by_odoo:
        to_fix.append(item)
        seen_refund_ids.add(item["refund"]["id"])
    summary["refunds_incoherentes_por_odoo"] = len(incoherent_by_odoo)

    if summary["refunds_analizadas"] > 0:
        print("[PROCESO] Comprobando incoherencias por Mongo...")
        fields = [
            "id",
            "name",
            "partner_id",
            "partner_codigo_colegiado",
            "state",
            "move_type",
            "amount_total",
            "journal_id",
            "reversed_entry_id",
            "invoice_origin",
            "payment_reference",
        ]
        limit = MAX_REFUNDS_TO_SCAN if MAX_REFUNDS_TO_SCAN > 0 else 0
        mongo_domain = [
            ("move_type", "=", "out_refund"),
            ("state", "!=", "cancel"),
        ]
        if FILTER_YEAR:
            mongo_domain.extend([
                ("invoice_date", ">=", f"{FILTER_YEAR}-01-01"),
                ("invoice_date", "<=", f"{FILTER_YEAR}-12-31"),
            ])
        all_refunds = odoo_search_read(
            models,
            uid,
            "account.move",
            mongo_domain,
            fields,
            limit=limit,
            order="invoice_date desc, id desc",
        )

        for refund in all_refunds:
            if refund["id"] in seen_refund_ids:
                continue
            refund_name = refund.get("name")
            effects = get_refund_effects_in_mongo(col_efectos, refund_name)
            mongo_bad = mongo_effects_are_inconsistent(
                effects,
                refund.get("partner_codigo_colegiado"),
            )
            if not mongo_bad:
                continue

            real_original, origin_reason = find_real_original_invoice_for_refund(
                models, uid, refund
            )
            to_fix.append(
                {
                    "refund": refund,
                    "original": real_original,
                    "refund_chain_ids": [refund["id"]],
                    "origin_source": origin_reason,
                    "reason": "inconsistencia_mongo",
                }
            )
            seen_refund_ids.add(refund["id"])
            summary["refunds_incoherentes_por_mongo"] += 1

    summary["refunds_incoherentes"] = len(to_fix)
    logger.info(
        "Rectificativas incoherentes total (Odoo + Mongo): %d",
        summary["refunds_incoherentes"],
    )

    if not to_fix:
        print("[PROCESO] No hay rectificativas incoherentes que procesar.")
        return summary

    print(f"\n[PROCESO] Iniciando procesamiento de {len(to_fix)} rectificativas incoherentes...")

    # Cuando DEFER_ODOO_DELETE: crear rectificativas primero, borrar en Odoo al final.
    deferred_odoo_delete_ids: List[int] = []

    for idx, item in enumerate(to_fix, 1):
        refund = item["refund"]
        original = item.get("original")
        refund_id = refund["id"]
        refund_name = refund.get("name")
        partner_id = _m2o_id(refund.get("partner_id"))
        partner_name = _m2o_name(refund.get("partner_id"))

        print(f"\n[PROCESO {idx}/{len(to_fix)}] Procesando refund {refund_name} (ID {refund_id}) - Partner: {partner_name}")

        detail: Dict[str, Any] = {
            "refund_id": refund_id,
            "refund_name": refund_name,
            "refund_partner_id": partner_id,
            "refund_partner_name": partner_name,
            "refund_codigo_colegiado": refund.get("partner_codigo_colegiado"),
            "original_id": original.get("id") if original else None,
            "original_name": original.get("name") if original else None,
            "original_partner_id": (
                _m2o_id(original.get("partner_id")) if original else None
            ),
            "original_partner_name": (
                _m2o_name(original.get("partner_id")) if original else None
            ),
            "razon_inconsistencia": item.get("reason"),
            "origen_resolucion_original": item.get("origin_source"),
            "cadena_refund_ids": item.get("refund_chain_ids", []),
            "accion_borrado_odoo": None,
            "accion_borrado_mongo": None,
            "original_real_id": None,
            "original_real_name": None,
            "origen_seleccion_original_real": None,
            "new_refund_id": None,
            "new_refund_name": None,
            "accion_creacion_odoo": None,
            "accion_insert_mongo": None,
        }

        # 1) Borrar refund incoherente en Odoo (solo si no DEFER_ODOO_DELETE)
        if not DEFER_ODOO_DELETE:
            print(f"  [PASO 1] Borrando refund {refund_name} en Odoo...")
            ok_delete_odoo, delete_msg = delete_odoo_refund(models, uid, refund_id)
            print(f"  [PASO 1] Resultado: {delete_msg}")
            detail["accion_borrado_odoo"] = delete_msg
            if ok_delete_odoo:
                summary["refunds_incoherentes_borradas_odoo"] += 1
            else:
                summary["refunds_incoherentes_error_borrado_odoo"] += 1
                err_log = f"Error borrando refund en Odoo | Refund: {refund_name} (ID {refund_id}) | Razon: {delete_msg}"
                logger.error(err_log)
                summary["errores"].append(
                    {
                        "etapa": "borrar_refund_odoo",
                        "refund_id": refund_id,
                        "refund_name": refund_name,
                        "razon_inconsistencia": item.get("reason"),
                        "error": delete_msg,
                    }
                )

        if not DEFER_ODOO_DELETE:
            # 2) Borrar efectos en Mongo para esa refund
            print(f"  [PASO 2] Borrando efectos en Mongo para {refund_name}...")
            deleted_count, mongo_delete_msg = delete_refund_effects_in_mongo(
                col_efectos, refund_name
            )
            print(f"  [PASO 2] Borrado Mongo: {deleted_count} efectos ({mongo_delete_msg})")
            summary["efectos_borrados_mongo"] += deleted_count
            detail["accion_borrado_mongo"] = {
                "estado": mongo_delete_msg,
                "cantidad": deleted_count,
            }

        # 3) Determinar factura original real del mismo colegiado (out_invoice)
        if not partner_id:
            print("  [SALTO] Sin partner - se omite creacion")
            detail["accion_creacion_odoo"] = "saltada_sin_partner"
            summary["rectificativas_recreadas_error_odoo"] += 1
            err_log = f"Saltada creacion Odoo (sin partner) | Refund: {refund_name} (ID {refund_id})"
            logger.error(err_log)
            summary["errores"].append(
                {
                    "etapa": "resolver_factura_real",
                    "refund_id": refund_id,
                    "refund_name": refund_name,
                    "razon_inconsistencia": item.get("reason"),
                    "error": "saltada_sin_partner",
                }
            )
            summary["detalles"].append(detail)
            continue

        real_original, origin_reason = find_real_original_invoice_for_refund(
            models,
            uid,
            refund,
        )
        if not real_original:
            print(f"  [PASO 3] Sin factura original (origen: {origin_reason}) - se omite creacion")
            detail["accion_creacion_odoo"] = f"saltada_sin_factura_real:{origin_reason}"
            summary["rectificativas_recreadas_error_odoo"] += 1
            err_log = f"Saltada creacion Odoo (sin factura real) | Refund: {refund_name} (ID {refund_id}) | Detalle: {origin_reason}"
            logger.error(err_log)
            summary["errores"].append(
                {
                    "etapa": "resolver_factura_real",
                    "refund_id": refund_id,
                    "refund_name": refund_name,
                    "razon_inconsistencia": item.get("reason"),
                    "error": f"saltada_sin_factura_real:{origin_reason}",
                }
            )
            summary["detalles"].append(detail)
            continue

        print(f"  [PASO 3] OK - Factura original: {real_original.get('name')} (origen: {origin_reason})")
        detail["original_real_id"] = real_original.get("id")
        detail["original_real_name"] = real_original.get("name")
        detail["origen_seleccion_original_real"] = origin_reason

        # 4) Crear nueva rectificativa en base a la factura real a revertir
        print(f"  [PASO 4] Creando nueva rectificativa basada en {real_original.get('name')}...")
        new_refund, create_msg = create_refund_from_original(
            models,
            uid,
            refund,
            real_original,
            deleted_refund_name=refund_name,
        )
        detail["accion_creacion_odoo"] = create_msg
        if create_msg in ("creada", "creacion_dry_run"):
            summary["rectificativas_recreadas_odoo"] += 1
        elif create_msg == "ya_existia":
            summary["rectificativas_recreadas_ya_existian"] += 1
        else:
            print(f"  [ERROR] Creacion fallida: {create_msg}")
            summary["rectificativas_recreadas_error_odoo"] += 1
            err_log = f"Error creando rectificativa en Odoo | Refund original: {refund_name} (ID {refund_id}) | Detalle: {create_msg}"
            logger.error(err_log)
            summary["errores"].append(
                {
                    "etapa": "crear_refund_odoo",
                    "refund_id": refund_id,
                    "refund_name": refund_name,
                    "razon_inconsistencia": item.get("reason"),
                    "error": create_msg,
                }
            )
            summary["detalles"].append(detail)
            continue

        print("  [PASO 4] OK - Rectificativa creada")
        detail["new_refund_id"] = new_refund.get("id") if new_refund else None
        detail["new_refund_name"] = new_refund.get("name") if new_refund else None
        print(f"  [PASO 4] Creacion: {create_msg} -> nueva refund: {detail.get('new_refund_name')}")

        # 5) Insertar efecto en Mongo para la nueva rectificativa
        print(f"  [PASO 5] Insertando efecto en Mongo para {detail.get('new_refund_name')}...")
        partner_cc = get_partner_codigo_colegiado(models, uid, partner_id)
        ok_insert, insert_msg = insert_new_effect_in_mongo(
            col_efectos=col_efectos,
            source_refund_name=refund_name,
            new_refund=new_refund or {},
            codigo_colegiado=partner_cc,
        )
        detail["accion_insert_mongo"] = insert_msg
        if ok_insert:
            summary["efectos_insertados_mongo"] += 1
        else:
            summary["efectos_error_insert_mongo"] += 1
            err_log = f"Error insertando efecto en Mongo | Refund: {refund_name} (ID {refund_id}) | Detalle: {insert_msg}"
            logger.error(err_log)
            summary["errores"].append(
                {
                    "etapa": "insertar_efecto_mongo",
                    "refund_id": refund_id,
                    "refund_name": refund_name,
                    "razon_inconsistencia": item.get("reason"),
                    "error": insert_msg,
                }
            )
        print(f"  [PASO 5] Insercion Mongo: {insert_msg}")

        # 6) Borrar efectos en Mongo (cuando DEFER: despues de crear e insertar)
        if DEFER_ODOO_DELETE:
            print(f"  [PASO 6] Borrando efectos en Mongo de refund antigua {refund_name}...")
            deleted_count, mongo_delete_msg = delete_refund_effects_in_mongo(
                col_efectos, refund_name
            )
            print(f"  [PASO 6] Borrado Mongo: {deleted_count} efectos ({mongo_delete_msg})")
            summary["efectos_borrados_mongo"] += deleted_count
            detail["accion_borrado_mongo"] = {
                "estado": mongo_delete_msg,
                "cantidad": deleted_count,
            }

        # 7) Borrar en Odoo al final (solo cuando DEFER_ODOO_DELETE)
        if DEFER_ODOO_DELETE:
            deferred_odoo_delete_ids.append(refund_id)
            print("  [PASO 7] Anadido a cola de borrado diferido en Odoo")

        print(f"  [OK] Refund {refund_name} procesada correctamente")
        summary["detalles"].append(detail)

    # Borrar en Odoo al final (modo DEFER_ODOO_DELETE)
    if DEFER_ODOO_DELETE and deferred_odoo_delete_ids:
        print(f"\n[PROCESO] Fase final: borrando {len(deferred_odoo_delete_ids)} rectificativas incoherentes en Odoo...")
        logger.info(
            "Borrando %d rectificativas incoherentes en Odoo (diferido al final)...",
            len(deferred_odoo_delete_ids),
        )
        for refund_id in deferred_odoo_delete_ids:
            print(f"  [BORRADO ODOO] Borrando refund ID {refund_id}...")
            ok_delete_odoo, delete_msg = delete_odoo_refund(models, uid, refund_id)
            detail_for_id = next(
                (d for d in summary["detalles"] if d.get("refund_id") == refund_id),
                None,
            )
            if detail_for_id:
                detail_for_id["accion_borrado_odoo"] = delete_msg
            if ok_delete_odoo:
                summary["refunds_incoherentes_borradas_odoo"] += 1
                print(f"  [BORRADO ODOO] ID {refund_id}: OK")
            else:
                summary["refunds_incoherentes_error_borrado_odoo"] += 1
                refund_name = (
                    detail_for_id.get("refund_name", "?") if detail_for_id else "?"
                )
                print(f"  [BORRADO ODOO] ID {refund_id}: ERROR - {delete_msg}")
                err_log = f"Error borrando refund en Odoo | Refund ID {refund_id} | Razon: {delete_msg}"
                logger.error(err_log)
                summary["errores"].append(
                    {
                        "etapa": "borrar_refund_odoo_diferido",
                        "refund_id": refund_id,
                        "refund_name": refund_name,
                        "razon_inconsistencia": (
                            detail_for_id.get("razon_inconsistencia")
                            if detail_for_id
                            else None
                        ),
                        "error": delete_msg,
                    }
                )

    print("\n[PROCESO] Proceso completado.")
    return summary


# ---------------------------------------------------------------------------
# SALIDAS
# ---------------------------------------------------------------------------


def save_summary_json(summary: Dict[str, Any]) -> str:
    os.makedirs(SUMMARY_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(SUMMARY_DIR, f"fix_incoherent_refunds_summary_{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    return path


def save_summary_md(summary: Dict[str, Any]) -> str:
    os.makedirs(SUMMARY_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(SUMMARY_DIR, f"fix_incoherent_refunds_report_{ts}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Reparacion de Rectificativas Incoherentes\n\n")
        f.write(f"- Fecha: {summary.get('timestamp')}\n")
        f.write(f"- Dry run: {summary.get('dry_run')}\n")
        f.write(f"- Defer Odoo delete: {summary.get('defer_odoo_delete')}\n")
        f.write(f"- Filtro ano: {summary.get('filter_year') or 'ninguno'}\n")
        f.write(f"- Mongo DB: {summary.get('mongo_db')}\n")
        f.write(f"- Coleccion efectos: {summary.get('mongo_collection_efectos')}\n\n")

        f.write("## Totales\n\n")
        f.write(f"- Rectificativas analizadas: {summary.get('refunds_analizadas')}\n")
        f.write(
            f"- Rectificativas incoherentes detectadas: {summary.get('refunds_incoherentes')}\n"
        )
        f.write(
            f"- Incoherentes por Odoo: {summary.get('refunds_incoherentes_por_odoo')}\n"
        )
        f.write(
            f"- Incoherentes por Mongo: {summary.get('refunds_incoherentes_por_mongo')}\n"
        )
        f.write(
            f"- Rectificativas incoherentes borradas en Odoo: {summary.get('refunds_incoherentes_borradas_odoo')}\n"
        )
        f.write(
            f"- Errores al borrar en Odoo: {summary.get('refunds_incoherentes_error_borrado_odoo')}\n"
        )
        f.write(
            f"- Efectos borrados en Mongo: {summary.get('efectos_borrados_mongo')}\n"
        )
        f.write(
            f"- Rectificativas recreadas en Odoo: {summary.get('rectificativas_recreadas_odoo')}\n"
        )
        f.write(
            f"- Rectificativas ya existentes: {summary.get('rectificativas_recreadas_ya_existian')}\n"
        )
        f.write(
            f"- Errores al recrear en Odoo: {summary.get('rectificativas_recreadas_error_odoo')}\n"
        )
        f.write(
            f"- Efectos insertados en Mongo: {summary.get('efectos_insertados_mongo')}\n"
        )
        f.write(
            f"- Errores insertando efecto en Mongo: {summary.get('efectos_error_insert_mongo')}\n\n"
        )

        if summary.get("errores"):
            f.write("## Errores\n\n")
            for err in summary["errores"]:
                f.write(
                    f"- Refund `{err.get('refund_name')}` (ID {err.get('refund_id')}), "
                    f"etapa `{err.get('etapa')}`, razon `{err.get('razon_inconsistencia')}`: "
                    f"{err.get('error')}\n"
                )
            f.write("\n")

        if summary.get("detalles"):
            f.write("## Detalles por refund incoherente\n\n")
            for d in summary["detalles"]:
                f.write(
                    f"### Refund `{d.get('refund_name')}` (ID {d.get('refund_id')})\n\n"
                )
                f.write(
                    f"- Partner refund: `{d.get('refund_partner_name')}` (ID {d.get('refund_partner_id')})\n"
                )
                f.write(
                    f"- Factura original apuntada: `{d.get('original_name')}` (ID {d.get('original_id')})\n"
                )
                f.write(f"- Razon inconsistencia: `{d.get('razon_inconsistencia')}`\n")
                f.write(
                    f"- Resolucion de original: `{d.get('origen_resolucion_original')}`\n"
                )
                f.write(
                    f"- Borrado Odoo: `{_safe_str(d.get('accion_borrado_odoo'))}`\n"
                )
                f.write(
                    f"- Borrado Mongo: `{_safe_str(d.get('accion_borrado_mongo'))}`\n"
                )
                f.write(
                    f"- Factura real a revertir: `{d.get('original_real_name')}` "
                    f"(ID {d.get('original_real_id')})\n"
                )
                f.write(
                    f"- Criterio de seleccion factura real: `{d.get('origen_seleccion_original_real')}`\n"
                )
                f.write(
                    f"- Nueva refund: `{d.get('new_refund_name')}` (ID {d.get('new_refund_id')})\n"
                )
                f.write(
                    f"- Creacion Odoo: `{_safe_str(d.get('accion_creacion_odoo'))}`\n"
                )
                f.write(
                    f"- Insercion Mongo: `{_safe_str(d.get('accion_insert_mongo'))}`\n\n"
                )
    return path


def main() -> None:
    logger.info("=" * 80)
    logger.info("REPARACION DE RECTIFICATIVAS INCOHERENTES ODOO + MONGO")
    logger.info("=" * 80)
    if DRY_RUN:
        logger.warning("DRY_RUN=True -> solo simulacion.")
    else:
        logger.warning("DRY_RUN=False -> se aplicaran cambios reales.")
    if DEFER_ODOO_DELETE:
        logger.warning(
            "DEFER_ODOO_DELETE=True -> crear primero, borrar en Odoo al final."
        )

    summary = process()
    json_path = save_summary_json(summary)
    md_path = save_summary_md(summary)

    logger.info("=" * 80)
    logger.info("RESUMEN")
    logger.info("=" * 80)
    logger.info("Rectificativas analizadas:         %s", summary["refunds_analizadas"])
    logger.info(
        "Incoherentes detectadas:           %s", summary["refunds_incoherentes"]
    )
    logger.info(
        "  - Por Odoo:                      %s",
        summary["refunds_incoherentes_por_odoo"],
    )
    logger.info(
        "  - Por Mongo:                     %s",
        summary["refunds_incoherentes_por_mongo"],
    )
    logger.info(
        "Incoherentes borradas Odoo:        %s",
        summary["refunds_incoherentes_borradas_odoo"],
    )
    logger.info(
        "Errores borrado Odoo:              %s",
        summary["refunds_incoherentes_error_borrado_odoo"],
    )
    logger.info(
        "Efectos borrados Mongo:            %s", summary["efectos_borrados_mongo"]
    )
    logger.info(
        "Rectificativas recreadas Odoo:     %s",
        summary["rectificativas_recreadas_odoo"],
    )
    logger.info(
        "Rectificativas ya existentes:      %s",
        summary["rectificativas_recreadas_ya_existian"],
    )
    logger.info(
        "Errores recreacion Odoo:           %s",
        summary["rectificativas_recreadas_error_odoo"],
    )
    logger.info(
        "Efectos insertados Mongo:          %s", summary["efectos_insertados_mongo"]
    )
    logger.info(
        "Errores insercion Mongo:           %s", summary["efectos_error_insert_mongo"]
    )
    
    total_errores = len(summary.get("errores", []))
    if total_errores > 0:
        logger.error("Errores totales:                   %s", total_errores)
        logger.error("=> Por favor, revisa el archivo Markdown/JSON generado o los logs anteriores para mas detalles.")
    else:
        logger.info("Errores totales:                   0")
        
    logger.info("JSON: %s", json_path)
    logger.info("Markdown: %s", md_path)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.error("El script ha fallado por una excepcion no controlada:", exc_info=True)
        sys.exit(1)