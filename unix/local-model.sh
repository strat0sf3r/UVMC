#!/usr/bin/env bash
# Runs a model on this computer's graphics chip for UVMC, with llama.cpp's own server (no account,
# no subscription). Mac: Metal (Apple Silicon). Linux: Vulkan (NVIDIA, AMD, Intel; needs the
# Vulkan loader, e.g. "sudo apt install libvulkan1 mesa-vulkan-drivers").
#   unix/local-model.sh                    set up: download llama.cpp and a model sized to your
#                                          chip, start it, connect UVMC
#   unix/local-model.sh --model qwen3-4b   switch model (the old file is deleted: one at a time)
#   unix/local-model.sh --update           get the newest llama.cpp
#   unix/local-model.sh --status | --start | --stop | --remove
#   unix/local-model.sh --autostart        start it each time you log in (off unless you ask)
#   unix/local-model.sh --no-autostart     stop starting it at login
#   --dir <folder>                         where to keep it (default: uvmc-local next to UVMC)
#
# The server listens only on this computer and on the network to the UVMC machine, and answers
# only with a key kept inside UVMC (out of the agent's reach). The model only writes text;
# whatever it asks for is done by the supervisor inside the VM.
set -euo pipefail
PORT=8091
VM=uvmc
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="$HOME/.config/uvmc/local-model.env"
OS="$(uname -s)"
ARCH="$(uname -m)"

say()  { printf '%s\n' "$*"; }
fail() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# name|repo|file|min MiB|label: from the Qwen team's own Hugging Face pages, 4-bit (Q4_K_M)
MODELS="qwen3-4b|Qwen/Qwen3-4B-GGUF|Qwen3-4B-Q4_K_M.gguf|0|Qwen3 4B (2.4 GB), basic tasks
qwen3-8b|Qwen/Qwen3-8B-GGUF|Qwen3-8B-Q4_K_M.gguf|6000|Qwen3 8B (4.8 GB)
qwen3-14b|Qwen/Qwen3-14B-GGUF|Qwen3-14B-Q4_K_M.gguf|11000|Qwen3 14B (8.6 GB)
qwen3-32b|Qwen/Qwen3-32B-GGUF|Qwen3-32B-Q4_K_M.gguf|20000|Qwen3 32B (18.8 GB)"
model_field() { printf '%s\n' "$MODELS" | awk -F'|' -v n="$1" -v f="$2" '$1 == n { print $f }'; }

DIR="" MODEL="" LLAMA="" MODEL_SHA="" CARD="" CARD_MIB=0 VM_IF=""
[ -f "$STATE" ] && . "$STATE"
save_state() {
  mkdir -p "$(dirname "$STATE")"
  { for v in DIR MODEL LLAMA MODEL_SHA CARD CARD_MIB VM_IF; do printf '%s=%q\n' "$v" "${!v}"; done; } > "$STATE"
}

ACTION=setup WANT_MODEL="" UPDATE=0 YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --model) WANT_MODEL="$2"; shift ;;
    --dir) DIR="$(mkdir -p "$2" && cd "$2" && pwd)"; shift ;;
    --update) UPDATE=1 ;;
    --status) ACTION=status ;; --start) ACTION=start ;; --stop) ACTION=stop ;; --remove) ACTION=remove ;;
    --autostart) ACTION=autostart ;; --no-autostart) ACTION=no-autostart ;;
    --yes) YES=1 ;;
    *) fail "Unknown option $1 (see the top of this script)." ;;
  esac
  shift
done
[ -n "$DIR" ] || DIR="$(dirname "$(dirname "$HERE")")/uvmc-local"
SERVER="$DIR/llama.cpp/llama-server"
PIDFILE="$DIR/server.pid"

running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }
stop_server() {
  if running; then kill "$(cat "$PIDFILE")"; rm -f "$PIDFILE"; say "Stopped."; else say "It was not running."; fi
}

# This computer's address on a network interface (the one to the UVMC machine).
if_address() {
  if [ "$OS" = Darwin ]; then ifconfig "$1" 2>/dev/null | awk '/inet /{print $2; exit}'
  else ip -4 -o addr show dev "$1" 2>/dev/null | awk '{sub(/\/.*/, "", $4); print $4; exit}'; fi
}
# The UVMC machine reaches this computer at its gateway: find which interface here has it.
find_vm_interface() {
  command -v multipass >/dev/null || return 1
  multipass start "$VM" >/dev/null 2>&1 || return 1
  local gw
  gw="$(multipass exec "$VM" -- ip -4 route show default 2>/dev/null | awk '/via/{print $3; exit}')"
  [ -n "$gw" ] || return 1
  if [ "$OS" = Darwin ]; then
    for i in $(ifconfig -l); do [ "$(if_address "$i")" = "$gw" ] && { echo "$i"; return 0; }; done
  else
    ip -4 -o addr | awk -v gw="$gw" '{a=$4; sub(/\/.*/, "", a); if (a == gw) { print $2; exit }}'
    return 0
  fi
  return 1
}

get_llama() {
  local pat
  case "$OS-$ARCH" in
    Darwin-arm64) pat='llama-*-bin-macos-arm64.tar.gz' ;;
    Darwin-x86_64) pat='llama-*-bin-macos-x64.tar.gz' ;;
    Linux-x86_64) pat='llama-*-bin-ubuntu-vulkan-x64.tar.gz' ;;
    Linux-aarch64|Linux-arm64) pat='llama-*-bin-ubuntu-vulkan-arm64.tar.gz' ;;
    *) fail "No llama.cpp build for $OS $ARCH." ;;
  esac
  say "Looking up the newest llama.cpp..."
  local info tag name url size digest
  info="$(curl -fsSL -H 'User-Agent: uvmc' 'https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=10' | python3 -c '
import fnmatch, json, sys
for r in json.load(sys.stdin):
    for a in r["assets"]:
        if fnmatch.fnmatch(a["name"], sys.argv[1]):
            print(r["tag_name"], a["name"], a["browser_download_url"], a["size"], (a.get("digest") or "").removeprefix("sha256:"))
            sys.exit()' "$pat")" || fail "Could not reach GitHub."
  read -r tag name url size digest <<<"$info"
  [ -n "$tag" ] || fail "No build found in the latest llama.cpp releases. Try again later."
  if [ "$LLAMA" = "$tag" ] && [ -x "$SERVER" ]; then say "llama.cpp $tag is already the newest."; return; fi
  say "Downloading llama.cpp $tag ($((size / 1048576)) MB)..."
  local tmp; tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/$name" "$url" || fail "The download failed."
  local got; got="$( (command -v sha256sum >/dev/null && sha256sum "$tmp/$name" || shasum -a 256 "$tmp/$name") | awk '{print $1}')"
  [ -n "$digest" ] && [ "$got" = "$digest" ] || fail "The download does not match its published checksum."
  tar -xzf "$tmp/$name" -C "$tmp"
  local bin; bin="$(dirname "$(find "$tmp" -name llama-server -type f | head -1)")"
  [ -x "$bin/llama-server" ] || fail "llama-server is missing from the download."
  running && stop_server
  mkdir -p "$DIR"
  [ -d "$DIR/llama.cpp" ] && mv "$DIR/llama.cpp" "$tmp/old"
  mv "$bin" "$DIR/llama.cpp"
  rm -rf -- "$tmp"
  LLAMA="$tag"
  say "llama.cpp $tag installed (checksum verified)."
}

# The biggest device llama.cpp sees: "Vulkan0: AMD Radeon RX 5700 XT (8176 MiB, ...)", "MTL0: Apple M2 (...)"
get_card() {
  local line
  line="$("$SERVER" --list-devices 2>&1 | grep -E '^[[:space:]]*[A-Za-z]+[0-9]*: .+\([0-9]+ MiB' | sort -t'(' -k2 -n -r | head -1)"
  [ -n "$line" ] || return 1
  CARD="$(printf '%s' "$line" | sed -E 's/^[[:space:]]*[A-Za-z]+[0-9]*: (.+) \([0-9]+ MiB.*/\1/')"
  CARD_MIB="$(printf '%s' "$line" | sed -E 's/.*\(([0-9]+) MiB.*/\1/')"
}

pick_model() {
  printf '%s\n' "$MODELS" | awk -F'|' -v m="$1" '$4 <= m { pick = $1 } END { print pick }'
}

get_model() {
  local repo file want path
  repo="$(model_field "$1" 2)"; file="$(model_field "$1" 3)"
  path="$DIR/models/$file"
  want="$(curl -fsSL -H 'User-Agent: uvmc' "https://huggingface.co/api/models/$repo/tree/main" | python3 -c '
import json, sys
print(next((f["lfs"]["oid"] for f in json.load(sys.stdin) if f["path"] == sys.argv[1] and f.get("lfs")), ""))' "$file")"
  [ -n "$want" ] || fail "$file was not found on huggingface.co/$repo."
  sha() { (command -v sha256sum >/dev/null && sha256sum "$1" || shasum -a 256 "$1") | awk '{print $1}'; }
  if ! { [ -f "$path" ] && [ "$MODEL_SHA" = "$want" ]; }; then
    mkdir -p "$DIR/models"
    if [ -f "$path" ] && { say "Checking the model file already in the folder..."; [ "$(sha "$path")" = "$want" ]; }; then :
    else
      say "Downloading $(model_field "$1" 5) from huggingface.co/$repo... (it can be stopped and resumed)"
      curl -fL -C - -o "$path" "https://huggingface.co/$repo/resolve/main/$file" || fail "The download stopped. Run this again to resume it."
      say "Checking the file..."
      [ "$(sha "$path")" = "$want" ] || { rm -f "$path"; fail "The model does not match its published checksum; deleted it. Run this again."; }
    fi
    MODEL_SHA="$want"
  fi
  for f in "$DIR"/models/*.gguf; do
    [ "$f" = "$path" ] || { say "Deleting the previous model $(basename "$f")."; rm -f "$f"; }
  done
  MODEL="$1"
}

server_args() {
  # Measured on an 8 GB AMD card through Vulkan: flash attention fell back to the processor (10x
  # slower on long prompts), and a smaller work buffer kept the model off shared memory.
  ARGS=(-ngl 99 -np 1 --jinja --reasoning-budget 0)
  if [ "$OS" = Darwin ] || [[ "$CARD" == *NVIDIA* ]]; then ARGS+=(-fa auto); else ARGS+=(-fa off); fi
  if [ "$CARD_MIB" -ge 11000 ]; then ARGS+=(-c 16384); else ARGS+=(-c 8192 -ub 256); fi
}

start_server() {
  running && { say "The model is already running."; return; }
  local hosts=127.0.0.1 ip="" i
  # On a Mac the network to the UVMC machine only exists while it runs: wait for it a while.
  if [ -n "$VM_IF" ]; then
    for i in $(seq 60); do ip="$(if_address "$VM_IF")"; [ -n "$ip" ] && break; [ "$ACTION" = start ] || break; sleep 10; done
  fi
  if [ -n "$ip" ]; then hosts="$hosts,$ip"; else say "The network to the UVMC machine was not found; the model starts for this computer only."; fi
  server_args
  nohup "$SERVER" -m "$DIR/models/$(model_field "$MODEL" 3)" --alias "$MODEL" --host "$hosts" --port "$PORT" \
    --api-key-file "$DIR/api_key" --log-file "$DIR/server.log" "${ARGS[@]}" >/dev/null 2>&1 &
  echo $! > "$PIDFILE"
  local key; key="$(cat "$DIR/api_key")"
  for i in $(seq 120); do
    sleep 1
    curl -fs -H "Authorization: Bearer $key" "http://127.0.0.1:$PORT/health" | grep -q '"ok"' && {
      say "Running: $(model_field "$MODEL" 5) on $CARD."; return; }
    running || break
  done
  fail "The model did not start. See $DIR/server.log."
}

PLIST="$HOME/Library/LaunchAgents/com.uvmc.local-model.plist"
UNIT="$HOME/.config/systemd/user/uvmc-local-model.service"
autostart_on() { [ -f "$PLIST" ] || { [ -f "$UNIT" ] && systemctl --user is-enabled uvmc-local-model.service >/dev/null 2>&1; }; }

# Only when you ask: a LaunchAgent on a Mac, a systemd user service on Linux.
autostart() {
  local me; me="$HERE/local-model.sh"
  if [ "$OS" = Darwin ]; then
    local plist="$PLIST"
    mkdir -p "$(dirname "$plist")"
    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.uvmc.local-model</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$me</string><string>--start</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
EOF
    say "It now starts each time you log in (--no-autostart turns that off)."
  elif command -v systemctl >/dev/null; then
    local unit="$UNIT"
    mkdir -p "$(dirname "$unit")"
    printf '[Unit]\nDescription=UVMC local model\n\n[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/bin/bash %s --start\nExecStop=/bin/bash %s --stop\n\n[Install]\nWantedBy=default.target\n' "$me" "$me" > "$unit"
    systemctl --user daemon-reload && systemctl --user enable uvmc-local-model.service >/dev/null 2>&1 || true
    say "It now starts each time you log in (--no-autostart turns that off)."
  else
    say "No LaunchAgent or systemd here: start it yourself with --start."
  fi
}

no_autostart() {
  if [ ! -f "$PLIST" ] && [ ! -f "$UNIT" ]; then say "It was not starting at login."; return; fi
  rm -f "$PLIST"
  if [ -f "$UNIT" ]; then systemctl --user disable uvmc-local-model.service >/dev/null 2>&1 || true; rm -f "$UNIT"; fi
  say "It no longer starts when you log in."
}

# Stores a key in UVMC with uvmc-setkey. Piped input into "multipass exec" hung on Windows
# (Multipass 1.16), so the key goes over as a file, into the ubuntu user's home (the agent users
# can't read it), and is deleted right after.
send_key() {
  local name tmp ok=1
  name="uvmc-key-$(od -An -tx1 -N8 /dev/urandom | tr -d ' \n')"
  tmp="$(mktemp)"; printf '%s\n' "$1" > "$tmp"
  if multipass transfer "$tmp" "$VM:/home/ubuntu/$name" >/dev/null 2>&1 &&
     multipass exec "$VM" -- sudo sh -c "uvmc-setkey local < /home/ubuntu/$name; s=\$?; rm -f /home/ubuntu/$name; exit \$s" >/dev/null 2>&1
  then ok=0; fi
  rm -f "$tmp"
  return $ok
}

connect_uvmc() {
  if ! command -v multipass >/dev/null; then say "Multipass is not installed, so UVMC was not connected."; return; fi
  if send_key "$(cat "$DIR/api_key")"; then
    say 'UVMC connected: pick "Local model on my PC" in the panel.'
  else say "Could not give UVMC the key. Is the UVMC machine installed and running?"; fi
}

case "$ACTION" in
  status)
    [ -n "$MODEL" ] || { say "Not set up yet. Run unix/local-model.sh."; exit 0; }
    say "Folder:    $DIR"; say "llama.cpp: $LLAMA"; say "Chip:      $CARD ($CARD_MIB MiB)"
    say "Model:     $(model_field "$MODEL" 5)"
    if running; then say "Running, port $PORT."; else say "Not running (unix/local-model.sh --start)."; fi
    if autostart_on; then say "Starts at login: yes (--no-autostart to stop that)"; else say "Starts at login: no (--autostart if you want it)"; fi
    exit 0 ;;
  autostart) [ -n "$MODEL" ] || fail "Not set up yet. Run unix/local-model.sh first."; autostart; exit 0 ;;
  no-autostart) no_autostart; exit 0 ;;
  stop) stop_server; exit 0 ;;
  start) [ -n "$MODEL" ] || fail "Not set up yet. Run unix/local-model.sh first."; start_server; exit 0 ;;
  remove)
    if [ "$YES" = 0 ]; then read -rp "Delete $DIR (llama.cpp and the model)? [y/N] " a; [[ "$a" =~ ^[yY] ]] || exit 0; fi
    stop_server
    no_autostart >/dev/null
    [ -d "$DIR" ] && rm -rf -- "$DIR"
    command -v multipass >/dev/null && { send_key "" || true; }   # an empty key removes it
    rm -f "$STATE"
    say "Removed."; exit 0 ;;
esac

if [ -n "$WANT_MODEL" ] && [ -z "$(model_field "$WANT_MODEL" 1)" ]; then
  fail "Unknown model '$WANT_MODEL'. Choose one of: $(printf '%s\n' "$MODELS" | cut -d'|' -f1 | tr '\n' ' ')"
fi
mkdir -p "$DIR"
say "Folder: $DIR"
if [ "$UPDATE" = 1 ] || [ ! -x "$SERVER" ]; then get_llama; fi
get_card || fail "llama.cpp found no graphics chip it can use. (Linux: is the Vulkan loader installed?)"
say "Graphics: $CARD, $CARD_MIB MiB."
name="${WANT_MODEL:-${MODEL:-$(pick_model "$CARD_MIB")}}"
[ "$(model_field "$name" 4)" -gt "$CARD_MIB" ] && say "$(model_field "$name" 5) is meant for bigger chips; it will be slow on this one."
say "Model: $(model_field "$name" 5)"
get_model "$name"
[ -f "$DIR/api_key" ] || (umask 077; head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$DIR/api_key")
VM_IF="$(find_vm_interface || true)"
[ -n "$VM_IF" ] || say "The UVMC machine was not found; run this again once it's installed to connect it."
save_state
running && { kill "$(cat "$PIDFILE")"; rm -f "$PIDFILE"; sleep 2; }   # restart on today's addresses
start_server
[ -n "$VM_IF" ] && connect_uvmc
say "Done. It runs until you stop it (--stop) or restart; --start starts it again."
if autostart_on; then say "It also starts each time you log in (--no-autostart turns that off)."
else say "It does not start by itself when you log in; --autostart does that, if you want it."; fi
