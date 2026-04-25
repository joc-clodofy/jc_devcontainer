#!/usr/bin/env python3
"""
Script para cambiar la versión de los repositorios OCA.
Lee los módulos del addons_path del archivo de configuración y hace checkout
a la rama especificada. Los repos sin esa rama se omiten y se continúa con el resto.
"""

import argparse
import configparser
import subprocess
import sys
from pathlib import Path


def parse_addons_path(config_path: Path) -> list[str]:
    """Extrae los paths del addons_path del archivo de configuración."""
    parser = configparser.ConfigParser()
    parser.read(config_path)
    if "options" not in parser or "addons_path" not in parser["options"]:
        return []
    addons_path = parser["options"]["addons_path"]
    return [p.strip() for p in addons_path.split(",") if p.strip()]


def extract_oca_repos(addons_paths: list[str]) -> list[str]:
    """Extrae los nombres de repos OCA (paths que contienen /oca/)."""
    repos = []
    seen = set()
    for path_str in addons_paths:
        if "/oca/" in path_str:
            repo_name = Path(path_str).name
            if repo_name and repo_name not in seen:
                seen.add(repo_name)
                repos.append(repo_name)
    return repos


def get_oca_base_path(config_path: Path) -> Path:
    """Obtiene la ruta base de los repos OCA (directorio del config + oca/)."""
    return config_path.resolve().parent / "oca"


def branch_exists(repo_path: Path, version: str) -> bool:
    """Verifica si existe la rama (local o remota)."""
    for ref in [version, f"origin/{version}"]:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", ref],
            cwd=repo_path,
            capture_output=True,
        )
        if result.returncode == 0:
            return True
    return False


def checkout_repo(repo_path: Path, version: str) -> tuple[bool, str]:
    """
    Hace checkout a la rama especificada en el repo.
    Retorna (éxito, mensaje).
    """
    if not (repo_path / ".git").exists():
        return False, "no es un repo git"

    try:
        subprocess.run(
            ["git", "fetch", "origin", version],
            cwd=repo_path,
            capture_output=True,
            timeout=60,
        )

        if not branch_exists(repo_path, version):
            return False, "rama no existe"

        subprocess.run(
            ["git", "checkout", version],
            cwd=repo_path,
            capture_output=True,
            check=True,
        )

        subprocess.run(
            ["git", "pull", "origin", version],
            cwd=repo_path,
            capture_output=True,
            timeout=60,
        )

        return True, "OK"

    except subprocess.CalledProcessError as e:
        err = e.stderr.decode().strip() if e.stderr else "error en git"
        return False, err or "error en git"
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, str(e)


def main() -> int:
    arg_parser = argparse.ArgumentParser(
        description="Cambia la versión de los repos OCA según el addons_path del archivo de configuración."
    )
    arg_parser.add_argument(
        "--conf",
        required=True,
        help="Archivo de configuración (ej: odoo.conf, inbiku.conf)",
    )
    arg_parser.add_argument(
        "--v",
        "--version",
        dest="version",
        required=True,
        help="Versión/rama a la que hacer checkout (ej: 16.0, 17.0)",
    )
    args = arg_parser.parse_args()

    config_path = Path(args.conf)
    if not config_path.exists():
        print(f"Error: archivo no encontrado: {config_path}", file=sys.stderr)
        return 1

    addons_paths = parse_addons_path(config_path)
    if not addons_paths:
        print("Error: no se encontró addons_path en [options]", file=sys.stderr)
        return 1

    oca_repos = extract_oca_repos(addons_paths)
    oca_base = get_oca_base_path(config_path)

    if not oca_base.exists():
        print(f"Error: directorio OCA no encontrado: {oca_base}", file=sys.stderr)
        return 1

    print(f"Config: {config_path}")
    print(f"Versión: {args.version}")
    print(f"Repos OCA: {len(oca_repos)}")
    print("-" * 50)

    ok_count = 0
    skip_count = 0
    fail_count = 0

    for repo in oca_repos:
        repo_path = oca_base / repo
        if not repo_path.exists():
            print(f"  {repo}: directorio no existe")
            fail_count += 1
            continue

        success, msg = checkout_repo(repo_path, args.version)
        if success:
            print(f"  {repo}: OK")
            ok_count += 1
        elif "rama no existe" in msg:
            print(f"  {repo}: omitido (sin rama {args.version})")
            skip_count += 1
        else:
            print(f"  {repo}: fallo ({msg})")
            fail_count += 1

    print("-" * 50)
    print(f"Resumen: {ok_count} OK, {skip_count} omitidos, {fail_count} fallos")

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
