#!/usr/bin/env bash
set -euo pipefail
RELEASE="${1:?Release directory required}"
[[ $EUID -eq 0 ]] || { echo 'Root required' >&2; exit 1; }
[[ "$RELEASE" =~ ^/opt/memoverse/releases/[a-f0-9]{40}$ ]] || exit 2
[[ -d "$RELEASE" && "$(realpath "$RELEASE")" == "$RELEASE" ]] || exit 2
[[ -f "$RELEASE/demo/dist/client/index.html" && -x "$RELEASE/backend/.venv/bin/python" ]] || exit 2
[[ -s "$RELEASE/.artifact-sha256" && -f "$RELEASE/.prepared-sha256" ]] || { echo 'Release is not verified/prepared.' >&2; exit 2; }
cmp -s "$RELEASE/.artifact-sha256" "$RELEASE/.prepared-sha256" || exit 2
PREVIOUS=''
# GNU readlink -f can print a canonical path even when the final component is absent.
if [[ -e /opt/memoverse/current || -L /opt/memoverse/current ]]; then
  PREVIOUS="$(readlink -f /opt/memoverse/current || true)"
  [[ "$PREVIOUS" =~ ^/opt/memoverse/releases/[a-f0-9]{40}$ && -d "$PREVIOUS" ]] || { echo 'Existing current link is unexpected; not changed.' >&2; exit 2; }
fi
activate_link() {
  ln -sfn "$1" /opt/memoverse/current.next && mv -Tf /opt/memoverse/current.next /opt/memoverse/current
}
health() {
  local attempt
  for attempt in {1..30}; do
    if systemctl is-active --quiet memoverse && curl --fail --silent --connect-timeout 2 --max-time 3 http://127.0.0.1:8000/api/health >/dev/null; then return 0; fi
    sleep 1
  done
  return 1
}
install_unit() {
  install -o root -g root -m 0644 "$1/deploy/ubuntu/memoverse.service" /etc/systemd/system/memoverse.service && systemctl daemon-reload
}
if [[ "$PREVIOUS" == "$RELEASE" ]] && health; then
  if ! systemctl enable memoverse; then echo 'Release is healthy but enable-on-boot failed.' >&2; exit 1; fi
  echo "Already active and healthy: $RELEASE"
  exit 0
fi
# Reap the old process tree before changing paths used by its subprocesses.
if [[ -n "$PREVIOUS" ]] || systemctl is-active --quiet memoverse; then
  if ! systemctl stop memoverse; then echo 'Cannot stop old service; current link was not changed.' >&2; exit 1; fi
fi
if install_unit "$RELEASE" && activate_link "$RELEASE" && systemctl restart memoverse && health; then
  if ! systemctl enable memoverse; then echo 'Release is healthy but enable-on-boot failed.' >&2; exit 1; fi
  echo "Active release: $RELEASE"
  exit 0
fi
echo 'Activation failed; attempting rollback.' >&2
if [[ -n "$PREVIOUS" && "$PREVIOUS" != "$RELEASE" ]]; then
  if systemctl stop memoverse && install_unit "$PREVIOUS" && activate_link "$PREVIOUS" && systemctl restart memoverse && health; then
    echo "Restored and healthy: $PREVIOUS" >&2
  else
    echo "ROLLBACK FAILED. Inspect current link and service; desired previous release: $PREVIOUS" >&2
    exit 2
  fi
else
  systemctl stop memoverse || true
  echo 'No alternate prior release; service stopped. Data was not changed.' >&2
fi
exit 1
