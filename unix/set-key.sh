#!/usr/bin/env bash
# Sets or replaces an API key inside UVMC. Press Enter on an empty line to remove it.
#   unix/set-key.sh            the Anthropic (Claude) key
#   unix/set-key.sh deepseek   the key for a model profile (its "key" in config.toml)
set -euo pipefail
NAME="${1:-anthropic}"
multipass start uvmc
read -rsp "API key for $NAME (Enter on its own removes it): " KEY; echo
printf '%s\n' "$KEY" | multipass exec uvmc -- sudo uvmc-setkey "$NAME"
