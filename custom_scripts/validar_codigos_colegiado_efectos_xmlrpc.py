#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script para validar y corregir discrepancias de código de colegiado 
entre facturas en Odoo y efectos en MongoDB usando XML-RPC

Compara el código de colegiado de cada factura en Odoo con el código 
del efecto correspondiente en MongoDB (usando n_factura como referencia)
y actualiza el código de colegiado en MongoDB si no coinciden.

Este script es completamente independiente y no requiere modificar módulos de Odoo.
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
# Opción 1: Conexión directa (si MongoDB es accesible directamente)
# Si tienes MongoDB local o accesible directamente, configura esto:
MONGODB_URL_DIRECTA = None  # Ejemplo: "mongodb://usuario:password@localhost:27017/database"
                            # O "mongodb://localhost:27017/database" si no requiere autenticación

# Opción 2: Conexión vía SSH Tunnel (si MongoDB está en servidor remoto)
SSH_HOST = None  # Ejemplo: "192.168.1.100" o None si es conexión directa
SSH_USER = None
SSH_PASSWORD = None
SSH_PORT = 22
MONGO_PORT = 27017  # Puerto remoto de MongoDB
MONGO_DB_NAME = "tu_base_datos"  # Nombre de la base de datos MongoDB
MONGO_URL_TEMPLATE = None  # Se construirá automáticamente si se usa SSH

# ====== OPCIONES ======
DRY_RUN = False  # Cambia a True para solo validar sin corregir
LIMIT_FACTURAS = 5000  # Límite de facturas a procesar

# ========= CONEXIÓN ODOO =========
print("=" * 80)
print("VALIDACIÓN DE CÓDIGOS DE COLEGIADO ENTRE ODOO Y MONGODB")
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
        {'fields': ['ssh_host', 'ssh_user', 'ssh_pass', 'mongo_port', 'mongo_db', 'mongo_db_url'], 'limit': 1}
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
        print(f"✓ Configuración obtenida de Odoo")
        print(f"  SSH Host: {SSH_HOST if SSH_HOST else 'No configurado (conexión directa)'}")
        print(f"  MongoDB DB: {MONGO_DB_NAME}")
    else:
        print("⚠ No se encontró configuración en Odoo, usando valores por defecto")
        print("  Asegúrate de configurar las variables en el script")
except Exception as e:
    print(f"⚠ Error obteniendo configuración de Odoo: {e}")
    print("  Usando valores por defecto del script")

# ========= PREPARAR CONFIGURACIÓN MONGODB =========
# Guardamos la configuración para crear la conexión dentro del event loop
usar_ssh = bool(SSH_HOST and SSH_USER and SSH_PASSWORD)
print(f"\nConfiguración MongoDB:")
print(f"  Modo: {'SSH Tunnel' if usar_ssh else 'Conexión directa'}")
print(f"  MongoDB DB: {MONGO_DB_NAME}")

# ========= FUNCIÓN ASÍNCRONA PARA VALIDAR =========
async def validar_codigos_colegiado():
    """Función asíncrona que realiza la validación y corrección"""
    
    server = None
    mongo_client = None
    
    try:
        # Crear conexión MongoDB dentro del event loop
        print("Conectando a MongoDB...")
        
        if usar_ssh:
            # Conexión vía SSH Tunnel
            print(f"  Creando túnel SSH a {SSH_HOST}...")
            
            server = SSHTunnelForwarder(
                (SSH_HOST, SSH_PORT),
                ssh_username=SSH_USER,
                ssh_password=SSH_PASSWORD,
                remote_bind_address=('127.0.0.1', MONGO_PORT)
            )
            server.start()
            print(f"  ✓ Túnel SSH establecido (puerto local: {server.local_bind_port})")
            
            # Conectar a MongoDB a través del túnel
            if MONGO_URL_TEMPLATE:
                mongo_url = MONGO_URL_TEMPLATE.replace(str(MONGO_PORT), str(server.local_bind_port))
            else:
                # Construir URL por defecto si no hay template
                mongo_url = f"mongodb://127.0.0.1:{server.local_bind_port}/{MONGO_DB_NAME}"
        else:
            # Conexión directa (local o sin SSH)
            if MONGODB_URL_DIRECTA:
                # Usar URL directa configurada
                mongo_url = MONGODB_URL_DIRECTA
            elif MONGO_URL_TEMPLATE:
                # Usar template sin SSH (reemplazar puerto remoto por local)
                mongo_url = MONGO_URL_TEMPLATE.replace(f":{MONGO_PORT}/", f":27017/")
                if "{port}" in mongo_url:
                    mongo_url = mongo_url.replace("{port}", "27017")
            else:
                # Conexión local por defecto
                mongo_url = f"mongodb://localhost:27017/{MONGO_DB_NAME}"
            
            print(f"  URL: {mongo_url.split('@')[-1] if '@' in mongo_url else mongo_url}")
        
        # Crear cliente MongoDB dentro del event loop
        mongo_client = AsyncIOMotorClient(mongo_url, uuidRepresentation="standard")
        
        # Probar conexión
        await mongo_client.admin.command('ping')
        print(f"✓ Conectado a MongoDB\n")
        
        # Estadísticas
        total_procesadas = 0
        discrepancias = 0
        corregidas = 0
        errores = 0
        
        # Crear carpeta de logs
        ruta_carpeta = os.path.join(os.path.expanduser("~"), "logs_validacion_codigos_colegiado")
        if not os.path.exists(ruta_carpeta):
            os.makedirs(ruta_carpeta)
        
        now = datetime.now()
        fecha_hora_str = now.strftime("%Y-%m-%d_%H-%M-%S")
        log_file = os.path.join(ruta_carpeta, f"validacion_codigos_colegiado_{fecha_hora_str}.log")
        
        # Obtener base de datos y colección
        db = mongo_client[MONGO_DB_NAME]
        db_efectos = db.efectos
        
        # Buscar la factura específica primero para debugging
        print("Buscando factura específica INV/2025/33768...")
        try:
            factura_especifica = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'account.move',
                'search_read',
                [[('id', '=', 544105)]],
                {'fields': ['name', 'partner_codigo_colegiado', 'move_type', 'state', 'payment_state', 'partner_id']}
            )
            if factura_especifica:
                fact = factura_especifica[0]
                print(f"DEBUG: Factura encontrada por ID:")
                print(f"  ID: {fact.get('id')}")
                print(f"  Nombre: {fact.get('name')}")
                print(f"  partner_codigo_colegiado: {fact.get('partner_codigo_colegiado')}")
                print(f"  move_type: {fact.get('move_type')}")
                print(f"  state: {fact.get('state')}")
                print(f"  payment_state: {fact.get('payment_state')}")
                print(f"  partner_id: {fact.get('partner_id')}")
                
                # Obtener el código de colegiado del partner directamente
                if fact.get('partner_id'):
                    partner_id = fact.get('partner_id')[0] if isinstance(fact.get('partner_id'), list) else fact.get('partner_id')
                    partner_info = models.execute_kw(
                        ODOO_DB, uid, ODOO_PASSWORD,
                        'res.partner',
                        'read',
                        [[partner_id]],
                        {'fields': ['codigo_colegiado', 'name']}
                    )
                    if partner_info:
                        print(f"  Partner codigo_colegiado directo: {partner_info[0].get('codigo_colegiado')}")
                        print(f"  Partner name: {partner_info[0].get('name')}")
                print()
        except Exception as e:
            print(f"DEBUG: Error buscando factura específica: {e}\n")
        
        # Buscar facturas en Odoo con código de colegiado
        # Usar método más robusto: buscar partners con codigo_colegiado y luego sus facturas
        print("Buscando facturas en Odoo...")
        
        # Buscar partners con codigo_colegiado != 0
        partners_con_codigo = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'res.partner',
            'search',
            [[('codigo_colegiado', '!=', 0)]],
            {}
        )
        
        print(f"  Encontrados {len(partners_con_codigo)} partners con código de colegiado")
        
        # Buscar facturas de esos partners (método más confiable)
        facturas = []
        if partners_con_codigo:
            # Procesar en lotes para evitar límites
            batch_size = 1000
            for i in range(0, len(partners_con_codigo), batch_size):
                batch_partners = partners_con_codigo[i:i+batch_size]
                facturas_batch = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    'account.move',
                    'search_read',
                    [[
                        ('partner_id', 'in', batch_partners),
                        ('move_type', 'in', ['out_invoice', 'out_refund']),
                        ('state', '=', 'posted')
                    ]],
                    {'fields': ['name', 'partner_id'], 'limit': LIMIT_FACTURAS}
                )
                facturas.extend(facturas_batch)
        
        # Obtener codigo_colegiado del partner para cada factura
        print(f"  Procesando {len(facturas)} facturas para obtener códigos de colegiado...")
        
        # Agrupar por partner_id para optimizar consultas
        partners_a_consultar = {}
        for factura in facturas:
            if factura.get('partner_id'):
                partner_id = factura['partner_id'][0] if isinstance(factura['partner_id'], list) else factura['partner_id']
                if partner_id not in partners_a_consultar:
                    partners_a_consultar[partner_id] = []
                partners_a_consultar[partner_id].append(factura)
        
        # Obtener códigos de colegiado de partners en lotes
        partner_ids_list = list(partners_a_consultar.keys())
        for i in range(0, len(partner_ids_list), 100):
            batch_partner_ids = partner_ids_list[i:i+100]
            partners_info = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'res.partner',
                'read',
                [batch_partner_ids],
                {'fields': ['codigo_colegiado']}
            )
            
            # Asignar codigo_colegiado a las facturas
            for partner_info in partners_info:
                partner_id = partner_info['id']
                codigo_colegiado = partner_info.get('codigo_colegiado')
                if partner_id in partners_a_consultar:
                    for factura in partners_a_consultar[partner_id]:
                        factura['partner_codigo_colegiado'] = codigo_colegiado
        
        # Filtrar facturas que tienen codigo_colegiado válido
        facturas = [f for f in facturas if f.get('partner_codigo_colegiado')]
        
        # Eliminar duplicados por ID
        facturas_dict = {}
        for f in facturas:
            factura_id = f.get('id')
            if factura_id not in facturas_dict:
                facturas_dict[factura_id] = f
        
        facturas = list(facturas_dict.values())
        
        print(f"✓ Encontradas {len(facturas)} facturas para procesar\n")
        
        # Verificar si la factura específica está en la lista
        factura_en_lista = [f for f in facturas if f.get('name') == 'INV/2025/33768' or f.get('id') == 544105]
        if factura_en_lista:
            print(f"DEBUG: Factura INV/2025/33768 encontrada en la lista con código: {factura_en_lista[0].get('partner_codigo_colegiado')}\n")
        else:
            print(f"DEBUG: Factura INV/2025/33768 NO encontrada en la lista de facturas filtradas\n")
            print(f"DEBUG: Esto puede deberse a que partner_codigo_colegiado es 0 o None\n")
        print("Iniciando validación...")
        print(f"Modo: {'DRY RUN (solo validación)' if DRY_RUN else 'EJECUCIÓN REAL (corrige discrepancias)'}")
        print("-" * 80)
        
        for factura in facturas:
            total_procesadas += 1
            codigo_colegiado_odoo = factura.get('partner_codigo_colegiado')
            n_factura = factura.get('name')
            
            if not codigo_colegiado_odoo or not n_factura:
                errores += 1
                with open(log_file, 'a', encoding='utf-8') as f:
                    f.write(f"ERROR: Factura {n_factura} sin código de colegiado o número de factura\n")
                continue
            
            try:
                # Buscar TODOS los efectos en MongoDB por número de factura
                efectos_encontrados = 0
                efectos_procesados = 0
                efectos_sin_discrepancia = 0
                
                # Debug para factura específica
                debug_factura = (n_factura == 'INV/2025/33768')
                if debug_factura:
                    print(f"DEBUG: Buscando efectos para factura {n_factura} con código Odoo: {codigo_colegiado_odoo}")
                
                async for efecto_mongo in db_efectos.find({'n_factura': n_factura}):
                    if debug_factura:
                        print(f"DEBUG: Efecto encontrado - n_efecto: {efecto_mongo.get('n_efecto')}, codigo_colegiado: {efecto_mongo.get('codigo_colegiado')}")
                    efectos_encontrados += 1
                    codigo_colegiado_mongo = efecto_mongo.get('codigo_colegiado')
                    n_efecto = efecto_mongo.get('n_efecto', 'N/A')
                    
                    # Comparar códigos de colegiado (convertir a int para comparación segura)
                    codigo_odoo_int = int(codigo_colegiado_odoo) if codigo_colegiado_odoo else None
                    codigo_mongo_int = int(codigo_colegiado_mongo) if codigo_colegiado_mongo is not None else None
                    
                    if debug_factura:
                        print(f"DEBUG: Comparando - Odoo: {codigo_odoo_int} (tipo: {type(codigo_colegiado_odoo)}), MongoDB: {codigo_mongo_int} (tipo: {type(codigo_colegiado_mongo)})")
                    
                    if codigo_mongo_int != codigo_odoo_int:
                        discrepancias += 1
                        efectos_procesados += 1
                        
                        log_msg = f"DISCREPANCIA - Factura: {n_factura}, Efecto: {n_efecto}\n"
                        log_msg += f"  Odoo (partner): {codigo_colegiado_odoo}\n"
                        log_msg += f"  MongoDB (efecto): {codigo_colegiado_mongo}\n"
                        
                        # Corregir la discrepancia solo si no es DRY_RUN
                        if DRY_RUN:
                            log_msg += f"  [DRY RUN] No se aplicarán correcciones\n"
                            with open(log_file, 'a', encoding='utf-8') as f:
                                f.write(log_msg + "\n")
                        else:
                            try:
                                # Actualizar el código de colegiado en MongoDB
                                myquery = {"_id": efecto_mongo["_id"]}
                                now_update = datetime.now()
                                efecto_update = {
                                    "$set": {
                                        'codigo_colegiado': codigo_odoo_int,  # Usar el valor convertido a int
                                        'changed_at': now_update
                                    }
                                }
                                if debug_factura:
                                    print(f"DEBUG: Actualizando efecto {n_efecto} con código {codigo_odoo_int}")
                                result = await db_efectos.update_one(myquery, efecto_update)
                                
                                if result.modified_count > 0:
                                    log_msg += f"  ACCIÓN: Código de colegiado actualizado en MongoDB ({codigo_colegiado_odoo})\n"
                                    corregidas += 1
                                else:
                                    log_msg += f"  ADVERTENCIA: No se pudo actualizar el efecto\n"
                                
                                with open(log_file, 'a', encoding='utf-8') as f:
                                    f.write(log_msg + "\n")
                                    
                            except Exception as e:
                                errores += 1
                                log_msg += f"  ERROR al corregir: {str(e)}\n"
                                with open(log_file, 'a', encoding='utf-8') as f:
                                    f.write(log_msg + "\n")
                                print(f"✗ Error al corregir código de colegiado para factura {n_factura}, efecto {n_efecto}: {e}")
                    else:
                        # Los códigos coinciden
                        efectos_sin_discrepancia += 1
                
                # Registrar facturas sin efectos en MongoDB (importante para debugging)
                if efectos_encontrados == 0:
                    msg = f"INFO - Factura: {n_factura} (Código Odoo: {codigo_colegiado_odoo}) - No se encontró efecto en MongoDB\n"
                    with open(log_file, 'a', encoding='utf-8') as f:
                        f.write(msg)
                    if debug_factura:
                        print(f"DEBUG: {msg.strip()}")
                elif efectos_sin_discrepancia > 0 and efectos_procesados == 0:
                    # Factura con efectos pero todos coinciden (solo registrar si hay múltiples efectos para no saturar el log)
                    if efectos_encontrados > 1:
                        with open(log_file, 'a', encoding='utf-8') as f:
                            f.write(f"OK - Factura: {n_factura} - Todos los efectos coinciden ({efectos_encontrados} efecto(s), Código: {codigo_colegiado_odoo})\n")
                
                # Log informativo cada 100 facturas
                if total_procesadas % 100 == 0:
                    print(f"Procesadas: {total_procesadas} facturas, Discrepancias: {discrepancias}, Corregidas: {corregidas}")
                    
            except Exception as e:
                errores += 1
                with open(log_file, 'a', encoding='utf-8') as f:
                    f.write(f"ERROR procesando factura {n_factura}: {str(e)}\n")
                print(f"✗ Error procesando factura {n_factura}: {e}")
        
        # Escribir resumen final en el log
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write("\n" + "=" * 80 + "\n")
            f.write("RESUMEN FINAL\n")
            f.write("=" * 80 + "\n")
            f.write(f"Total facturas procesadas: {total_procesadas}\n")
            f.write(f"Total discrepancias encontradas: {discrepancias}\n")
            f.write(f"Total efectos corregidos: {corregidas}\n")
            f.write(f"Total errores: {errores}\n")
            f.write("=" * 80 + "\n")
        
        return {
            'total_procesadas': total_procesadas,
            'discrepancias': discrepancias,
            'corregidas': corregidas,
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
        # Cerrar conexiones
        if mongo_client:
            mongo_client.close()
        if server:
            server.stop()
            print("\n✓ Conexiones cerradas")

# ========= EJECUTAR VALIDACIÓN =========
try:
    print("=" * 80)
    result = asyncio.run(validar_codigos_colegiado())
    
    print("\n" + "=" * 80)
    print("RESULTADO")
    print("=" * 80)
    print(f"\n✓ Validación completada exitosamente")
    print("\n" + "-" * 80)
    print("ESTADÍSTICAS:")
    print("-" * 80)
    print(f"Total facturas procesadas: {result['total_procesadas']}")
    print(f"Discrepancias encontradas: {result['discrepancias']}")
    if not result.get('dry_run', DRY_RUN):
        print(f"Efectos corregidos: {result['corregidas']}")
    else:
        print(f"Efectos que se corregirían: {result['discrepancias']}")
    print(f"Errores: {result['errores']}")
    print("\n" + "-" * 80)
    print("LOGS:")
    print("-" * 80)
    print(f"Carpeta de logs: {result['log_folder']}")
    print(f"Archivo de log: {result['log_file']}")
    print("\n" + "-" * 80)
    print("IMPORTANTE:")
    print("Revisa el archivo de log para ver el detalle de las discrepancias")
    print("encontradas y corregidas.")
    print("=" * 80)
    
except Exception as e:
    print(f"\n✗ Error ejecutando validación: {e}")
    import traceback
    traceback.print_exc()
