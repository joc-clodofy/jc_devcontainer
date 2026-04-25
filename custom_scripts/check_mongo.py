#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Script de Auditoría de Efectos (Borrados Lógicos vs Odoo)

Objetivo:
    - Recorrer efectos en Mongo que ya están marcados como borrados (delete=True).
    - Comprobar si la factura existe en Odoo (EN LOTE).
    - Verificar si el código de colegiado en Mongo coincide con el partner de la factura en Odoo.
    - Si COINCIDEN, significa que el registro se borró por error (Falso Positivo).
    - Para esos falsos positivos, revertir el borrado lógico en Mongo (delete=False).
    - Generar un reporte por consola y un archivo Markdown con los casos revisados.

Nota:
    - Este script AHORA modifica Mongo (revirtiendo delete=True a delete=False solo para los registros detectados como borrados incorrectos).
"""

import os
import sys
import json
import logging
import datetime
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import urllib.request
import xmlrpc.client

try:
    import paramiko

    if not hasattr(paramiko, "DSSKey"):
        class _DSSKeyPlaceholder:
            @staticmethod
            def from_private_key_file(*args, **kwargs):
                raise NotImplementedError("DSA no soportado; use password SSH")
        paramiko.DSSKey = _DSSKeyPlaceholder
    from sshtunnel import SSHTunnelForwarder
except ImportError:
    SSHTunnelForwarder = None
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database


# ---------------------------------------------------------------------------
# CONFIGURACIÓN
# ---------------------------------------------------------------------------

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
SUMMARY_DIR = os.environ.get("EFECTOS_SUMMARY_DIR", ".")

BATCH_SIZE = int(os.environ.get("EFECTOS_BATCH_SIZE", "2000"))
if BATCH_SIZE <= 0:
    BATCH_SIZE = 1000

PROGRESS_LOG_INTERVAL = int(os.environ.get("EFECTOS_PROGRESS_LOG_INTERVAL", "2000"))
FILTER_YEAR = 0

ODOO_RPC_TIMEOUT = int(os.environ.get("ODOO_RPC_TIMEOUT", "180"))
ODOO_RPC_RETRIES = int(os.environ.get("ODOO_RPC_RETRIES", "4"))
ODOO_RPC_RETRY_DELAY = int(os.environ.get("ODOO_RPC_RETRY_DELAY", "15"))

SCRIPT_START_TIME = time.time()

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

logger = logging.getLogger("audit_efectos")
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler(sys.stdout)
console_formatter = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
console_handler.setFormatter(console_formatter)
logger.addHandler(console_handler)

# ---------------------------------------------------------------------------
# JSON‑RPC ODOO
# ---------------------------------------------------------------------------

def _jsonrpc_call(service: str, method: str, args: List[Any]) -> Any:
    url = f"{ODOO_URL.rstrip('/')}/jsonrpc"
    payload = {
        "jsonrpc": "2.0", "method": "call",
        "params": {"service": service, "method": method, "args": args}, "id": 1,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
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
                logger.warning("Odoo RPC timeout/red (intento %s/%s): %s", intento + 1, ODOO_RPC_RETRIES - 1, e)
                time.sleep(ODOO_RPC_RETRY_DELAY)
            else:
                raise
    if last_error is not None:
        raise last_error

def get_odoo_jsonrpc() -> Tuple[int, Callable[..., Any]]:
    uid = _jsonrpc_call("common", "authenticate", [ODOO_DB, ODOO_USER, ODOO_PASSWORD, {}])
    if not uid:
        raise RuntimeError("No se pudo autenticar en Odoo.")
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
            {"fields": ["mongo_db_url", "mongo_db", "ssh_host", "ssh_user", "ssh_pass", "ssh_port", "mongo_port"], "limit": 1},
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
            ssh_username=ssh_user, ssh_password=ssh_pass, remote_bind_address=("127.0.0.1", mongo_port),
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

def odoo_invoices_exist_batch_json(execute_kw: Callable[..., Any], n_facturas: List[str]) -> Dict[str, Set[str]]:
    if not n_facturas:
        return {}
    domain = [
        ["name", "in", n_facturas],
    ]
    res = execute_kw("account.move", "search_read", [domain], {"fields": ["name", "partner_codigo_colegiado"]})
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
# LÓGICA PRINCIPAL LOTE (AUDITORÍA)
# ---------------------------------------------------------------------------

def process_efectos_audit() -> Dict[str, Any]:
    uid, execute_kw = get_odoo_jsonrpc()
    mongo_cfg = get_mongo_config_from_parametrizacion_json(execute_kw)
    mongo_uri, mongo_db_name, _tunnel = _start_ssh_tunnel_and_mongo_uri(mongo_cfg)

    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=20000, connectTimeoutMS=20000, uuidRepresentation="standard")
    client.admin.command("ping")
    db: Database = client[mongo_db_name]
    col_efectos: Collection = db[COL_EFECTOS]

    resumen: Dict[str, Any] = {
        "mongo_uri": mongo_uri,
        "mongo_db": mongo_db_name,
        "coleccion_efectos": COL_EFECTOS,
        "batch_size": BATCH_SIZE,
        "filter_year": FILTER_YEAR or None,
        "total_borrados_analizados": 0,
        "borrados_correctos": 0,      # Estaba en Mongo y NO en Odoo (o era de otro colegiado)
        "borrados_INCORRECTOS": 0,    # Estaba marcado delete=True, PERO SÍ existe en Odoo para el MISMO colegiado
        "efectos_sin_n_factura": 0,
        "errores_odoo": 0,
        "errores_mongo": 0,
        "detalles": {
            "borrados_incorrectos": [],  # Falsos positivos (también revertidos en Mongo)
            "sin_n_factura": [],
            "errores": [],
        },
    }

    # NUEVO FILTRO: Buscamos SOLAMENTE los que están marcados como borrados
    base_deleted_filter = {"delete": True}

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
        query = {"$and": [base_deleted_filter, year_filter]}
    else:
        query = base_deleted_filter

    total_candidatos = col_efectos.count_documents(query)
    logger.info("Total registros marcados como delete=True (Año %s): %d", FILTER_YEAR or "Todos", total_candidatos)

    if total_candidatos == 0:
        logger.info("No hay registros marcados como borrados para analizar.")
        return resumen

    cursor = col_efectos.find(query)
    batch_docs = []
    lote_idx = 1
    procesados_totales = 0

    def process_batch(docs: List[Dict[str, Any]]):
        nonlocal procesados_totales
        
        n_facturas_a_consultar = list({(d.get("n_factura") or "").strip() for d in docs if (d.get("n_factura") or "").strip()})
        facturas_existentes_en_odoo: Dict[str, Set[str]] = {}

        if n_facturas_a_consultar:
            try:
                facturas_existentes_en_odoo = odoo_invoices_exist_batch_json(execute_kw, n_facturas_a_consultar)
            except Exception as exc:
                resumen["errores_odoo"] += len(docs)
                logger.error("Error en Odoo para el lote %s. Error: %s", lote_idx, exc)
                return 
        
        for efecto in docs:
            efecto_id = str(efecto.get("_id"))
            n_factura = (efecto.get("n_factura") or "").strip()
            procesados_totales += 1
            resumen["total_borrados_analizados"] = procesados_totales

            detalle = {
                "_id": efecto_id,
                "n_factura": n_factura,
                "codigo_colegiado": efecto.get("codigo_colegiado"),
                "importe": efecto.get("importe"),
                "situacion": efecto.get("situacion"),
            }

            if not n_factura:
                resumen["efectos_sin_n_factura"] += 1
                continue

            ccs_odoo = facturas_existentes_en_odoo.get(n_factura, set())
            codigo_mongo = efecto.get("codigo_colegiado")
            codigo_mongo_norm = str(codigo_mongo).strip() if codigo_mongo is not None else ""

            # LÓGICA CORE DE AUDITORÍA + REPARACIÓN:
            if ccs_odoo and codigo_mongo_norm in ccs_odoo:
                # FALSO POSITIVO: En Mongo dice delete=True, pero en Odoo SÍ existe y es del mismo colegiado.
                resumen["borrados_INCORRECTOS"] += 1
                resumen["detalles"]["borrados_incorrectos"].append(detalle)
                logger.info(
                    "Borrado incorrecto detectado: n_factura=%s, codigo_colegiado=%s -> revertir delete en Mongo",
                    n_factura,
                    codigo_mongo_norm,
                )
                # Revertir borrado lógico en Mongo: delete=False
                try:
                    col_efectos.update_one(
                        {"_id": efecto["_id"]},
                        {"$set": {"delete": False}},
                    )
                except Exception as exc:
                    resumen["errores_mongo"] += 1
                    logger.error(
                        "Error revirtiendo delete para efecto %s (n_factura=%s): %s",
                        efecto_id,
                        n_factura,
                        exc,
                    )
                    resumen["detalles"]["errores"].append(
                        {
                            "_id": efecto_id,
                            "n_factura": n_factura,
                            "origen": "mongo_update",
                            "error": str(exc),
                        }
                    )
            else:
                # BORRADO CORRECTO: No existe en Odoo, o existe pero pertenece a OTRO colegiado.
                resumen["borrados_correctos"] += 1

        if procesados_totales % PROGRESS_LOG_INTERVAL == 0 or procesados_totales == total_candidatos:
            elapsed = time.time() - SCRIPT_START_TIME
            logger.info("Progreso: %s / %s analizados. Tiempo transcurrido: %.1f segs", procesados_totales, total_candidatos, elapsed)

    for efecto in cursor:
        batch_docs.append(efecto)
        if len(batch_docs) >= BATCH_SIZE:
            process_batch(batch_docs)
            batch_docs = []
            lote_idx += 1
            
    if batch_docs:
        process_batch(batch_docs)

    elapsed_total = time.time() - SCRIPT_START_TIME
    logger.info("Fin auditoría. Analizados: %d. Tiempo total: %.1f segundos", procesados_totales, elapsed_total)
    return resumen

def save_audit_md(summary: Dict[str, Any]) -> str:
    """Genera un Markdown con el detalle de los falsos positivos (Borrados Incorrectos)."""
    os.makedirs(SUMMARY_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(SUMMARY_DIR, f"auditoria_borrados_incorrectos_{ts}.md")

    detalles = summary["detalles"]["borrados_incorrectos"]

    with open(filename, "w", encoding="utf-8") as f:
        f.write("# Auditoría: Efectos Borrados Incorrectamente en Mongo\n\n")
        f.write("> **Alerta**: Los siguientes registros tienen `delete: True` en MongoDB, pero se encontró una factura válida en Odoo para el **mismo código de colegiado**.\n\n")
        f.write(f"- **Fecha Auditoría**: {datetime.datetime.now().isoformat()}\n")
        f.write(f"- **Total Anomalías**: {len(detalles)}\n")
        f.write(f"- **Año Filtrado**: {summary.get('filter_year') or 'Sin filtro'}\n\n")
        f.write("---\n\n")

        if not detalles:
            f.write("✅ **Todo correcto.** No se encontraron falsos positivos.\n")
        else:
            for d in detalles:
                f.write(f"### 📄 Factura: `{d.get('n_factura')}`\n")
                f.write(f"- **_id Mongo**: `{d.get('_id')}`\n")
                f.write(f"- **Código Colegiado**: `{d.get('codigo_colegiado')}`\n")
                f.write(f"- **Importe Mongo**: `{d.get('importe')}`\n")
                f.write(f"- **Situación**: `{d.get('situacion')}`\n\n")

    return filename

def main() -> None:
    logger.info("Iniciando MODO AUDITORÍA (Solo lectura)...")
    try:
        summary = process_efectos_audit()
    except Exception as exc:
        logger.error("Error crítico: %s", exc)
        raise

    md_path = save_audit_md(summary)

    logger.info("=" * 70)
    logger.info("RESULTADO DE AUDITORÍA: REGISTROS CON DELETE=TRUE")
    logger.info("=" * 70)
    logger.info("Borrados analizados:                     %s", summary["total_borrados_analizados"])
    logger.info("-" * 40)
    logger.info("Borrados CORRECTOS (No en Odoo):         %s", summary["borrados_correctos"])
    logger.info("Borrados INCORRECTOS (Falsos Positivos): %s", summary["borrados_INCORRECTOS"])
    logger.info("-" * 40)
    logger.info("Errores consulta Odoo:                   %s", summary["errores_odoo"])
    logger.info("=" * 70)
    
    if summary["borrados_INCORRECTOS"] > 0:
        logger.warning("⚠️ SE ENCONTRARON REGISTROS BORRADOS POR ERROR.")
    else:
        logger.info("✅ Todo parece estar en orden. No hay falsos positivos detectados.")
        
    logger.info("Reporte detallado Markdown guardado en:  %s", md_path)

if __name__ == "__main__":
    main()