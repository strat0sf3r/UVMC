#!/usr/bin/env bash
# Pushes the supervisor code and config from this repo into UVMC and installs it.
# Run it after editing anything in the supervisor folder.
set -euo pipefail
cd "$(dirname "$0")/.."
multipass start uvmc
multipass exec uvmc -- rm -rf /tmp/supervisor
# Sent through tar on stdin, so a sandboxed (snap) Multipass can read it from anywhere.
tar --exclude='__pycache__' -cf - supervisor | multipass exec uvmc -- tar -C /tmp -xf -
multipass exec uvmc -- sudo bash /tmp/supervisor/setup.sh
