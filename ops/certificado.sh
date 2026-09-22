#!/bin/sh
# Certificado autofirmado para https://$CERT_IP:8081, válido 825 días (el máximo
# que aceptan Chrome y Safari). Corre dentro del contenedor `certificado`:
#
#   docker compose run --rm certificado && docker compose restart nginx
#
# Lo correcto a largo plazo es un nombre DNS de TI con su certificado; entonces
# se reemplazan certs/cert.pem y certs/cert.key y este script deja de usarse.
set -eu
ip="${CERT_IP:?falta CERT_IP}"
dir="${CERT_DIR:-/certs}"      # el buscador usa su propio certificado
uid="${CERT_UID:-1001}"       # quién puede leer la llave dentro del contenedor
openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
  -keyout "$dir/cert.key" -out "$dir/cert.pem" \
  -subj "/CN=$ip/O=HUB Label Studio" \
  -addext "subjectAltName=IP:$ip" \
  -addext "extendedKeyUsage=serverAuth" 2>/dev/null
# La llave queda legible solo para el nginx del contenedor (uid 1001), no para
# los demás usuarios del servidor compartido.
chown "$uid":0 "$dir/cert.key" "$dir/cert.pem"
chmod 600 "$dir/cert.key"
chmod 644 "$dir/cert.pem"
openssl x509 -in "$dir/cert.pem" -noout -subject -enddate -fingerprint -sha256
