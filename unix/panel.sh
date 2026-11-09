#!/usr/bin/env bash
# Opens the UVMC panel in your browser, signed in. Starts the machine if needed.
# First asks which folders the agent may see this time (skip it with "don't ask again").
#   --folders   always ask about folders
set -euo pipefail
cd "$(dirname "$0")/.."
PORT=8080
force=""
[ "${1:-}" = "--folders" ] && force="--force"
unix/folders.sh $force || exit 0   # cancelled
multipass start uvmc || { echo "UVMC is not installed. Run unix/install.sh first."; exit 1; }
echo "Sharing the chosen folders..."
unix/share.sh || true
# Quick lookups, stopped after 20 s if the machine doesn't answer (see timeouts.sh).
. unix/timeouts.sh
code=0; out="$(multipass exec uvmc -- timeout -k 5 20 hostname -I)" || code=$?
check_timeout panel "hostname -I" 20 "$code" && { echo "Try again in a moment."; exit 1; }
ip="$(printf '%s\n' "$out" | awk '{print $1}')"
code=0; out="$(multipass exec uvmc -- timeout -k 5 20 sudo cat /etc/uvmc/panel_token 2>/dev/null)" || code=$?
check_timeout panel "sudo cat /etc/uvmc/panel_token" 20 "$code" && { echo "Try again in a moment."; exit 1; }
token="$(printf '%s' "$out" | tr -d '\r\n')"
[ -n "$ip" ] || { echo "Could not find the machine's address."; exit 1; }
[ -n "$token" ] || { echo "The panel is not set up in the machine yet. Run unix/update.sh, then try again."; exit 1; }
for _ in $(seq 30); do
  (exec 3<>"/dev/tcp/$ip/$PORT") 2>/dev/null && break
  sleep 1
done
url="http://$ip:$PORT/?token=$token"
if command -v open >/dev/null 2>&1; then open "$url"
elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$url"
else echo "Open this address in your browser: $url"; fi
