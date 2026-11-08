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
ip="$(multipass exec uvmc -- hostname -I | awk '{print $1}')"
token="$(multipass exec uvmc -- sudo cat /etc/uvmc/panel_token 2>/dev/null | tr -d '\r\n')"
[ -n "$token" ] || { echo "The panel is not set up in the machine yet. Run unix/update.sh, then try again."; exit 1; }
for _ in $(seq 30); do
  (exec 3<>"/dev/tcp/$ip/$PORT") 2>/dev/null && break
  sleep 1
done
url="http://$ip:$PORT/?token=$token"
if command -v open >/dev/null 2>&1; then open "$url"
elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$url"
else echo "Open this address in your browser: $url"; fi
