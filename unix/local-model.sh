#!/usr/bin/env bash
# Runs models on this computer's graphics chip for UVMC, with llama.cpp's own server (no account,
# no subscription). Mac: Metal (Apple Silicon). Linux: Vulkan (NVIDIA, AMD, Intel; needs the
# Vulkan loader, e.g. "sudo apt install libvulkan1 mesa-vulkan-drivers").
#   unix/local-model.sh                    set up: download llama.cpp and a model sized to your
#                                          chip, start it, connect UVMC
#   unix/local-model.sh --list             the models you can add, by model and size, and which fit
#   unix/local-model.sh --add gemma4-e4b   download another model (several can be kept; the panel
#                                          picks which one UVMC uses)
#   unix/local-model.sh --add-file my.gguf [--name mine]   a GGUF file of your own (not checked)
#   unix/local-model.sh --delete <model>   delete a model's file (your own files are only forgotten)
#   unix/local-model.sh --test [model]     check a model with UVMC's tools: tool call right? speed?
#   unix/local-model.sh --update           get the newest llama.cpp
#   unix/local-model.sh --status | --start | --stop | --remove
#   unix/local-model.sh --autostart        start it each time you log in (off unless you ask)
#   unix/local-model.sh --no-autostart     stop starting it at login
#   unix/local-model.sh --logs on|off      the server's normal log (off: warnings and errors only,
#                                          which never hold what you and UVMC write; on: testing)
#   --dir <folder>                         where to keep it (default: uvmc-local next to UVMC)
#
# The server listens only on this computer and on the network to the UVMC machine, and answers
# only with a key kept inside UVMC (out of the agent's reach). The model only writes text;
# whatever it asks for is done by the supervisor inside the VM. It runs in llama.cpp's router
# mode: every downloaded model is listed, and the one a request names is loaded (one at a time).
set -euo pipefail
PORT=8091
VM=uvmc
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/timeouts.sh"
STATE="$HOME/.config/uvmc/local-model.env"
OS="$(uname -s)"
ARCH="$(uname -m)"

say()  { printf '%s\n' "$*"; }
fail() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# The models on offer, by model and size, with what each is good and bad at: one list for this
# script, windows/local-model.ps1 and the panel. Every file's SHA-256 is pinned there.
CATALOG="$(dirname "$HERE")/supervisor/uvmc/local_models.json"
[ -f "$CATALOG" ] || fail "The list of models ($CATALOG) is missing."
# catalog field NAME KEY | names | pick MIB | source NAME | files | fit NAME MIB | list "HAVE" MIB
catalog() {
  python3 - "$CATALOG" "$@" <<'PY'
import json, sys
c = json.load(open(sys.argv[1], encoding="utf-8")); cmd = sys.argv[2]; a = sys.argv[3:]
m = {}
for f in c["families"]:
    for s in f["sizes"]:
        m[s["id"]] = {**{k: f.get(k) for k in ("maker", "kind", "by", "recommended", "about", "good", "weak")}, **s}
def source(x):
    return {"official": f"from {x['by']}'s own page",
            "quantized": f"{x['maker']}'s model, converted to GGUF by {x['by']}",
            "uncensored": f"UNOFFICIAL: changed by {x['by']}, outside {x['maker']}"}[x["kind"]]
def fit(x, mib):
    # mib: what llama.cpp can use on the chip (on a Mac, the part of memory the graphics may use)
    if not mib: return ""
    if mib >= 1000 * x["card_gb"]: return "fits"
    return "tight" if mib >= 1000 * x["size_gb"] + 500 else "too big"
if cmd == "field":
    print(m.get(a[0], {}).get(a[1], ""))
elif cmd == "names":
    print(" ".join(m))
elif cmd == "pick":   # the default model's largest size that fits (its smallest if none does)
    sizes = next(f["sizes"] for f in c["families"] if f["id"] == c["default"])
    pick = sizes[0]["id"]
    for s in sizes:
        if int(a[0]) >= 1000 * s["card_gb"]: pick = s["id"]
    print(pick)
elif cmd == "source":
    print(source(m[a[0]]))
elif cmd == "files":
    print("\n".join(x["file"] for x in m.values()))
elif cmd == "fit":
    print(fit(m[a[0]], int(a[1] or 0)))
elif cmd == "list":
    have, mib = a[0].split(), int(a[1] or 0)
    for f in c["families"]:
        print(f"\n{f['label']}{' (recommended)' if f.get('recommended') else ''}  -  {f['maker']}")
        print(f"    {f['about']} {source(f)}.")
        print(f"    Good: {f['good']}")
        print(f"    Weak: {f['weak']}")
        for s in f["sizes"]:
            ft = fit(s, mib)
            print(f"    {'[x]' if s['id'] in have else '[ ]'} {s['id']:<38} {s['size']:<8} v{s['version']:<4} {s['size_gb']:>5} GB"
                  f"   card {s['card_gb']:>2} GB   Mac {s['mac_gb']:>2} GB{'   ' + ft if ft else ''}{'' if s['tested'] else '   (untested)'}")
PY
}
field() { catalog field "$1" "$2"; }

DIR="" MODEL="" HAVE="" OWN="" VERIFIED="" LLAMA="" MODEL_SHA="" CARD="" CARD_MIB=0 VM_IF="" LOGS=off
[ -f "$STATE" ] && . "$STATE"
save_state() {
  mkdir -p "$(dirname "$STATE")"
  { for v in DIR HAVE OWN VERIFIED LLAMA CARD CARD_MIB VM_IF LOGS; do printf '%s=%q\n' "$v" "${!v}"; done; } > "$STATE"
}
# Your own GGUF files (--add-file): one "name<TAB>path" a line. Not on the list, not checked by UVMC.
own_names() { [ -n "$OWN" ] && printf '%s\n' "$OWN" | cut -f1 | tr '\n' ' ' | sed 's/ $//'; true; }
own_path() { [ -n "$OWN" ] && printf '%s\n' "$OWN" | awk -F'\t' -v n="$1" '$1 == n { print $2 }'; true; }
installed() { printf '%s' "$HAVE${OWN:+ }$(own_names)"; }
has() { case " $(installed) " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }
label() { local l; l="$(field "$1" label)"; printf '%s' "${l:-$1 (your own)}"; }

ACTION=setup WANT_MODEL="" UPDATE=0 YES=0 TEST_ONLY="" WANT_LOGS="" ADD_FILE="" OWN_NAME=""
while [ $# -gt 0 ]; do
  case "$1" in
    --model|--add) WANT_MODEL="$2"; shift ;;   # --model was "switch to"; now models are kept side by side
    --add-file) ACTION=add-file; ADD_FILE="$2"; shift ;;
    --name) OWN_NAME="$2"; shift ;;
    --delete) ACTION=delete; WANT_MODEL="$2"; shift ;;
    --list) ACTION=list ;;
    --test) ACTION=test; if [ $# -gt 1 ] && [ "${2#--}" = "$2" ]; then TEST_ONLY="$2"; shift; fi ;;
    --dir) DIR="$(mkdir -p "$2" && cd "$2" && pwd)"; shift ;;
    --update) UPDATE=1 ;;
    --status) ACTION=status ;; --start) ACTION=start ;; --stop) ACTION=stop ;; --remove) ACTION=remove ;;
    --autostart) ACTION=autostart ;; --no-autostart) ACTION=no-autostart ;;
    --logs) ACTION=logs; WANT_LOGS="${2:-}"; shift ;;
    --yes) YES=1 ;;
    *) fail "Unknown option $1 (see the top of this script)." ;;
  esac
  shift
done
[ -n "$DIR" ] || DIR="$(dirname "$(dirname "$HERE")")/uvmc-local"
SERVER="$DIR/llama.cpp/llama-server"
PIDFILE="$DIR/server.pid"
PRESET="$DIR/models.ini"
model_path() {
  local p; p="$(own_path "$1")"
  if [ -n "$p" ]; then printf '%s' "$p"; else printf '%s/models/%s' "$DIR" "$(field "$1" file)"; fi
}
# Before 0.16 one model was kept (MODEL). Carry it over if it's still offered.
if [ -n "$MODEL" ]; then
  if [ -n "$(field "$MODEL" file)" ] && [ -f "$(model_path "$MODEL")" ] && ! has "$MODEL"; then HAVE="${HAVE:+$HAVE }$MODEL"; fi
  MODEL=""
fi
HAVE="$(for n in $HAVE; do [ -n "$(field "$n" file)" ] && printf '%s ' "$n"; done)"; HAVE="${HAVE% }"

running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }
stop_server() {
  if ! running; then say "It was not running."; return; fi
  # The router ends the model it loaded; wait until both are gone, so a restart can take the port.
  local pid i; pid="$(cat "$PIDFILE")"
  kill "$pid"
  for i in $(seq 30); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
  pkill -f "^$SERVER " 2>/dev/null || true
  rm -f "$PIDFILE"; say "Stopped."
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
  local gw out code=0
  out="$(multipass exec "$VM" -- timeout -k 5 20 ip -4 route show default </dev/null 2>/dev/null)" || code=$?
  check_timeout local-model "ip -4 route show default" 20 "$code" && return 1
  gw="$(printf '%s\n' "$out" | awk '/via/{print $3; exit}')"
  [ -n "$gw" ] || return 1
  if [ "$OS" = Darwin ]; then
    for i in $(ifconfig -l); do [ "$(if_address "$i")" = "$gw" ] && { echo "$i"; return 0; }; done
  else
    ip -4 -o addr | awk -v gw="$gw" '{a=$4; sub(/\/.*/, "", a); if (a == gw) { print $2; exit }}'
    return 0
  fi
  return 1
}

# Downloads with curl: download URL PATH TOTAL_BYTES [resume] [quiet]. In a terminal, curl draws its
# own progress bar. Run by the UVMC helper (UVMC_HELPER=1, no terminal), it prints
# "Progress: 37% (2.1 of 5.6 GB)" lines instead, which the helper passes to the panel.
download() {
  local url="$1" path="$2" total="${3:-0}" resume="${4:-}" quiet="${5:-}"
  local a=(-fL)
  [ -n "$resume" ] && a+=(-C -)
  if [ -z "${UVMC_HELPER:-}" ]; then
    [ -n "$quiet" ] && a+=(-sS)
    curl "${a[@]}" -o "$path" "$url"
    return
  fi
  curl "${a[@]}" -sS -o "$path" "$url" </dev/null &
  local pid=$! last=-1 have pct code=0
  while kill -0 "$pid" 2>/dev/null; do
    sleep 2
    if [ "$total" -gt 0 ] && [ -f "$path" ]; then
      have="$(wc -c <"$path" | tr -d ' ')"
      pct=$((have * 100 / total)); [ "$pct" -gt 99 ] && pct=99
      if [ "$pct" != "$last" ]; then
        last=$pct
        awk -v p="$pct" -v h="$have" -v t="$total" 'BEGIN { u = (t >= 1073741824) ? 1073741824 : 1048576
          printf "Progress: %d%% (%.1f of %.1f %s)\n", p, h / u, t / u, (u == 1073741824) ? "GB" : "MB" }'
      fi
    fi
  done
  wait "$pid" || code=$?
  [ "$code" = 0 ] && say "Progress: 100%"
  return "$code"
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
  download "$url" "$tmp/$name" "$size" "" quiet || fail "The download failed."
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

sha() { (command -v sha256sum >/dev/null && sha256sum "$1" || shasum -a 256 "$1") | awk '{print $1}'; }

# Downloads a model, checked against the SHA-256 pinned in local_models.json.
get_model() {
  local repo file want path
  repo="$(field "$1" repo)"; file="$(field "$1" file)"; want="$(field "$1" sha256)"
  path="$DIR/models/$file"
  if ! { [ -f "$path" ] && case " $VERIFIED " in *" $file=$want "*) true ;; *) false ;; esac; }; then
    mkdir -p "$DIR/models"
    if [ -f "$path" ] && { say "Checking $file, already in the folder..."; [ "$(sha "$path")" = "$want" ]; }; then :
    else
      [ "$(field "$1" kind)" = uncensored ] && say "$(field "$1" label) is $(catalog source "$1"). UVMC pinned the file it checked; a changed file is refused."
      say "Downloading $(field "$1" label) ($(field "$1" size_gb) GB) from huggingface.co/$repo... (it can be stopped and resumed)"
      download "https://huggingface.co/$repo/resolve/main/$file" "$path" \
        "$(awk -v g="$(field "$1" size_gb)" 'BEGIN { printf "%d", g * 1073741824 }')" resume \
        || fail "The download stopped. Run --add $1 again to resume it."
      say "Checking the file..."
      [ "$(sha "$path")" = "$want" ] || { rm -f "$path"; fail "$file does not match the checksum UVMC pinned; deleted it. Try again; if it happens again, the file was changed on Hugging Face and UVMC has to check it again before using it."; }
    fi
    VERIFIED="$(for v in $VERIFIED; do [ "${v%%=*}" = "$file" ] || printf '%s ' "$v"; done)$file=$want"
  fi
  has "$1" || HAVE="${HAVE:+$HAVE }$1"
}

# llama.cpp's router reads this when it starts: the settings for every model ([*]), then one section
# per downloaded model. The section name is the name UVMC asks for.
write_preset() {
  # Measured on an 8 GB AMD card through Vulkan: flash attention fell back to the processor (10x
  # slower on long prompts), and a smaller work buffer kept the model off shared memory.
  local fa=off n
  if [ "$OS" = Darwin ] || [[ "$CARD" == *NVIDIA* ]]; then fa=auto; fi
  {
    printf '; Written by local-model. Changes are lost the next time it runs.\nversion = 1\n\n[*]\n'
    printf 'n-gpu-layers = 99\nparallel = 1\njinja = true\nreasoning-budget = 0\nflash-attn = %s\n' "$fa"
    if [ "$CARD_MIB" -ge 11000 ]; then printf 'ctx-size = 16384\n'; else printf 'ctx-size = 8192\nubatch-size = 256\n'; fi
    for n in $(installed); do printf '\n[%s]\nmodel = %s\n' "$n" "$(model_path "$n")"; done
  } > "$PRESET"
}

start_server() {
  running && { say "The model server is already running."; return; }
  [ -n "$(installed)" ] || fail "No model downloaded yet. Run unix/local-model.sh, or --add <model> (--list shows them)."
  [ -f "$PRESET" ] || write_preset
  local hosts=127.0.0.1 ip="" i
  # On a Mac the network to the UVMC machine only exists while it runs: wait for it a while.
  if [ -n "$VM_IF" ]; then
    for i in $(seq 60); do ip="$(if_address "$VM_IF")"; [ -n "$ip" ] && break; [ "$ACTION" = start ] || break; sleep 10; done
  fi
  if [ -n "$ip" ]; then hosts="$hosts,$ip"; else say "The network to the UVMC machine was not found; the model starts for this computer only."; fi
  # Router mode: no model on the command line. --models-max 1: one model on the chip at a time.
  # Logs off (the default): warnings and errors only and no /slots endpoint, so nothing of the
  # conversations is written or shown (llama.cpp writes prompts only with --log-prompts-dir or -v).
  local logargs=(--log-verbosity 2 --no-slots)
  [ "$LOGS" = on ] && logargs=(--log-verbosity 3)
  nohup "$SERVER" --models-preset "$PRESET" --models-max 1 --host "$hosts" --port "$PORT" \
    --api-key-file "$DIR/api_key" --log-file "$DIR/server.log" "${logargs[@]}" >/dev/null 2>&1 &
  echo $! > "$PIDFILE"
  local key; key="$(cat "$DIR/api_key")"
  for i in $(seq 60); do
    sleep 1
    curl -fs -H "Authorization: Bearer $key" "http://127.0.0.1:$PORT/models" | grep -q '"data"' && {
      say "Running on $CARD, with: $(installed)."
      say "A model is loaded on the chip the first time UVMC asks for it (a few seconds)."; return; }
    running || break
  done
  fail "The model server did not start. See $DIR/server.log."
}
restart_if_running() { if running; then stop_server; start_server; fi; }   # it reads models.ini when it starts

# The same request UVMC makes: a system prompt, a task, its run_command tool. Passes when the model
# asks for the tool with a sensible command. Then a plain reply, to measure speed.
test_model() {
  say ""; say "Testing $(label "$1")... (loading it on the chip first can take a minute)"
  python3 - "$PORT" "$(cat "$DIR/api_key")" "$1" <<'PY'
import json, sys, time, urllib.request
port, key, name = sys.argv[1:]
def post(body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    return json.load(urllib.request.urlopen(req, timeout=600))
tool = {"type": "function", "function": {"name": "run_command",
        "description": "Run a bash command as the agent user and return its exit code and combined output.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "The bash command to run."}},
                       "required": ["command"]}}}
t = time.time()
try:
    r = post({"model": name, "max_tokens": 400, "tool_choice": "auto", "tools": [tool], "messages": [
        {"role": "system", "content": "You are an agent working on a Linux machine. Use the tools to do the task."},
        {"role": "user", "content": "How many files are in the folder /tmp/demo? Find out with a command."}]})
except Exception as e:
    print(f"  Failed: {e}. See server.log."); sys.exit(1)
first = round(time.time() - t)
msg = r["choices"][0]["message"]; calls = msg.get("tool_calls") or []
ok = False
if calls:
    f = calls[0]["function"]
    try: cmd = json.loads(f["arguments"]).get("command", "")
    except ValueError: cmd = ""
    ok = f["name"] == "run_command" and "/tmp/demo" in cmd
    print(f"  Tool call: right (run_command: {cmd})" if ok else f"  Tool call: wrong ({f['name']}: {f['arguments']})")
else:
    print("  Tool call: none. It wrote instead: " + " ".join((msg.get("content") or "").split()))
r = post({"model": name, "max_tokens": 200, "messages": [{"role": "user", "content": "Write a short paragraph about why backups matter."}]})
tm = r.get("timings") or {}
print(f"  Speed: {round(tm.get('predicted_per_second', 0))} tokens a second writing. First answer after {first} s (loading included).")
sys.exit(0 if ok else 1)
PY
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
  local name tmp ok=1 code=0
  name="uvmc-key-$(od -An -tx1 -N8 /dev/urandom | tr -d ' \n')"
  tmp="$(mktemp)"; printf '%s\n' "$1" > "$tmp"
  if multipass transfer "$tmp" "$VM:/home/ubuntu/$name" >/dev/null 2>&1; then
    multipass exec "$VM" -- timeout -k 5 20 sudo sh -c "uvmc-setkey local < /home/ubuntu/$name; s=\$?; rm -f /home/ubuntu/$name; exit \$s" </dev/null >/dev/null 2>&1 || code=$?
    if ! check_timeout local-model "uvmc-setkey local" 20 "$code" && [ "$code" = 0 ]; then ok=0; fi
  fi
  rm -f "$tmp"
  return $ok
}

# Tells UVMC the chip found above (name, memory llama.cpp can use), so the panel can say which
# sizes fit. Same way as the key: a file over "multipass transfer", put in place, deleted.
send_hardware() {
  [ "$CARD_MIB" -gt 0 ] || return 0
  local name tmp kind=card code=0
  [ "$OS" = Darwin ] && kind=mac
  name="uvmc-hw-$(od -An -tx1 -N8 /dev/urandom | tr -d ' \n')"
  tmp="$(mktemp)"
  python3 -c 'import json, sys; print(json.dumps({"kind": sys.argv[1], "name": sys.argv[2], "mib": int(sys.argv[3])}))' "$kind" "$CARD" "$CARD_MIB" > "$tmp"
  if multipass transfer "$tmp" "$VM:/home/ubuntu/$name" >/dev/null 2>&1; then
    multipass exec "$VM" -- timeout -k 5 20 sudo sh -c "install -m 644 /home/ubuntu/$name /etc/uvmc/pc-hardware.json; s=\$?; rm -f /home/ubuntu/$name; exit \$s" </dev/null >/dev/null 2>&1 || code=$?
    check_timeout local-model "install pc-hardware.json" 20 "$code" || true
  fi
  rm -f "$tmp"
}

connect_uvmc() {
  if ! command -v multipass >/dev/null; then say "Multipass is not installed, so UVMC was not connected."; return; fi
  if send_key "$(cat "$DIR/api_key")"; then
    send_hardware
    say 'UVMC connected: pick "Local model on my PC" in the panel, and which model it uses under Settings > Models.'
  else say "Could not give UVMC the key. Is the UVMC machine installed and running?"; fi
}

case "$ACTION" in
  add-file)
    # A GGUF file of your own: used where it is, with your chip's settings. Its contents are not
    # checked (no pinned checksum), and it's marked as your own in the panel.
    [ -x "$SERVER" ] || fail "Not set up yet. Run unix/local-model.sh first, then add your own file."
    case "$ADD_FILE" in *.gguf) ;; *) fail "$ADD_FILE is not a .gguf file." ;; esac
    [ -f "$ADD_FILE" ] || fail "$ADD_FILE is not a .gguf file."
    file="$(cd "$(dirname "$ADD_FILE")" && pwd)/$(basename "$ADD_FILE")"
    id="${OWN_NAME:-$(basename "$file" .gguf)}"
    id="$(printf '%s' "$id" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9._-]+/-/g; s/^[-.]+//; s/[-.]+$//' | cut -c1-60)"
    { [ -n "$id" ] && [ "$id" != local ]; } || fail "Give it a name with --name (letters, digits, dots and dashes)."
    [ -z "$(field "$id" file)" ] || fail "'$id' is a model on UVMC's list: --add $id gets the checked file. Use --name for yours."
    OWN="$( { [ -z "$OWN" ] || printf '%s\n' "$OWN" | awk -F'\t' -v n="$id" '$1 != n'; printf '%s\t%s\n' "$id" "$file"; } | sed '/^$/d')"
    save_state; write_preset; restart_if_running; running || start_server
    say "Added your own model as '$id' ($file). UVMC hasn't checked this file: only use files from sources you trust."
    say "Pick it in the panel (Settings > Models on your PC), and try it with --test $id."
    exit 0 ;;
  status)
    [ -n "$(installed)" ] || { say "Not set up yet. Run unix/local-model.sh."; exit 0; }
    say "Folder:    $DIR"; say "llama.cpp: $LLAMA"; say "Chip:      $CARD ($CARD_MIB MiB)"
    say "Models:    $(installed)"
    if running; then say "Running, port $PORT."; else say "Not running (unix/local-model.sh --start)."; fi
    if autostart_on; then say "Starts at login: yes (--no-autostart to stop that)"; else say "Starts at login: no (--autostart if you want it)"; fi
    exit 0 ;;
  list)
    if [ "$CARD_MIB" -gt 0 ]; then say "Your chip: $CARD, $CARD_MIB MiB usable for models. [x] = downloaded; each size says whether it fits."; else say "[x] = downloaded. (Run this once so it knows your chip and says which sizes fit.)"; fi
    catalog list "$HAVE" "$CARD_MIB"
    if [ -n "$OWN" ]; then
      say ""; say "Your own models (not on the list, not checked by UVMC):"
      printf '%s\n' "$OWN" | while IFS=$'\t' read -r n f; do say "    [x] $n  -  $f"; done
    fi
    say ""; say "Add one with --add <name> (the name in the first column), or your own file with --add-file."
    say "The panel (Settings > Models on your PC) picks which one UVMC uses. (untested): --test checks it after adding."
    exit 0 ;;
  test)
    running || fail "The model server is not running (--start)."
    which="${TEST_ONLY:-$(installed)}"; failed=0; count=0
    for n in $which; do has "$n" || fail "Not downloaded: $n. Downloaded: $(installed)."; done
    for n in $which; do count=$((count + 1)); test_model "$n" || failed=$((failed + 1)); done
    say ""
    [ "$failed" = 0 ] || { say "$failed of $count did not call the tool right: UVMC's agent would struggle with them."; exit 1; }
    say "All passed."; exit 0 ;;
  delete)
    has "$WANT_MODEL" || fail "'$WANT_MODEL' is not downloaded. Downloaded: $(installed)."
    rest="$(for n in $(installed); do [ "$n" = "$WANT_MODEL" ] || printf '%s ' "$n"; done)"; rest="${rest% }"
    [ -n "$rest" ] || fail "It is the only model left. --remove removes everything."
    path="$(model_path "$WANT_MODEL")"; lbl="$(label "$WANT_MODEL")"
    if [ -n "$(own_path "$WANT_MODEL")" ]; then
      OWN="$(printf '%s\n' "$OWN" | awk -F'\t' -v n="$WANT_MODEL" '$1 != n')"
      save_state; write_preset; restart_if_running
      say "Forgot $lbl. Your file stays where it is: $path"; exit 0
    fi
    HAVE="$(for n in $HAVE; do [ "$n" = "$WANT_MODEL" ] || printf '%s ' "$n"; done)"; HAVE="${HAVE% }"
    save_state; write_preset; restart_if_running
    rm -f "$path"
    say "Deleted $lbl. If the panel had picked it, UVMC now uses another one until you pick again."
    exit 0 ;;
  autostart) [ -n "$(installed)" ] || fail "Not set up yet. Run unix/local-model.sh first."; autostart; exit 0 ;;
  no-autostart) no_autostart; exit 0 ;;
  stop) stop_server; exit 0 ;;
  logs)
    case "$WANT_LOGS" in on|off) ;; *) fail "Use --logs on or --logs off." ;; esac
    LOGS="$WANT_LOGS"; save_state
    if [ "$LOGS" = on ]; then say "The server's normal log is on (requests and timings, not what's written), in $DIR/server.log. Turn it off with --logs off."
    else say "The server logs only warnings and errors."; fi
    if running; then stop_server; start_server; fi
    exit 0 ;;
  start) [ -n "$(installed)" ] || fail "Not set up yet. Run unix/local-model.sh first."; start_server; exit 0 ;;
  remove)
    if [ "$YES" = 0 ]; then read -rp "Delete $DIR (llama.cpp and the models)? [y/N] " a; [[ "$a" =~ ^[yY] ]] || exit 0; fi
    stop_server
    no_autostart >/dev/null
    [ -d "$DIR" ] && rm -rf -- "$DIR"
    command -v multipass >/dev/null && { send_key "" || true; }   # an empty key removes it
    rm -f "$STATE"
    say "Removed."; exit 0 ;;
esac

if [ -n "$WANT_MODEL" ] && [ -z "$(field "$WANT_MODEL" file)" ]; then
  fail "Unknown model '$WANT_MODEL'. --list shows them: $(catalog names)"
fi
mkdir -p "$DIR"
say "Folder: $DIR"
if [ "$UPDATE" = 1 ] || [ ! -x "$SERVER" ]; then get_llama; fi
get_card || fail "llama.cpp found no graphics chip it can use. (Linux: is the Vulkan loader installed?)"
say "Graphics: $CARD, $CARD_MIB MiB."
name="$WANT_MODEL"; [ -n "$name" ] || [ -n "$HAVE" ] || name="$(catalog pick "$CARD_MIB")"
if [ -n "$name" ]; then
  case "$(catalog fit "$name" "$CARD_MIB")" in
    tight) say "$(field "$name" label) needs $(field "$name" card_gb) GB for the graphics: it fits on yours, but tightly, and may be slow." ;;
    "too big") say "$(field "$name" label) needs $(field "$name" card_gb) GB for the graphics: it will spill and be very slow on yours." ;;
  esac
  say "Model: $(field "$name" label). $(field "$name" about)"
  get_model "$name"
fi
[ -f "$DIR/api_key" ] || (umask 077; head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$DIR/api_key")
save_state
write_preset
# Files that are no longer on the list (older lists, other versions) stay until you delete them.
known="$(catalog files; [ -z "$OWN" ] || printf '%s\n' "$OWN" | cut -f2 | while read -r f; do basename "$f"; done)"
for f in "$DIR"/models/*.gguf; do
  [ -f "$f" ] || continue
  printf '%s\n' "$known" | grep -qxF "$(basename "$f")" || say "$(basename "$f") is no longer on the list, so UVMC doesn't use it. Delete it by hand to free space: $f"
done
if [ -n "$WANT_MODEL" ] && [ "$UPDATE" = 0 ]; then
  # Only a model added: no need to look for the UVMC machine again.
  restart_if_running; running || start_server
  if [ -n "$VM_IF" ]; then say "Added. Pick it in the panel (Settings > Models), and try it with --test $WANT_MODEL."
  else say "Added, and running for this computer only (try it with --test $WANT_MODEL). UVMC is not connected yet: run unix/local-model.sh with no options to connect it."; fi
  exit 0
fi
VM_IF="$(find_vm_interface || true)"
[ -n "$VM_IF" ] || say "The UVMC machine was not found; run this again once it's installed to connect it."
save_state
running && { kill "$(cat "$PIDFILE")"; rm -f "$PIDFILE"; sleep 2; }   # restart on today's addresses
start_server
[ -n "$VM_IF" ] && connect_uvmc
say "Done. It runs until you stop it (--stop) or restart; --start starts it again. More models: --list."
if autostart_on; then say "It also starts each time you log in (--no-autostart turns that off)."
else say "It does not start by itself when you log in; --autostart does that, if you want it."; fi
