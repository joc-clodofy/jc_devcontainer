# Odoo 16 Devcontainers

[![Watch the video](https://github.com/mjavint/devcontainers-odoo/blob/main/img/miniatura.png?raw=true)](https://youtu.be/I4vswyVg2K0)

## Instalación y Configuración

> Nota: para una guia actualizada de onboarding (Linux/macOS), revisa `DEVCONTAINER_GUIDE.md`.

1. Clonar el repositorio

```bash
git clone https://github.com/mjavint/devcontainers-odoo
```

2. Configurar entorno virtual de python

```bash
# Entrar al proyecto
cd devcontainers-odoo
# Sncronizar el proyecto
uv sync
# Activar el entorno
source .venv/bin/activate
```

3. Configurar variables del dev container

```bash
cp .devcontainer/.env.example .devcontainer/.env
# Edita ODOO_SERVER con una ruta absoluta local (Linux o macOS)
```

4. Crear red Docker externa requerida

```bash
docker network create shared-net
```

5. Instalar dependencias de odoo

```bash
uv pip install -r odoo.16.0/requirements.txt
```

6. Configurar el role odoo en la base de datos usando el servicio `pgadmin` instalado.
   ![pgadmin](https://github.com/mjavint/devcontainers-odoo/blob/main/img/pgadmin.png?raw=true)

7. Iniciar el servidor de odoo

```bash
python odoo.16.0/odoo-bin -c odoo.conf
```

## Enlaces útiles

- [Docker Desktop](https://docs.docker.com/get-started/get-docker/)
- [Visual Studio Code](https://code.visualstudio.com/)
- [Ultraviolet (UV) y Ruff](https://docs.astral.sh/)
- [Devcontainers](https://containers.dev)
