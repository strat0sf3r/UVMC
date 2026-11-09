#!/usr/bin/env bash
# Copies the agent's run logs from UVMC into the repo's logs folder, so they survive a reset.
# stop, reset and uninstall call this for you.
set -uo pipefail
multipass info uvmc 2>/dev/null | grep -q Running || exit 0
cd "$(dirname "$0")/.." || exit 0
mkdir -p logs
. unix/timeouts.sh
# Stopped after 300 s if the machine doesn't answer (see timeouts.sh).
multipass exec uvmc -- timeout -k 5 300 sudo tar -czf - -C /var/log/uvmc runs | tar -xzf - -C logs
codes=("${PIPESTATUS[@]}")
if check_timeout save-logs "sudo tar -czf - -C /var/log/uvmc runs" 300 "${codes[0]}"; then
  echo "The run logs were not saved completely."
  exit 0
fi
if [ "${codes[0]}" = 0 ] && [ "${codes[1]}" = 0 ]; then
  echo "Run logs saved to $PWD/logs/runs"
fi
exit 0
