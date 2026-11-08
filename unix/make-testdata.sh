#!/usr/bin/env bash
# Creates a small test folder inside this repo (testdata/, ignored by git) to try shared
# folders with, and lists it in shares.txt. It never touches anything outside the repo,
# and leaves a testdata folder that already has files alone (an empty one gets filled).
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="$PWD/testdata"
if [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
  echo "testdata already has files, leaving it as it is: $DIR"
else
  mkdir -p "$DIR/models" "$DIR/textures" "$DIR/notes" "$DIR/config"
  printf '# Test project\n\nSample files for trying UVMC'"'"'s shared folders.\n\n- models: two binary "statues" (1 MB and 60 MB)\n- textures: a palette as CSV\n- config: settings as JSON\n' > "$DIR/notes/readme.md"
  printf 'fix the palette names\nraise max_statues to 12\nadd a '"'"'marble'"'"' texture\n' > "$DIR/notes/todo.txt"
  printf '{\n  "title": "Test project",\n  "max_statues": 8,\n  "quality": "medium"\n}\n' > "$DIR/config/settings.json"
  printf 'name,red,green,blue\nstone,128,128,120\nsand,194,178,128\nmoss,92,110,60\nbrick,150,70,55\n' > "$DIR/textures/palette.csv"
  head -c 1048576 /dev/urandom > "$DIR/models/statue_small.bin"
  head -c 62914560 /dev/urandom > "$DIR/models/statue_big.bin"
  echo "Made test data in $DIR"
fi
[ -f shares.txt ] || cp shares.example.txt shares.txt
grep -qE '^[[:space:]]*testdata[[:space:]]*=' shares.txt 2>/dev/null || { echo 'testdata = testdata' >> shares.txt; echo "Added testdata to shares.txt"; }
echo "Next: run unix/share.sh to share it with the agent."
