#!/usr/bin/env bash
# Copies the agent's run logs from UVMC into the repo's logs folder, so they survive a reset.
# stop, reset and uninstall call this for you.
set -uo pipefail
multipass info uvmc 2>/dev/null | grep -q Running || exit 0
cd "$(dirname "$0")/.." || exit 0
mkdir -p logs
if multipass exec uvmc -- sudo tar -czf - -C /var/log/uvmc runs | tar -xzf - -C logs; then
  echo "Run logs saved to $PWD/logs/runs"
fi
exit 0
