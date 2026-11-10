#!/usr/bin/env bash
# Pushes the supervisor code and config from this repo into UVMC and installs it.
# Run it after editing anything in the supervisor folder.
set -euo pipefail
cd "$(dirname "$0")/.."
multipass start uvmc
multipass exec uvmc -- rm -rf /tmp/supervisor
# Sent through tar on stdin, so a sandboxed (snap) Multipass can read it from anywhere.
tar --exclude='__pycache__' -cf - supervisor | multipass exec uvmc -- tar -C /tmp -xf -
# The VM can't see which Multipass this computer has, so tell it (the panel's Settings > Updates).
# To be replaced by a direct check once UVMC has a PC-side app (design.md, 7m).
mpv="$(multipass version 2>/dev/null | awk '$1 == "multipass" {print $2; exit}')"
multipass exec uvmc -- sudo env "UVMC_PC_MULTIPASS=$mpv" bash /tmp/supervisor/setup.sh
