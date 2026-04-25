#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Script de limpieza lógica de efectos en MongoDB basándose en facturas de Odoo.

Objetivo:
    - Recorrer efectos en Mongo por lotes.
    - Comprobar si la factura/número de efecto existe en Odoo (EN LOTE).
    - Si NO existe en Odoo -> marcar el efecto como borrado lógico en Mongo (EN LOTE)
      (delete=True, fecha_hora_registro=None).
    - Generar un resumen por pantalla y en un fichero de log/resumen (JSON).

Notas:
    - El script solo modifica Mongo; Odoo se utiliza únicamente en lectura.
    - Respetar DRY_RUN para simulaciones sin cambios reales.
"""

import os
import sys
import json
import logging
import datetime
import time
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import urllib.request
import xmlrpc.client

try:
    import paramiko

    if not hasattr(paramiko, "DSSKey"):
        # Dummy para que get_keys() no falle; auth por contraseña no usa claves DSA
        class _DSSKeyPlaceholder:
            @staticmethod
            def from_private_key_file(*args, **kwargs):
                raise NotImplementedError(
                    "DSA no soportado en esta versión de paramiko; use password SSH"
                )

        paramiko.DSSKey = _DSSKeyPlaceholder
    from sshtunnel import SSHTunnelForwarder
except ImportError:
    SSHTunnelForwarder = None
from bson import ObjectId
from pymongo import MongoClient, UpdateOne
from pymongo.collection import Collection
from pymongo.database import Database


# ---------------------------------------------------------------------------
# CONFIGURACIÓN
# ---------------------------------------------------------------------------

# Config XML‑RPC Odoo (usar mismos valores que AJUSTE_COLFISIO)
ODOO_URL = os.environ.get("ODOO_URL", "https://colfisio.v16.srv6.clodofy.net")
ODOO_DB = os.environ.get("ODOO_DB", "colfisio.v16.srv6.clodofy.cloud")
ODOO_USER = os.environ.get("ODOO_USER", "admin")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "0")

MONGO_URI_FALLBACK = os.environ.get(
    "MONGO_URI",
    "mongodb://anasinf_user:Y34CeEdJNjut3fQm3lOKaQwpbtnW3c7@localhost:27017/?authSource=admin&readPreference=primary&directConnection=true&ssl=false",
)
MONGO_DB_NAME_FALLBACK = os.environ.get("MONGO_DB_NAME", "anasinf")

COL_EFECTOS = os.environ.get("MONGO_COL_EFECTOS", "efectos")
COL_INSCRIPCIONES = os.environ.get("MONGO_COL_INSCRIPCIONES", "inscripciones_cursos")

SUMMARY_DIR = os.environ.get("EFECTOS_SUMMARY_DIR", ".")

DUPLICATE_KEY_FIELDS = ["importe", "codigo_colegiado", "situacion", "referencia_curso"]

DRY_RUN = os.environ.get("EFECTOS_DRY_RUN", "false").lower() in ("true", "1", "yes")

# Ahora el BATCH_SIZE se usa de verdad para agrupar consultas y escrituras
BATCH_SIZE = int(os.environ.get("EFECTOS_BATCH_SIZE", "2000"))
if BATCH_SIZE <= 0:
    BATCH_SIZE = 1000

PROGRESS_LOG_INTERVAL = int(os.environ.get("EFECTOS_PROGRESS_LOG_INTERVAL", "2000"))

FILTER_YEAR = int(os.environ.get("EFECTOS_FILTER_YEAR", "2026") or "0")

ODOO_RPC_TIMEOUT = int(os.environ.get("ODOO_RPC_TIMEOUT", "180"))
ODOO_RPC_RETRIES = int(os.environ.get("ODOO_RPC_RETRIES", "4"))
ODOO_RPC_RETRY_DELAY = int(os.environ.get("ODOO_RPC_RETRY_DELAY", "15"))

SCRIPT_START_TIME = time.time()


# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

logger = logging.getLogger("sync_efectos")
logger.setLevel(logging.INFO)

console_handler = logging.StreamHandler(sys.stdout)
console_formatter = logging.Formatter(
    "[%(asctime)s] %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)
console_handler.setFormatter(console_formatter)
logger.addHandler(console_handler)


# ---------------------------------------------------------------------------
# JSON‑RPC ODOO
# ---------------------------------------------------------------------------

def _jsonrpc_call(service: str, method: str, args: List[Any]) -> Any:
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
    last_error: Optional[BaseException] = None
    for intento in range(ODOO_RPC_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=ODOO_RPC_TIMEOUT) as resp:
                result = json.loads(resp.read().decode())
            if "error" in result:
                err = result["error"]
                msg = err.get("data", {}).get("message") or err.get("message", str(err))
                raise RuntimeError(f"Odoo JSON‑RPC error: {msg}")
            return result.get("result")
        except (TimeoutError, OSError) as e:
            last_error = e
            if intento < ODOO_RPC_RETRIES - 1:
                logger.warning(
                    "Odoo RPC timeout/red (intento %s/%s), reintento en %ss: %s",
                    intento + 1, ODOO_RPC_RETRIES - 1, ODOO_RPC_RETRY_DELAY, e,
                )
                time.sleep(ODOO_RPC_RETRY_DELAY)
            else:
                raise
    if last_error is not None:
        raise last_error
    return None

def get_odoo_jsonrpc() -> Tuple[int, Callable[..., Any]]:
    uid = _jsonrpc_call("common", "authenticate", [ODOO_DB, ODOO_USER, ODOO_PASSWORD, {}])
    if not uid:
        raise RuntimeError("No se pudo autenticar en Odoo. Revisa credenciales.")

    def execute_kw(model: str, method: str, args: List[Any], kwargs: Optional[Dict] = None) -> Any:
        call_args: List[Any] = [ODOO_DB, uid, ODOO_PASSWORD, model, method, args]
        if kwargs:
            call_args.append(kwargs)
        return _jsonrpc_call("object", "execute_kw", call_args)

    return uid, execute_kw

def get_mongo_config_from_parametrizacion_json(execute_kw: Callable[..., Any]) -> Dict[str, Any]:
    try:
        res = execute_kw(
            "parametrizacion", "search_read", [[]],
            {
                "fields": [
                    "mongo_db_url", "mongo_db", "ssh_host",
                    "ssh_user", "ssh_pass", "ssh_port", "mongo_port",
                ],
                "limit": 1,
            },
        )
    except Exception as exc:
        logger.error("Error parametrizacion Odoo, usando fallback: %s", exc)
        return {"mongo_db_url": MONGO_URI_FALLBACK, "mongo_db": MONGO_DB_NAME_FALLBACK, "ssh_port": "22", "mongo_port": 27017}

    if not res:
        return {"mongo_db_url": MONGO_URI_FALLBACK, "mongo_db": MONGO_DB_NAME_FALLBACK, "ssh_port": "22", "mongo_port": 27017}

    registro = res[0]
    return {
        "mongo_db_url": registro.get("mongo_db_url") or MONGO_URI_FALLBACK,
        "mongo_db": registro.get("mongo_db") or MONGO_DB_NAME_FALLBACK,
        "ssh_host": (registro.get("ssh_host") or "").strip() or None,
        "ssh_user": registro.get("ssh_user") or None,
        "ssh_pass": registro.get("ssh_pass") or None,
        "ssh_port": str(registro.get("ssh_port") or "22").strip() or "22",
        "mongo_port": int(registro.get("mongo_port") or 27017),
    }

def _start_ssh_tunnel_and_mongo_uri(config: Dict[str, Any]) -> Tuple[str, str, Optional[Any]]:
    mongo_db_url = (config.get("mongo_db_url") or "").strip() or MONGO_URI_FALLBACK
    mongo_db_name = config.get("mongo_db") or MONGO_DB_NAME_FALLBACK
    ssh_host = (config.get("ssh_host") or "").strip() or None
    mongo_port = int(config.get("mongo_port") or 27017)

    if not ssh_host or not SSHTunnelForwarder:
        return mongo_db_url, mongo_db_name, None

    try:
        ssh_user, ssh_pass, ssh_port = config.get("ssh_user") or "", config.get("ssh_pass") or "", int(config.get("ssh_port") or 22)
        server = SSHTunnelForwarder(
            ssh_host if ssh_port == 22 else (ssh_host, ssh_port),
            ssh_username=ssh_user,
            ssh_password=ssh_pass,
            remote_bind_address=("127.0.0.1", mongo_port),
        )
        server.start()
        uri_local = mongo_db_url.replace(str(mongo_port), str(server.local_bind_port))
        return uri_local, mongo_db_name, server
    except Exception as e:
        logger.warning("Error SSH (%s). Fallback a conexion directa.", e)
        return mongo_db_url, mongo_db_name, None


# ---------------------------------------------------------------------------
# UTILIDADES ODOO EN LOTE
# ---------------------------------------------------------------------------

def odoo_invoices_exist_batch_json(
    execute_kw: Callable[..., Any],
    n_facturas: List[str],
) -> Dict[str, Set[str]]:
    """
    Recibe una lista de números de factura y devuelve un dict:
        { name_factura: {codigo_colegiado_odoo, ...}, ... }

    Se limita a moves de tipo out_invoice/out_refund y state=posted.
    """
    if not n_facturas:
        return {}

    domain = [
        ["name", "in", n_facturas],
        ["move_type", "in", ["out_invoice", "out_refund"]],
        ["state", "=", "posted"],
    ]

    # Consultamos name y partner_codigo_colegiado para poder comparar con Mongo
    res = execute_kw(
        "account.move",
        "search_read",
        [domain],
        {"fields": ["name", "partner_codigo_colegiado"]},
    )

    mapping: Dict[str, Set[str]] = {}
    for record in res:
        name = (record.get("name") or "").strip()
        if not name:
            continue
        cc = record.get("partner_codigo_colegiado")
        cc_norm = str(cc).strip() if cc is not None else ""
        if name not in mapping:
            mapping[name] = set()
        mapping[name].add(cc_norm)
    return mapping


# ---------------------------------------------------------------------------
# LÓGICA PRINCIPAL LOTE (BATCH)
# ---------------------------------------------------------------------------

def process_efectos() -> Dict[str, Any]:
    uid, execute_kw = get_odoo_jsonrpc()

    mongo_cfg = get_mongo_config_from_parametrizacion_json(execute_kw)
    mongo_uri, mongo_db_name, _tunnel = _start_ssh_tunnel_and_mongo_uri(mongo_cfg)

    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=20000, connectTimeoutMS=20000, uuidRepresentation="standard")
    client.admin.command("ping")
    db: Database = client[mongo_db_name]
    col_efectos: Collection = db[COL_EFECTOS]

    resumen: Dict[str, Any] = {
        "dry_run": DRY_RUN,
        "mongo_uri": mongo_uri,
        "mongo_db": mongo_db_name,
        "coleccion_efectos": COL_EFECTOS,
        "batch_size": BATCH_SIZE,
        "filter_year": FILTER_YEAR or None,
        "total_efectos_candidatos": 0,
        "total_efectos_candidatos_filtrados": 0,
        "efectos_analizados": 0,
        "efectos_con_factura_odoo": 0,
        "efectos_marcados_delete": 0,
        "efectos_sin_n_factura": 0,
        "errores_odoo": 0,
        "errores_mongo": 0,
        "detalles": {
            "marcados_delete": [], "con_factura": [], "sin_n_factura": [], "errores": [],
        },
    }

    base_delete_filter = {"$or": [{"delete": {"$exists": False}}, {"delete": False}]}

    if FILTER_YEAR:
        start = datetime.datetime(FILTER_YEAR, 1, 1)
        end = datetime.datetime(FILTER_YEAR + 1, 1, 1)
        year_filter = {
            "$or": [
                {"fecha_efecto": {"$gte": start, "$lt": end}},
                {"created_at": {"$gte": start, "$lt": end}},
                {"fecha_vencimiento": {"$gte": start, "$lt": end}},
            ]
        }
        query = {"$and": [base_delete_filter, year_filter]}
    else:
        query = base_delete_filter

    total_candidatos = col_efectos.count_documents(query)
    resumen["total_efectos_candidatos"] = resumen["total_efectos_candidatos_filtrados"] = total_candidatos
    logger.info("Total candidatos: %d. Procesando en lotes de %d...", total_candidatos, BATCH_SIZE)

    cursor = col_efectos.find(query)
    batch_docs = []
    lote_idx = 1
    procesados_totales = 0

    def process_batch(docs: List[Dict[str, Any]]):
        nonlocal procesados_totales
        
        # 1. Extraer todas las facturas a consultar a Odoo
        n_facturas_a_consultar = list(
            {
                (d.get("n_factura") or "").strip()
                for d in docs
                if (d.get("n_factura") or "").strip()
            }
        )
        facturas_existentes_en_odoo: Dict[str, Set[str]] = {}

        if n_facturas_a_consultar:
            try:
                facturas_existentes_en_odoo = odoo_invoices_exist_batch_json(
                    execute_kw, n_facturas_a_consultar
                )
            except Exception as exc:
                resumen["errores_odoo"] += len(docs)
                logger.error("Error en Odoo para el lote %s. Error: %s", lote_idx, exc)
                for d in docs:
                    resumen["detalles"]["errores"].append(
                        {
                            "_id": str(d.get("_id")),
                            "origen": "odoo_batch",
                            "error": str(exc),
                        }
                    )
                return  # Saltamos el lote en caso de error masivo

        bulk_operations = []
        
        # 2. Analizar localmente contra los resultados del lote
        for efecto in docs:
            efecto_id = str(efecto.get("_id"))
            n_factura = (efecto.get("n_factura") or "").strip()
            procesados_totales += 1
            resumen["efectos_analizados"] = procesados_totales

            detalle = {
                "_id": efecto_id,
                "n_factura": n_factura,
                "codigo_colegiado": efecto.get("codigo_colegiado"),
                "importe": efecto.get("importe"),
                "situacion": efecto.get("situacion"),
                "referencia_curso": efecto.get("referencia_curso"),
            }

            if not n_factura:
                resumen["efectos_sin_n_factura"] += 1
                resumen["detalles"]["sin_n_factura"].append(detalle)
                continue

            # Comparar con Odoo: por número de factura y colegiado
            ccs_odoo = facturas_existentes_en_odoo.get(n_factura, set())
            codigo_mongo = efecto.get("codigo_colegiado")
            codigo_mongo_norm = (
                str(codigo_mongo).strip() if codigo_mongo is not None else ""
            )

            if ccs_odoo:
                # Hay factura(s) en Odoo con ese número
                if codigo_mongo_norm in ccs_odoo:
                    # Factura coherente para este colegiado -> conservar
                    resumen["efectos_con_factura_odoo"] += 1
                    resumen["detalles"]["con_factura"].append(detalle)
                    continue
                # Existe en Odoo pero para OTRO colegiado -> efecto incoherente, marcar delete
                logger.info(
                    "Efecto incoherente: n_factura=%s, codigo_mongo=%s, codigos_odoo=%s -> marcado delete",
                    n_factura,
                    codigo_mongo_norm,
                    ",".join(sorted(ccs_odoo)) or "-",
                )
            else:
                # No existe factura en Odoo con ese número -> incoherente también
                logger.info(
                    "Efecto sin factura en Odoo: n_factura=%s, codigo_mongo=%s -> marcado delete",
                    n_factura,
                    codigo_mongo_norm,
                )

            # Si llegamos aquí, NO es coherente con Odoo -> Marcado para borrado lógico
            resumen["efectos_marcados_delete"] += 1
            resumen["detalles"]["marcados_delete"].append(detalle)
            
            if not DRY_RUN:
                bulk_operations.append(
                    UpdateOne(
                        {"_id": efecto["_id"]},
                        {"$set": {"delete": True, "fecha_hora_registro": None}}
                    )
                )
            else:
                logger.debug("[DRY_RUN] Marcaría delete=True para: %s", n_factura)

        # 3. Ejecutar Bulk Write en Mongo
        if bulk_operations and not DRY_RUN:
            try:
                result = col_efectos.bulk_write(bulk_operations, ordered=False)
                logger.debug("Mongo BulkWrite completado. Modificados: %s", result.modified_count)
            except Exception as exc:
                resumen["errores_mongo"] += len(bulk_operations)
                logger.error("Error ejecutando BulkWrite en Mongo para el lote %s: %s", lote_idx, exc)

        if procesados_totales % PROGRESS_LOG_INTERVAL == 0 or procesados_totales == total_candidatos:
            elapsed = time.time() - SCRIPT_START_TIME
            logger.info("Progreso: %s / %s analizados. Tiempo transcurrido: %.1f segs", procesados_totales, total_candidatos, elapsed)

    # Iteramos el cursor e inyectamos a la funcion de lotes
    for efecto in cursor:
        batch_docs.append(efecto)
        if len(batch_docs) >= BATCH_SIZE:
            process_batch(batch_docs)
            batch_docs = []
            lote_idx += 1
            
    # Procesar residuos si la cantidad total no era múltiplo exacto de BATCH_SIZE
    if batch_docs:
        process_batch(batch_docs)

    elapsed_total = time.time() - SCRIPT_START_TIME
    logger.info("Fin. Efectos analizados: %d. Tiempo total: %.1f segundos", procesados_totales, elapsed_total)
    return resumen


def save_summary_to_file(summary: Dict[str, Any]) -> str:
    os.makedirs(SUMMARY_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(SUMMARY_DIR, f"efectos_sync_summary_{ts}.json")
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    return filename


def save_marked_delete_md(summary: Dict[str, Any]) -> str:
    """
    Genera un Markdown con el detalle de todos los efectos marcados con delete=True en Mongo.
    """
    os.makedirs(SUMMARY_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(SUMMARY_DIR, f"efectos_marked_delete_{ts}.md")

    detalles = (summary.get("detalles") or {}).get("marcados_delete") or []

    with open(filename, "w", encoding="utf-8") as f:
        f.write("# Efectos marcados delete=True en Mongo\n\n")
        f.write(f"- Fecha: {datetime.datetime.now().isoformat()}\n")
        f.write(f"- Total afectados: {len(detalles)}\n")
        f.write(f"- Año filtro: {summary.get('filter_year') or 'sin filtro'}\n\n")

        for d in detalles:
            f.write(f"## Efecto `{d.get('n_factura')}` (_id: {d.get('_id')})\n")
            f.write(f"- Codigo colegiado: `{d.get('codigo_colegiado')}`\n")
            f.write(f"- Importe: `{d.get('importe')}`\n")
            f.write(f"- Situacion: `{d.get('situacion')}`\n")
            f.write(f"- Referencia curso: `{d.get('referencia_curso')}`\n\n")

    return filename

def main() -> None:
    logger.info("Iniciando modo OPTIMIZADO: Conectando a Mongo y Odoo...")
    try:
        summary = process_efectos()
    except Exception as exc:
        logger.error("Error crítico: %s", exc)
        raise

    json_path = save_summary_to_file(summary)
    md_path = save_marked_delete_md(summary)

    logger.info("=" * 70)
    logger.info("RESUMEN DE LIMPIEZA LÓGICA DE EFECTOS (BATCHED)")
    logger.info("=" * 70)
    if summary.get("dry_run"):
        logger.warning("*** MODO DRY_RUN: No se realizaron actualizaciones ***")

    logger.info("Efectos candidatos:                      %s", summary["total_efectos_candidatos_filtrados"])
    logger.info("Efectos analizados:                      %s", summary["efectos_analizados"])
    logger.info("-" * 40)
    logger.info("Efectos CON factura en Odoo:             %s", summary["efectos_con_factura_odoo"])
    logger.info("Efectos SIN n_factura (omitidos):        %s", summary["efectos_sin_n_factura"])
    logger.info("Efectos MARCADOS delete=True:            %s", summary["efectos_marcados_delete"])
    logger.info("-" * 40)
    logger.info("Errores consulta Odoo:                   %s", summary["errores_odoo"])
    logger.info("Errores actualización Mongo:             %s", summary["errores_mongo"])
    logger.info("=" * 70)
    logger.info("Resumen JSON:                            %s", json_path)
    logger.info("Markdown afectados (delete=True):        %s", md_path)

if __name__ == "__main__":
    main()