#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Script XML-RPC para actualizar la preferencia de notificaciones de los usuarios
de Odoo, de modo que dejen de recibirlas por correo y pasen a recibirlas en
el buzón interno de Odoo ("inbox").

No realiza ninguna ejecución automática al importarse; solo al llamarse como
script principal.
"""

import datetime
import os
import sys
import xmlrpc.client


# ---------------------------------------------------------------------------
# CONFIGURACIÓN (tomada de AJUSTE_COLFISIO.py:68-71)
# ---------------------------------------------------------------------------

ODOO_URL = os.environ.get("ODOO_URL", "https://segurymat.v17.srv2.clodofy.net")
ODOO_DB = os.environ.get("ODOO_DB", "segurymat.v17.srv2.clodofy.cloud")
ODOO_USER = os.environ.get("ODOO_USER", "admin")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "admin")


# ---------------------------------------------------------------------------
# CLIENTE XML-RPC
# ---------------------------------------------------------------------------

def get_xmlrpc_proxies():
    """Devuelve los proxies XML-RPC a los endpoints 'common' y 'object'."""
    base = ODOO_URL.rstrip("/")
    common = xmlrpc.client.ServerProxy(f"{base}/xmlrpc/2/common", allow_none=True)
    models = xmlrpc.client.ServerProxy(f"{base}/xmlrpc/2/object", allow_none=True)
    return common, models


def authenticate(common):
    """Autentica contra Odoo y devuelve el uid."""
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        raise RuntimeError("No se pudo autenticar en Odoo. Revisa credenciales.")
    return uid


# ---------------------------------------------------------------------------
# LÓGICA DE ACTUALIZACIÓN DE USUARIOS
# ---------------------------------------------------------------------------

def fetch_users_to_update(models, uid):
    """
    Obtiene los usuarios cuya preferencia de notificación está en 'email'.

    Se limita a usuarios internos activos (share=False, active=True), para no
    tocar usuarios portal/compartidos.
    """
    domain = [
        ("notification_type", "=", "email"),
        ("share", "=", False),
        ("active", "=", True),
    ]
    fields = ["id", "name", "login", "notification_type", "active", "share"]
    users = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "res.users",
        "search_read",
        [domain],
        {"fields": fields},
    )
    return users


def update_users_notification(models, uid, users):
    """
    Actualiza notification_type='inbox' para cada usuario y devuelve:
    (lista_actualizados, lista_errores).
    """
    updated = []
    errors = []

    for user in users:
        user_id = user["id"]
        label = f"{user.get('name')} (login={user.get('login')}, id={user_id})"
        try:
            ok = models.execute_kw(
                ODOO_DB,
                uid,
                ODOO_PASSWORD,
                "res.users",
                "write",
                [[user_id], {"notification_type": "inbox"}],
            )
            if ok:
                print(f"[OK] Actualizado {label} -> notification_type='inbox'")
                user_copy = dict(user)
                user_copy["new_notification_type"] = "inbox"
                updated.append(user_copy)
            else:
                msg = "write devolvió False"
                print(f"[ERROR] No se pudo actualizar {label}: {msg}")
                errors.append({"user": user, "error": msg})
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            print(f"[ERROR] Excepción actualizando {label}: {msg}")
            errors.append({"user": user, "error": msg})

    return updated, errors


# ---------------------------------------------------------------------------
# GENERACIÓN DE INFORME MARKDOWN
# ---------------------------------------------------------------------------

def build_markdown_report(updated, errors):
    """Construye un informe Markdown con usuarios afectados y errores."""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []

    lines.append("# Actualización de preferencias de notificación de usuarios\n\n")
    lines.append(f"- Fecha: {now}\n")
    lines.append(f"- Total usuarios actualizados: {len(updated)}\n")
    lines.append(f"- Total errores: {len(errors)}\n")

    if updated:
        lines.append("\n## Usuarios actualizados\n\n")
        for u in updated:
            lines.append(
                f"- `ID {u.get('id')}` - **{u.get('name')}** "
                f"(`{u.get('login')}`): `email` → `inbox`\n"
            )

    if errors:
        lines.append("\n## Errores\n\n")
        for e in errors:
            u = e.get("user") or {}
            lines.append(
                f"- Usuario `ID {u.get('id')}` - {u.get('name')} "
                f"(`{u.get('login')}`): {e.get('error')}\n"
            )

    return "".join(lines)


def save_markdown_report(content, directory="."):
    """Guarda el informe Markdown en un fichero y devuelve su ruta."""
    os.makedirs(directory, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(directory, f"update_odoo_notifications_report_{ts}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


# ---------------------------------------------------------------------------
# ENTRADA PRINCIPAL
# ---------------------------------------------------------------------------

def main():
    print("[INFO] Conectando a Odoo vía XML-RPC...")
    common, models = get_xmlrpc_proxies()

    print("[INFO] Autenticando usuario técnico de Odoo...")
    uid = authenticate(common)
    print(f"[INFO] Autenticado en Odoo como '{ODOO_USER}' (uid={uid})")

    print("[INFO] Buscando usuarios con notification_type='email'...")
    users = fetch_users_to_update(models, uid)
    print(f"[INFO] Usuarios encontrados para actualizar: {len(users)}")

    print("[INFO] Actualizando preferencias de notificación a 'inbox'...")
    updated, errors = update_users_notification(models, uid, users)

    print("\n[INFO] Generando informe Markdown...")
    md_content = build_markdown_report(updated, errors)
    md_path = save_markdown_report(md_content, ".")
    print(f"[INFO] Informe Markdown guardado en: {md_path}")

    print("\n[INFO] Informe Markdown (vista previa):\n")
    print(md_content)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"[FATAL] El script ha fallado: {exc}", file=sys.stderr)
        sys.exit(1)

