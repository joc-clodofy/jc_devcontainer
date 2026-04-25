#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Script de limpieza y corrección de efectos en MongoDB basado en facturas de Odoo.

Objetivo:
    - Partir de las facturas de Odoo (account.move) como FUENTE DE VERDAD.
    - Para cada factura, buscar efectos relacionados en MongoDB.
    - CORREGIR efectos con codigo_colegiado diferente al de Odoo (actualizar, no borrar).
    - BORRAR efectos DUPLICADOS en MongoDB (solo en Mongo, Odoo es intocable).
    - Generar un resumen completo para auditoría.

IMPORTANTE:
    - Este script NUNCA modifica ni borra datos en Odoo.
    - Odoo es la fuente de verdad.
    - Solo se modifican/borran datos en MongoDB.

Campos clave para detección de duplicados en EFECTOS:
    - codigo_colegiado
    - importe
    - referencia_curso
    - fecha_efecto

NOTA: revisa y ajusta las constantes de configuración antes de ejecutar.
"""

import os
import sys
import json
import logging
import datetime
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import xmlrpc.client
from bson import ObjectId
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database


# ---------------------------------------------------------------------------
# CONFIGURACIÓN
# ---------------------------------------------------------------------------

# Config XML‑RPC Odoo
ODOO_URL = os.environ.get("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.environ.get("ODOO_DB", "colfisio")
ODOO_USER = os.environ.get("ODOO_USER", "admin")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "123")

# Config MongoDB (fallback si no hay parametrización en Odoo)
MONGO_URI_FALLBACK = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME_FALLBACK = os.environ.get("MONGO_DB_NAME", "colfisio_mongo")

# Nombre de colección
COL_EFECTOS = os.environ.get("MONGO_COL_EFECTOS", "efectos")

# Directorio para archivos de resumen
SUMMARY_DIR = os.environ.get("CLEANUP_SUMMARY_DIR", ".")

# Filtros para facturas de Odoo
INVOICE_DOMAIN = [
    ("move_type", "in", ["out_invoice", "out_refund"]),
    ("state", "=", "posted"),
]

# Modo de ejecución: True = solo análisis sin modificar/borrar
DRY_RUN = os.environ.get("CLEANUP_DRY_RUN", "true").lower() in ("true", "1", "yes")

# Límite de facturas a procesar (0 = sin límite)
INVOICE_LIMIT = int(os.environ.get("CLEANUP_INVOICE_LIMIT", "0"))


# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

logger = logging.getLogger("cleanup_mongo_duplicates")
logger.setLevel(logging.INFO)

console_handler = logging.StreamHandler(sys.stdout)
console_formatter = logging.Formatter(
    "[%(asctime)s] %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)
console_handler.setFormatter(console_formatter)
logger.addHandler(console_handler)


# ---------------------------------------------------------------------------
# CONEXIONES
# ---------------------------------------------------------------------------

def get_odoo_models_proxy() -> Tuple[xmlrpc.client.ServerProxy, int]:
    """Autentica contra Odoo y devuelve (proxy_models, uid)."""
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
    """Lee en Odoo (modelo parametrizacion) los datos de conexión a Mongo."""
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

    logger.info("Conectando a Mongo: url=%s, db=%s", mongo_url, mongo_db_name)
    return mongo_url, mongo_db_name


# ---------------------------------------------------------------------------
# UTILIDADES ODOO
# ---------------------------------------------------------------------------

def odoo_search_read(
    models: xmlrpc.client.ServerProxy,
    uid: int,
    model: str,
    domain: List,
    fields: List[str],
    limit: int = 0,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """search_read genérico."""
    kwargs: Dict[str, Any] = {"fields": fields}
    if limit > 0:
        kwargs["limit"] = limit
    if offset > 0:
        kwargs["offset"] = offset
    
    return models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        model,
        "search_read",
        [domain],
        kwargs,
    )


def get_all_invoices(
    models: xmlrpc.client.ServerProxy,
    uid: int,
) -> List[Dict[str, Any]]:
    """Obtiene todas las facturas de Odoo que cumplen INVOICE_DOMAIN."""
    fields = [
        "id",
        "name",
        "partner_id",
        "invoice_date",
        "amount_total",
        "move_type",
        "state",
    ]
    
    invoices = odoo_search_read(
        models, uid, "account.move", INVOICE_DOMAIN, fields, limit=INVOICE_LIMIT
    )
    
    logger.info("Facturas obtenidas de Odoo: %d", len(invoices))
    return invoices


def get_partner_codigo_colegiado(
    models: xmlrpc.client.ServerProxy,
    uid: int,
    partner_id: int,
) -> Optional[Union[int, str]]:
    """Obtiene el codigo_colegiado de un partner."""
    res = odoo_search_read(
        models, uid, "res.partner", [("id", "=", partner_id)], ["codigo_colegiado"], limit=1
    )
    if res and res[0].get("codigo_colegiado"):
        return res[0]["codigo_colegiado"]
    return None


# ---------------------------------------------------------------------------
# UTILIDADES MONGODB
# ---------------------------------------------------------------------------

def normalize_date(value: Any) -> Optional[str]:
    """Normaliza una fecha a formato YYYY-MM-DD para comparación."""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, datetime.date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, str):
        try:
            dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            return value[:10] if len(value) >= 10 else value
    return None


def normalize_importe(value: Any) -> float:
    """Normaliza un importe a float con 2 decimales."""
    if value is None:
        return 0.0
    try:
        return round(float(value), 2)
    except (ValueError, TypeError):
        return 0.0


def normalize_codigo_colegiado(value: Any) -> str:
    """Normaliza codigo_colegiado a string."""
    if value is None:
        return ""
    return str(value).strip()


def get_efecto_timestamp(doc: Dict[str, Any]) -> datetime.datetime:
    """Obtiene el timestamp más relevante de un efecto."""
    for field in ["fecha_efecto", "fecha_hora_registro", "created_at", "changed_at"]:
        val = doc.get(field)
        if isinstance(val, datetime.datetime):
            return val
    
    oid = doc.get("_id")
    if isinstance(oid, ObjectId):
        return oid.generation_time.replace(tzinfo=None)
    
    return datetime.datetime(1970, 1, 1)


# ---------------------------------------------------------------------------
# BÚSQUEDA Y DETECCIÓN DE DUPLICADOS EN EFECTOS
# ---------------------------------------------------------------------------

def find_efectos_by_invoice(
    col_efectos: Collection,
    invoice_name: str,
) -> List[Dict[str, Any]]:
    """
    Busca efectos en MongoDB relacionados con una factura.
    
    Criterios de búsqueda:
        1. Por n_factura exacto
        2. Por n_efecto que contenga el número de factura
    """
    query = {"$or": [
        {"n_factura": invoice_name},
        {"n_efecto": {"$regex": f"^{invoice_name}"}},
    ]}
    
    return list(col_efectos.find(query))


def build_efecto_duplicate_key(doc: Dict[str, Any]) -> Tuple:
    """
    Construye clave de duplicado para efectos.
    
    Campos clave:
        - codigo_colegiado
        - importe (normalizado a 2 decimales)
        - referencia_curso
        - fecha_efecto (normalizado a YYYY-MM-DD)
    """
    codigo_colegiado = normalize_codigo_colegiado(doc.get("codigo_colegiado"))
    importe = normalize_importe(doc.get("importe"))
    referencia_curso = str(doc.get("referencia_curso") or "").strip()
    fecha = normalize_date(doc.get("fecha_efecto"))
    
    return (codigo_colegiado, importe, referencia_curso, fecha)


def find_duplicates_in_efectos(
    efectos: List[Dict[str, Any]],
) -> Dict[Tuple, List[Dict[str, Any]]]:
    """Agrupa efectos por clave y devuelve grupos con más de 1 elemento."""
    grupos: Dict[Tuple, List[Dict[str, Any]]] = defaultdict(list)
    
    for efecto in efectos:
        key = build_efecto_duplicate_key(efecto)
        grupos[key].append(efecto)
    
    return {k: v for k, v in grupos.items() if len(v) > 1}


def select_efecto_to_keep(
    efectos: List[Dict[str, Any]],
    codigo_colegiado_odoo: Optional[Union[int, str]] = None,
) -> Dict[str, Any]:
    """
    Selecciona qué efecto conservar de una lista de duplicados.
    
    Criterios (en orden de prioridad):
        1. El que tenga codigo_colegiado igual al de Odoo
        2. El que tenga n_factura no vacío
        3. El más reciente por timestamp
    """
    candidatos = efectos
    
    # Filtrar por codigo_colegiado de Odoo si se proporciona
    if codigo_colegiado_odoo is not None:
        codigo_odoo_str = normalize_codigo_colegiado(codigo_colegiado_odoo)
        matching = [
            e for e in efectos 
            if normalize_codigo_colegiado(e.get("codigo_colegiado")) == codigo_odoo_str
        ]
        if matching:
            candidatos = matching
    
    # Preferir los que tienen n_factura
    con_factura = [e for e in candidatos if e.get("n_factura")]
    if con_factura:
        return max(con_factura, key=get_efecto_timestamp)
    
    # Si no, el más reciente
    return max(candidatos, key=get_efecto_timestamp)


# ---------------------------------------------------------------------------
# PROCESAMIENTO PRINCIPAL
# ---------------------------------------------------------------------------

def process_invoice_in_mongo(
    invoice: Dict[str, Any],
    codigo_colegiado_odoo: Optional[Union[int, str]],
    col_efectos: Collection,
    resumen: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Procesa una factura de Odoo buscando efectos en MongoDB.
    
    Acciones:
        1. CORREGIR: Si el codigo_colegiado del efecto difiere del de Odoo -> actualizar
        2. BORRAR: Si hay duplicados en MongoDB -> borrar los extras, conservar 1
    
    IMPORTANTE: Nunca se modifica ni borra nada en Odoo.
    """
    invoice_name = invoice.get("name", "")
    invoice_id = invoice.get("id")
    
    resultado = {
        "invoice_id": invoice_id,
        "invoice_name": invoice_name,
        "codigo_colegiado_odoo": codigo_colegiado_odoo,
        "efectos_encontrados": 0,
        "efectos_duplicados": 0,
        "efectos_duplicados_borrados": 0,
        "efectos_colegiado_corregido": 0,
        "detalles_duplicados_borrados": [],
        "detalles_duplicados_conservados": [],
        "detalles_colegiado_corregido": [],
    }
    
    # Buscar efectos relacionados con la factura
    efectos = find_efectos_by_invoice(col_efectos, invoice_name)
    resultado["efectos_encontrados"] = len(efectos)
    
    if not efectos:
        return resultado
    
    # =========================================================================
    # PASO 1: Detectar y borrar DUPLICADOS en MongoDB
    # =========================================================================
    duplicados = find_duplicates_in_efectos(efectos)
    ids_borrados: Set[ObjectId] = set()
    
    for key, grupo in duplicados.items():
        resultado["efectos_duplicados"] += len(grupo) - 1
        
        # Seleccionar cuál conservar
        conservar = select_efecto_to_keep(grupo, codigo_colegiado_odoo)
        
        # Registrar el conservado
        resultado["detalles_duplicados_conservados"].append({
            "_id": str(conservar["_id"]),
            "n_factura": conservar.get("n_factura"),
            "n_efecto": conservar.get("n_efecto"),
            "codigo_colegiado": conservar.get("codigo_colegiado"),
            "importe": conservar.get("importe"),
            "referencia_curso": conservar.get("referencia_curso"),
            "fecha_efecto": normalize_date(conservar.get("fecha_efecto")),
            "situacion": conservar.get("situacion"),
            "clave_duplicado": {
                "codigo_colegiado": key[0],
                "importe": key[1],
                "referencia_curso": key[2],
                "fecha_efecto": key[3],
            },
            "total_duplicados_en_grupo": len(grupo),
        })
        
        # Procesar los que se van a borrar (solo en MongoDB)
        for efecto in grupo:
            if efecto["_id"] == conservar["_id"]:
                continue  # Este se conserva
            
            detalle = {
                "_id": str(efecto["_id"]),
                "n_factura": efecto.get("n_factura"),
                "n_efecto": efecto.get("n_efecto"),
                "codigo_colegiado": efecto.get("codigo_colegiado"),
                "importe": efecto.get("importe"),
                "referencia_curso": efecto.get("referencia_curso"),
                "fecha_efecto": normalize_date(efecto.get("fecha_efecto")),
                "situacion": efecto.get("situacion"),
                "descripcion": efecto.get("descripcion"),
                "clave_duplicado": {
                    "codigo_colegiado": key[0],
                    "importe": key[1],
                    "referencia_curso": key[2],
                    "fecha_efecto": key[3],
                },
                "conservado_id": str(conservar["_id"]),
            }
            
            if DRY_RUN:
                detalle["accion"] = "borrar_dry_run"
                logger.debug(
                    "[DRY_RUN] Se borraría efecto duplicado: %s (factura: %s)",
                    efecto["_id"], invoice_name
                )
            else:
                try:
                    col_efectos.delete_one({"_id": efecto["_id"]})
                    resultado["efectos_duplicados_borrados"] += 1
                    resumen["efectos_duplicados_borrados"] += 1
                    ids_borrados.add(efecto["_id"])
                    detalle["accion"] = "borrado"
                    logger.info(
                        "Efecto duplicado BORRADO en MongoDB: %s (factura: %s, colegiado: %s)",
                        efecto["_id"], invoice_name, efecto.get("codigo_colegiado")
                    )
                except Exception as exc:
                    detalle["accion"] = "error_borrado"
                    detalle["error"] = str(exc)
                    logger.error("Error borrando efecto %s: %s", efecto["_id"], exc)
            
            resultado["detalles_duplicados_borrados"].append(detalle)
    
    # =========================================================================
    # PASO 2: CORREGIR efectos con codigo_colegiado diferente al de Odoo
    # =========================================================================
    if codigo_colegiado_odoo is not None:
        codigo_odoo_str = normalize_codigo_colegiado(codigo_colegiado_odoo)
        
        for efecto in efectos:
            # Saltar si ya fue borrado como duplicado
            if efecto["_id"] in ids_borrados:
                continue
            
            efecto_cc = efecto.get("codigo_colegiado")
            efecto_cc_str = normalize_codigo_colegiado(efecto_cc)
            
            # Si el codigo_colegiado es diferente, corregirlo
            if efecto_cc_str and efecto_cc_str != codigo_odoo_str:
                resumen["efectos_colegiado_incorrecto"] += 1
                
                detalle = {
                    "_id": str(efecto["_id"]),
                    "n_factura": efecto.get("n_factura"),
                    "n_efecto": efecto.get("n_efecto"),
                    "codigo_colegiado_anterior": efecto_cc,
                    "codigo_colegiado_nuevo": codigo_colegiado_odoo,
                    "importe": efecto.get("importe"),
                    "referencia_curso": efecto.get("referencia_curso"),
                    "fecha_efecto": normalize_date(efecto.get("fecha_efecto")),
                    "situacion": efecto.get("situacion"),
                }
                
                if DRY_RUN:
                    detalle["accion"] = "corregir_dry_run"
                    logger.debug(
                        "[DRY_RUN] Se corregiría codigo_colegiado: %s -> %s (efecto: %s)",
                        efecto_cc, codigo_colegiado_odoo, efecto["_id"]
                    )
                else:
                    try:
                        # Actualizar el codigo_colegiado en MongoDB
                        col_efectos.update_one(
                            {"_id": efecto["_id"]},
                            {
                                "$set": {
                                    "codigo_colegiado": codigo_colegiado_odoo,
                                    "codigo_colegiado_anterior": efecto_cc,  # Guardar el anterior para auditoría
                                    "corregido_desde_odoo": True,
                                    "fecha_correccion": datetime.datetime.now(),
                                }
                            }
                        )
                        resultado["efectos_colegiado_corregido"] += 1
                        resumen["efectos_colegiado_corregido"] += 1
                        detalle["accion"] = "corregido"
                        logger.info(
                            "Efecto CORREGIDO en MongoDB: %s (colegiado: %s -> %s, factura: %s)",
                            efecto["_id"], efecto_cc, codigo_colegiado_odoo, invoice_name
                        )
                    except Exception as exc:
                        detalle["accion"] = "error_correccion"
                        detalle["error"] = str(exc)
                        logger.error(
                            "Error corrigiendo codigo_colegiado en efecto %s: %s",
                            efecto["_id"], exc
                        )
                
                resultado["detalles_colegiado_corregido"].append(detalle)
    
    return resultado


def process_all_invoices() -> Dict[str, Any]:
    """
    Procesa todas las facturas de Odoo, corrigiendo y limpiando efectos en MongoDB.
    
    IMPORTANTE: Este script NUNCA modifica ni borra datos en Odoo.
    """
    # Conectar a Odoo
    models, uid = get_odoo_models_proxy()
    
    # Conectar a MongoDB
    mongo_uri, mongo_db_name = get_mongo_config_from_parametrizacion(models, uid)
    client = MongoClient(mongo_uri)
    db: Database = client[mongo_db_name]
    col_efectos: Collection = db[COL_EFECTOS]
    
    # Inicializar resumen
    resumen: Dict[str, Any] = {
        "dry_run": DRY_RUN,
        "timestamp": datetime.datetime.now().isoformat(),
        "nota_importante": "Este script NUNCA modifica ni borra datos en Odoo. Solo actúa sobre MongoDB.",
        "campos_clave_duplicados": ["codigo_colegiado", "importe", "referencia_curso", "fecha_efecto"],
        "total_facturas_odoo": 0,
        "facturas_procesadas": 0,
        "facturas_con_efectos": 0,
        "facturas_con_duplicados": 0,
        "facturas_con_colegiado_incorrecto": 0,
        "efectos_encontrados_total": 0,
        "efectos_duplicados_total": 0,
        "efectos_duplicados_borrados": 0,
        "efectos_colegiado_incorrecto": 0,
        "efectos_colegiado_corregido": 0,
        "detalles_por_factura": [],
        "errores": [],
    }
    
    if DRY_RUN:
        logger.warning("=" * 70)
        logger.warning("MODO DRY_RUN ACTIVO: No se realizarán cambios reales.")
        logger.warning("=" * 70)
    else:
        logger.warning("=" * 70)
        logger.warning("MODO EJECUCIÓN REAL: Se modificarán/borrarán datos en MongoDB.")
        logger.warning("Odoo NO será modificado (es la fuente de verdad).")
        logger.warning("=" * 70)
    
    # Obtener facturas de Odoo (solo lectura)
    logger.info("Obteniendo facturas de Odoo (solo lectura)...")
    invoices = get_all_invoices(models, uid)
    resumen["total_facturas_odoo"] = len(invoices)
    
    # Cache de codigo_colegiado por partner_id
    partner_codigo_cache: Dict[int, Optional[Union[int, str]]] = {}
    
    # Procesar cada factura
    for i, invoice in enumerate(invoices, 1):
        invoice_name = invoice.get("name", "")
        partner_data = invoice.get("partner_id")
        partner_id = partner_data[0] if isinstance(partner_data, (list, tuple)) else partner_data
        
        # Obtener codigo_colegiado (con cache)
        if partner_id not in partner_codigo_cache:
            partner_codigo_cache[partner_id] = get_partner_codigo_colegiado(models, uid, partner_id)
        codigo_colegiado = partner_codigo_cache[partner_id]
        
        if i % 500 == 0 or i == len(invoices):
            logger.info("Procesando factura %d/%d: %s", i, len(invoices), invoice_name)
        
        try:
            resultado = process_invoice_in_mongo(
                invoice,
                codigo_colegiado,
                col_efectos,
                resumen,
            )
            
            # Actualizar contadores
            resumen["facturas_procesadas"] += 1
            resumen["efectos_encontrados_total"] += resultado["efectos_encontrados"]
            resumen["efectos_duplicados_total"] += resultado["efectos_duplicados"]
            
            if resultado["efectos_encontrados"] > 0:
                resumen["facturas_con_efectos"] += 1
            
            if resultado["efectos_duplicados"] > 0:
                resumen["facturas_con_duplicados"] += 1
            
            if resultado["efectos_colegiado_corregido"] > 0 or len(resultado["detalles_colegiado_corregido"]) > 0:
                resumen["facturas_con_colegiado_incorrecto"] += 1
            
            # Guardar detalles si hubo acciones
            if (resultado["detalles_duplicados_borrados"] or 
                resultado["detalles_colegiado_corregido"]):
                resumen["detalles_por_factura"].append(resultado)
        
        except Exception as exc:
            logger.error("Error procesando factura %s: %s", invoice_name, exc)
            resumen["errores"].append({
                "invoice_name": invoice_name,
                "invoice_id": invoice.get("id"),
                "error": str(exc),
            })
    
    logger.info("Procesamiento completado.")
    return resumen


# ---------------------------------------------------------------------------
# GENERACIÓN DE ARCHIVOS DE SALIDA
# ---------------------------------------------------------------------------

def save_summary_json(summary: Dict[str, Any]) -> str:
    """Guarda el resumen en un fichero JSON."""
    os.makedirs(SUMMARY_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(SUMMARY_DIR, f"cleanup_mongo_summary_{ts}.json")
    
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    
    logger.info("Resumen JSON guardado en: %s", filename)
    return filename


def save_summary_markdown(summary: Dict[str, Any]) -> str:
    """Genera un archivo Markdown con el resumen de la limpieza."""
    os.makedirs(SUMMARY_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(SUMMARY_DIR, f"cleanup_mongo_report_{ts}.md")
    
    with open(filename, "w", encoding="utf-8") as f:
        f.write("# Reporte de Limpieza y Corrección de Efectos en MongoDB\n\n")
        f.write(f"**Fecha de ejecución:** {summary['timestamp']}\n\n")
        
        f.write("> **IMPORTANTE:** Este script NUNCA modifica ni borra datos en Odoo.\n")
        f.write("> Odoo es la fuente de verdad. Solo se actúa sobre MongoDB.\n\n")
        
        if summary.get("dry_run"):
            f.write("> **MODO DRY_RUN**: No se realizaron cambios reales.\n\n")
        else:
            f.write("> **MODO EJECUCIÓN REAL**: Se realizaron cambios en MongoDB.\n\n")
        
        f.write("## Campos clave para detección de duplicados\n\n")
        f.write("| Campo | Descripción |\n")
        f.write("|-------|-------------|\n")
        f.write("| `codigo_colegiado` | Número de colegiado |\n")
        f.write("| `importe` | Monto del efecto |\n")
        f.write("| `referencia_curso` | ID de referencia del curso |\n")
        f.write("| `fecha_efecto` | Fecha del efecto (YYYY-MM-DD) |\n\n")
        
        f.write("## Resumen General\n\n")
        f.write("| Métrica | Valor |\n")
        f.write("|---------|-------|\n")
        f.write(f"| Total facturas Odoo | {summary['total_facturas_odoo']} |\n")
        f.write(f"| Facturas procesadas | {summary['facturas_procesadas']} |\n")
        f.write(f"| Facturas con efectos en Mongo | {summary['facturas_con_efectos']} |\n")
        f.write(f"| Facturas con duplicados | {summary['facturas_con_duplicados']} |\n")
        f.write(f"| Facturas con colegiado incorrecto | {summary['facturas_con_colegiado_incorrecto']} |\n")
        f.write(f"| Errores | {len(summary.get('errores', []))} |\n\n")
        
        f.write("## Acciones en MongoDB\n\n")
        f.write("### Duplicados (BORRADOS)\n\n")
        f.write("| Métrica | Valor |\n")
        f.write("|---------|-------|\n")
        f.write(f"| Duplicados detectados | {summary['efectos_duplicados_total']} |\n")
        f.write(f"| Duplicados borrados | {summary['efectos_duplicados_borrados']} |\n\n")
        
        f.write("### Colegiado Incorrecto (CORREGIDOS)\n\n")
        f.write("| Métrica | Valor |\n")
        f.write("|---------|-------|\n")
        f.write(f"| Efectos con colegiado incorrecto | {summary['efectos_colegiado_incorrecto']} |\n")
        f.write(f"| Efectos corregidos | {summary['efectos_colegiado_corregido']} |\n\n")
        
        # Detalles de duplicados borrados
        if summary.get("detalles_por_factura"):
            # Sección de duplicados borrados
            facturas_con_borrados = [
                f for f in summary["detalles_por_factura"] 
                if f.get("detalles_duplicados_borrados")
            ]
            
            if facturas_con_borrados:
                f.write("## Detalle de Duplicados BORRADOS\n\n")
                
                for detalle in facturas_con_borrados:
                    f.write(f"### Factura: `{detalle['invoice_name']}`\n\n")
                    f.write(f"- **ID Odoo:** {detalle['invoice_id']}\n")
                    f.write(f"- **Código Colegiado Odoo:** {detalle['codigo_colegiado_odoo']}\n")
                    f.write(f"- **Duplicados borrados:** {detalle['efectos_duplicados_borrados']}\n\n")
                    
                    # Tabla de conservados
                    if detalle.get("detalles_duplicados_conservados"):
                        f.write("**Efectos CONSERVADOS (1 por grupo):**\n\n")
                        f.write("| ID Mongo | N° Efecto | Colegiado | Importe | Fecha |\n")
                        f.write("|----------|-----------|-----------|---------|-------|\n")
                        for item in detalle["detalles_duplicados_conservados"]:
                            mongo_id = item["_id"][:12] + "..."
                            n_efecto = item.get("n_efecto", "-") or "-"
                            colegiado = item.get("codigo_colegiado", "-")
                            importe = item.get("importe", "-")
                            fecha = item.get("fecha_efecto", "-")
                            f.write(f"| `{mongo_id}` | {n_efecto} | {colegiado} | {importe} | {fecha} |\n")
                        f.write("\n")
                    
                    # Tabla de borrados
                    f.write("**Efectos BORRADOS:**\n\n")
                    f.write("| ID Mongo | N° Efecto | Colegiado | Importe | Fecha | Acción |\n")
                    f.write("|----------|-----------|-----------|---------|-------|--------|\n")
                    
                    for item in detalle["detalles_duplicados_borrados"]:
                        mongo_id = item["_id"][:12] + "..."
                        n_efecto = item.get("n_efecto", "-") or "-"
                        colegiado = item.get("codigo_colegiado", "-")
                        importe = item.get("importe", "-")
                        fecha = item.get("fecha_efecto", "-")
                        accion = item.get("accion", "-")
                        f.write(f"| `{mongo_id}` | {n_efecto} | {colegiado} | {importe} | {fecha} | {accion} |\n")
                    
                    f.write("\n---\n\n")
            
            # Sección de colegiados corregidos
            facturas_con_corregidos = [
                f for f in summary["detalles_por_factura"] 
                if f.get("detalles_colegiado_corregido")
            ]
            
            if facturas_con_corregidos:
                f.write("## Detalle de Colegiados CORREGIDOS\n\n")
                
                for detalle in facturas_con_corregidos:
                    if not detalle.get("detalles_colegiado_corregido"):
                        continue
                    
                    f.write(f"### Factura: `{detalle['invoice_name']}`\n\n")
                    f.write(f"- **ID Odoo:** {detalle['invoice_id']}\n")
                    f.write(f"- **Código Colegiado Odoo (correcto):** {detalle['codigo_colegiado_odoo']}\n")
                    f.write(f"- **Efectos corregidos:** {detalle['efectos_colegiado_corregido']}\n\n")
                    
                    f.write("| ID Mongo | N° Efecto | Colegiado Anterior | Colegiado Nuevo | Importe | Acción |\n")
                    f.write("|----------|-----------|-------------------|-----------------|---------|--------|\n")
                    
                    for item in detalle["detalles_colegiado_corregido"]:
                        mongo_id = item["_id"][:12] + "..."
                        n_efecto = item.get("n_efecto", "-") or "-"
                        cc_anterior = item.get("codigo_colegiado_anterior", "-")
                        cc_nuevo = item.get("codigo_colegiado_nuevo", "-")
                        importe = item.get("importe", "-")
                        accion = item.get("accion", "-")
                        f.write(f"| `{mongo_id}` | {n_efecto} | {cc_anterior} | {cc_nuevo} | {importe} | {accion} |\n")
                    
                    f.write("\n---\n\n")
        
        # Errores
        if summary.get("errores"):
            f.write("## Errores\n\n")
            for error in summary["errores"]:
                f.write(f"- **Factura:** `{error.get('invoice_name')}`\n")
                f.write(f"  - Error: {error.get('error')}\n\n")
        
        # Nota final
        f.write("---\n\n")
        f.write("## Notas\n\n")
        f.write("- Los efectos corregidos tienen un campo `codigo_colegiado_anterior` para auditoría.\n")
        f.write("- Los efectos corregidos tienen `corregido_desde_odoo: true` y `fecha_correccion`.\n")
        f.write("- Este reporte sirve como registro de auditoría de todos los cambios realizados.\n")
    
    logger.info("Reporte Markdown guardado en: %s", filename)
    return filename


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("=" * 70)
    logger.info("LIMPIEZA Y CORRECCIÓN DE EFECTOS EN MONGODB")
    logger.info("=" * 70)
    logger.info("Fuente de verdad: ODOO (nunca se modifica)")
    logger.info("Acciones en MongoDB:")
    logger.info("  - BORRAR duplicados (conservar 1 por grupo)")
    logger.info("  - CORREGIR codigo_colegiado (alinear con Odoo)")
    logger.info("Campos clave: codigo_colegiado, importe, referencia_curso, fecha_efecto")
    logger.info("=" * 70)
    
    try:
        summary = process_all_invoices()
    except Exception as exc:
        logger.error("Error crítico durante la ejecución: %s", exc)
        raise
    
    # Guardar archivos de resumen
    json_file = save_summary_json(summary)
    md_file = save_summary_markdown(summary)
    
    # Resumen por consola
    logger.info("=" * 70)
    logger.info("RESUMEN DE LIMPIEZA Y CORRECCIÓN")
    logger.info("=" * 70)
    
    if summary.get("dry_run"):
        logger.warning("*** MODO DRY_RUN: No se realizaron cambios reales ***")
    else:
        logger.info("*** MODO EJECUCIÓN REAL: Se realizaron cambios en MongoDB ***")
    
    logger.info("")
    logger.info("FACTURAS ODOO (solo lectura):")
    logger.info("  Total facturas:                    %s", summary["total_facturas_odoo"])
    logger.info("  Facturas procesadas:               %s", summary["facturas_procesadas"])
    logger.info("  Facturas con efectos:              %s", summary["facturas_con_efectos"])
    logger.info("  Facturas con duplicados:           %s", summary["facturas_con_duplicados"])
    logger.info("  Facturas con colegiado incorrecto: %s", summary["facturas_con_colegiado_incorrecto"])
    logger.info("")
    logger.info("ACCIONES EN MONGODB:")
    logger.info("  [DUPLICADOS]")
    logger.info("    Detectados:                      %s", summary["efectos_duplicados_total"])
    logger.info("    Borrados:                        %s", summary["efectos_duplicados_borrados"])
    logger.info("  [COLEGIADO INCORRECTO]")
    logger.info("    Detectados:                      %s", summary["efectos_colegiado_incorrecto"])
    logger.info("    Corregidos:                      %s", summary["efectos_colegiado_corregido"])
    logger.info("")
    logger.info("Errores:                             %s", len(summary.get("errores", [])))
    logger.info("")
    logger.info("Archivos generados:")
    logger.info("  JSON: %s", json_file)
    logger.info("  Markdown: %s", md_file)
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
