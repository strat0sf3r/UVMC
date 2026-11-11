#!/usr/bin/env bash
# UVMC installer for macOS and Linux. Safe to run again: finished steps are skipped.
set -euo pipefail
cd "$(dirname "$0")/.."
. unix/timeouts.sh

NAME=uvmc
STATE="$HOME/.uvmc"
step() { printf '\n==> %s\n' "$1"; }
fail() { printf '\nERROR: %s\n' "$1" >&2; exit 1; }
mkdir -p "$STATE"

# 1. Multipass
if ! command -v multipass >/dev/null 2>&1; then
  step "Installing Multipass"
  case "$(uname -s)" in
    Darwin)
      command -v brew >/dev/null 2>&1 \
        || fail "Homebrew not found. Install it from https://brew.sh, or Multipass from https://canonical.com/multipass/install, then run this again."
      brew install --cask multipass ;;
    Linux)
      command -v snap >/dev/null 2>&1 \
        || fail "snap not found. Install Multipass from https://canonical.com/multipass/install, then run this again."
      sudo snap install multipass ;;
    *) fail "Unsupported system: $(uname -s)" ;;
  esac
  command -v multipass >/dev/null 2>&1 || fail "Multipass did not install."
  touch "$STATE/installed-multipass"
fi

step "Waiting for Multipass to be ready"
for _ in $(seq 60); do multipass list >/dev/null 2>&1 && break; sleep 3; done
multipass list >/dev/null 2>&1 || fail "Multipass is not responding. Restart and run the installer again."

# 2. The machine. Steps after creating it are remembered when they finish, so running the
# installer again after an error carries on where it stopped.
DONE="$HOME/.uvmc/setup-done"
mkdir -p "$HOME/.uvmc"
if multipass info "$NAME" >/dev/null 2>&1; then
  if [ -f "$DONE" ]; then
    echo "The '$NAME' machine already exists, keeping it. To start over, run uninstall.sh first."
  else
    echo "The '$NAME' machine exists; finishing its setup."
    multipass start "$NAME"
  fi
else
  step "Creating the UVMC machine (downloads Ubuntu; about 5-10 minutes)"
  # 3 GB when this computer has 12 GB or more, else 2 GB (then the agent gets less). UVMC_MEMORY overrides.
  VM_MEM="${UVMC_MEMORY:-}"
  if [ -z "$VM_MEM" ]; then
    if [ "$(uname -s)" = Darwin ]; then ram_gb=$(( $(sysctl -n hw.memsize) / 1073741824 ))
    else ram_gb=$(( ($(awk '/MemTotal/ {print $2}' /proc/meminfo) + 524288) / 1048576 )); fi
    if [ "$ram_gb" -ge 12 ]; then VM_MEM=3G; else VM_MEM=2G; fi
  fi
  echo "Memory for the machine: $VM_MEM"
  # cloud-init goes in on stdin, so a sandboxed (snap) Multipass can read it from anywhere.
  multipass launch 24.04 --name "$NAME" --cpus 2 --memory "$VM_MEM" --disk 15G \
    --cloud-init - --timeout 1800 < cloud-init.yaml
fi

if [ ! -f "$DONE" ]; then
  step "Finishing setup inside the machine"
  multipass exec "$NAME" -- sudo cloud-init status --wait >/dev/null || true
  code=0; multipass exec "$NAME" -- timeout -k 5 30 sudo test -d /home/agent/work || code=$?
  check_timeout install "sudo test -d /home/agent/work" 30 "$code" \
    && fail "The machine did not answer. Run unix/install.sh again to continue."
  [ "$code" = 0 ] \
    || fail "Setup inside the machine did not finish (the agent's work folder is missing). Details: multipass exec $NAME -- sudo cat /var/log/cloud-init-output.log"

  step "Installing the supervisor"
  unix/update.sh

  # 3. API keys and the Claude plan sign-in are done in the panel (Settings), not here.

  # 4. Factory snapshot (kept if an earlier run already took it)
  if ! multipass info "$NAME.clean" >/dev/null 2>&1; then
    step "Saving the clean snapshot (your factory reset point)"
    multipass stop "$NAME"
    multipass snapshot "$NAME" --name clean
    multipass start "$NAME"
  fi
  touch "$DONE"
fi

# Claude apps on this computer (optional; unix/connect-claude-apps.sh does the same later)
if [ ! -f "$HOME/.uvmc/asked-claude-apps" ]; then
  step "Claude apps on this computer (optional)"
  echo "Claude Code and Claude Desktop on this computer can hand tasks to the UVMC agent, as a tool."
  echo "Their tasks follow the same rules, and anything risky still waits for your approval in the panel."
  read -rp "Let the Claude apps on this computer use UVMC? You can change this later. [y/N] " a
  touch "$HOME/.uvmc/asked-claude-apps"
  if [[ "$a" =~ ^[yY] ]]; then
    unix/connect-claude-apps.sh --yes
    on=0
    for _ in $(seq 30); do
      rc=0; code="$(multipass exec "$NAME" -- timeout -k 5 30 sudo uvmc-pc-apps on 2>/dev/null)" || rc=$?
      check_timeout install "sudo uvmc-pc-apps on" 30 "$rc" || true   # tried again below
      [ "$code" = 200 ] && { on=1; break; }
      sleep 2
    done
    [ "$on" = 1 ] && echo "Switched on in UVMC." || echo "Could not switch it on in UVMC yet: do it in the panel, Settings, \"Claude apps on your PC\"."
  fi
fi

# The UVMC helper (optional; python3 unix/helper --autostart / --no-autostart does the same later)
if [ ! -f "$HOME/.uvmc/asked-helper" ]; then
  step "The UVMC helper (optional)"
  echo "A small program that lets the panel do what only this computer can do: download, start,"
  echo "stop and delete the models on your graphics chip. It listens on this computer only."
  # No unless you say yes: nothing of UVMC's starts at login by itself.
  read -rp "Start the UVMC helper each time you log in? unix/panel.sh starts it anyway. [y/N] " a
  touch "$HOME/.uvmc/asked-helper"
  if [[ "$a" =~ ^[yY] ]]; then python3 unix/helper --autostart || true
  else echo "Not at login. unix/panel.sh starts it when you open the panel (helper --autostart if you change your mind)."; fi
fi

# Models on this computer's graphics chip (optional; unix/local-model.sh does it later)
if [ ! -f "$HOME/.uvmc/asked-local-model" ] && [ ! -f "$HOME/.config/uvmc/local-model.env" ]; then
  step "Models on this computer (optional)"
  echo "UVMC's agent can also run on your graphics chip (Apple Silicon, NVIDIA, AMD, Intel), with no account and nothing to buy."
  echo "This downloads llama.cpp and a model sized to your chip (several GB)."
  read -rp "Set up a model on this computer now? You can do it later with unix/local-model.sh. [y/N] " a
  touch "$HOME/.uvmc/asked-local-model"
  if [[ "$a" =~ ^[yY] ]]; then unix/local-model.sh || echo "The model set-up stopped; run unix/local-model.sh again to continue."; fi
fi

step "Done"
echo "Open the control panel with unix/panel.sh. Then, in its Settings tab, add your API key or sign in with your Claude plan."
