#!/usr/bin/env bash
# Installs the latest Claude Code in UVMC (for the "my plan" models). It never updates itself.
# The panel has the same button: Settings, Your Claude plan, Update Claude Code.
set -euo pipefail
multipass start uvmc
multipass exec uvmc -- sudo uvmc-install-claude
