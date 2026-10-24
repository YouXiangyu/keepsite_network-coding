#!/usr/bin/env bash
set -euo pipefail
DOMAIN="${1:?Domain required}"
EXPECTED_IP="${2:?Expected public IPv4 required}"
[[ "$DOMAIN" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}$ ]] || exit 2
[[ "$EXPECTED_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || exit 2
echo "Plan: require $DOMAIN to resolve to $EXPECTED_IP, obtain a webroot certificate, enable TLS with existing BasicAuth and certificate renewal."
if [[ "${3:-}" != --execute ]]; then echo 'No changes made. Add --execute.'; exit 0; fi
[[ $EUID -eq 0 ]] || { echo 'Root required' >&2; exit 1; }
getent ahostsv4 "$DOMAIN" | awk '{print $1}' | sort -u | grep -Fxq "$EXPECTED_IP" || { echo 'DNS has not reached this server yet; no certificate request made.' >&2; exit 1; }
[[ -s /etc/nginx/keepsite.htpasswd ]] || { echo 'Prepare BasicAuth first.' >&2; exit 1; }
certbot certonly --webroot -w /var/www/letsencrypt -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email
TEMPLATE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/nginx.conf.template"
sed "s/__DOMAIN__/$DOMAIN/g" "$TEMPLATE" > /etc/nginx/sites-available/keepsite
nginx -t
systemctl reload nginx
install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
printf '#!/bin/sh\nnginx -t && systemctl reload nginx\n' > /etc/letsencrypt/renewal-hooks/deploy/keepsite-nginx
chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/keepsite-nginx
systemctl enable --now certbot.timer
echo "HTTPS enabled for https://$DOMAIN with private access control."
