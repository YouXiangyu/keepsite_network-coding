#!/usr/bin/env bash
set -euo pipefail
SOURCE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${1:-}" != --execute ]]; then
  printf 'Plan: install root-owned pull worker/SDK and a boot + five-minute timer. Existing app, data, secrets and auto-stop timer stay in place.\n'
  exit 0
fi
[[ $EUID -eq 0 ]] || { echo 'Root required' >&2; exit 1; }
ADMIN=/opt/keepsite/admin
install -d -o root -g root -m 0755 "$ADMIN"
[[ ! -L "$ADMIN" && "$(stat -c '%u' "$ADMIN")" == 0 ]] || exit 2
VENV="$ADMIN/autodeploy-venv"
if [[ -e "$VENV" || -L "$VENV" ]]; then
  [[ -d "$VENV" && ! -L "$VENV" && "$(stat -c '%u' "$VENV")" == 0 ]] || exit 2
else
  python3.12 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --only-binary=:all: --no-cache-dir boto3==1.43.114
for name in pull-release.py release_manifest.py prepare-release.py; do
  install -o root -g root -m 0644 "$SOURCE/$name" "$ADMIN/$name"
done
for name in keepsite-autodeploy.service keepsite-autodeploy.timer; do
  install -o root -g root -m 0644 "$SOURCE/$name" "/etc/systemd/system/$name"
done
systemd-analyze verify /etc/systemd/system/keepsite-autodeploy.service /etc/systemd/system/keepsite-autodeploy.timer
systemctl daemon-reload
systemctl enable --now keepsite-autodeploy.timer
systemctl start keepsite-autodeploy.service
systemctl list-timers keepsite-autodeploy.timer keepsite-auto-stop.timer --no-pager
