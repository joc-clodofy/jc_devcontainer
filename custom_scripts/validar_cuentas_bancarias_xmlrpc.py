#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script para validar y corregir discrepancias de cuentas bancarias 
entre colegiados en Odoo y datos en MongoDB usando XML-RPC

Compara la cuenta bancaria de cada colegiado en Odoo con la cuenta 
correspondiente en MongoDB (usando codigo_colegiado como referencia)
y corrige automáticamente las discrepancias.

USO:
====

1. Desde dentro del contenedor (recomendado):
   docker exec -it odoo-dev python3 /workspace/validar_cuentas_bancarias_xmlrpc.py

2. Desde fuera del contenedor:
   python3 validar_cuentas_bancarias_xmlrpc.py
   # Asegúrate de que ODOO_URL esté configurado:
   export ODOO_URL=http://localhost

3. Con variables de entorno (opcional):
   export ODOO_URL=http://localhost:8069
   export ODOO_DB=colfisio
   export ODOO_USER=admin
   export ODOO_PASSWORD=123
   export DRY_RUN=true  # Para solo validar sin corregir
   python3 validar_cuentas_bancarias_xmlrpc.py

NOTAS:
- El script detecta automáticamente si está dentro o fuera del contenedor
- Si Odoo está corriendo en el contenedor, usa localhost:8069
- Si estás fuera del contenedor, usa http://localhost (a través de nginx)
"""

import xmlrpc.client
import os
import socket
from datetime import datetime

# ====== CONFIGURA ESTO ======
# Detectar automáticamente la URL de Odoo según el entorno
def detect_odoo_url():
    """Detecta automáticamente la URL de Odoo según el entorno"""
    # Primero verificar variables de entorno
    env_url = os.getenv('ODOO_URL')
    if env_url:
        return env_url
    
    # Verificar si estamos dentro del contenedor Docker
    # Intentar conectarse a localhost:8069 primero
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(('localhost', 8069))
        sock.close()
        if result == 0:
            return "http://localhost:8069"
    except:
        pass
    
    # Si estamos fuera del contenedor, usar nginx (puerto 80 o 443)
    # O intentar con el hostname del contenedor
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(('localhost', 80))
        sock.close()
        if result == 0:
            return "http://localhost"
    except:
        pass
    
    # Por defecto, usar localhost:8069
    return "http://localhost:8069"

url = detect_odoo_url()  # Detección automática
db = os.getenv('ODOO_DB', 'colfisio')  # Cambia por el nombre de tu base de datos
username = os.getenv('ODOO_USER', 'admin')  # Cambia por tu usuario
password = os.getenv('ODOO_PASSWORD', '123')  # Cambia por tu contraseña

DRY_RUN = os.getenv('DRY_RUN', 'False').lower() == 'true'  # Cambia a True para solo validar sin corregir

# ========= CONEXIÓN =========
print("=" * 80)
print("VALIDACIÓN DE CUENTAS BANCARIAS ENTRE ODOO Y MONGODB")
print("=" * 80)
print(f"\nConectando a Odoo...")
print(f"URL: {url}")
print(f"Base de datos: {db}")
print(f"Usuario: {username}")

try:
    print(f"Intentando conectar a: {url}/xmlrpc/2/common")
    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    
    # Verificar versión de Odoo primero
    try:
        version = common.version()
        print(f"✓ Versión de Odoo detectada: {version.get('server_version', 'N/A')}")
    except:
        print("⚠ No se pudo obtener la versión de Odoo, pero continuando...")
    
    uid = common.authenticate(db, username, password, {})
    if not uid:
        raise Exception("Error de autenticación: Credenciales incorrectas o base de datos no existe")
    
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
    print(f"✓ Conectado como usuario ID: {uid}\n")
except xmlrpc.client.ProtocolError as e:
    print(f"✗ Error de protocolo HTTP: {e.errcode} - {e.errmsg}")
    print(f"  URL intentada: {url}/xmlrpc/2/common")
    print("\n💡 Posibles soluciones:")
    print("  1. Verifica que Odoo esté corriendo: docker exec odoo-dev ps aux | grep odoo-bin")
    print("  2. Si estás fuera del contenedor, usa: export ODOO_URL=http://localhost")
    print("  3. Si estás dentro del contenedor, verifica que Odoo escuche en el puerto 8069")
    exit(1)
except Exception as e:
    print(f"✗ Error de conexión: {e}")
    print(f"  URL intentada: {url}/xmlrpc/2/common")
    print("\n💡 Posibles soluciones:")
    print("  1. Verifica que Odoo esté corriendo")
    print("  2. Verifica la URL de Odoo (usa variable de entorno ODOO_URL si es necesario)")
    print("  3. Verifica las credenciales y el nombre de la base de datos")
    import traceback
    traceback.print_exc()
    exit(1)

# ========= EJECUTAR VALIDACIÓN =========
print("Iniciando validación y corrección de cuentas bancarias...")
print(f"Modo: {'DRY RUN (solo validación)' if DRY_RUN else 'EJECUCIÓN REAL (corrige discrepancias)'}")
print("-" * 80)

try:
    # Llamar al método de validación pasando el parámetro dry_run
    result = models.execute_kw(
        db, uid, password,
        'res.partner',
        'ejecutar_validacion_cuentas_bancarias',
        [DRY_RUN],  # Pasar dry_run como parámetro posicional
        {}
    )
    
    print("\n✓ Validación completada")
    print("\n" + "=" * 80)
    print("RESULTADO")
    print("=" * 80)
    
    if result and isinstance(result, dict):
        success = result.get('success', False)
        message = result.get('message', 'Sin mensaje')
        log_file = result.get('log_file', 'No disponible')
        log_folder = result.get('log_folder', 'No disponible')
        error = result.get('error', None)
        
        # Estadísticas
        total_procesadas = result.get('total_procesadas', 0)
        discrepancias = result.get('discrepancias', 0)
        corregidas = result.get('corregidas', 0)
        errores = result.get('errores', 0)
        
        if success:
            print(f"\n✓ {message}")
            print("\n" + "-" * 80)
            print("ESTADÍSTICAS:")
            print("-" * 80)
            print(f"Total colegiados procesados: {total_procesadas}")
            print(f"Discrepancias encontradas: {discrepancias}")
            if not result.get('dry_run', False):
                print(f"Cuentas bancarias corregidas: {corregidas}")
            else:
                print(f"Cuentas bancarias que se corregirían: {discrepancias}")
            print(f"Errores: {errores}")
            print("\n" + "-" * 80)
            print("LOGS:")
            print("-" * 80)
            print(f"Carpeta de logs: {log_folder}")
            if log_file:
                print(f"Archivo de log: {log_file}")
            print("\n" + "-" * 80)
            print("IMPORTANTE:")
            print("Revisa el archivo de log para ver el detalle de las discrepancias")
            print("encontradas y corregidas.")
        else:
            print(f"\n✗ Error: {message}")
            if error:
                print(f"Detalle: {error}")
            if total_procesadas > 0:
                print(f"\nProcesadas antes del error: {total_procesadas}")
                print(f"Discrepancias: {discrepancias}")
                print(f"Corregidas: {corregidas}")
    
    print("=" * 80)
    
except Exception as e:
    print(f"\n✗ Error ejecutando validación: {e}")
    import traceback
    traceback.print_exc()
    exit(1)

