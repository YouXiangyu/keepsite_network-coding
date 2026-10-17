#!/usr/bin/env bash
# Switch code/config as one guarded transaction; never expose an old anonymous API.
set -euo pipefail
RELEASE="${1:?Release directory required}"
shift
ENVIRONMENT=''
NGINX=''
while (($#)); do
  case "$1" in
    --environment) ENVIRONMENT="${2:?Environment path required}"; shift 2 ;;
    --nginx) NGINX="${2:?Nginx path required}"; shift 2 ;;
    *) echo 'Unknown activation argument' >&2; exit 2 ;;
  esac
done
[[ $EUID -eq 0 ]] || { echo 'Root required' >&2; exit 1; }
[[ "$RELEASE" =~ ^/opt/memoverse/releases/[a-f0-9]{40}$ ]] || exit 2
[[ -d "$RELEASE" && "$(realpath "$RELEASE")" == "$RELEASE" ]] || exit 2
[[ -f "$RELEASE/demo/dist/client/index.html" && -x "$RELEASE/backend/.venv/bin/python" ]] || exit 2
[[ -s "$RELEASE/.artifact-sha256" && -f "$RELEASE/.prepared-sha256" ]] || { echo 'Release is not verified/prepared.' >&2; exit 2; }
cmp -s "$RELEASE/.artifact-sha256" "$RELEASE/.prepared-sha256" || exit 2
LIVE_ENV=/etc/memoverse.env
LIVE_SITE=/etc/nginx/sites-available/memoverse
LIVE_UNIT=/etc/systemd/system/memoverse.service
[[ -z "$ENVIRONMENT" || "$ENVIRONMENT" == /etc/memoverse.env.next ]] || { echo 'Unexpected staged environment path.' >&2; exit 2; }
[[ -z "$NGINX" || "$NGINX" == /etc/nginx/sites-available/memoverse.next ]] || { echo 'Unexpected staged Nginx path.' >&2; exit 2; }
# Serialize activation, including rollback, and protect its secret-bearing backup.
umask 077
install -d -o root -g root -m 0700 /opt/memoverse/activation-backups
exec 9>/opt/memoverse/activation-backups/.lock
flock -n 9 || { echo 'Another activation is running.' >&2; exit 2; }
check_file() {
  local path="$1" kind="$2" mode group
  [[ -f "$path" && ! -L "$path" && "$(realpath "$path")" == "$path" ]] || { echo 'Configuration must be a regular, non-symlink file.' >&2; return 1; }
  [[ "$(stat -c '%u' "$path")" == 0 ]] || { echo 'Configuration must be root-owned.' >&2; return 1; }
  mode="$(stat -c '%a' "$path")"
  if [[ "$kind" == secret ]]; then
    group="$(stat -c '%G' "$path")"
    [[ "$mode" == 600 || ( "$mode" == 640 && ( "$group" == root || "$group" == memoverse ) ) ]] || { echo 'Environment permissions must be root-only or root:memoverse 0640.' >&2; return 1; }
  else
    [[ "$mode" == 644 || "$mode" == 640 || "$mode" == 600 ]] || return 1
  fi
}
check_file "${ENVIRONMENT:-$LIVE_ENV}" secret
check_file "$RELEASE/deploy/ubuntu/memoverse.service" config
for path in "$LIVE_ENV" "$LIVE_SITE" "$LIVE_UNIT"; do
  if [[ -e "$path" || -L "$path" ]]; then
    check_file "$path" "$([[ "$path" == "$LIVE_ENV" ]] && echo secret || echo config)"
  fi
done
# Read environment syntax without executing it or emitting any values/secrets.
python3 - "${ENVIRONMENT:-$LIVE_ENV}" "$RELEASE" <<'PY'
import base64
from pathlib import Path
import sys
from urllib.parse import urlsplit
values = {}
for line in Path(sys.argv[1]).read_text().splitlines():
    line = line.strip()
    if not line or line.startswith('#'):
        continue
    if '=' not in line:
        raise SystemExit('Invalid environment entry')
    key, value = line.split('=', 1)
    if key in values:
        raise SystemExit('Duplicate environment key')
    if value.startswith(('"', "'")):
        if len(value) < 2 or value[-1] != value[0]:
            raise SystemExit('Unbalanced environment quoting')
        value = value[1:-1]
    values[key] = value
required = ['MEMOVERSE_COGNITO_REGION', 'MEMOVERSE_COGNITO_USER_POOL_ID',
            'MEMOVERSE_COGNITO_CLIENT_ID', 'MEMOVERSE_COGNITO_DOMAIN',
            'MEMOVERSE_PUBLIC_URL', 'MEMOVERSE_SESSION_KEY', 'MEMOVERSE_TEMPLATE_DIR']
if any(not values.get(key) for key in required):
    raise SystemExit('Authentication/template configuration is incomplete')
if values.get('MEMOVERSE_DATA_DIR') != '/var/lib/memoverse-v2':
    raise SystemExit('New application must use the separate schema-2 data directory')
if values.get('MEMOVERSE_STORAGE') != 's3' or not values.get('MEMOVERSE_S3_BUCKET') or not values.get('AWS_REGION'):
    raise SystemExit('Private S3 configuration is incomplete')
if values.get('ANYIMAGE_PYTHON') not in (None, '', sys.argv[2] + '/backend/.venv/bin/python'):
    raise SystemExit('ANYIMAGE_PYTHON must be omitted or point to this release interpreter')
for key in ('MEMOVERSE_PUBLIC_URL', 'MEMOVERSE_COGNITO_DOMAIN'):
    parsed = urlsplit(values[key])
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise SystemExit('Authentication URLs must be HTTPS origins')
try:
    assert len(base64.b64decode(values['MEMOVERSE_SESSION_KEY'], altchars=b'-_', validate=True)) == 32
except Exception:
    raise SystemExit('Session encryption key is invalid')
PY
PREVIOUS=''
if [[ -e /opt/memoverse/current || -L /opt/memoverse/current ]]; then
  PREVIOUS="$(readlink -f /opt/memoverse/current || true)"
  [[ "$PREVIOUS" =~ ^/opt/memoverse/releases/[a-f0-9]{40}$ && -d "$PREVIOUS" ]] || { echo 'Existing current link is unexpected.' >&2; exit 2; }
fi
BACKUP="$(mktemp -d /opt/memoverse/activation-backups/activation-XXXXXXXX)"
for pair in "environment:$LIVE_ENV" "nginx:$LIVE_SITE" "unit:$LIVE_UNIT"; do
  name="${pair%%:*}"; path="${pair#*:}"
  if [[ -f "$path" ]]; then cp -p -- "$path" "$BACKUP/$name"; else : > "$BACKUP/$name.absent"; fi
done
printf '%s\n' "$PREVIOUS" > "$BACKUP/previous-release"
if [[ -n "$ENVIRONMENT" ]]; then cp -p -- "$ENVIRONMENT" "$BACKUP/environment.next"; fi
if [[ -n "$NGINX" ]]; then
  check_file "$NGINX" config
  [[ -L /etc/nginx/sites-enabled/memoverse && "$(readlink -f /etc/nginx/sites-enabled/memoverse)" == "$LIVE_SITE" ]] || { echo 'Expected enabled Nginx site link is missing; configure it explicitly first.' >&2; exit 2; }
  cp -p -- "$NGINX" "$BACKUP/nginx.next"
  printf 'pid %s/nginx-test.pid; error_log stderr; events {} http { include /etc/nginx/mime.types; include %s/nginx.next; }\n' "$BACKUP" "$BACKUP" > "$BACKUP/nginx-test.conf"
  nginx -t -c "$BACKUP/nginx-test.conf" || { echo 'Staged Nginx syntax rejected; live configuration unchanged.' >&2; exit 2; }
fi
activate_link() { ln -sfn "$1" /opt/memoverse/current.next && mv -Tf /opt/memoverse/current.next /opt/memoverse/current; }
atomic_config() {
  local source="$1" target="$2" mode="$3"
  install -o root -g root -m "$mode" "$source" "$target.activate-$$" && mv -Tf "$target.activate-$$" "$target"
}
health() {
  local attempt
  for attempt in {1..30}; do
    if systemctl is-active --quiet memoverse && curl --fail --silent --connect-timeout 2 --max-time 3 http://127.0.0.1:8000/api/health >/dev/null; then return 0; fi
    sleep 1
  done
  return 1
}
auth_health() {
  local code
  health || return 1
  code="$(curl --silent --connect-timeout 2 --max-time 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/state)" || return 1
  [[ "$code" == 401 ]] || { echo 'Anonymous API access was not rejected.' >&2; return 1; }
  curl --fail --silent --connect-timeout 2 --max-time 5 http://127.0.0.1:8000/api/auth/session |
    python3 -c 'import json,sys; s=json.load(sys.stdin); assert s.get("authenticated") is False and s.get("configured") is True and s.get("providers",{}).get("email") is True and "user" not in s and "csrfToken" not in s'
}
configs_same=1
cmp -s "$LIVE_UNIT" "$RELEASE/deploy/ubuntu/memoverse.service" || configs_same=0
if [[ -n "$ENVIRONMENT" ]]; then cmp -s "$LIVE_ENV" "$BACKUP/environment.next" || configs_same=0; fi
if [[ -n "$NGINX" ]]; then cmp -s "$LIVE_SITE" "$BACKUP/nginx.next" || configs_same=0; fi
if [[ "$PREVIOUS" == "$RELEASE" && "$configs_same" == 1 ]] && auth_health; then
  systemctl enable memoverse || { echo 'Healthy release, but enable-on-boot failed.' >&2; exit 1; }
  echo "Already active, configured and authenticated: $RELEASE"
  exit 0
fi
NGINX_CHANGED=0
NGINX_STOPPED=0
restore_file() {
  local name="$1" target="$2"
  if [[ -f "$BACKUP/$name.absent" ]]; then rm -f -- "$target"; else cp -p -- "$BACKUP/$name" "$target"; fi
}
rollback() {
  echo "Activation failed; rollback evidence: $BACKUP" >&2
  # Never start the old anonymous application while the public site is installed.
  systemctl stop memoverse || { systemctl stop nginx || true; return 1; }
  if (( NGINX_CHANGED || NGINX_STOPPED )); then
    # A graceful reload can leave public workers alive on existing connections.
    # Stop them completely before restoring an older, possibly anonymous API.
    if ! { systemctl stop nginx && restore_file nginx "$LIVE_SITE" && nginx -t && systemctl start nginx; }; then
      systemctl stop nginx || true
      echo 'Cannot restore private Nginx configuration; old application remains stopped.' >&2
      return 1
    fi
  fi
  restore_file environment "$LIVE_ENV" && restore_file unit "$LIVE_UNIT" && systemctl daemon-reload || return 1
  if [[ -n "$PREVIOUS" ]]; then
    activate_link "$PREVIOUS" && systemctl restart memoverse && health || return 1
    echo "Restored previous code, environment and unit: $PREVIOUS" >&2
  else
    rm -f -- /opt/memoverse/current
    echo 'No prior release; application remains stopped.' >&2
  fi
}
if [[ -n "$PREVIOUS" || -f "$LIVE_UNIT" ]] || systemctl is-active --quiet memoverse; then
  systemctl stop memoverse || { echo 'Cannot stop old service; configuration/link unchanged.' >&2; exit 1; }
fi
switch_release() {
  if [[ -n "$NGINX" ]] && ! cmp -s "$LIVE_SITE" "$BACKUP/nginx.next"; then
    # Old site logs may record OAuth query strings. Keep the site offline during
    # the auth/config transition rather than sending new auth traffic through it.
    systemctl stop nginx || return 1
    NGINX_STOPPED=1
  fi
  if [[ -n "$ENVIRONMENT" ]]; then atomic_config "$BACKUP/environment.next" "$LIVE_ENV" 600 || return 1; fi
  atomic_config "$RELEASE/deploy/ubuntu/memoverse.service" "$LIVE_UNIT" 644 || return 1
  activate_link "$RELEASE" && systemctl daemon-reload && systemctl restart memoverse && auth_health || return 1
  if [[ -n "$NGINX" ]] && ! cmp -s "$LIVE_SITE" "$BACKUP/nginx.next"; then
    atomic_config "$BACKUP/nginx.next" "$LIVE_SITE" 644 || return 1
    NGINX_CHANGED=1
    nginx -t && systemctl start nginx || return 1
  fi
  return 0
}
if switch_release; then
  systemctl enable memoverse || { echo 'New release is healthy but enable-on-boot failed; retry activation.' >&2; exit 1; }
  echo "Active release with authenticated backend: $RELEASE; rollback evidence: $BACKUP"
  exit 0
fi
if rollback; then exit 1; fi
echo "ROLLBACK FAILED. Application/site may be stopped; inspect $BACKUP and service state." >&2
exit 2
