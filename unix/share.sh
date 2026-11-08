#!/usr/bin/env bash
# Shares the folders listed in shares.txt with UVMC (and stops sharing removed ones).
# The agent sees them read-only under /mnt/shared; changes need your approval in the panel.
# On Linux with the snap version of Multipass, folders outside your home folder need:
#   sudo snap connect multipass:removable-media
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
REPO="$PWD"
[ -f shares.txt ] || cp shares.example.txt shares.txt
PREFIX=/srv/uvmc-host/
STATE="$HOME/.uvmc/shared"
mkdir -p "$HOME/.uvmc"
touch "$STATE"
multipass start uvmc || { echo "UVMC is not installed. Run unix/install.sh first."; exit 1; }

names=()
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%%#*}"
  [[ "$line" == *=* ]] || continue
  name="$(echo "${line%%=*}" | xargs)"
  path="$(echo "${line#*=}" | xargs)"
  [ -n "$name" ] || continue
  if ! [[ "$name" =~ ^[a-z0-9][a-z0-9_-]{0,40}$ ]]; then
    echo "Skipped '$name': use lowercase letters, digits, - and _"; continue
  fi
  [[ "$path" == /* ]] || path="$REPO/$path"
  if [ ! -d "$path" ]; then echo "Skipped '$name': folder not found: $path"; continue; fi
  path="$(cd "$path" && pwd)"
  if [ "$path" = "$HOME" ] || [ "$path" = "/" ]; then
    echo "Skipped '$name': sharing your whole home folder or disk is not allowed."; continue
  fi
  names+=("$name")
  if grep -qx "$name" "$STATE"; then
    echo "Already shared: $name = $path"
  elif multipass mount "$path" "uvmc:$PREFIX$name"; then
    echo "$name" >> "$STATE"
    echo "Shared: $name = $path"
  else
    echo "Could not share $name."
  fi
done < shares.txt

# Stop sharing folders that are no longer listed.
keep=()
while IFS= read -r old; do
  [ -n "$old" ] || continue
  if printf '%s\n' "${names[@]}" | grep -qx "$old"; then
    keep+=("$old")
  else
    multipass umount "uvmc:$PREFIX$old" && echo "Stopped sharing: $old"
  fi
done < "$STATE"
printf '%s\n' "${keep[@]}" > "$STATE"

multipass exec uvmc -- sudo /usr/local/sbin/uvmc-shares-sync
echo "Done. The agent sees these under /mnt/shared (read-only)."
