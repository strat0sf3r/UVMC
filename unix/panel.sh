#!/usr/bin/env bash
# Opens the UVMC panel in your browser, signed in, through the UVMC helper (started if needed).
# Starts the machine if needed.
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
open_url() {
  if command -v open >/dev/null 2>&1; then open "$1"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$1"
  else echo "Open this address in your browser: $1"; fi
}
# Through the UVMC helper (started if it isn't running): it finds the machine's current address,
# signs you in, and gives the panel its key, so the panel can manage the model on this computer.
# If the helper doesn't answer, the panel opens the old way below.
helper_up() { (exec 3<>/dev/tcp/127.0.0.1/8092) 2>/dev/null; }
if ! helper_up && [ -f unix/helper ] && command -v python3 >/dev/null; then
  echo "Starting the UVMC helper..."
  python3 unix/helper --background >/dev/null 2>&1 || true
fi
if helper_up; then
  open_url "http://127.0.0.1:8092/panel"
  exit 0
fi
echo "The UVMC helper did not start; opening the panel without it (the model on this computer is then managed with unix/local-model.sh)."
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
open_url "http://$ip:$PORT/?token=$token"
