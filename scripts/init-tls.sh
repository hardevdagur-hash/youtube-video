#!/usr/bin/env bash
# TLS certificates for the nginx container (docker/nginx/certs/{fullchain,privkey}.pem).
#
#   scripts/init-tls.sh self-signed [hostname]      bootstrap / staging only (30 days)
#   scripts/init-tls.sh letsencrypt <domain> <email> real certificate via HTTP-01
#   scripts/init-tls.sh renew                        renew Let's Encrypt cert + reload nginx
#
# Let's Encrypt needs the stack running (nginx answers the challenge on port 80) and
# the domain's DNS pointing at this host. First boot: self-signed -> deploy -> letsencrypt.
# Renewal: run "scripts/init-tls.sh renew" daily from cron.
set -Eeuo pipefail

cd "$(dirname "$0")/.."
CERT_DIR="docker/nginx/certs"
ACME_DIR="docker/nginx/acme"
LE_DIR="docker/letsencrypt"
CERTBOT_IMAGE="${CERTBOT_IMAGE:-certbot/certbot:v2.11.0}"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

install_cert() {
    local fullchain="$1" key="$2"
    mkdir -p "$CERT_DIR"
    umask 077
    cp -L "$fullchain" "$CERT_DIR/fullchain.pem.new"
    cp -L "$key" "$CERT_DIR/privkey.pem.new"
    chmod 644 "$CERT_DIR/fullchain.pem.new"
    chmod 600 "$CERT_DIR/privkey.pem.new"
    mv -f "$CERT_DIR/fullchain.pem.new" "$CERT_DIR/fullchain.pem"
    mv -f "$CERT_DIR/privkey.pem.new" "$CERT_DIR/privkey.pem"
}

reload_nginx() {
    if [ -n "$(docker compose ps -q nginx 2>/dev/null)" ]; then
        docker compose exec -T nginx nginx -t && docker compose exec -T nginx nginx -s reload
        log "nginx reloaded"
    else
        log "nginx is not running; the certificate is used on next start"
    fi
}

certbot() {
    mkdir -p "$ACME_DIR" "$LE_DIR"
    docker run --rm \
        -v "$PWD/$ACME_DIR:/var/www/acme" \
        -v "$PWD/$LE_DIR:/etc/letsencrypt" \
        "$CERTBOT_IMAGE" "$@"
}

cmd="${1:-}"
case "$cmd" in
    self-signed)
        host="${2:-localhost}"
        command -v openssl >/dev/null || die "openssl is required"
        tmp="$(mktemp -d)"
        trap 'rm -rf "$tmp"' EXIT
        openssl req -x509 -nodes -newkey rsa:2048 -days 30 -sha256 \
            -subj "/CN=${host}" -addext "subjectAltName=DNS:${host}" \
            -keyout "$tmp/privkey.pem" -out "$tmp/fullchain.pem" 2>/dev/null
        install_cert "$tmp/fullchain.pem" "$tmp/privkey.pem"
        log "Self-signed certificate for ${host} installed (30 days). Browsers will warn: not for production."
        reload_nginx
        ;;
    letsencrypt)
        domain="${2:-}"; email="${3:-}"
        [ -n "$domain" ] && [ -n "$email" ] || die "usage: $0 letsencrypt <domain> <email>"
        [ -n "$(docker compose ps -q nginx 2>/dev/null)" ] \
            || die "start the stack first (scripts/deploy.sh); nginx must serve the HTTP-01 challenge"
        certbot certonly --webroot -w /var/www/acme -d "$domain" -m "$email" \
            --agree-tos --no-eff-email --non-interactive --keep-until-expiring
        install_cert "$LE_DIR/live/$domain/fullchain.pem" "$LE_DIR/live/$domain/privkey.pem"
        printf '%s\n' "$domain" > "$LE_DIR/.domain"
        log "Let's Encrypt certificate for ${domain} installed"
        reload_nginx
        ;;
    renew)
        [ -f "$LE_DIR/.domain" ] || die "no Let's Encrypt domain recorded; run '$0 letsencrypt' first"
        domain="$(cat "$LE_DIR/.domain")"
        certbot renew --webroot -w /var/www/acme --non-interactive
        install_cert "$LE_DIR/live/$domain/fullchain.pem" "$LE_DIR/live/$domain/privkey.pem"
        log "Certificate for ${domain} checked/renewed"
        reload_nginx
        ;;
    *)
        die "usage: $0 self-signed [hostname] | letsencrypt <domain> <email> | renew"
        ;;
esac
