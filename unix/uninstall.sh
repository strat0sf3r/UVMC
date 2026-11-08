#!/usr/bin/env bash
# UVMC uninstaller for macOS and Linux.
set -euo pipefail

NAME=uvmc
STATE="$HOME/.uvmc"
step() { printf '\n==> %s\n' "$1"; }
fail() { printf '\nERROR: %s\n' "$1" >&2; exit 1; }
ask()  { read -rp "$1 " a; [[ "$a" =~ ^[yY] ]]; }   # default: no
ask_y(){ read -rp "$1 " a; [[ ! "$a" =~ ^[nN] ]]; } # default: yes

echo "This removes the UVMC machine (everything inside it and all its snapshots)."
echo "This folder (the repo) is not touched."
ask "Continue? [y/N]" || exit 0

if command -v multipass >/dev/null 2>&1 && multipass info "$NAME" >/dev/null 2>&1; then
  if ask_y "Save the agent's work folder to your home folder first? [Y/n]"; then
    step "Saving the work folder"
    file="uvmc-work-$(date +%Y%m%d-%H%M).tgz"
    multipass start "$NAME"
    multipass exec "$NAME" -- sudo tar -czf /tmp/uvmc-work.tgz -C /home/agent work
    if (cd "$HOME" && multipass transfer "$NAME:/tmp/uvmc-work.tgz" "$file"); then
      echo "Saved to $HOME/$file (open with: tar -xzf $file)"
    else
      ask "Saving failed. Delete the machine anyway? [y/N]" || exit 1
    fi
  fi
  "$(dirname "$0")/save-logs.sh"
  step "Deleting the machine and its snapshots"
  multipass delete --purge "$NAME" || fail "Deleting the machine failed."
else
  echo "No UVMC machine found."
fi

if [ -f "$STATE/installed-multipass" ] && command -v multipass >/dev/null 2>&1; then
  if ask_y "Multipass was installed by UVMC. Remove it too? [Y/n]"; then
    step "Removing Multipass"
    case "$(uname -s)" in
      Darwin) brew uninstall --zap --cask multipass ;;
      Linux)  sudo snap remove --purge multipass ;;
    esac
  fi
fi
"$(dirname "$0")/connect-claude-apps.sh" --remove >/dev/null 2>&1 || true   # take UVMC out of the Claude apps
rm -rf "$STATE"

step "Done"
