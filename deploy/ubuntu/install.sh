#!/usr/bin/env bash
# Prepare a pre-uploaded release. Never creates a password, certificate or swap.
set -euo pipefail
EXECUTE=0
INITIALIZE_HOST=0
RELEASE=''
DOMAIN=''
while (($#)); do
  case "$1" in
    --execute) EXECUTE=1; shift ;;
    --initialize-host) INITIALIZE_HOST=1; shift ;;
    --release) RELEASE="${2:?Missing --release value}"; shift 2 ;;
    --domain) DOMAIN="${2:?Missing --domain value}"; shift 2 ;;
    *) printf 'Usage: %s --release /opt/memoverse/releases/<commit> --domain <hostname> [--initialize-host] [--execute]\n' "$0" >&2; exit 2 ;;
  esac
done
[[ "$RELEASE" =~ ^/opt/memoverse/releases/[a-f0-9]{7,40}$ ]] || { printf 'Release must be /opt/memoverse/releases/<git-commit>.\n' >&2; exit 2; }
[[ "$DOMAIN" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}$ ]] || { printf 'A plain DNS hostname is required.\n' >&2; exit 2; }
cat <<PLAN
Plan for Ubuntu 24.04:
  Host initialization requested: $INITIALIZE_HOST (apt/user/global logs only when 1).
  Prepare memoverse service user and $RELEASE Python 3.12 venv.
  Link release .cache to /opt/memoverse/shared/cache; persist data in /var/lib/memoverse.
  Install pinned CPU inference dependencies and verify/download one MoGe2 model.
  Initial host setup writes defaults/log limits; activation installs the release service unit.
  Do NOT change current symlink, start application, enable a public Nginx site,
  create credentials/TLS/swap, or modify DNS. Planned origin: https://$DOMAIN
PLAN
if (( ! EXECUTE )); then printf 'No changes made. Add --execute to perform this plan.\n'; exit 0; fi
[[ $EUID -eq 0 ]] || { printf 'Execution requires root.\n' >&2; exit 1; }
source /etc/os-release
[[ "${ID:-}" == ubuntu && "${VERSION_ID:-}" == 24.04 ]] || { printf 'This installer targets Ubuntu 24.04 only.\n' >&2; exit 1; }
[[ -d "$RELEASE" && "$(realpath "$RELEASE")" == "$RELEASE" ]] || { printf 'Upload a real release directory first; symlink releases are rejected.\n' >&2; exit 1; }
for file in backend/requirements.txt scripts/setup-anyimage.sh deploy/ubuntu/memoverse.service deploy/ubuntu/nginx.conf.template demo/dist/client/index.html; do
  [[ -f "$RELEASE/$file" ]] || { printf 'Release missing %s\n' "$file" >&2; exit 1; }
done
if (( INITIALIZE_HOST )); then
  # Prevent first package installation from briefly exposing the default nginx site.
  # Do not replace or assume the behavior of an administrator's existing policy.
  [[ ! -e /usr/sbin/policy-rc.d && ! -L /usr/sbin/policy-rc.d ]] || { printf 'Existing policy-rc.d requires administrator review before package installation; it was not changed.\n' >&2; exit 1; }
  POLICY_CREATED=0
  cleanup_policy() { if (( POLICY_CREATED )); then rm -f -- /usr/sbin/policy-rc.d; fi; }
  trap cleanup_policy EXIT
  if [[ ! -e /usr/sbin/policy-rc.d ]]; then
    printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
    chmod 755 /usr/sbin/policy-rc.d
    POLICY_CREATED=1
  fi
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y nginx python3-venv python3-pip unzip git curl ca-certificates apache2-utils certbot libgomp1 logrotate time
  cleanup_policy
  POLICY_CREATED=0
  if ! id memoverse >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/memoverse --shell /usr/sbin/nologin memoverse
  fi
  install -d -o memoverse -g memoverse -m 0750 /var/lib/memoverse
  install -d -o memoverse -g memoverse -m 0755 /opt/memoverse/shared /opt/memoverse/shared/cache
  install -d -o memoverse -g memoverse -m 0750 /opt/memoverse/shared/cache/tmp /opt/memoverse/shared/cache/pip
  install -d -o root -g root -m 0755 /var/www/letsencrypt
  install -d -o root -g adm -m 0750 /var/log/memoverse
else
  id memoverse >/dev/null 2>&1 || { echo "Host not initialized; review --initialize-host." >&2; exit 1; }
  command -v python3.12 >/dev/null
  command -v nginx >/dev/null
  [[ -d /var/lib/memoverse && -d /opt/memoverse/shared/cache && -f /etc/memoverse.env ]] || { echo "Host paths/environment are incomplete." >&2; exit 1; }
fi
if [[ -e "$RELEASE/.cache" || -L "$RELEASE/.cache" ]]; then
  [[ -L "$RELEASE/.cache" && "$(realpath "$RELEASE/.cache")" == /opt/memoverse/shared/cache ]] || { printf 'Release .cache exists and is not the expected shared link; nothing removed.\n' >&2; exit 1; }
else
  ln -s /opt/memoverse/shared/cache "$RELEASE/.cache"
fi
if [[ ! -x "$RELEASE/backend/.venv/bin/python" ]]; then
  python3.12 -m venv "$RELEASE/backend/.venv"
  chown -R memoverse:memoverse "$RELEASE/backend/.venv"
fi
runuser -u memoverse -- env PIP_CACHE_DIR=/opt/memoverse/shared/cache/pip TMPDIR=/opt/memoverse/shared/cache/tmp \
  "$RELEASE/backend/.venv/bin/python" -B -m pip install --only-binary=:all: -r "$RELEASE/backend/requirements.txt"
runuser -u memoverse -- bash "$RELEASE/scripts/setup-anyimage.sh" --execute --python "$RELEASE/backend/.venv/bin/python"
if [[ ! -e /etc/memoverse.env ]]; then
  umask 077
  cat > /etc/memoverse.env <<ENV
MEMOVERSE_DATA_DIR=/var/lib/memoverse
MEMOVERSE_ALLOWED_ORIGINS=https://$DOMAIN,http://127.0.0.1:4173,http://localhost:4173
ANYIMAGE_PYTHON=/opt/memoverse/current/backend/.venv/bin/python
PYTHONDONTWRITEBYTECODE=1
HF_HUB_OFFLINE=1
OMP_NUM_THREADS=1
OPENBLAS_NUM_THREADS=1
MKL_NUM_THREADS=1
ENV
  chown root:memoverse /etc/memoverse.env
  chmod 0640 /etc/memoverse.env
fi
if (( INITIALIZE_HOST )); then
  install -d -o root -g root -m 0755 /etc/systemd/journald.conf.d
  cat > /etc/systemd/journald.conf.d/60-memoverse-limits.conf <<'JOURNAL'
[Journal]
SystemMaxUse=100M
RuntimeMaxUse=30M
MaxRetentionSec=7day
JOURNAL
  cat > /etc/logrotate.d/memoverse <<'ROTATE'
/var/log/memoverse/nginx-access.log /var/log/memoverse/nginx-error.log {
    daily
    maxsize 10M
    rotate 3
    missingok
    notifempty
    compress
    delaycompress
    sharedscripts
    postrotate
        if [ -s /run/nginx.pid ]; then kill -USR1 "$(cat /run/nginx.pid)"; fi
    endscript
}
ROTATE
fi
printf 'Preparation complete. Review/activate current release separately. No site or application was started.\n'
printf 'Existing environment was preserved. Journal limits apply after journald reload/restart; review this host-wide change before applying.\n'
