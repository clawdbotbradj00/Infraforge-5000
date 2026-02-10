#!/usr/bin/env bash
# Generate self-signed SSL certificate for phpIPAM
set -euo pipefail

SSL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ssl"
mkdir -p "$SSL_DIR"

if [[ -f "$SSL_DIR/phpipam-cert.pem" && -f "$SSL_DIR/phpipam-key.pem" ]]; then
    echo "SSL certificates already exist in $SSL_DIR"
    exit 0
fi

openssl req -x509 -nodes -days 3650 \
    -newkey rsa:2048 \
    -keyout "$SSL_DIR/phpipam-key.pem" \
    -out "$SSL_DIR/phpipam-cert.pem" \
    -subj "/C=US/ST=Local/L=Local/O=InfraForge/CN=phpipam.local" \
    2>/dev/null

echo "SSL certificates generated in $SSL_DIR"
