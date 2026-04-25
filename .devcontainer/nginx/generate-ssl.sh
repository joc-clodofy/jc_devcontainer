#!/bin/bash

# Script para generar certificados SSL autofirmados para desarrollo local

SSL_DIR="./ssl"
mkdir -p "$SSL_DIR"

# Generar clave privada
openssl genrsa -out "$SSL_DIR/key.pem" 2048

# Generar certificado autofirmado válido por 365 días
openssl req -new -x509 -key "$SSL_DIR/key.pem" -out "$SSL_DIR/cert.pem" -days 365 -subj "/C=ES/ST=State/L=City/O=Organization/CN=localhost"

# Ajustar permisos
chmod 600 "$SSL_DIR/key.pem"
chmod 644 "$SSL_DIR/cert.pem"

echo "Certificados SSL generados en $SSL_DIR/"
echo "Certificado: $SSL_DIR/cert.pem"
echo "Clave privada: $SSL_DIR/key.pem"



















