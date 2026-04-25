#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script para corregir inscripciones de cursos que tienen n_factura = null en MongoDB

Este script busca inscripciones con n_factura null y las actualiza con el número
de factura correspondiente en Odoo, basándose en el partner y el curso.
"""

import xmlrpc.client
import os
from datetime import datetime
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from sshtunnel import SSHTunnelForwarder

# ====== CONFIGURACIÓN ODOO ======
ODOO_URL = "http://localhost:8069"
ODOO_DB = "colfisio"
ODOO_USERNAME = "admin"
ODOO_PASSWORD = "123"

# ====== CONFIGURACIÓN MONGODB ======
MONGODB_URL_DIRECTA = None
SSH_HOST = None
SSH_USER = None
SSH_PASSWORD = None
SSH_PORT = 22
MONGO_PORT = 27017
MONGO_DB_NAME = "tu_base_datos"
MONGO_URL_TEMPLATE = None

# ====== OPCIONES ======
DRY_RUN = False  # Cambia a False para aplicar correcciones
LIMIT_INSCRIPCIONES = 20000  # Aumentado para procesar más inscripciones

# ========= CONEXIÓN ODOO =========
print("=" * 80)
print("CORRECCIÓN DE n_factura EN INSCRIPCIONES DE CURSOS")
print("=" * 80)
print(f"\nConectando a Odoo...")
print(f"URL: {ODOO_URL}")
print(f"Base de datos: {ODOO_DB}")
print(f"Usuario: {ODOO_USERNAME}")

try:
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
    if not uid:
        raise Exception("Error de autenticación")
    
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    print(f"✓ Conectado a Odoo como usuario ID: {uid}\n")
except Exception as e:
    print(f"✗ Error de conexión a Odoo: {e}")
    exit(1)

# ========= OBTENER CONFIGURACIÓN MONGODB DESDE ODOO =========
print("Obteniendo configuración de MongoDB desde Odoo...")
try:
    parametrizacion = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'parametrizacion',
        'search_read',
        [[]],
        {'fields': ['ssh_host', 'ssh_user', 'ssh_pass', 'mongo_port', 'mongo_db', 'mongo_db_url', 'diario_formacion2'], 'limit': 1}
    )
    
    if parametrizacion:
        config = parametrizacion[0]
        ssh_host_config = config.get('ssh_host')
        SSH_HOST = ssh_host_config if ssh_host_config else SSH_HOST
        SSH_USER = config.get('ssh_user') if config.get('ssh_user') else SSH_USER
        SSH_PASSWORD = config.get('ssh_pass') if config.get('ssh_pass') else SSH_PASSWORD
        MONGO_PORT = config.get('mongo_port', MONGO_PORT)
        MONGO_DB_NAME = config.get('mongo_db', MONGO_DB_NAME)
        mongo_url_config = config.get('mongo_db_url')
        MONGO_URL_TEMPLATE = mongo_url_config if mongo_url_config else MONGO_URL_TEMPLATE
        diario_formacion_id = config.get('diario_formacion2', [None])[0] if isinstance(config.get('diario_formacion2'), list) else config.get('diario_formacion2')
        print(f"✓ Configuración obtenida de Odoo")
        print(f"  SSH Host: {SSH_HOST if SSH_HOST else 'No configurado (conexión directa)'}")
        print(f"  MongoDB DB: {MONGO_DB_NAME}")
        print(f"  Diario Formación ID: {diario_formacion_id}")
    else:
        print("⚠ No se encontró configuración en Odoo, usando valores por defecto")
except Exception as e:
    print(f"⚠ Error obteniendo configuración de Odoo: {e}")
    print("  Usando valores por defecto del script")

# ========= PREPARAR CONFIGURACIÓN MONGODB =========
usar_ssh = bool(SSH_HOST and SSH_USER and SSH_PASSWORD)
print(f"\nConfiguración MongoDB:")
print(f"  Modo: {'SSH Tunnel' if usar_ssh else 'Conexión directa'}")
print(f"  MongoDB DB: {MONGO_DB_NAME}")

# ========= FUNCIÓN ASÍNCRONA PARA CORREGIR =========
async def corregir_n_factura_inscripciones():
    """Función asíncrona que corrige n_factura en inscripciones"""
    
    server = None
    mongo_client = None
    
    try:
        # Crear conexión MongoDB dentro del event loop
        print("Conectando a MongoDB...")
        
        if usar_ssh:
            server = SSHTunnelForwarder(
                (SSH_HOST, SSH_PORT),
                ssh_username=SSH_USER,
                ssh_password=SSH_PASSWORD,
                remote_bind_address=('127.0.0.1', MONGO_PORT)
            )
            server.start()
            print(f"  ✓ Túnel SSH establecido (puerto local: {server.local_bind_port})")
            
            if MONGO_URL_TEMPLATE:
                mongo_url = MONGO_URL_TEMPLATE.replace(str(MONGO_PORT), str(server.local_bind_port))
            else:
                mongo_url = f"mongodb://127.0.0.1:{server.local_bind_port}/{MONGO_DB_NAME}"
        else:
            if MONGODB_URL_DIRECTA:
                mongo_url = MONGODB_URL_DIRECTA
            elif MONGO_URL_TEMPLATE:
                mongo_url = MONGO_URL_TEMPLATE.replace(f":{MONGO_PORT}/", f":27017/")
                if "{port}" in mongo_url:
                    mongo_url = mongo_url.replace("{port}", "27017")
            else:
                mongo_url = f"mongodb://localhost:27017/{MONGO_DB_NAME}"
        
        mongo_client = AsyncIOMotorClient(mongo_url, uuidRepresentation="standard")
        await mongo_client.admin.command('ping')
        print(f"✓ Conectado a MongoDB\n")
        
        # Estadísticas
        total_procesadas = 0
        corregidas = 0
        errores = 0
        no_encontradas = 0
        
        # Crear carpeta de logs
        ruta_carpeta = os.path.join(os.path.expanduser("~"), "logs_correccion_n_factura")
        if not os.path.exists(ruta_carpeta):
            os.makedirs(ruta_carpeta)
        
        now = datetime.now()
        fecha_hora_str = now.strftime("%Y-%m-%d_%H-%M-%S")
        log_file = os.path.join(ruta_carpeta, f"correccion_n_factura_{fecha_hora_str}.log")
        
        # Obtener base de datos y colección
        db = mongo_client[MONGO_DB_NAME]
        db_inscripciones = db.inscripciones_cursos
        
        # Buscar inscripciones con n_factura null o vacío
        print("Buscando inscripciones con n_factura null...")
        query = {
            '$or': [
                {'n_factura': None},
                {'n_factura': ''},
                {'n_factura': {'$exists': False}}
            ],
            'referencia_curso': {'$exists': True, '$ne': None, '$nin': [None, '']},
            'codigo_colegiado': {'$exists': True, '$ne': None, '$nin': [None, 0]}
        }
        
        # Contar total primero
        total_count = await db_inscripciones.count_documents(query)
        print(f"✓ Total de inscripciones sin n_factura en MongoDB: {total_count}")
        
        inscripciones = []
        async for inscripcion in db_inscripciones.find(query).limit(LIMIT_INSCRIPCIONES):
            inscripciones.append(inscripcion)
        
        print(f"✓ Procesando {len(inscripciones)} inscripciones (límite: {LIMIT_INSCRIPCIONES})\n")
        print("Iniciando corrección...")
        print(f"Modo: {'DRY RUN (solo validación)' if DRY_RUN else 'EJECUCIÓN REAL (corrige n_factura)'}")
        print("-" * 80)
        
        for inscripcion in inscripciones:
            total_procesadas += 1
            codigo_colegiado = inscripcion.get('codigo_colegiado')
            referencia_curso = inscripcion.get('referencia_curso')
            inscripcion_id = inscripcion.get('_id')
            
            try:
                # Buscar facturas directamente usando partner_codigo_colegiado (campo del sistema del cliente)
                if diario_formacion_id:
                    facturas = models.execute_kw(
                        ODOO_DB, uid, ODOO_PASSWORD,
                        'account.move',
                        'search_read',
                        [[
                            ('partner_codigo_colegiado', '=', codigo_colegiado),
                            ('journal_id', '=', diario_formacion_id),
                            ('move_type', '!=', 'out_refund'),
                            ('state', '=', 'posted')
                        ]],
                        {'fields': ['name', 'invoice_line_ids'], 'limit': 500}  # Aumentado el límite
                    )
                else:
                    # Si no hay diario configurado, buscar en todos los diarios de venta
                    facturas = models.execute_kw(
                        ODOO_DB, uid, ODOO_PASSWORD,
                        'account.move',
                        'search_read',
                        [[
                            ('partner_codigo_colegiado', '=', codigo_colegiado),
                            ('move_type', '!=', 'out_refund'),
                            ('state', '=', 'posted')
                        ]],
                        {'fields': ['name', 'invoice_line_ids', 'journal_id'], 'limit': 500}  # Aumentado el límite
                    )
                
                # Buscar la factura que tenga el producto con referencia_curso
                n_factura_encontrada = None
                for factura in facturas:
                    # Obtener líneas de factura
                    line_ids = factura.get('invoice_line_ids', [])
                    for line_id in line_ids:
                        line_data = models.execute_kw(
                            ODOO_DB, uid, ODOO_PASSWORD,
                            'account.move.line',
                            'read',
                            [[line_id]],
                            {'fields': ['product_id']}
                        )
                        if line_data:
                            product_id = line_data[0].get('product_id', [None])[0] if isinstance(line_data[0].get('product_id'), list) else line_data[0].get('product_id')
                            if product_id:
                                product_data = models.execute_kw(
                                    ODOO_DB, uid, ODOO_PASSWORD,
                                    'product.product',
                                    'read',
                                    [[product_id]],
                                    {'fields': ['default_code']}
                                )
                                if product_data and product_data[0].get('default_code') == referencia_curso:
                                    n_factura_encontrada = factura['name']
                                    break
                    if n_factura_encontrada:
                        break
                
                if n_factura_encontrada:
                    log_msg = f"CORRECCIÓN - Inscripción: {inscripcion_id}\n"
                    log_msg += f"  codigo_colegiado: {codigo_colegiado}\n"
                    log_msg += f"  Curso: {referencia_curso}\n"
                    log_msg += f"  Facturas revisadas: {len(facturas)}\n"
                    log_msg += f"  Factura encontrada: {n_factura_encontrada}\n"
                    
                    if DRY_RUN:
                        log_msg += f"  [DRY RUN] No se aplicará la corrección\n"
                        with open(log_file, 'a', encoding='utf-8') as f:
                            f.write(log_msg + "\n")
                    else:
                        # Actualizar n_factura en MongoDB
                        myquery = {"_id": inscripcion_id}
                        newvalues = {"$set": {"n_factura": n_factura_encontrada}}
                        result = await db_inscripciones.update_one(myquery, newvalues)
                        
                        if result.modified_count > 0:
                            log_msg += f"  ACCIÓN: n_factura actualizado en MongoDB\n"
                            corregidas += 1
                        else:
                            log_msg += f"  ADVERTENCIA: No se pudo actualizar\n"
                        
                        with open(log_file, 'a', encoding='utf-8') as f:
                            f.write(log_msg + "\n")
                else:
                    no_encontradas += 1
                    with open(log_file, 'a', encoding='utf-8') as f:
                        f.write(f"INFO - Inscripción: {inscripcion_id} (codigo_colegiado: {codigo_colegiado}, curso: {referencia_curso})\n")
                        f.write(f"  Facturas revisadas: {len(facturas)}\n")
                        f.write(f"  No se encontró factura con producto {referencia_curso} en Odoo\n")
                
                # Log informativo cada 50 inscripciones
                if total_procesadas % 50 == 0:
                    print(f"Procesadas: {total_procesadas} inscripciones, Corregidas: {corregidas}, No encontradas: {no_encontradas}")
                    
            except Exception as e:
                errores += 1
                with open(log_file, 'a', encoding='utf-8') as f:
                    f.write(f"ERROR procesando inscripción {inscripcion_id}: {str(e)}\n")
                print(f"✗ Error procesando inscripción {inscripcion_id}: {e}")
        
        # Escribir resumen final
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write("\n" + "=" * 80 + "\n")
            f.write("RESUMEN FINAL\n")
            f.write("=" * 80 + "\n")
            f.write(f"Total inscripciones procesadas: {total_procesadas}\n")
            f.write(f"Total corregidas: {corregidas}\n")
            f.write(f"Total no encontradas: {no_encontradas}\n")
            f.write(f"Total errores: {errores}\n")
            f.write("=" * 80 + "\n")
        
        return {
            'total_procesadas': total_procesadas,
            'corregidas': corregidas,
            'no_encontradas': no_encontradas,
            'errores': errores,
            'log_file': log_file,
            'log_folder': ruta_carpeta
        }
        
    except Exception as e:
        print(f"✗ Error crítico: {e}")
        if 'log_file' in locals():
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(f"ERROR CRÍTICO: {str(e)}\n")
        raise
    finally:
        if mongo_client:
            mongo_client.close()
        if server:
            server.stop()

# ========= EJECUTAR CORRECCIÓN =========
try:
    print("=" * 80)
    result = asyncio.run(corregir_n_factura_inscripciones())
    
    print("\n" + "=" * 80)
    print("RESULTADO")
    print("=" * 80)
    print(f"\n✓ Corrección completada exitosamente")
    print("\n" + "-" * 80)
    print("ESTADÍSTICAS:")
    print("-" * 80)
    print(f"Total inscripciones procesadas: {result['total_procesadas']}")
    if not DRY_RUN:
        print(f"Inscripciones corregidas: {result['corregidas']}")
    else:
        print(f"Inscripciones que se corregirían: {result['corregidas']}")
    print(f"Inscripciones sin factura encontrada: {result['no_encontradas']}")
    print(f"Errores: {result['errores']}")
    print("\n" + "-" * 80)
    print("LOGS:")
    print("-" * 80)
    print(f"Carpeta de logs: {result['log_folder']}")
    print(f"Archivo de log: {result['log_file']}")
    print("=" * 80)
    
except Exception as e:
    print(f"\n✗ Error ejecutando corrección: {e}")
    import traceback
    traceback.print_exc()
