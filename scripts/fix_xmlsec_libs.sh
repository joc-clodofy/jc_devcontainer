#!/usr/bin/env bash
# Recompila lxml y xmlsec contra la misma libxml2 del sistema.
# Necesario cuando aparece: "lxml & xmlsec libxml2 library version mismatch"
set -euo pipefail

cd /workspace

if ! pkg-config --exists libxml-2.0 libxmlsec1; then
    echo "Faltan librerías de sistema. Ejecuta como root en el contenedor:"
    echo "  apt-get update && apt-get install -y libxmlsec1-dev libxmlsec1-openssl pkg-config libxml2-dev"
    exit 1
fi

uv pip install --force-reinstall --no-binary lxml --no-binary xmlsec "lxml==5.2.1" "xmlsec==1.3.17"

.venv/bin/python -c "from lxml import etree; import xmlsec; print('OK: lxml libxml', etree.LIBXML_VERSION, '| xmlsec', xmlsec.__version__)"
