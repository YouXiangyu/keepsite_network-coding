#!/usr/bin/env bash
set -euo pipefail
RELEASE="${1:?Release directory required}"
[[ $EUID -eq 0 ]] || { echo 'Root required' >&2; exit 1; }
[[ "$RELEASE" =~ ^/opt/memoverse/releases/[a-f0-9]{7,40}$ ]] || exit 2
[[ -f "$RELEASE/demo/dist/client/index.html" && -x "$RELEASE/backend/.venv/bin/python" ]] || exit 2
PREVIOUS="$(readlink -f /opt/memoverse/current || true)"
activate_link() {
  ln -sfn "$1" /opt/memoverse/current.next
  mv -Tf /opt/memoverse/current.next /opt/memoverse/current
}
activate_link "$RELEASE"
systemctl restart memoverse
for attempt in {1..30}; do
  if curl --fail --silent http://127.0.0.1:8000/api/health >/dev/null; then
    systemctl enable memoverse
    echo "Active release: $RELEASE"
    exit 0
  fi
  sleep 1
done
if [[ -n "$PREVIOUS" && "$PREVIOUS" != "$RELEASE" ]]; then
  activate_link "$PREVIOUS"
  systemctl restart memoverse
  echo "Health check failed; restored $PREVIOUS" >&2
else
  systemctl stop memoverse
  echo 'Initial release failed health check; service stopped.' >&2
fi
exit 1
