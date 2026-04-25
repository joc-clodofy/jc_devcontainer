#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Script XML-RPC para copiar el valor del campo de Odoo Studio 'x_comment'
al campo 'purchase_supplier_reference' en account.move (facturas/asientos).

Completa las credenciales abajo antes de ejecutar.
No realiza ninguna ejecución automática al importarse; solo al llamarse como
script principal.
"""

import datetime
import os
import sys
import xmlrpc.client
from argparse import ArgumentParser


# ---------------------------------------------------------------------------
# CONFIGURACIÓN — rellena las credenciales antes de ejecutar
# ---------------------------------------------------------------------------

ODOO_URL = os.environ.get("ODOO_URL", "https://cclean.v17.srv1.clodofy.net")  # ej: https://tu-instancia.odoo.com
ODOO_DB = os.environ.get("ODOO_DB", "cclean.v17.srv1.clodofy.cloud")   # nombre de la base de datos
ODOO_USER = os.environ.get("ODOO_USER","yanira@cclean.app")       # usuario (ej: admin)
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD","3030")  # contraseña


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
# LÓGICA: x_comment → purchase_supplier_reference
# ---------------------------------------------------------------------------

def fetch_moves_with_comment(models, uid):
    """
    Obtiene los account.move que tienen valor en el campo de Odoo Studio
    'x_comment', para copiarlo a 'purchase_supplier_reference'.
    """
    domain = [
        ("x_comment", "!=", False),
        ("x_comment", "!=", ""),
    ]
    fields = ["id", "name", "x_comment", "purchase_supplier_reference"]
    moves = models.execute_kw(
        ODOO_DB,
        uid,
        ODOO_PASSWORD,
        "account.move",
        "search_read",
        [domain],
        {"fields": fields},
    )
    return moves


def update_moves_reference(models, uid, moves, dry_run=False):
    """
    Copia el valor de x_comment a purchase_supplier_reference en cada
    account.move. Devuelve (lista_actualizados, lista_errores).
    """
    updated = []
    errors = []

    for move in moves:
        move_id = move["id"]
        name = move.get("name", "")
        x_comment_val = move.get("x_comment") or ""
        label = f"account.move ID {move_id} ({name})"
        try:
            if dry_run:
                print(
                    f"[DRY-RUN] Simular actualización {label}: "
                    f"x_comment → purchase_supplier_reference"
                )
                move_copy = dict(move)
                move_copy["new_purchase_supplier_reference"] = x_comment_val
                updated.append(move_copy)
                continue

            ok = models.execute_kw(
                ODOO_DB,
                uid,
                ODOO_PASSWORD,
                "account.move",
                "write",
                [[move_id], {"purchase_supplier_reference": x_comment_val}],
            )
            if ok:
                print(
                    f"[OK] Actualizado {label}: "
                    f"x_comment → purchase_supplier_reference"
                )
                move_copy = dict(move)
                move_copy["new_purchase_supplier_reference"] = x_comment_val
                updated.append(move_copy)
            else:
                msg = "write devolvió False"
                print(f"[ERROR] No se pudo actualizar {label}: {msg}")
                errors.append({"move": move, "error": msg})
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            print(f"[ERROR] Excepción actualizando {label}: {msg}")
            errors.append({"move": move, "error": msg})

    return updated, errors


# ---------------------------------------------------------------------------
# GENERACIÓN DE INFORME MARKDOWN
# ---------------------------------------------------------------------------

def build_markdown_report(updated, errors):
    """Construye un informe Markdown con movimientos afectados y errores."""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []

    lines.append("# Copia x_comment → purchase_supplier_reference (account.move)\n\n")
    lines.append(f"- Fecha: {now}\n")
    lines.append(f"- Total movimientos actualizados: {len(updated)}\n")
    lines.append(f"- Total errores: {len(errors)}\n")

    if updated:
        lines.append("\n## Movimientos actualizados\n\n")
        for m in updated:
            ref = (m.get("new_purchase_supplier_reference") or m.get("x_comment") or "")
            # Escapar posibles saltos de línea en el informe
            ref_short = (ref[:80] + "…") if len(ref) > 80 else ref
            lines.append(
                f"- `ID {m.get('id')}` - **{m.get('name')}**: "
                f"`purchase_supplier_reference` = \"{ref_short}\"\n"
            )

    if errors:
        lines.append("\n## Errores\n\n")
        for e in errors:
            m = e.get("move") or {}
            lines.append(
                f"- account.move `ID {m.get('id')}` - {m.get('name')}: {e.get('error')}\n"
            )

    return "".join(lines)


def save_markdown_report(content, directory="."):
    """Guarda el informe Markdown en un fichero y devuelve su ruta."""
    os.makedirs(directory, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(
        directory, f"update_account_move_comment_to_reference_report_{ts}.md"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


# ---------------------------------------------------------------------------
# ENTRADA PRINCIPAL
# ---------------------------------------------------------------------------

def main():
    if not ODOO_URL or not ODOO_DB or not ODOO_USER or not ODOO_PASSWORD:
        print(
            "[FATAL] Configura ODOO_URL, ODOO_DB, ODOO_USER y ODOO_PASSWORD "
            "en el script o con variables de entorno.",
            file=sys.stderr,
        )
        sys.exit(1)

    parser = ArgumentParser(
        description=(
            "Copia el valor del campo Odoo Studio 'x_comment' al campo "
            "'purchase_supplier_reference' en account.move."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "No realiza escrituras en Odoo, solo muestra qué se actualizaría y "
            "genera el informe."
        ),
    )
    args = parser.parse_args()

    dry_run = bool(args.dry_run)

    print("[INFO] Conectando a Odoo vía XML-RPC...")
    common, models = get_xmlrpc_proxies()

    print("[INFO] Autenticando...")
    uid = authenticate(common)
    print(f"[INFO] Autenticado como '{ODOO_USER}' (uid={uid})")

    print("[INFO] Buscando account.move con x_comment no vacío...")
    moves = fetch_moves_with_comment(models, uid)
    print(f"[INFO] Movimientos encontrados para actualizar: {len(moves)}")

    if not moves:
        print("[INFO] No hay movimientos que actualizar. Finalizando.")
        return

    if dry_run:
        print("[INFO] Modo DRY-RUN activado: no se escribirán cambios en Odoo.")
    else:
        print("[INFO] Copiando x_comment → purchase_supplier_reference...")

    updated, errors = update_moves_reference(models, uid, moves, dry_run=dry_run)

    print("\n[INFO] Generando informe Markdown...")
    md_content = build_markdown_report(updated, errors)
    md_path = save_markdown_report(md_content, ".")
    print(f"[INFO] Informe Markdown guardado en: {md_path}")

    print("\n[INFO] Vista previa del informe:\n")
    print(md_content)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"[FATAL] El script ha fallado: {exc}", file=sys.stderr)
        sys.exit(1)
