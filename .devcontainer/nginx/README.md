# Configuración Nginx con HTTPS

Esta configuración permite acceder a Odoo a través de HTTPS usando nginx como proxy reverso.

## Características

- ✅ Proxy reverso con nginx
- ✅ HTTPS con certificados SSL autofirmados
- ✅ Redirección automática de HTTP a HTTPS
- ✅ Soporte para WebSockets (longpolling de Odoo)
- ✅ Headers de seguridad configurados

## Uso

### 1. Certificados SSL

Los certificados SSL autofirmados ya están generados en `./ssl/`. Si necesitas regenerarlos:

```bash
cd .devcontainer/nginx
./generate-ssl.sh
```

### 2. Iniciar los servicios

Al iniciar el devcontainer, nginx se iniciará automáticamente junto con los demás servicios.

### 3. Acceder a Odoo

- **HTTPS**: https://localhost
- **HTTP**: http://localhost (redirige automáticamente a HTTPS)

**Nota**: Como los certificados son autofirmados, tu navegador mostrará una advertencia de seguridad. Esto es normal en desarrollo. Puedes hacer clic en "Avanzado" y luego "Continuar a localhost" para acceder.

### 4. Puertos

- **80**: HTTP (redirige a HTTPS)
- **443**: HTTPS (acceso principal a Odoo)
- **8085**: pgAdmin (sin cambios)

## Configuración

La configuración de nginx está en `nginx.conf`. Incluye:

- Proxy reverso para Odoo en el puerto 8069
- Soporte para longpolling en el puerto 8072
- Headers de seguridad (HSTS, X-Frame-Options, etc.)
- Configuración SSL moderna (TLS 1.2 y 1.3)

## Solución de problemas

### El navegador muestra error de certificado

Esto es normal con certificados autofirmados. Acepta la excepción en tu navegador.

### No puedo acceder a Odoo

1. Verifica que los contenedores estén corriendo: `docker ps`
2. Verifica los logs de nginx: `docker logs nginx-proxy`
3. Verifica los logs de Odoo: `docker logs odoo-dev`

### Regenerar certificados

Si necesitas regenerar los certificados SSL:

```bash
cd .devcontainer/nginx
rm -rf ssl/
./generate-ssl.sh
docker-compose restart nginx
```



















