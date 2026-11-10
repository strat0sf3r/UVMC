#!/usr/bin/env bash
# Pushes the supervisor code and config from this repo into UVMC and installs it.
# Run it after editing anything in the supervisor folder.
set -euo pipefail
cd "$(dirname "$0")/.."
multipass start uvmc
# The lock (panel) keeps every program on this computer out of the VM, this one too.
if ! multipass exec uvmc -- true >/dev/null 2>&1; then
  echo "UVMC doesn't let this computer in: it's probably locked. Unlock it in the panel (or restart the VM: multipass restart uvmc), then run update again." >&2
  exit 1
fi
multipass exec uvmc -- rm -rf /tmp/supervisor
# Sent through tar on stdin, so a sandboxed (snap) Multipass can read it from anywhere.
tar --exclude='__pycache__' -cf - supervisor | multipass exec uvmc -- tar -C /tmp -xf -
# The VM can't see which Multipass this computer has, so tell it (the panel's Settings > Updates).
# To be replaced by a direct check once UVMC has a PC-side app (design.md, 7m).
mpv="$(multipass version 2>/dev/null | awk '$1 == "multipass" {print $2; exit}')"
multipass exec uvmc -- sudo env "UVMC_PC_MULTIPASS=$mpv" bash /tmp/supervisor/setup.sh
