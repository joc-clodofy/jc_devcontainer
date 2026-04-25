#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Script de limpieza lógica de efectos en MongoDB basándose en facturas de Odoo.

Objetivo:
    - Recorrer efectos en Mongo por lotes.
    - Comprobar si la factura/número de efecto existe en Odoo.
    - Si NO existe en Odoo -> marcar el efecto como borrado lógico en Mongo
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
from pymongo import MongoClient
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

# Config MongoDB (solo como *fallback* si no hay parametrización en Odoo;
# normalmente se sobrescribe con los valores de 'parametrizacion' en Odoo)
MONGO_URI_FALLBACK = os.environ.get(
    "MONGO_URI",
    "mongodb://anasinf_user:Y34CeEdJNjut3fQm3lOKaQwpbtnW3c7@localhost:27017/?authSource=admin&readPreference=primary&directConnection=true&ssl=false",
)
MONGO_DB_NAME_FALLBACK = os.environ.get("MONGO_DB_NAME", "anasinf")  # se sobreescribe con parametrización

# Nombre de colecciones usadas (las tablas/colecciones concretas sí vienen fijas)
COL_EFECTOS = os.environ.get("MONGO_COL_EFECTOS", "efectos")
COL_INSCRIPCIONES = os.environ.get("MONGO_COL_INSCRIPCIONES", "inscripciones_cursos")

# Ruta del fichero de resumen (se añade timestamp automáticamente)
SUMMARY_DIR = os.environ.get("EFECTOS_SUMMARY_DIR", ".")

# Campos clave para detectar duplicados
DUPLICATE_KEY_FIELDS = ["importe", "codigo_colegiado", "situacion", "referencia_curso"]

# Modo de ejecución: True = solo análisis sin marcar delete, False = ejecuta actualizaciones
DRY_RUN = os.environ.get("EFECTOS_DRY_RUN", "false").lower() in ("true", "1", "yes")

# Tamaño lógico de lote solo para logs/progreso (no limita el total procesado; 0 = sin lotes explícitos)
BATCH_SIZE = int(os.environ.get("EFECTOS_BATCH_SIZE", "2000"))

# Cada cuántos efectos loguear progreso, aunque no se haya completado lote (0 = desactivar)
PROGRESS_LOG_INTERVAL = int(os.environ.get("EFECTOS_PROGRESS_LOG_INTERVAL", "100"))

# Filtro por año de los efectos (por defecto, 2026 como has pedido; 0 = sin filtro)
FILTER_YEAR = int(os.environ.get("EFECTOS_FILTER_YEAR", "2026") or "0")

# Parámetros JSON‑RPC Odoo (alineados con AJUSTE_COLFISIO)
ODOO_RPC_TIMEOUT = int(os.environ.get("ODOO_RPC_TIMEOUT", "180"))
ODOO_RPC_RETRIES = int(os.environ.get("ODOO_RPC_RETRIES", "4"))
ODOO_RPC_RETRY_DELAY = int(os.environ.get("ODOO_RPC_RETRY_DELAY", "15"))

# Timestamp de inicio del script para medir tiempos de lote y totales
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
# CONEXIONES / HELPERS
# ---------------------------------------------------------------------------

def get_odoo_models_proxy() -> Tuple[xmlrpc.client.ServerProxy, int]:
    """
    Autentica contra Odoo y devuelve (proxy_models, uid).
    Lanza RuntimeError si la autenticación falla.
    """
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        raise RuntimeError("No se pudo autenticar en Odoo. Revisa URL/DB/usuario/password.")

    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return models, uid


def get_mongo_config_from_parametrizacion(
    models: xmlrpc.client.ServerProxy,
    uid: int,
) -> Tuple[str, str]:
    """
    Lee en Odoo (modelo parametrizacion) los datos de conexión a Mongo:
        - mongo_db_url
        - mongo_db

    Si no están configurados, utiliza los valores de fallback por entorno.
    """
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
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "No se ha podido leer la parametrizacion de Odoo para MongoDB, usando fallback: %s",
            exc,
        )
        return MONGO_URI_FALLBACK, MONGO_DB_NAME_FALLBACK

    if not res:
        logger.warning(
            "No existen registros en 'parametrizacion'; usando configuración Mongo de fallback."
        )
        return MONGO_URI_FALLBACK, MONGO_DB_NAME_FALLBACK

    registro = res[0]
    mongo_url = registro.get("mongo_db_url") or MONGO_URI_FALLBACK
    mongo_db_name = registro.get("mongo_db") or MONGO_DB_NAME_FALLBACK

    if not registro.get("mongo_db_url") or not registro.get("mongo_db"):
        logger.warning(
            "Campos 'mongo_db_url' o 'mongo_db' no están completos en parametrizacion; "
            "usando valores de entorno como fallback (url=%s, db=%s).",
            mongo_url,
            mongo_db_name,
        )

    logger.info("Conectando a Mongo con valores de parametrizacion: url=%s, db=%s", mongo_url, mongo_db_name)
    return mongo_url, mongo_db_name


# ---------------------------------------------------------------------------
# JSON‑RPC ODOO (helpers alternativos, usados por la lógica nueva)
# ---------------------------------------------------------------------------


def _jsonrpc_call(service: str, method: str, args: List[Any]) -> Any:
    """Llama al endpoint JSON‑RPC de Odoo con reintentos."""
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
                    intento + 1,
                    ODOO_RPC_RETRIES - 1,
                    ODOO_RPC_RETRY_DELAY,
                    e,
                )
                time.sleep(ODOO_RPC_RETRY_DELAY)
            else:
                raise
    if last_error is not None:
        raise last_error
    return None


def get_odoo_jsonrpc() -> Tuple[int, Callable[..., Any]]:
    """Autentica en Odoo por JSON‑RPC y devuelve (uid, execute_kw)."""
    uid = _jsonrpc_call("common", "authenticate", [ODOO_DB, ODOO_USER, ODOO_PASSWORD, {}])
    if not uid:
        raise RuntimeError("No se pudo autenticar en Odoo. Revisa credenciales.")

    def execute_kw(model: str, method: str, args: List[Any], kwargs: Optional[Dict] = None) -> Any:
        call_args: List[Any] = [ODOO_DB, uid, ODOO_PASSWORD, model, method, args]
        if kwargs:
            call_args.append(kwargs)
        return _jsonrpc_call("object", "execute_kw", call_args)

    return uid, execute_kw


def get_mongo_config_from_parametrizacion_json(
    execute_kw: Callable[..., Any],
) -> Dict[str, Any]:
    """
    Variante JSON‑RPC de get_mongo_config_from_parametrizacion de AJUSTE_COLFISIO.

    Lee en Odoo (modelo parametrizacion) los datos de conexión a Mongo:
        - mongo_db_url
        - mongo_db
        - ssh_host, ssh_user, ssh_pass, ssh_port, mongo_port

    Si no están configurados, utiliza los valores de fallback por entorno.
    """
    try:
        res = execute_kw(
            "parametrizacion",
            "search_read",
            [[]],
            {
                "fields": [
                    "mongo_db_url",
                    "mongo_db",
                    "ssh_host",
                    "ssh_user",
                    "ssh_pass",
                    "ssh_port",
                    "mongo_port",
                ],
                "limit": 1,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "No se ha podido leer la parametrizacion de Odoo para MongoDB (JSON‑RPC), usando fallback: %s",
            exc,
        )
        return {
            "mongo_db_url": MONGO_URI_FALLBACK,
            "mongo_db": MONGO_DB_NAME_FALLBACK,
            "ssh_host": None,
            "ssh_user": None,
            "ssh_pass": None,
            "ssh_port": "22",
            "mongo_port": 27017,
        }

    if not res:
        logger.warning(
            "No existen registros en 'parametrizacion'; usando configuración Mongo de fallback."
        )
        return {
            "mongo_db_url": MONGO_URI_FALLBACK,
            "mongo_db": MONGO_DB_NAME_FALLBACK,
            "ssh_host": None,
            "ssh_user": None,
            "ssh_pass": None,
            "ssh_port": "22",
            "mongo_port": 27017,
        }

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
    """
    Conexión a Mongo igual que en AJUSTE_COLFISIO (conexion.py):
    - Si hay ssh_host: túnel SSH al servidor y Mongo por puerto local.
    - Si no hay ssh_host o falla SSH: conexión directa con mongo_db_url.

    Devuelve (uri_para_pymongo, mongo_db_name, tunnel_o_None).
    """
    mongo_db_url = (config.get("mongo_db_url") or "").strip() or MONGO_URI_FALLBACK
    mongo_db_name = config.get("mongo_db") or MONGO_DB_NAME_FALLBACK
    ssh_host = (config.get("ssh_host") or "").strip() or None
    mongo_port = int(config.get("mongo_port") or 27017)

    if not ssh_host:
        logger.info("Sin Ssh Host en Parametrizacion; conexion directa a Mongo.")
        return mongo_db_url, mongo_db_name, None

    if not SSHTunnelForwarder:
        logger.warning(
            "Parametrizacion tiene Ssh Host pero 'sshtunnel' no esta instalado. Usando conexion directa."
        )
        return mongo_db_url, mongo_db_name, None

    ssh_user = config.get("ssh_user") or ""
    ssh_pass = config.get("ssh_pass") or ""
    ssh_port = int(config.get("ssh_port") or 22)

    try:
        logger.info(
            "Abriendo tunel SSH a %s (como en Parametrizacion / Conexiones Mongo)...",
            ssh_host,
        )
        server = SSHTunnelForwarder(
            ssh_host if ssh_port == 22 else (ssh_host, ssh_port),
            ssh_username=ssh_user,
            ssh_password=ssh_pass,
            remote_bind_address=("127.0.0.1", mongo_port),
        )
        server.start()
        # Igual que AJUSTE_COLFISIO: reemplazar puerto Mongo en la URL por el puerto local del túnel
        uri_local = mongo_db_url.replace(str(mongo_port), str(server.local_bind_port))
        return uri_local, mongo_db_name, server
    except (AttributeError, ImportError, OSError, Exception) as e:
        logger.warning(
            "Error al abrir tunel SSH (%s). Usando conexion directa a Mongo como fallback.", e
        )
        return mongo_db_url, mongo_db_name, None


# ---------------------------------------------------------------------------
# UTILIDADES ODOO
# ---------------------------------------------------------------------------

def odoo_search_one(
    models: xmlrpc.client.ServerProxy,
    uid: int,
    model: str,
    domain: List,
    fields: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """search_read limit=1 simplificado."""
    res = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        model,
        "search_read",
        [domain],
        {"fields": fields or ["id"], "limit": 1},
    )
    return res[0] if res else None


def odoo_invoice_exists(
    models: xmlrpc.client.ServerProxy,
    uid: int,
    n_factura: str,
) -> bool:
    """
    Devuelve True si existe en Odoo una factura/rectificativa con ese número.
    Solo lee Odoo, no crea ni modifica nada.
    """
    if not n_factura:
        return False

    domain = [
        ["name", "=", n_factura],
        ["move_type", "in", ["out_invoice", "out_refund"]],
        ["state", "!=", "cancel"],
    ]
    res = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "account.move",
        "search_read",
        [domain],
        {"fields": ["id"], "limit": 1},
    )
    return bool(res)


def odoo_invoice_exists_json(
    execute_kw: Callable[..., Any],
    n_factura: str,
) -> bool:
    """
    Versión JSON‑RPC: devuelve True si existe en Odoo una factura/rectificativa con ese número.
    """
    if not n_factura:
        return False

    domain = [
        ["name", "=", n_factura],
        ["move_type", "in", ["out_invoice", "out_refund"]],
        ["state", "!=", "cancel"],
    ]
    res = execute_kw(
        "account.move",
        "search_read",
        [domain],
        {"fields": ["id"], "limit": 1},
    )
    return bool(res)


def ensure_partner_for_efecto(
    models: xmlrpc.client.ServerProxy,
    uid: int,
    efecto: Dict[str, Any],
) -> Optional[int]:
    """Devuelve el ID del partner en Odoo según codigo_colegiado del efecto."""
    codigo = efecto.get("codigo_colegiado")
    if not codigo:
        logger.warning("Efecto sin 'codigo_colegiado', no se puede localizar partner en Odoo.")
        return None

    partner = odoo_search_one(
        models,
        uid,
        "res.partner",
        [["codigo_colegiado", "=", codigo]],
        ["id", "name"],
    )
    if not partner:
        logger.warning("No existe partner en Odoo con codigo_colegiado=%s", codigo)
        return None
    return partner["id"]


def ensure_product_for_efecto(
    models: xmlrpc.client.ServerProxy,
    uid: int,
    efecto: Dict[str, Any],
) -> Optional[int]:
    """Devuelve el ID del producto/curso según referencia_curso del efecto (si está disponible)."""
    referencia = efecto.get("referencia_curso")
    if not referencia:
        logger.warning("Efecto sin 'referencia_curso', se creará factura sin producto específico.")
        return None

    product = odoo_search_one(
        models,
        uid,
        "product.product",
        [["default_code", "=", referencia]],
        ["id", "name", "default_code"],
    )
    if not product:
        logger.warning(
            "No existe product.product con default_code=%s para el efecto %s",
            referencia,
            efecto.get("_id"),
        )
        return None
    return product["id"]


def get_journal_formacion(
    models: xmlrpc.client.ServerProxy,
    uid: int,
) -> Optional[int]:
    """
    Intenta obtener el diario de formación:
      1) Desde el modelo 'parametrizacion.diario_formacion2'
      2) Como fallback, un diario de ventas con código 'FO'
    """
    # 1) Parametrizacion
    param = odoo_search_one(
        models,
        uid,
        "parametrizacion",
        [],
        ["diario_formacion2"],
    )
    if param and param.get("diario_formacion2"):
        diario = param["diario_formacion2"]
        # diario_formacion2 viene como [id, name] o solo id dependiendo de la configuración
        if isinstance(diario, list):
            return diario[0]
        return diario

    # 2) Fallback: buscar journal 'FO'
    journal = odoo_search_one(
        models,
        uid,
        "account.journal",
        [["type", "=", "sale"], ["code", "=", "FO"]],
        ["id", "name", "code"],
    )
    if journal:
        return journal["id"]

    logger.error("No se ha podido determinar el diario de formación (diario_formacion2 / código 'FO').")
    return None


def ensure_invoice_for_efecto(
    models: xmlrpc.client.ServerProxy,
    uid: int,
    efecto: Dict[str, Any],
) -> Tuple[Optional[int], str]:
    """
    Asegura que existe una factura en Odoo para el efecto.

    Devuelve (invoice_id, estado):
        - estado in {"ok_existente", "creada", "error"}
    """
    n_factura = efecto.get("n_factura")
    if not n_factura:
        return None, "error"

    # 1) Buscar factura existente
    factura = odoo_search_one(
        models,
        uid,
        "account.move",
        [["name", "=", n_factura]],
        ["id", "name", "move_type", "payment_state"],
    )
    if factura:
        return factura["id"], "ok_existente"

    # 2) No existe -> intentar crear
    partner_id = ensure_partner_for_efecto(models, uid, efecto)
    if not partner_id:
        return None, "error"

    journal_id = get_journal_formacion(models, uid)
    if not journal_id:
        return None, "error"

    product_id = ensure_product_for_efecto(models, uid, efecto)

    # Determinar tipo de factura (aproximado): si empieza por 'R', asumir rectificativa
    move_type = "out_refund" if str(n_factura).upper().startswith("R") else "out_invoice"

    fecha_efecto = efecto.get("fecha_efecto") or efecto.get("created_at")
    if isinstance(fecha_efecto, datetime.datetime):
        invoice_date = fecha_efecto.date().isoformat()
    else:
        invoice_date = datetime.date.today().isoformat()

    importe = float(efecto.get("importe") or 0.0)
    descripcion = efecto.get("descripcion") or "Efecto sincronizado desde Mongo"

    line_vals: Dict[str, Any] = {
        "name": descripcion,
        "quantity": 1.0,
        "price_unit": importe,
    }
    if product_id:
        line_vals["product_id"] = product_id

    vals = {
        "move_type": move_type,
        "name": n_factura,  # Forzar el número para alinearlo con Mongo
        "partner_id": partner_id,
        "invoice_date": invoice_date,
        "journal_id": journal_id,
        "invoice_line_ids": [(0, 0, line_vals)],
    }

    try:
        invoice_id = models.execute_kw(
            ODOO_DB,
            uid,
            ODOO_PASSWORD,
            "account.move",
            "create",
            [vals],
        )
        logger.info(
            "Factura creada en Odoo para efecto %s: n_factura=%s, id=%s",
            efecto.get("_id"),
            n_factura,
            invoice_id,
        )
        return invoice_id, "creada"
    except Exception as exc:  # noqa: BLE001
        logger.error("Error creando factura en Odoo para efecto %s: %s", efecto.get("_id"), exc)
        return None, "error"


# ---------------------------------------------------------------------------
# DETECCIÓN DE DUPLICADOS
# ---------------------------------------------------------------------------

def build_duplicate_key(efecto: Dict[str, Any]) -> Tuple:
    """
    Construye una clave de tupla para agrupar efectos duplicados.
    
    Campos clave:
        - importe (normalizado a float con 2 decimales)
        - codigo_colegiado
        - situacion
        - referencia_curso
    """
    # Normalizar importe a float con 2 decimales para evitar falsos negativos
    importe_raw = efecto.get("importe")
    try:
        importe = round(float(importe_raw or 0), 2)
    except (ValueError, TypeError):
        importe = 0.0

    codigo_colegiado = str(efecto.get("codigo_colegiado") or "").strip().upper()
    situacion = str(efecto.get("situacion") or "").strip().upper()
    referencia_curso = str(efecto.get("referencia_curso") or "").strip().upper()

    return (importe, codigo_colegiado, situacion, referencia_curso)


def get_efecto_timestamp(efecto: Dict[str, Any]) -> datetime.datetime:
    """
    Obtiene el timestamp más relevante del efecto para determinar antigüedad.
    Prioridad: fecha_efecto > created_at > _id (si es ObjectId)
    """
    # Intentar fecha_efecto
    fecha = efecto.get("fecha_efecto")
    if isinstance(fecha, datetime.datetime):
        return fecha

    # Intentar created_at
    created = efecto.get("created_at")
    if isinstance(created, datetime.datetime):
        return created

    # Intentar extraer del ObjectId (contiene timestamp de creación)
    oid = efecto.get("_id")
    if isinstance(oid, ObjectId):
        return oid.generation_time.replace(tzinfo=None)

    # Fallback: fecha muy antigua
    return datetime.datetime(1970, 1, 1)


def find_duplicates(
    efectos: List[Dict[str, Any]],
) -> Dict[Tuple, List[Dict[str, Any]]]:
    """
    Agrupa efectos por clave de duplicado y devuelve solo los grupos con más de 1 elemento.
    """
    grupos: Dict[Tuple, List[Dict[str, Any]]] = defaultdict(list)
    
    for efecto in efectos:
        key = build_duplicate_key(efecto)
        grupos[key].append(efecto)

    # Filtrar solo grupos con duplicados (más de 1 efecto)
    return {k: v for k, v in grupos.items() if len(v) > 1}


def select_efecto_to_keep(efectos: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    De una lista de efectos duplicados, selecciona el que se debe conservar.
    
    Criterios de selección (en orden de prioridad):
        1. El que tiene factura asociada (n_factura no vacío)
        2. El más reciente por timestamp
    """
    # Separar los que tienen factura
    con_factura = [e for e in efectos if e.get("n_factura")]

    # Si hay alguno con factura, elegir el más reciente de esos
    if con_factura:
        return max(con_factura, key=get_efecto_timestamp)

    # Si ninguno tiene factura, elegir el más reciente de todos
    return max(efectos, key=get_efecto_timestamp)


# ---------------------------------------------------------------------------
# VALIDACIÓN MEJORADA DE INSCRIPCIONES
# ---------------------------------------------------------------------------

def build_inscripcion_match_criteria(efecto: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Construye múltiples criterios de búsqueda para encontrar inscripciones relacionadas.
    
    Busca por combinaciones de:
        - id + codigo_colegiado (criterio original)
        - referencia_curso + codigo_colegiado + importe (criterio ampliado)
        - n_factura si existe
    """
    criterios = []
    
    # Criterio 1: id + codigo_colegiado (original)
    if efecto.get("id") and efecto.get("codigo_colegiado"):
        criterios.append({
            "id": efecto.get("id"),
            "codigo_colegiado": efecto.get("codigo_colegiado"),
        })

    # Criterio 2: referencia_curso + codigo_colegiado + importe
    if efecto.get("referencia_curso") and efecto.get("codigo_colegiado"):
        criterio_ampliado = {
            "referencia_curso": efecto.get("referencia_curso"),
            "codigo_colegiado": efecto.get("codigo_colegiado"),
        }
        # Añadir importe si está disponible (con tolerancia)
        if efecto.get("importe") is not None:
            try:
                importe = float(efecto.get("importe"))
                # Usamos $gte y $lte para tolerancia de centavos
                criterio_ampliado["importe"] = {
                    "$gte": importe - 0.01,
                    "$lte": importe + 0.01,
                }
            except (ValueError, TypeError):
                pass
        criterios.append(criterio_ampliado)

    # Criterio 3: por n_factura si existe
    if efecto.get("n_factura"):
        criterios.append({"n_factura": efecto.get("n_factura")})

    return criterios


def has_related_inscripcion(
    col_inscripciones: Collection,
    efecto: Dict[str, Any],
) -> bool:
    """
    Verifica si el efecto tiene alguna inscripción relacionada en Mongo
    usando múltiples criterios de búsqueda.
    """
    criterios = build_inscripcion_match_criteria(efecto)
    
    if not criterios:
        # Sin criterios válidos, consideramos huérfano
        return False

    # Usar $or para buscar por cualquiera de los criterios
    query = {"$or": criterios} if len(criterios) > 1 else criterios[0]
    
    return col_inscripciones.count_documents(query, limit=1) > 0


# ---------------------------------------------------------------------------
# UTILIDAD: AÑO DEL EFECTO (PARA FILTRAR 2026)
# ---------------------------------------------------------------------------


def get_effect_year(efecto: Dict[str, Any]) -> Optional[int]:
    """
    Intenta obtener el año del efecto a partir de sus campos de fecha más habituales.
    Prioridad: fecha_efecto > created_at > fecha_vencimiento.
    """
    fecha = (
        efecto.get("fecha_efecto")
        or efecto.get("created_at")
        or efecto.get("fecha_vencimiento")
    )
    if isinstance(fecha, datetime.datetime):
        return fecha.year
    if not fecha:
        return None
    try:
        # Soporta strings tipo '2026-01-15...' o similares
        return int(str(fecha)[:4])
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# LÓGICA PRINCIPAL
# ---------------------------------------------------------------------------

def process_efectos() -> Dict[str, Any]:
    """
    Procesa efectos en Mongo y marca con borrado lógico aquellos cuya factura
    no existe en Odoo.

    Lógica:
        - Conectar a Odoo vía JSON‑RPC.
        - Leer configuración de Mongo (incluyendo SSH) desde Odoo (modelo parametrizacion),
          igual que en AJUSTE_COLFISIO.
        - Abrir túnel SSH si procede y conectarse a Mongo con esa URI.
        - Recorrer efectos (delete != True) por lotes.
        - Para cada efecto, comprobar si existe factura en Odoo.
        - Si no existe, marcar delete=True y fecha_hora_registro=None.
    """
    # Conectar a Odoo por JSON‑RPC (solo lectura) y obtener configuración de Mongo
    uid, execute_kw = get_odoo_jsonrpc()

    mongo_cfg = get_mongo_config_from_parametrizacion_json(execute_kw)
    mongo_uri, mongo_db_name, _tunnel = _start_ssh_tunnel_and_mongo_uri(mongo_cfg)

    client = MongoClient(
        mongo_uri,
        serverSelectionTimeoutMS=20000,
        connectTimeoutMS=20000,
        uuidRepresentation="standard",
    )
    # Verificar conexión (como en AJUSTE_COLFISIO)
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
            "marcados_delete": [],
            "con_factura": [],
            "sin_n_factura": [],
            "errores": [],
        },
    }

    if DRY_RUN:
        logger.warning(
            "MODO DRY_RUN ACTIVO: no se realizarán actualizaciones en Mongo (solo simulación)."
        )

    # Solo efectos no marcados previamente como borrados lógicos
    base_delete_filter: Dict[str, Any] = {
        "$or": [
            {"delete": {"$exists": False}},
            {"delete": False},
        ]
    }

    # Filtro por año aplicado directamente en Mongo para optimizar
    if FILTER_YEAR:
        start = datetime.datetime(FILTER_YEAR, 1, 1)
        end = datetime.datetime(FILTER_YEAR + 1, 1, 1)
        year_filter: Dict[str, Any] = {
            "$or": [
                {"fecha_efecto": {"$gte": start, "$lt": end}},
                {"created_at": {"$gte": start, "$lt": end}},
                {"fecha_vencimiento": {"$gte": start, "$lt": end}},
            ]
        }
        query: Dict[str, Any] = {"$and": [base_delete_filter, year_filter]}
    else:
        query = base_delete_filter

    total_candidatos = col_efectos.count_documents(query)
    resumen["total_efectos_candidatos"] = total_candidatos
    resumen["total_efectos_candidatos_filtrados"] = total_candidatos
    logger.info(
        "Total de efectos candidatos (delete!=True, año=%s): %d",
        FILTER_YEAR or "todos",
        total_candidatos,
    )

    cursor = col_efectos.find(query)
    procesados = 0
    lote_idx = 1
    procesados_lote = 0

    for efecto in cursor:
        efecto_id = str(efecto.get("_id"))
        n_factura = (efecto.get("n_factura") or "").strip()

        procesados += 1
        resumen["efectos_analizados"] = procesados
        procesados_lote += 1

        # Log de progreso periódico, aunque no se haya completado el lote
        if PROGRESS_LOG_INTERVAL and procesados % PROGRESS_LOG_INTERVAL == 0:
            elapsed_partial = time.time() - SCRIPT_START_TIME
            logger.info(
                "Progreso: %s efectos analizados (año=%s). Tiempo total transcurrido: %.1f segundos",
                procesados,
                FILTER_YEAR or "todos",
                elapsed_partial,
            )

        # Sin número de factura: lo registramos pero no tocamos el documento
        if not n_factura:
            resumen["efectos_sin_n_factura"] += 1
            resumen["detalles"]["sin_n_factura"].append(
                {
                    "_id": efecto_id,
                    "codigo_colegiado": efecto.get("codigo_colegiado"),
                    "importe": efecto.get("importe"),
                    "situacion": efecto.get("situacion"),
                    "referencia_curso": efecto.get("referencia_curso"),
                }
            )
            continue

        # Consultar en Odoo (JSON‑RPC) si existe la factura/rectificativa
        try:
            existe = odoo_invoice_exists_json(execute_kw, n_factura)
        except Exception as exc:  # noqa: BLE001
            resumen["errores_odoo"] += 1
            logger.error(
                "Error consultando Odoo para efecto %s (n_factura=%s): %s",
                efecto_id,
                n_factura,
                exc,
            )
            resumen["detalles"]["errores"].append(
                {
                    "_id": efecto_id,
                    "n_factura": n_factura,
                    "origen": "odoo",
                    "error": str(exc),
                }
            )
            continue

        # Si la factura existe en Odoo, no hacemos nada en Mongo
        if existe:
            resumen["efectos_con_factura_odoo"] += 1
            resumen["detalles"]["con_factura"].append(
                {
                    "_id": efecto_id,
                    "n_factura": n_factura,
                    "codigo_colegiado": efecto.get("codigo_colegiado"),
                    "importe": efecto.get("importe"),
                    "situacion": efecto.get("situacion"),
                    "referencia_curso": efecto.get("referencia_curso"),
                }
            )
            continue

        # No existe factura en Odoo -> borrado lógico en Mongo
        detalle = {
            "_id": efecto_id,
            "n_factura": n_factura,
            "codigo_colegiado": efecto.get("codigo_colegiado"),
            "importe": efecto.get("importe"),
            "situacion": efecto.get("situacion"),
            "referencia_curso": efecto.get("referencia_curso"),
        }

        if DRY_RUN:
            logger.info(
                "[DRY_RUN] Se marcaría delete=True y fecha_hora_registro=None para efecto %s (n_factura=%s)",
                efecto_id,
                n_factura,
            )
        else:
            try:
                col_efectos.update_one(
                    {"_id": efecto["_id"]},
                    {"$set": {"delete": True, "fecha_hora_registro": None}},
                )
                logger.info(
                    "Marcado delete=True en Mongo para efecto %s (n_factura=%s)",
                    efecto_id,
                    n_factura,
                )
            except Exception as exc:  # noqa: BLE001
                resumen["errores_mongo"] += 1
                logger.error(
                    "Error actualizando efecto %s en Mongo (n_factura=%s): %s",
                    efecto_id,
                    n_factura,
                    exc,
                )
                detalle["error"] = str(exc)
                resumen["detalles"]["errores"].append(detalle)
                continue

        resumen["efectos_marcados_delete"] += 1
        resumen["detalles"]["marcados_delete"].append(detalle)

        # Si hay tamaño de lote definido, loguear tiempo tras cada lote completo
        if BATCH_SIZE and procesados_lote >= BATCH_SIZE:
            elapsed = time.time() - SCRIPT_START_TIME
            logger.info(
                "Lote %s completado: %s efectos analizados (año=%s). Tiempo total transcurrido: %.1f segundos",
                lote_idx,
                procesados,
                FILTER_YEAR or "todos",
                elapsed,
            )
            lote_idx += 1
            procesados_lote = 0

    elapsed_total = time.time() - SCRIPT_START_TIME
    logger.info(
        "Procesamiento de efectos finalizado. Efectos analizados en total: %d. Tiempo total transcurrido: %.1f segundos",
        procesados,
        elapsed_total,
    )
    return resumen


def save_summary_to_file(summary: Dict[str, Any]) -> str:
    """Guarda el resumen en un fichero JSON con timestamp y devuelve la ruta."""
    os.makedirs(SUMMARY_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(SUMMARY_DIR, f"efectos_sync_summary_{ts}.json")
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    logger.info("Resumen detallado guardado en: %s", filename)
    return filename


def main() -> None:
    logger.info("Conectando a Mongo y Odoo...")
    try:
        summary = process_efectos()
    except Exception as exc:  # noqa: BLE001
        logger.error("Error crítico durante la ejecución: %s", exc)
        raise

    save_summary_to_file(summary)

    # Resumen corto por consola
    logger.info("=" * 70)
    logger.info("RESUMEN DE LIMPIEZA LÓGICA DE EFECTOS")
    logger.info("=" * 70)

    if summary.get("dry_run"):
        logger.warning("*** MODO DRY_RUN: No se realizaron actualizaciones reales en Mongo ***")

    logger.info(
        "Total efectos candidatos (delete!=True):  %s",
        summary["total_efectos_candidatos"],
    )
    logger.info(
        "Efectos candidatos tras filtro año %s:     %s",
        summary.get("filter_year") or "-",
        summary["total_efectos_candidatos_filtrados"],
    )
    logger.info(
        "Efectos analizados en este lote:         %s",
        summary["efectos_analizados"],
    )
    logger.info("-" * 40)
    logger.info(
        "Efectos con factura en Odoo:             %s",
        summary["efectos_con_factura_odoo"],
    )
    logger.info(
        "Efectos sin n_factura (omitidos):        %s",
        summary["efectos_sin_n_factura"],
    )
    logger.info(
        "Efectos marcados delete=True:            %s",
        summary["efectos_marcados_delete"],
    )
    logger.info("-" * 40)
    logger.info("Errores consulta Odoo:                  %s", summary["errores_odoo"])
    logger.info("Errores actualización Mongo:            %s", summary["errores_mongo"])
    logger.info("=" * 70)


if __name__ == "__main__":
    main()