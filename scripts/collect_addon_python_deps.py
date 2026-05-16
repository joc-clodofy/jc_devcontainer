#!/usr/bin/env python3
"""
Recorre addons_path (happy.conf) y agrupa external_dependencies.python.
Opcional: escribe requirements-addons-manifests.txt en la raíz del workspace.

Incluye dependencias conocidas usadas por import pero no declaradas en __manifest__
(p. ej. l10n_es_reports -> xlsxwriter).
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from collections import defaultdict

# Manifest a veces usa nombre de import; pip suele ser otro.
PIP_NAME_FIXES = {
    "Pyjwt": "PyJWT",
    "pyjwt": "PyJWT",
    "stdnum": "python-stdnum",
    "OpenSSL": "pyOpenSSL",
    "PIL": "Pillow",
    "yaml": "PyYAML",
    "cv2": "opencv-python",
    "ldap": "python-ldap",
    "LDAP": "python-ldap",
    "serial": "pyserial",
    "google_auth": "google-auth",
    "py3o.formats": "py3o-formats",
    "py3o.template": "py3o-template",
}

# Paquetes que no deben ir a pip (stdlib 3.12+ o metapaquetes raros en dev)
SKIP_PIP = frozenset({"dataclasses"})

# Import usado en código sin external_dependencies en manifest (addons bajo path)
IMPLICIT_BY_MODULE = {
    "l10n_es_reports": ["xlsxwriter"],
}


def to_pip(name: str) -> str | None:
    s = name.strip()
    if not s or s in SKIP_PIP:
        return None
    return PIP_NAME_FIXES.get(s, s)


def parse_addons_path(conf_path: str) -> list[str]:
    with open(conf_path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^[ \t]*addons_path[ \t]*=[ \t]*(.+)$", line)
            if m:
                return [p.strip() for p in m.group(1).split(",") if p.strip()]
    raise SystemExit(f"No addons_path in {conf_path}")


def load_manifest_dict(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    for node in tree.body:
        if isinstance(node, ast.Expr):
            return ast.literal_eval(ast.unparse(node.value))
    return {}


def collect(conf_path: str) -> tuple[dict[str, set[str]], set[str], list[str]]:
    paths = parse_addons_path(conf_path)
    mod_to_raw: dict[str, set[str]] = defaultdict(set)
    raw_pkgs: set[str] = set()
    errors: list[str] = []

    for base in paths:
        if not os.path.isdir(base):
            errors.append(f"MISSING_DIR {base}")
            continue
        for dirpath, _dirnames, filenames in os.walk(base):
            if "__manifest__.py" not in filenames:
                continue
            mf = os.path.join(dirpath, "__manifest__.py")
            mod_name = os.path.basename(dirpath)
            try:
                data = load_manifest_dict(mf)
            except Exception as e:  # noqa: BLE001
                errors.append(f"PARSE_FAIL {mf}: {e}")
                continue
            ext = data.get("external_dependencies") or {}
            py = ext.get("python")
            if py:
                if isinstance(py, (list, tuple)):
                    pkgs = [str(x) for x in py]
                elif isinstance(py, str):
                    pkgs = [py]
                else:
                    errors.append(f"ODD_PYTHON_TYPE {mf} {type(py)}")
                    pkgs = []
                for p in pkgs:
                    p = p.strip()
                    if not p:
                        continue
                    mod_to_raw[mod_name].add(p)
                    raw_pkgs.add(p)
            # implícitos si existe el addon
            if mod_name in IMPLICIT_BY_MODULE:
                for imp in IMPLICIT_BY_MODULE[mod_name]:
                    mod_to_raw[mod_name].add(imp)
                    raw_pkgs.add(imp)

    return mod_to_raw, raw_pkgs, errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "-c",
        "--config",
        default=os.path.join(os.path.dirname(__file__), "..", "happy.conf"),
        help="Ruta a odoo.conf con addons_path",
    )
    ap.add_argument(
        "-w",
        "--write",
        metavar="FILE",
        help="Escribir lista pip (una spec por línea) en FILE",
    )
    args = ap.parse_args()
    conf = os.path.abspath(args.config)

    mod_to_raw, raw_pkgs, errors = collect(conf)

    paths = parse_addons_path(conf)
    print("# addons_path from:", conf)
    print("# --- requirements.txt encontrados ---")
    req_files: list[str] = []
    for base in paths:
        if not os.path.isdir(base):
            continue
        for dirpath, _dirnames, filenames in os.walk(base):
            if "requirements.txt" in filenames:
                req_files.append(os.path.join(dirpath, "requirements.txt"))
    for rf in sorted(set(req_files)):
        print(rf)

    print("\n# --- external_dependencies.python (+ implícitos), raw ---")
    for p in sorted(raw_pkgs):
        print(p)

    mapped_lines: list[str] = []
    for p in sorted(raw_pkgs):
        m = to_pip(p)
        if m:
            mapped_lines.append(m)
    # dedupe conservando orden estable
    seen: set[str] = set()
    deduped: list[str] = []
    for line in mapped_lines:
        if line not in seen:
            seen.add(line)
            deduped.append(line)

    print("\n# --- mapeado a pip (deduplicado) ---")
    for line in deduped:
        print(line)

    print("\n# --- por addon ---")
    for mod in sorted(mod_to_raw):
        parts = []
        for x in sorted(mod_to_raw[mod]):
            m = to_pip(x)
            parts.append(m if m else x)
        print(f"{mod}: {', '.join(parts)}")

    if args.write:
        out = os.path.abspath(args.write)
        header = (
            "# Generado por scripts/collect_addon_python_deps.py -w\n"
            "# No editar a mano: regenerar tras cambiar manifests o happy.conf.\n"
            "# Instalar: uv pip install -r requirements-addons-manifests.txt\n"
        )
        with open(out, "w", encoding="utf-8") as f:
            f.write(header)
            for line in deduped:
                f.write(line + "\n")
        print(f"\n# escrito: {out}", file=sys.stderr)

    if errors:
        print("\n# --- avisos ---", file=sys.stderr)
        for e in errors:
            print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
