#!/usr/bin/env bash
# TLS certificates for the nginx container (docker/nginx/certs/{fullchain,privkey}.pem).
#
#   scripts/init-tls.sh self-signed [hostname]      bootstrap / staging only (30 days)
#   scripts/init-tls.sh letsencrypt <domain> <email> real certificate via HTTP-01
#   scripts/init-tls.sh renew                        renew Let's Encrypt cert + reload nginx
#
# Let's Encrypt needs the stack running (nginx answers the challenge on port 80) and
# the domain's DNS pointing at this host. First boot: self-signed -> deploy -> letsencrypt.
# Renewal: run "scripts/init-tls.sh renew" daily from cron (any user that can run docker):
# certbot's files under docker/letsencrypt are root-owned and private, so they are copied
# into docker/nginx/certs by a short-lived container, not by the host user. Every failure
# exits non-zero so cron (MAILTO) reports it.
set -Eeuo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib/common.sh
. scripts/lib/common.sh
CERT_DIR="docker/nginx/certs"
ACME_DIR="docker/nginx/acme"
LE_DIR="docker/letsencrypt"
CERTBOT_IMAGE="${CERTBOT_IMAGE:-certbot/certbot:v2.11.0}"

install_files() {
    # Atomically replace the served certificate with files readable by the invoking user.
    local fullchain="$1" key="$2"
    mkdir -p "$CERT_DIR"
    ( umask 077 && cp "$fullchain" "$CERT_DIR/fullchain.pem.new" && cp "$key" "$CERT_DIR/privkey.pem.new" )
    chmod 644 "$CERT_DIR/fullchain.pem.new"
    chmod 600 "$CERT_DIR/privkey.pem.new"
    mv -f "$CERT_DIR/fullchain.pem.new" "$CERT_DIR/fullchain.pem"
    mv -f "$CERT_DIR/privkey.pem.new" "$CERT_DIR/privkey.pem"
}

install_letsencrypt_cert() {
    # certbot (root in its container) keeps keys root-only; copy them as root inside a
    # container and hand the copies to the invoking user, then install them atomically.
    local domain="$1" uid gid
    uid="$(id -u)"; gid="$(id -g)"
    mkdir -p "$CERT_DIR"
    docker run --rm --network none \
        -v "$PWD/$LE_DIR:/etc/letsencrypt:ro" \
        -v "$PWD/$CERT_DIR:/out" \
        --entrypoint sh "$CERTBOT_IMAGE" -c '
            set -eu
            live="/etc/letsencrypt/live/$1"
            umask 077
            cp -L "$live/fullchain.pem" /out/fullchain.pem.le
            cp -L "$live/privkey.pem" /out/privkey.pem.le
            chown "$2:$3" /out/fullchain.pem.le /out/privkey.pem.le
        ' sh "$domain" "$uid" "$gid" \
        || die "could not read the certificate for ${domain} from ${LE_DIR}"
    install_files "$CERT_DIR/fullchain.pem.le" "$CERT_DIR/privkey.pem.le"
    rm -f "$CERT_DIR/fullchain.pem.le" "$CERT_DIR/privkey.pem.le"
}

reload_for_cert() {
    reload_nginx || die "nginx did not accept the new certificate; check 'docker compose logs nginx'"
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
        install_files "$tmp/fullchain.pem" "$tmp/privkey.pem"
        log "Self-signed certificate for ${host} installed (30 days). Browsers will warn: not for production."
        reload_for_cert
        ;;
    letsencrypt)
        domain="${2:-}"; email="${3:-}"
        if [ -z "$domain" ] || [ -z "$email" ]; then
            die "usage: $0 letsencrypt <domain> <email>"
        fi
        [ -n "$(docker compose ps -q nginx 2>/dev/null)" ] \
            || die "start the stack first (scripts/deploy.sh); nginx must serve the HTTP-01 challenge"
        certbot certonly --webroot -w /var/www/acme -d "$domain" -m "$email" \
            --agree-tos --no-eff-email --non-interactive --keep-until-expiring
        install_letsencrypt_cert "$domain"
        printf '%s\n' "$domain" > "$CERT_DIR/.letsencrypt-domain"
        log "Let's Encrypt certificate for ${domain} installed"
        reload_for_cert
        ;;
    renew)
        domain_file="$CERT_DIR/.letsencrypt-domain"
        [ -f "$domain_file" ] || domain_file="$LE_DIR/.domain"  # location used by earlier versions
        [ -f "$domain_file" ] || die "no Let's Encrypt domain recorded; run '$0 letsencrypt' first"
        domain="$(cat "$domain_file")"
        certbot renew --webroot -w /var/www/acme --non-interactive \
            || die "certbot renew failed for ${domain}; the current certificate is still served"
        install_letsencrypt_cert "$domain"
        log "Certificate for ${domain} checked/renewed"
        reload_for_cert
        ;;
    *)
        die "usage: $0 self-signed [hostname] | letsencrypt <domain> <email> | renew"
        ;;
esac
