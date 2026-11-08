#!/usr/bin/env bash
# Factory reset: returns UVMC to the clean snapshot taken at install.
set -e
echo "This wipes everything the agent did and returns UVMC to its clean install state."
read -rp "Type YES to continue: " ok
[ "$ok" = "YES" ] || exit 0
"$(dirname "$0")/save-logs.sh"
multipass stop uvmc
multipass restore uvmc.clean --destructive
multipass start uvmc
echo "Done: UVMC is back to its clean state."
