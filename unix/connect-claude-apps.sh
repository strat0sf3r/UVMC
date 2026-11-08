#!/usr/bin/env bash
# Lets the Claude apps on this computer use UVMC as a tool (MCP): Claude Code, and Claude Desktop
# on macOS. --remove takes UVMC out of both again. Claude Code starts "unix/uvmc mcp"; Claude
# Desktop gets the UVMC extension (extension/uvmc.mcpb). Both find the machine by themselves. UVMC must also allow it: panel, Settings, "Claude apps on your PC".
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
RELAY="$PWD/unix/uvmc"
REMOVE=0 YES=0
for a in "$@"; do
  case "$a" in --remove) REMOVE=1 ;; --yes) YES=1 ;; esac
done
did=0

if command -v claude >/dev/null 2>&1; then
  claude mcp remove --scope user uvmc >/dev/null 2>&1 || true
  if [ "$REMOVE" = 0 ]; then
    if claude mcp add --scope user uvmc -- "$RELAY" mcp; then echo "Claude Code: UVMC added (for all your projects)."; did=1
    else echo "Claude Code: adding UVMC failed (see above)."; fi
  else echo "Claude Code: UVMC removed."; did=1; fi
else
  echo "Claude Code: not found (skipped)."
fi

# Claude Desktop (macOS): UVMC comes as a desktop extension (extension/uvmc.mcpb), which Desktop
# installs itself. An older UVMC entry in Desktop's config file is taken out (backup kept).
CFG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
MCPB="$PWD/extension/uvmc.mcpb"
if [ "$(uname -s)" = Darwin ] && [ -d "$(dirname "$CFG")" ]; then
  if [ -f "$CFG" ] && grep -q '"uvmc"' "$CFG"; then
    cp "$CFG" "$CFG.bak-$(date +%Y%m%d-%H%M%S)"
    python3 - "$CFG" <<'PY' && echo "Claude Desktop: took the old UVMC entry out of its config (backup kept)."
import json, sys
cfg = json.load(open(sys.argv[1]))
cfg.get("mcpServers", {}).pop("uvmc", None)
json.dump(cfg, open(sys.argv[1], "w"), indent=2)
PY
  fi
  if [ "$REMOVE" = 1 ]; then
    echo "Claude Desktop: to remove the UVMC extension, open Claude Desktop, Settings, Extensions, and uninstall UVMC there."
    did=1
  else
    answer=y
    if [ "$YES" = 0 ]; then read -rp "Claude Desktop: install the UVMC extension? Claude Desktop opens an install window. [Y/n] " answer; fi
    if [[ ! "$answer" =~ ^[nN] ]]; then
      if open "$MCPB"; then echo "Claude Desktop: click Install in the window that opens. If none opens, drag $MCPB into the Claude window."; did=1
      else echo "Claude Desktop: could not open it. Drag $MCPB into the Claude window."; fi
    fi
  fi
else
  echo "Claude Desktop: not found (skipped)."
fi

if [ "$did" = 1 ] && [ "$REMOVE" = 0 ]; then
  echo
  echo 'Last step: allow it in UVMC. Open the panel, Settings, "Claude apps on your PC", and switch it on.'
fi
