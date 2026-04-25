#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Script XML-RPC para Odoo 17.
Importa o actualiza clientes (res.partner) desde un archivo CSV/Excel.
Requiere pasar la ruta del archivo y el ID de la compañía por parámetros.
"""

import argparse
import csv
import datetime
import os
import sys
import xmlrpc.client

# ---------------------------------------------------------------------------
# CONFIGURACIÓN DEL ENTORNO
# ---------------------------------------------------------------------------

ODOO_URL = os.environ.get("ODOO_URL", "https://staging-segurymat.v17.srv2.clodofy.net")
ODOO_DB = os.environ.get("ODOO_DB", "staging-segurymat.v17.srv2.clodofy.net")
ODOO_USER = os.environ.get("ODOO_USER", "admin")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "admin")

# ---------------------------------------------------------------------------
# CLIENTE XML-RPC
# ---------------------------------------------------------------------------

def get_xmlrpc_proxies():
    base = ODOO_URL.rstrip("/")
    common = xmlrpc.client.ServerProxy(f"{base}/xmlrpc/2/common", allow_none=True)
    models = xmlrpc.client.ServerProxy(f"{base}/xmlrpc/2/object", allow_none=True)
    return common, models

def authenticate(common):
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        raise RuntimeError("No se pudo autenticar en Odoo. Revisa credenciales.")
    return uid

# ---------------------------------------------------------------------------
# LÓGICA DE BÚSQUEDA Y RELACIONES
# ---------------------------------------------------------------------------

def get_country_id(models, uid, country_name):
    """Busca el ID del país por su nombre."""
    if not country_name:
        return False
    country_ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, 'res.country', 'search',
        [[('name', 'ilike', country_name)]], {'limit': 1}
    )
    return country_ids[0] if country_ids else False

def get_state_id(models, uid, state_name, country_id):
    """Busca el ID de la provincia/estado por su nombre y país."""
    if not state_name:
        return False
    domain = [('name', 'ilike', state_name)]
    if country_id:
        domain.append(('country_id', '=', country_id))
    state_ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, 'res.country.state', 'search',
        [domain], {'limit': 1}
    )
    return state_ids[0] if state_ids else False

# ---------------------------------------------------------------------------
# LÓGICA DE IMPORTACIÓN / ACTUALIZACIÓN
# ---------------------------------------------------------------------------

def sync_partners(models, uid, filepath, company_id):
    created = []
    updated = []
    errors = []

    # Caché para no consultar el mismo país/provincia repetidas veces
    cache_countries = {}
    cache_states = {}

    try:
        with open(filepath, mode='r', encoding='utf-8-sig') as file:
            reader = csv.reader(file, delimiter=',')
            headers = next(reader, None) # Saltar la cabecera
            
            for index, row in enumerate(reader, start=2):
                if not row or not row[0].strip():
                    continue # Saltar filas vacías
                
                try:
                    # Mapeo basado en el formato de tu CSV
                    # 0:Nombre, 1:Tel1, 2:Tel2, 3:Correo, 4:CP, 5:Dirección, 6:Ciudad, 7:Provincia, 8:Pais, 9:Contactos, 10:Nif
                    nombre = row[0].strip()
                    tel1 = row[1].strip() if len(row) > 1 else ""
                    tel2 = row[2].strip() if len(row) > 2 else ""
                    email = row[3].strip() if len(row) > 3 else ""
                    cp = row[4].strip() if len(row) > 4 else ""
                    direccion = row[5].strip() if len(row) > 5 else ""
                    ciudad = row[6].strip() if len(row) > 6 else ""
                    provincia_nombre = row[7].strip() if len(row) > 7 else ""
                    pais_nombre = row[8].strip() if len(row) > 8 else ""
                    nif = row[10].strip() if len(row) > 10 else ""

                    # 1. Resolver relaciones (País y Provincia)
                    country_id = False
                    if pais_nombre:
                        if pais_nombre not in cache_countries:
                            cache_countries[pais_nombre] = get_country_id(models, uid, pais_nombre)
                        country_id = cache_countries[pais_nombre]

                    state_id = False
                    if provincia_nombre:
                        state_key = f"{provincia_nombre}_{country_id}"
                        if state_key not in cache_states:
                            cache_states[state_key] = get_state_id(models, uid, provincia_nombre, country_id)
                        state_id = cache_states[state_key]

                    # 2. Preparar el diccionario de valores
                    partner_vals = {
                        'name': nombre,
                        'phone': tel1,
                        'mobile': tel2,
                        'email': email,
                        'zip': cp,
                        'street': direccion,
                        'city': ciudad,
                        'vat': nif,
                        'company_id': company_id,
                        'customer_rank': 1, # Para que Odoo lo marque como cliente
                    }
                    if country_id: partner_vals['country_id'] = country_id
                    if state_id: partner_vals['state_id'] = state_id

                    # 3. Buscar si el partner ya existe (Por NIF + Company, o Nombre + Company)
                    domain = [('company_id', 'in', [company_id, False])]
                    if nif:
                        domain.append(('vat', '=', nif))
                    else:
                        domain.append(('name', '=', nombre))

                    existing_partner_ids = models.execute_kw(
                        ODOO_DB, uid, ODOO_PASSWORD, 'res.partner', 'search',
                        [domain], {'limit': 1}
                    )

                    if existing_partner_ids:
                        # ACTUALIZAR
                        partner_id = existing_partner_ids[0]
                        models.execute_kw(
                            ODOO_DB, uid, ODOO_PASSWORD, 'res.partner', 'write',
                            [[partner_id], partner_vals]
                        )
                        print(f"[UPDATED] {nombre} (ID: {partner_id})")
                        updated.append({'name': nombre, 'nif': nif, 'id': partner_id})
                    else:
                        # CREAR
                        new_id = models.execute_kw(
                            ODOO_DB, uid, ODOO_PASSWORD, 'res.partner', 'create',
                            [partner_vals]
                        )
                        print(f"[CREATED] {nombre} (ID: {new_id})")
                        created.append({'name': nombre, 'nif': nif, 'id': new_id})
                        
                except Exception as exc:
                    msg = str(exc)
                    print(f"[ERROR] Fila {index} ({row[0]}): {msg}")
                    errors.append({'row': index, 'name': row[0] if row else 'N/A', 'error': msg})
                    
    except Exception as exc:
        raise RuntimeError(f"Error procesando el archivo CSV: {exc}")

    return created, updated, errors

# ---------------------------------------------------------------------------
# GENERACIÓN DE INFORME MARKDOWN
# ---------------------------------------------------------------------------

def build_markdown_report(created, updated, errors, company_id):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []

    lines.append(f"# Importación de Clientes - Compañía ID: {company_id}\n\n")
    lines.append(f"- Fecha: {now}\n")
    lines.append(f"- Total clientes creados: {len(created)}\n")
    lines.append(f"- Total clientes actualizados: {len(updated)}\n")
    lines.append(f"- Total errores: {len(errors)}\n")

    if created:
        lines.append("\n## Clientes Creados\n\n")
        for c in created:
            lines.append(f"- `ID {c['id']}` - **{c['name']}** (NIF: `{c['nif']}`)\n")

    if updated:
        lines.append("\n## Clientes Actualizados\n\n")
        for u in updated:
            lines.append(f"- `ID {u['id']}` - **{u['name']}** (NIF: `{u['nif']}`)\n")

    if errors:
        lines.append("\n## Errores\n\n")
        for e in errors:
            lines.append(f"- Fila {e['row']} - {e['name']}: {e['error']}\n")

    return "".join(lines)

def save_markdown_report(content, directory="."):
    os.makedirs(directory, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(directory, f"import_partners_report_{ts}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path

# ---------------------------------------------------------------------------
# ENTRADA PRINCIPAL
# ---------------------------------------------------------------------------

def main():
    # Parseo de argumentos de línea de comandos
    parser = argparse.ArgumentParser(description="Importar o actualizar clientes en Odoo desde CSV.")
    parser.add_argument("-f", "--file", required=True, help="Ruta al archivo CSV a importar.")
    parser.add_argument("-c", "--company", required=True, type=int, help="ID de la compañía en Odoo.")
    args = parser.parse_args()

    file_path = args.file
    company_id = args.company

    if not os.path.isfile(file_path):
        print(f"[FATAL] El archivo no existe: {file_path}", file=sys.stderr)
        sys.exit(1)

    print("[INFO] Conectando a Odoo vía XML-RPC...")
    common, models = get_xmlrpc_proxies()

    print("[INFO] Autenticando usuario técnico de Odoo...")
    uid = authenticate(common)
    print(f"[INFO] Autenticado en Odoo como '{ODOO_USER}' (uid={uid})")

    print(f"[INFO] Iniciando procesamiento del archivo {file_path} para la compañía ID {company_id}...")
    created, updated, errors = sync_partners(models, uid, file_path, company_id)

    print("\n[INFO] Generando informe Markdown...")
    md_content = build_markdown_report(created, updated, errors, company_id)
    md_path = save_markdown_report(md_content, ".")
    print(f"[INFO] Informe Markdown guardado en: {md_path}")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\n[FATAL] El script ha fallado: {exc}", file=sys.stderr)
        sys.exit(1)