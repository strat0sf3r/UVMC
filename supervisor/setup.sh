#!/usr/bin/env bash
# Installs or updates the UVMC supervisor and the agent's walls inside the VM. Run as root:
#   sudo bash /tmp/supervisor/setup.sh
#
# What it sets up (safe to run again; finished steps are skipped or refreshed):
#   /opt/uvmc            supervisor code and its Python environment
#   /etc/uvmc            config, house rules, API key, panel token
#   /var/lib/uvmc        approved sites, checkpoints, the agent's home disk
#   /var/log/uvmc        run logs
#   firewall             the agent user can only reach the supervisor's proxy
#   uvmc-agent.slice     memory / CPU / process limits for everything the agent runs
#   /home/agent          a fixed-size disk, so the agent can't fill the VM
#   /tmp, /var/tmp       size-limited memory disks (after the next restart)
#   audit rules          every program the agent starts is recorded
#   /opt/uvmc/browser    a headless browser (Playwright + Chromium) the agents and tools can use
#   uvmc-brain           the user Claude Code runs as when it's the brain on your Claude plan:
#                        no tools of its own, its own proxy (Anthropic only), login out of the agent's reach
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
OWNER="${UVMC_OWNER:-ubuntu}"   # the user the supervisor runs as
AGENT=agent
say() { printf '  %s\n' "$1"; }

id "$AGENT" >/dev/null 2>&1 || { echo "The '$AGENT' user does not exist." >&2; exit 1; }
AGENT_UID="$(id -u "$AGENT")"
AGENT_GID="$(id -g "$AGENT")"

# --- packages ---------------------------------------------------------------
need=()
for pkg in nftables auditd rsync acl python3-venv netcat-openbsd openssh-client git; do
  dpkg -s "$pkg" >/dev/null 2>&1 || need+=("$pkg")
done
if [ "${#need[@]}" -gt 0 ]; then
  say "Installing ${need[*]}"
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${need[@]}" >/dev/null \
    || { apt-get update -qq >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${need[@]}" >/dev/null; }
fi

# --- code, config, folders --------------------------------------------------
install -d -m 755 /opt/uvmc /etc/uvmc
rm -rf /opt/uvmc/uvmc
cp -r "$SRC/uvmc" /opt/uvmc/uvmc
find /opt/uvmc/uvmc -name __pycache__ -prune -exec rm -rf {} +
chmod -R a+rX,go-w /opt/uvmc/uvmc
[ -x /opt/uvmc/venv/bin/python ] || python3 -m venv /opt/uvmc/venv
/opt/uvmc/venv/bin/pip install -q --disable-pip-version-check -r "$SRC/requirements.txt"
# The pins, for the panel's update check (updates.py compares them with the newest releases).
install -m 644 "$SRC/requirements.txt" "$SRC/browser-requirements.txt" /opt/uvmc/

install -m 644 "$SRC/config.toml" /etc/uvmc/config.toml
install -m 644 "$SRC/house_rules.md" /etc/uvmc/house_rules.md
install -m 644 "$SRC/orchestrator_rules.md" /etc/uvmc/orchestrator_rules.md
install -d -m 750 -o "$OWNER" -g "$OWNER" /var/log/uvmc /var/log/uvmc/runs /var/lib/uvmc
install -d -m 700 -o "$OWNER" -g "$OWNER" /var/lib/uvmc/checkpoints
# Multipass is on the PC: update.cmd / update.sh pass its version in, for Settings > Updates.
# (Until UVMC has a PC-side app that checks it directly: design.md, 7m.)
if [[ "${UVMC_PC_MULTIPASS:-}" =~ ^[0-9][0-9A-Za-z.+~-]{0,40}$ ]]; then
  echo "$UVMC_PC_MULTIPASS" > /var/lib/uvmc/pc-multipass-version
  chown "$OWNER:$OWNER" /var/lib/uvmc/pc-multipass-version
fi

# Settings the shell part needs, read from config.toml.
eval "$(/opt/uvmc/venv/bin/python - <<'PY'
import tomllib, shlex
c = tomllib.load(open("/etc/uvmc/config.toml", "rb"))
n, l, b = c.get("network", {}), c.get("limits", {}), c.get("subscription", {})
mt = c.get("made_tools", {})
sv = c.get("services", {})
cv = c.get("conversations", {})
vals = {
    "CONV_MAX": cv.get("max_open", 8),
    "WORKER_MAX": c.get("delegation", {}).get("worker_users", 6),
    "CONV_PORT": cv.get("proxy_port", 3300),
    "TOOLS_DIR": mt.get("dir", "/var/lib/uvmc-tools"),
    "TOOLS_PORT": mt.get("proxy_port", 3200),
    "SVC_DIR": sv.get("dir", "/var/lib/uvmc-services"),
    "SVC_PORT": sv.get("proxy_port", 3400),
    "SVC_MEM": sv.get("memory", "768M"),
    "SVC_CPU": sv.get("cpu_percent", 100),
    "PROXY_PORT": n.get("proxy_port", 3128),
    "MEM": l.get("memory", "1200M"),
    "CPU": l.get("cpu_percent", 150),
    "TASKS": l.get("max_processes", 256),
    "HOME_SIZE": l.get("home_size", "6G"),
    "TMP_SIZE": l.get("tmp_size", "512M"),
    "BRAIN": b.get("user", "uvmc-brain"),
    "BRAIN_PROXY_PORT": b.get("proxy_port", 3129),
    "CLAUDE_BIN": b.get("claude", "/usr/local/bin/claude"),
    "BRAIN_MEM": b.get("memory", "1000M"),
}

# Memory budget: the agent's box + Claude Code's box + ~600 MB for Linux and the supervisor
# must fit in the VM. If they don't, the agent's box shrinks (never below 512 MB).
def mb(v):
    v = str(v).strip().upper()
    mult = {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}
    return int(float(v[:-1]) * mult[v[-1]]) if v[-1] in mult else int(int(v) / 1024 / 1024)
try:
    total = int(open("/proc/meminfo").read().split("MemTotal:")[1].split()[0]) // 1024
except Exception:
    total = 0
agent, brain = mb(vals["MEM"]), mb(vals["BRAIN_MEM"])
if total and agent + brain + 600 > total:
    vals["MEM"] = f"{max(512, total - brain - 600)}M"
    vals["MEM_NOTE"] = (f"The VM has {total} MB: the agent's memory limit is {vals['MEM']} instead of "
                        f"{agent}M. Give the VM more memory to raise it (see README).")
for k, v in vals.items():
    print(f"{k}={shlex.quote(str(v))}")
PY
)"
[ -n "${MEM_NOTE:-}" ] && say "$MEM_NOTE"

# --- Claude Code as the brain on your Claude plan -----------------------------------
# Its own user, so nothing the agent runs can read its login or its process. It starts in
# an empty folder only root can write, so no settings or instructions can be planted there.
if ! id "$BRAIN" >/dev/null 2>&1; then
  useradd --system --home-dir "/var/lib/$BRAIN" --create-home --shell /usr/sbin/nologin "$BRAIN"
fi
BRAIN_UID="$(id -u "$BRAIN")"
chmod 700 "/var/lib/$BRAIN"
install -d -m 755 /opt/uvmc/brain-cwd

# --- each conversation's own user: agent1, agent2, ... (workcopies.py) ----------------
# A conversation runs as its own user, in its own copy of the work folder, through its own
# proxy, so its files, its programs and every site it contacts are its own. They're in the
# agent's group, but the main work folder and the copies aren't (uvmc-files, below): each
# sees only its own copy; the supervisor compares it with the main folder when you bring its
# changes in.
# After the conversations' users come the workers' (a coordinator's jobs, each in its own
# copy of the coordinator's folder): agent9, agent10, ... with the default settings.
POOL=$((CONV_MAX + WORKER_MAX))
CONV_UIDS=""
for i in $(seq 1 "$POOL"); do
  u="$AGENT$i"
  id "$u" >/dev/null 2>&1 || useradd --no-create-home --home-dir "/home/agent/homes/$u" --gid "$AGENT" \
    --shell /bin/bash "$u"
  CONV_UIDS="$CONV_UIDS $(id -u "$u")"
done
usermod -aG "$AGENT" "$OWNER"
# Their firewall chains, a second wall: their commands run in their own network (netns.py,
# design.md 7t: only their own loopback, with their proxy in it), so in the VM's network these
# users may reach nothing at all, not even its loopback.
CONV_JUMPS="" CONV_CHAINS="" i=0
for uid in $CONV_UIDS; do
  i=$((i + 1))
  CONV_JUMPS+="    meta skuid $uid jump conv${i}_out"$'\n'
  CONV_CHAINS+="  chain conv${i}_out {
    meta l4proto tcp reject with tcp reset
    reject with icmpx type admin-prohibited
  }"$'\n'
done

# --- tools the agent builds (made_tools.py) ------------------------------------------
# Each approved tool gets its own user in the group uvmc-tools, made by the supervisor. Their
# frozen copies live in $TOOLS_DIR: the supervisor writes them, the tools can only read them
# (the group), and the agent can't see them at all.
getent group uvmc-tools >/dev/null || groupadd --system uvmc-tools
TOOLS_GID="$(getent group uvmc-tools | cut -d: -f3)"
install -d -m 2750 -o "$OWNER" -g uvmc-tools "$TOOLS_DIR"
install -d -m 750 -o "$OWNER" -g "$OWNER" /var/log/uvmc/tools

# --- services you add from git (services.py, docs/app-contract.md) --------------------
# Each approved service gets its own user in the group uvmc-services, made by the supervisor,
# and runs as a systemd unit uvmc-s-<name>.service. Their code (read-only, root's) and data
# folders live in $SVC_DIR; agents and tools can't see them.
getent group uvmc-services >/dev/null || groupadd --system uvmc-services
SVC_GID="$(getent group uvmc-services | cut -d: -f3)"
install -d -m 755 -o "$OWNER" -g "$OWNER" "$SVC_DIR"
# uvmc-service-shell <service> [command...]: a shell (or one command) as a service's user, in its
# walls, during an install session the supervisor opened (`uvmc shell <service>` on the PC opens
# it, runs this, and closes it). The properties come from the supervisor (the running unit's own,
# with the app folder writable and no secrets); without an open session it refuses.
cat > /usr/local/sbin/uvmc-service-shell <<'SHEOF'
#!/bin/bash
set -euo pipefail
name="${1:-}"
[[ "$name" =~ ^[a-z][a-z0-9-]{1,31}$ ]] || { echo "usage: uvmc-service-shell <service> [command...]" >&2; exit 2; }
shift
marker="/run/uvmc/service-sessions/$name"
props="@SVC_DIR@/$name/session-shell.properties"
owner="$(stat -c %U /run/uvmc 2>/dev/null || true)"
if [ ! -f "$marker" ] || [ "$(stat -c %U "$marker")" != "$owner" ] || [ ! -f "$props" ]; then
  echo "No install session is open for $name. Start one with: uvmc shell $name (on your PC)." >&2
  exit 1
fi
args=()
while IFS= read -r line; do [ -n "$line" ] && args+=(-p "$line"); done < "$props"
if [ "$#" -gt 0 ]; then
  exec systemd-run --quiet --wait --collect --pipe --service-type=exec "${args[@]}" -- "$@"
fi
echo "A shell as $name, in its walls: the app folder is yours to change, the data folder is visible,"
echo "no secrets; sites outside its profile are asked on its page in the panel. 'exit' ends it."
exec systemd-run --quiet --wait --collect --pty --service-type=exec "${args[@]}" -- /bin/bash -l
SHEOF
sed -i "s|@SVC_DIR@|$SVC_DIR|" /usr/local/sbin/uvmc-service-shell
chmod 755 /usr/local/sbin/uvmc-service-shell
# Claude Code for the "my plan" models: installed by uvmc-install-claude, which update-claude
# (on your PC) and the panel's "Update Claude Code" button also run.
cat > /usr/local/sbin/uvmc-install-claude <<'CCEOF'
#!/bin/bash
# Installs or updates Claude Code for the "my plan" models. Run as root.
# The installer is downloaded and run by uvmc-setup, a user with no rights beyond its own
# folder (no sudo, no login), so the script fetched from the internet never runs as root.
# Root only puts the finished program in place (atomically, so a running copy isn't disturbed).
set -u
CLAUDE_BIN=@CLAUDE_BIN@
LOG=/var/log/uvmc/claude-install.log
say() { printf '  %s\n' "$1"; }
before="$("$CLAUDE_BIN" --version 2>/dev/null | head -n1)"
say "Installing the latest Claude Code (for the 'my plan' models)..."
id uvmc-setup >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin uvmc-setup
inst=/var/lib/uvmc-setup
rm -rf "$inst"
install -d -m 700 -o uvmc-setup -g uvmc-setup "$inst"
trap 'rm -rf "$inst"' EXIT
mkdir -p "$(dirname "$LOG")"
if runuser -u uvmc-setup -- env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$inst" LANG=C.UTF-8 \
     bash -c 'set -o pipefail; curl -fsSL https://claude.ai/install.sh | bash' >"$LOG" 2>&1 \
   && [ -e "$inst/.local/bin/claude" ]; then
  install -m 755 -o root -g root "$(readlink -f "$inst/.local/bin/claude")" "$CLAUDE_BIN.new"
  mv -f "$CLAUDE_BIN.new" "$CLAUDE_BIN"
  after="$("$CLAUDE_BIN" --version 2>/dev/null | head -n1)"
  if [ -n "$before" ] && [ "$before" = "$after" ]; then
    say "Claude Code is already up to date: $after."
  else
    say "Claude Code $after installed${before:+ (was $before)}."
  fi
  exit 0
fi
say "Could not install Claude Code now; the 'my plan' models won't work until it is. What it said:"
tail -n 15 "$LOG" | sed 's/^/    /'
say "Full log: $LOG. Try again later."
exit 1
CCEOF
sed -i "s|@CLAUDE_BIN@|$CLAUDE_BIN|" /usr/local/sbin/uvmc-install-claude
chmod 755 /usr/local/sbin/uvmc-install-claude
if [ ! -x "$CLAUDE_BIN" ] || [ "${UVMC_UPDATE_CLAUDE:-}" = 1 ]; then
  /usr/local/sbin/uvmc-install-claude || true
fi

# --- headless browser for the agents and their tools (browser.py) ---------------------------
# Playwright (pinned in browser-requirements.txt) in /opt/uvmc/browser and Chromium's headless
# shell in /opt/uvmc/ms-playwright, both root's: every agent can use them, none can change them.
# Chromium's system libraries come from Ubuntu (apt, as root). The browser itself is downloaded
# from Playwright's servers by uvmc-setup, the same no-rights user that runs Claude Code's
# installer, so nothing fetched here runs as root. Skipped when that version is already in place.
BROWSER_VENV=/opt/uvmc/browser
BROWSERS=/opt/uvmc/ms-playwright
BROWSER_LOG=/var/log/uvmc/browser-install.log
BROWSER_WANT="$(grep -v '^[[:space:]]*#' "$SRC/browser-requirements.txt" | tr -d '[:space:]')"
BROWSER_VER="${BROWSER_WANT#*==}"
install_browser() {
  local inst=/var/lib/uvmc-setup
  [ -x "$BROWSER_VENV/bin/python" ] || python3 -m venv "$BROWSER_VENV" || return 1
  "$BROWSER_VENV/bin/pip" install -q --disable-pip-version-check -r "$SRC/browser-requirements.txt" || return 1
  chmod -R a+rX,go-w "$BROWSER_VENV"
  "$BROWSER_VENV/bin/playwright" install-deps chromium || return 1
  id uvmc-setup >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin uvmc-setup
  rm -rf "$inst"
  install -d -m 700 -o uvmc-setup -g uvmc-setup "$inst"
  runuser -u uvmc-setup -- env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$inst" LANG=C.UTF-8 \
    PLAYWRIGHT_BROWSERS_PATH="$inst/ms-playwright" \
    "$BROWSER_VENV/bin/playwright" install --only-shell chromium || { rm -rf "$inst"; return 1; }
  rm -rf "$BROWSERS.new" "$BROWSERS.old"
  cp -r "$inst/ms-playwright" "$BROWSERS.new" && rm -rf "$inst" || { rm -rf "$inst"; return 1; }
  chown -R root:root "$BROWSERS.new"
  chmod -R a+rX,go-w "$BROWSERS.new"
  [ -e "$BROWSERS" ] && mv "$BROWSERS" "$BROWSERS.old"
  mv "$BROWSERS.new" "$BROWSERS" || return 1
  rm -rf "$BROWSERS.old"
  printf '%s\n' "$BROWSER_VER" > "$BROWSER_VENV/READY"
  chmod 644 "$BROWSER_VENV/READY"
}
if [ "$(cat "$BROWSER_VENV/READY" 2>/dev/null)" != "$BROWSER_VER" ] || [ ! -d "$BROWSERS" ]; then
  say "Installing the headless browser (Playwright $BROWSER_VER with Chromium)..."
  mkdir -p "$(dirname "$BROWSER_LOG")"
  rm -f "$BROWSER_VENV/READY"
  if install_browser >"$BROWSER_LOG" 2>&1; then
    say "Headless browser ready."
  else
    say "Could not install the headless browser now (the rest of UVMC works). What it said:"
    tail -n 15 "$BROWSER_LOG" | sed 's/^/    /'
    say "Full log: $BROWSER_LOG. The next update tries again."
  fi
fi

# --- secrets -----------------------------------------------------------------
if [ ! -s /etc/uvmc/panel_token ]; then
  head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40 > /etc/uvmc/panel_token
fi
chown "$OWNER:$OWNER" /etc/uvmc/panel_token
chmod 600 /etc/uvmc/panel_token
[ -f /etc/uvmc/api_key ] && { chown "$OWNER:$OWNER" /etc/uvmc/api_key; chmod 600 /etc/uvmc/api_key; }
install -d -m 700 -o "$OWNER" -g "$OWNER" /etc/uvmc/keys

# --- commands ------------------------------------------------------------------
cat > /usr/local/bin/uvmc <<'EOF'
#!/bin/bash
# UVMC supervisor. Runs as the VM's admin user, never as the agent.
if [ "$(id -un)" = "agent" ]; then
  echo "The supervisor does not run as the agent user." >&2
  exit 1
fi
PYTHONPATH=/opt/uvmc exec /opt/uvmc/venv/bin/python -m uvmc "$@"
EOF

cat > /usr/local/bin/uvmc-setkey <<'EOF'
#!/bin/bash
# Run as root:  uvmc-setkey [name]   (default name: anthropic)
# Reads an API key on stdin and stores it where only the admin user can read it
# (never the agent): anthropic -> /etc/uvmc/api_key, others -> /etc/uvmc/keys/<name>.
# An empty line removes the stored key.
set -e
name="${1:-anthropic}"
if ! [[ "$name" =~ ^[a-z0-9][a-z0-9_-]{0,40}$ ]]; then
  echo "Key names use lowercase letters, digits, - and _." >&2
  exit 1
fi
key="$(head -n1 | tr -d '\r\n ')"
install -d -m 755 /etc/uvmc
install -d -m 700 -o ubuntu -g ubuntu /etc/uvmc/keys
if [ "$name" = anthropic ]; then f=/etc/uvmc/api_key; else f="/etc/uvmc/keys/$name"; fi
if [ -z "$key" ]; then rm -f "$f"; echo "API key '$name' removed."; exit 0; fi
umask 077
printf '%s\n' "$key" > "$f"
chown ubuntu:ubuntu "$f"
echo "API key '$name' saved."
EOF

cat > /usr/local/bin/uvmc-agent <<EOF
#!/bin/bash
# "uvmc-agent shell" opens a shell as the agent, for looking around: inside the agent's
# resource box, with the proxy as the only way online. The agent itself is driven only
# through the supervisor (the panel); there is no way to run an AI agent here directly.
PROXY=http://127.0.0.1:$PROXY_PORT
export HTTP_PROXY=\$PROXY HTTPS_PROXY=\$PROXY http_proxy=\$PROXY https_proxy=\$PROXY
export NO_PROXY=localhost,127.0.0.1,::1 no_proxy=localhost,127.0.0.1,::1
export PLAYWRIGHT_BROWSERS_PATH=/opt/uvmc/ms-playwright PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
keep=HTTP_PROXY,HTTPS_PROXY,http_proxy,https_proxy,NO_PROXY,no_proxy,PLAYWRIGHT_BROWSERS_PATH,PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD
if [ "\${1:-shell}" != "shell" ]; then
  echo "Usage: uvmc-agent shell. The agent is driven from the panel."
  exit 1
fi
run='cd ~ && exec bash -l'
exec sudo --preserve-env=\$keep systemd-run --scope --quiet --slice=uvmc-agent.slice -- \\
  setpriv --reuid=agent --regid=agent --init-groups -- \\
  env HOME=/home/agent USER=agent LOGNAME=agent SHELL=/bin/bash bash -lc "\$run"
EOF
install -d -m 755 /usr/local/lib/uvmc
cat > /usr/local/lib/uvmc/find-token.py <<'PYEOF'
"""Finds the token in a recording of `claude setup-token`, also when the terminal wrapped it."""
import re, sys
text = open(sys.argv[1], "rb").read().decode("utf-8", "replace")
# Colour codes vanish; cursor moves and other screen codes count as a line break, so the
# token never runs into the text drawn after it.
text = re.sub(r"\x1b\[[0-9;]*m", "", text)
text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]", "\n", text)
text = text.replace("\r\n", "\n").replace("\r", "\n")
lines = text.split("\n")
found = ""
for i, line in enumerate(lines):
    m = re.search(r"sk-ant-oat\d+-[A-Za-z0-9_-]+", line)
    if not m:
        continue
    token, j = m.group(0), i
    # Wrapped: the token runs to the end of a full-width line and continues on lines of token
    # characters only (a short line ending in the token is not a wrap).
    if line.rstrip().endswith(token) and len(line.rstrip()) >= 60:
        while j + 1 < len(lines) and re.fullmatch(r"[A-Za-z0-9_-]+", lines[j + 1].strip()):
            token += lines[j + 1].strip()
            j += 1
    found = token
print(found)
PYEOF
cat > /usr/local/sbin/uvmc-pc-apps <<'EOF'
#!/bin/bash
# Run as root:  uvmc-pc-apps on|off
# Lets the Claude apps on your PC use UVMC, like the panel's switch (Settings, "Claude apps on
# your PC"). The installers use it. Prints the HTTP status (200 = done).
v=false
[ "${1:-}" = on ] && v=true
curl -s -o /dev/null -w "%{http_code}" -X POST -H "Authorization: Bearer $(cat /etc/uvmc/panel_token)" \
  -H "Content-Type: application/json" -d "{\"enabled\": $v}" http://127.0.0.1:8080/api/settings/mcp
EOF
chmod 755 /usr/local/sbin/uvmc-pc-apps

cat > /usr/local/bin/uvmc-claude-signin <<EOF
#!/bin/bash
# Run as root (sign-in-claude on your PC does it). Signs Claude Code in with your Claude plan
# for the panel's "my plan" models: \`claude setup-token\` makes a one-year token, which is
# stored like an API key (key name claude-subscription), readable only by the supervisor.
set -u
if [ ! -x "$CLAUDE_BIN" ]; then
  echo "Claude Code is not installed in the machine yet. Run update on your PC, then try again."
  exit 1
fi
work="\$(mktemp -d -p /var/lib/uvmc)"
trap 'rm -rf "\$work"' EXIT
echo "A sign-in link appears below. Open it in your browser, sign in with your Claude account"
echo "and approve. If the browser shows a code, paste it here."
echo
HOME="\$work" DISABLE_AUTOUPDATER=1 script -q -e -c "$CLAUDE_BIN setup-token" "\$work/out"
found="\$(python3 /usr/local/lib/uvmc/find-token.py "\$work/out")"
echo
if [ -n "\$found" ]; then
  echo "Found a token of \${#found} characters: \${found:0:14}...\${found: -6}"
  echo "If that matches the token shown above, just press Enter. Otherwise paste the whole token"
  echo "(all its lines if the window split it), then press Enter on an empty line."
else
  echo "Paste the whole token shown above (it starts with sk-ant-oat; all its lines if the window"
  echo "split it), then press Enter on an empty line."
fi
# Read pasted lines until an empty one, so a token pasted over several lines arrives whole.
typed=""
while IFS= read -rs line; do
  line="\$(printf '%s' "\$line" | tr -d '\r ')"
  [ -z "\$line" ] && break
  typed="\$typed\$line"
done
echo
token="\${typed:-\$found}"
token="\$(printf '%s' "\$token" | tr -d '\r\n ')"
if [ -z "\$token" ]; then echo "Nothing saved."; exit 1; fi
# Check it with one tiny request (uses a few tokens of your plan), so a cut-off token is caught now.
echo "Checking the token..."
# (The token goes in through the environment, never on a command line others could see.)
answer="\$(cd "\$work" && unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN && HOME="\$work" CLAUDE_CODE_OAUTH_TOKEN="\$token" \\
  DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \\
  timeout 90 "$CLAUDE_BIN" -p "Reply with the single word OK." --model haiku --tools "" --max-turns 1 2>&1)"
if ! printf '%s' "\$answer" | grep -qiw 'ok'; then
  echo "The token didn't work: \$(printf '%s' "\$answer" | tail -n 3)"
  echo "Nothing saved. Run sign-in-claude again and paste the whole token by hand."
  exit 1
fi
printf '%s\n' "\$token" | /usr/local/bin/uvmc-setkey claude-subscription >/dev/null
echo "Signed in: the token works. Pick a 'my plan' model in the panel."
EOF
chmod 755 /usr/local/bin/uvmc /usr/local/bin/uvmc-setkey /usr/local/bin/uvmc-agent /usr/local/bin/uvmc-claude-signin

# Login shells of the agent get the proxy too (no programs run here, so nothing is logged).
cat > /etc/profile.d/uvmc-proxy.sh <<EOF
if [ "\${USER:-}" = "agent" ]; then
  export HTTP_PROXY=http://127.0.0.1:$PROXY_PORT HTTPS_PROXY=http://127.0.0.1:$PROXY_PORT
  export http_proxy=\$HTTP_PROXY https_proxy=\$HTTPS_PROXY
  export NO_PROXY=localhost,127.0.0.1,::1 no_proxy=localhost,127.0.0.1,::1
fi
EOF

# --- firewall: the agent can only reach the proxy ---------------------------------
cat > /etc/uvmc/firewall.nft <<EOF
# UVMC: outgoing traffic from the agent user may only reach the supervisor's proxy.
# Loopback stays open for a shell as the agent (uvmc-agent shell), except the panel port and
# Claude Code's proxy; the commands UVMC runs as any agent user run in that user's own network
# instead (netns.py). The brain user (Claude Code on your plan) may only reach its proxy.
table inet uvmc
delete table inet uvmc
table inet uvmc {
  chain output {
    type filter hook output priority 0; policy accept;
    meta skuid $AGENT_UID jump agent_out
${CONV_JUMPS}    meta skuid $BRAIN_UID jump brain_out
    meta skgid $TOOLS_GID jump tools_out
    meta skgid $SVC_GID jump services_out
  }
${CONV_CHAINS}  chain agent_out {
    oifname "lo" tcp dport $PROXY_PORT accept
    oifname "lo" tcp dport 8080 reject with tcp reset
    oifname "lo" tcp dport $BRAIN_PROXY_PORT reject with tcp reset
    oifname "lo" tcp dport { $TOOLS_PORT-$((TOOLS_PORT + 99)), $CONV_PORT-$((CONV_PORT + 99)), $SVC_PORT-$((SVC_PORT + 99)) } reject with tcp reset
    oifname "lo" accept
    meta l4proto tcp reject with tcp reset
    reject with icmpx type admin-prohibited
  }
  chain tools_out {
    oifname "lo" ip daddr 127.0.0.1 tcp dport $TOOLS_PORT-$((TOOLS_PORT + 99)) accept
    meta l4proto tcp reject with tcp reset
    reject with icmpx type admin-prohibited
  }
  chain services_out {
    oifname "lo" ip daddr 127.0.0.1 tcp dport { $SVC_PORT-$((SVC_PORT + 99)), 8080 } accept
    meta l4proto tcp reject with tcp reset
    reject with icmpx type admin-prohibited
  }
  chain brain_out {
    oifname "lo" ip daddr 127.0.0.1 tcp dport $BRAIN_PROXY_PORT accept
    meta l4proto tcp reject with tcp reset
    reject with icmpx type admin-prohibited
  }
}
EOF
cat > /etc/systemd/system/uvmc-firewall.service <<'EOF'
[Unit]
Description=UVMC firewall for the agent user
DefaultDependencies=no
Before=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/uvmc/firewall.nft
ExecStartPost=-/usr/sbin/nft -f /etc/uvmc/tools.nft
ExecStartPost=-/usr/sbin/nft -f /etc/uvmc/services.nft
ExecStop=-/usr/sbin/nft delete table inet uvmc

[Install]
WantedBy=multi-user.target
EOF

# --- resource box for everything the agent runs -----------------------------------
cat > /etc/systemd/system/uvmc-agent.slice <<EOF
[Unit]
Description=UVMC agent resource box

[Slice]
MemoryMax=$MEM
MemorySwapMax=0
CPUQuota=${CPU}%
TasksMax=$TASKS
EOF

# Claude Code's own box (the "my plan" models), separate from the agent's.
cat > /etc/systemd/system/uvmc-brain.slice <<EOF
[Unit]
Description=UVMC: Claude Code as the brain on your plan

[Slice]
MemoryMax=$BRAIN_MEM
MemorySwapMax=0
EOF

# The tools' box: every tool run together, apart from the agent's and Claude Code's.
cat > /etc/systemd/system/uvmc-tools.slice <<EOF
[Unit]
Description=UVMC: tools the agent built and you approved

[Slice]
MemoryMax=512M
MemorySwapMax=0
CPUQuota=100%
TasksMax=256
EOF

# The services' box: every service together, apart from the agents', the tools' and Claude Code's.
cat > /etc/systemd/system/uvmc-services.slice <<EOF
[Unit]
Description=UVMC: services you added and approved

[Slice]
MemoryMax=$SVC_MEM
MemorySwapMax=0
CPUQuota=$SVC_CPU%
TasksMax=512
EOF

# --- the supervisor service ------------------------------------------------------
cat > /etc/systemd/system/uvmc.service <<EOF
[Unit]
Description=UVMC supervisor, proxy and panel
After=network-online.target uvmc-firewall.service
Wants=network-online.target

[Service]
User=$OWNER
SupplementaryGroups=adm $AGENT
Environment=PYTHONPATH=/opt/uvmc
ExecStart=/opt/uvmc/venv/bin/python -m uvmc serve --host 0.0.0.0 --port 8080
# /run/uvmc holds the socket Claude Code's tools use (only the brain user gets answers).
RuntimeDirectory=uvmc
RuntimeDirectoryMode=0755
Restart=always
RestartSec=3
# Keep the supervisor alive when the agent misbehaves.
OOMScoreAdjust=-500
MemoryMin=150M

[Install]
WantedBy=multi-user.target
EOF

# --- activity log: record every program the agent starts --------------------------
# gid= also matches supplementary groups, and the supervisor's user is in the agent's group (to
# reach the copies), so it is left out by uid: otherwise every command the supervisor runs, with
# the agent's scripts in its sudo calls, would be recorded. The last rule: sudo writes each command
# it runs into the audit log too (USER_CMD, the whole command line). UVMC's run logs have them; a
# private conversation's must not stay anywhere (private.py checks it at every start).
OWNER_UID="$(id -u "$OWNER")"
cat > /etc/audit/rules.d/uvmc.rules <<EOF
-a always,exit -F arch=b64 -S execve -F gid=$AGENT_GID -F uid!=$OWNER_UID -k uvmc-agent
-a always,exit -F arch=b32 -S execve -F gid=$AGENT_GID -F uid!=$OWNER_UID -k uvmc-agent
-a exclude,always -F msgtype=USER_CMD -F uid=$OWNER_UID
EOF
usermod -aG adm "$OWNER"
sed -i 's/^log_group *=.*/log_group = adm/' /etc/audit/auditd.conf
grep -q '^log_group' /etc/audit/auditd.conf || echo 'log_group = adm' >> /etc/audit/auditd.conf
chgrp adm /var/log/audit /var/log/audit/audit.log 2>/dev/null || true
chmod 750 /var/log/audit 2>/dev/null || true
chmod 640 /var/log/audit/audit.log 2>/dev/null || true

# --- the system log keeps no agent commands -----------------------------------------
# Every tool call goes through the supervisor's sudo, whose log line would hold the whole
# command (the agent's script) in journald, and its file operations name the files. UVMC's own
# run logs already have them, and a private conversation's must not stay anywhere
# (supervisor/uvmc/private.py checks this at every start). So sudo doesn't log only these
# commands: the four that carry the agent's scripts (systemd-run, setpriv, env, unshare) and the file commands the supervisor runs
# with paths (keep the list in step with root()/_run() calls in supervisor/uvmc). Every other
# sudo use (package installs, editors, sudo -i, services, users) is logged as usual, and refused
# calls always are. Later: the supervisor as its own user, so this applies to it alone (design.md 7o).
QUIET=/etc/sudoers.d/uvmc-quiet
cat > "$QUIET.new" <<'QEOF'
# Written by UVMC setup: these commands, as the supervisor runs them, carry the agent's scripts
# or the names of a conversation's files, so sudo doesn't log them.
Cmnd_Alias UVMC_QUIET = /usr/bin/systemd-run, /usr/bin/setpriv, /usr/bin/env, /usr/bin/unshare, \
    /usr/bin/install, /usr/bin/rm, /usr/bin/rmdir, /usr/bin/mv, /usr/bin/stat, /usr/bin/rsync, \
    /usr/bin/find, /usr/bin/chown, /usr/bin/chmod, /usr/bin/chgrp, /usr/bin/cat, \
    /usr/bin/setfacl, /usr/bin/test
Defaults!UVMC_QUIET !log_allowed
QEOF
chmod 440 "$QUIET.new"
if visudo -cf "$QUIET.new" >/dev/null; then
  mv -f "$QUIET.new" "$QUIET"
else
  rm -f "$QUIET.new"
  say "Warning: sudo didn't accept $QUIET, so the system log keeps the agent's commands (private conversations stay off)."
fi

if [ -d /run/systemd/system ]; then
  systemctl daemon-reload
  systemctl enable --now auditd >/dev/null 2>&1 || true
  augenrules --load >/dev/null 2>&1 || true
  auditctl --signal reload >/dev/null 2>&1 || true
  systemctl enable uvmc-firewall.service >/dev/null 2>&1
  systemctl restart uvmc-firewall.service
  say "Firewall on: the agent can only go online through the proxy."
fi

# --- the agent's home on a fixed-size disk ----------------------------------------
IMG=/var/lib/uvmc/agent-home.img
if ! mountpoint -q /home/agent; then
  say "Moving the agent's home onto its own $HOME_SIZE disk"
  [ -d /run/systemd/system ] && systemctl kill --signal=KILL uvmc-agent.slice >/dev/null 2>&1 || true
  pkill -KILL -u "$AGENT" || true
  if [ ! -f "$IMG" ]; then
    fallocate -l "$HOME_SIZE" "$IMG"
    chmod 600 "$IMG"
    mkfs.ext4 -q -F -m 0 -L uvmc-home "$IMG"
  fi
  tmpmnt="$(mktemp -d)"
  mount -o loop "$IMG" "$tmpmnt"
  rsync -aHAX /home/agent/ "$tmpmnt/"
  chown "$AGENT:$AGENT" "$tmpmnt"
  chmod 750 "$tmpmnt"
  umount "$tmpmnt"
  rmdir "$tmpmnt"
  grep -q "^$IMG " /etc/fstab || echo "$IMG /home/agent ext4 loop,nodev,nosuid 0 2" >> /etc/fstab
  [ -d /run/systemd/system ] && systemctl daemon-reload
  mount /home/agent
fi
# The main work folder and every copy belong to a group only the supervisor is in (uvmc-files),
# not the agent's: no conversation or worker can read the main folder or another's copy, so the
# privacy levels hold (docs/design.md 7k). Each agent gets files only through its own copy.
getent group uvmc-files >/dev/null || groupadd --system uvmc-files
usermod -aG uvmc-files "$OWNER"
install -d -m 750 -o "$AGENT" -g uvmc-files /home/agent/work
chgrp uvmc-files /home/agent/work && chmod 750 /home/agent/work
chmod 750 /home/agent && chgrp "$AGENT" /home/agent   # the conversations' users pass through
install -d -m 750 -o root -g "$AGENT" /home/agent/conv /home/agent/homes
install -d -m 700 -o "$OWNER" -g "$OWNER" /home/agent/conv/.state
# Copies made before 0.15 were open to the agent's group: close them too.
for d in /home/agent/conv/*/; do
  [ -d "$d" ] || continue
  chgrp uvmc-files "$d" && chmod 750 "$d"
  setfacl -b "$d" 2>/dev/null || true
done
for i in $(seq 1 "$POOL"); do
  install -d -m 700 -o "$AGENT$i" -g "$AGENT" "/home/agent/homes/$AGENT$i"
  ln -sfn /mnt/shared "/home/agent/homes/$AGENT$i/shared"
done

# --- folders shared from your computer ---------------------------------------------
# The share script on your computer mounts each folder at /srv/uvmc-host/<name>, which
# only the supervisor can reach. uvmc-shares-sync makes a read-only view of each at
# /srv/uvmc-views/<name>/files (a read-only copy if the mount can't be viewed directly) and
# links /mnt/shared/<name> to it. /srv/uvmc-views/<name> is the supervisor's, mode 700: it
# lets in, by ACL, only the users of the conversations that opened that share at a level
# that allows it (privacy.py; the supervisor sets the ACL). /mnt/shared can't be listed, so
# an agent doesn't even learn the names of shares it wasn't given.
install -d -m 700 -o "$OWNER" -g "$OWNER" /srv/uvmc-host
install -d -m 711 -o root -g root /srv/uvmc-views
install -d -m 711 -o root -g root /mnt/shared
chmod 711 /mnt/shared
cat > /usr/local/sbin/uvmc-shares-sync <<EOF
#!/bin/bash
# Refreshes the read-only views of the folders shared from your computer.
set -u
RW=/srv/uvmc-host
HOLD=/srv/uvmc-views
VIEW=/mnt/shared
OWNER=$OWNER
AGENT=$AGENT
mkdir -p "\$VIEW" "\$HOLD"
# Mounts can be stacked on one folder: take them all off.
unmount_all() {
  while mountpoint -q "\$1"; do umount "\$1" 2>/dev/null || umount -l "\$1" || return 1; done
}
# How many mounts are stacked on a folder, and the top one's options (mountinfo lists them in
# the order they were made). Share names have no spaces, so the paths match as they are.
stacked() { awk -v m="\$1" '\$5 == m {n++; o = \$6} END {print n + 0, o}' /proc/self/mountinfo; }
# Views from before 0.15 were folders in /mnt/shared, open to every agent: take them down.
for old in "\$VIEW"/*; do
  [ -d "\$old" ] && [ ! -L "\$old" ] || continue
  unmount_all "\$old" || continue
  if [ -f "\$old/.uvmc-copy" ]; then rm -rf --one-file-system "\$old"; else rmdir "\$old" 2>/dev/null; fi
done
for src in "\$RW"/*/; do
  src="\${src%/}"
  [ -d "\$src" ] || continue
  name="\$(basename "\$src")"
  [[ "\$name" =~ ^[a-z0-9][a-z0-9_-]{0,40}\$ ]] || continue
  # Until the PC's folder is mounted there is only an empty folder here: viewing it would show
  # nothing (and a copy would be emptied), so wait for the mount.
  mountpoint -q "\$src" || continue
  hold="\$HOLD/\$name"
  files="\$hold/files"
  # Made once and never reset: its ACL is what lets conversations in (set by the supervisor).
  [ -d "\$hold" ] || install -d -m 700 -o "\$OWNER" -g "\$OWNER" "\$hold"
  mkdir -p "\$files"
  [ -e "\$VIEW/\$name" ] && [ ! -L "\$VIEW/\$name" ] || ln -sfn "\$files" "\$VIEW/\$name"
  # A good view is exactly one read-only mount. Anything else (a writable mount that landed on
  # top, several stacked) is taken down and made again.
  read -r count opts <<< "\$(stacked "\$files")"
  if [ "\$count" -eq 1 ] && [[ ",\$opts," == *,ro,* ]]; then continue; fi
  unmount_all "\$files" || continue
  if [ ! -f "\$files/.uvmc-copy" ]; then
    if mount --bind "\$src" "\$files" 2>/dev/null; then
      # Private: a later mount on the PC's folder must not be copied onto the view.
      mount --make-private "\$files"
      # Can the agents' users read the mount itself? Checked as the agent, let in for a moment.
      ok=no
      if mount -o remount,bind,ro "\$files" 2>/dev/null; then
        setfacl -m "u:\$AGENT:--x" "\$hold"
        runuser -u "\$AGENT" -- ls "\$files" >/dev/null 2>&1 && ok=yes
        setfacl -x "u:\$AGENT" "\$hold"
      fi
      [ "\$ok" = yes ] && continue
      unmount_all "\$files"
    fi
  fi
  # Copy mode: a read-only copy, refreshed on every run.
  chown "\$OWNER:\$OWNER" "\$files"
  runuser -u "\$OWNER" -- rsync -a --delete --exclude _uvmc_backups \\
    --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r "\$src" "\$files/"
  runuser -u "\$OWNER" -- touch "\$files/.uvmc-copy"
done
# A folder that is no longer shared: drop its view and link (a leftover copy stays, closed).
for hold in "\$HOLD"/*/; do
  [ -d "\$hold" ] || continue
  name="\$(basename "\$hold")"
  if [ ! -d "\$RW/\$name" ]; then
    unmount_all "\$hold/files"
    [ -L "\$VIEW/\$name" ] && rm -f "\$VIEW/\$name"
  fi
done
EOF
chmod 755 /usr/local/sbin/uvmc-shares-sync
cat > /etc/systemd/system/uvmc-shares-sync.service <<'EOF'
[Unit]
Description=UVMC: refresh the agent's read-only views of shared folders

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/uvmc-shares-sync
EOF
cat > /etc/systemd/system/uvmc-shares-sync.timer <<'EOF'
[Unit]
Description=UVMC: refresh shared folder views every 30 seconds

[Timer]
OnBootSec=20s
OnUnitActiveSec=30s

[Install]
WantedBy=timers.target
EOF
if [ -d /run/systemd/system ]; then
  systemctl daemon-reload
  systemctl enable --now uvmc-shares-sync.timer >/dev/null 2>&1 || true
fi
ln -sfn /mnt/shared /home/agent/shared

# --- /tmp and /var/tmp as size-limited memory disks (from the next restart) --------
for dir in /tmp /var/tmp; do
  if ! grep -qE "^tmpfs +$dir " /etc/fstab; then
    echo "tmpfs $dir tmpfs size=$TMP_SIZE,mode=1777,nosuid,nodev 0 0" >> /etc/fstab
    say "$dir becomes a $TMP_SIZE memory disk after the next restart."
  fi
done

# --- the lock: the VM closed to the PC (supervisor/uvmc/lock.py) ----------------------
# Everything on the PC (Multipass exec/shell/transfer/mount, the scripts, any program or AI
# agent) comes in by SSH as ubuntu with Multipass's key. SSH reads that user's keys from a file
# in memory, copied from ~/.ssh/authorized_keys at every boot (so restarting the VM always
# unlocks it: the way back if the panel can't). uvmc-lock empties it (lock) or copies it back.
# The panel and the model on the PC keep working: the VM calls the PC, not the other way.
LOCK_USER=ubuntu
cat > /etc/tmpfiles.d/uvmc-ssh.conf <<EOF
d /run/uvmc-ssh 0755 root root -
C /run/uvmc-ssh/$LOCK_USER.keys 0644 root root - /home/$LOCK_USER/.ssh/authorized_keys
EOF
cat > /usr/local/sbin/uvmc-lock <<'LEOF'
#!/bin/bash
# UVMC: closes (on) or opens (off) the VM to every program on the PC. Written by setup.sh.
set -u
U=ubuntu
KEYS=/run/uvmc-ssh/$U.keys
SRC=/home/$U/.ssh/authorized_keys
serves_folder() {   # a session carrying a shared folder (Multipass's sshfs): left alone
  local c
  for c in $(pgrep -P "$1"); do
    case "$(tr '\0' ' ' < "/proc/$c/cmdline" 2>/dev/null)" in *multipass-sshfs*|*"sshfs -o slave"*) return 0 ;; esac
    serves_folder "$c" && return 0
  done
  return 1
}
case "${1:-status}" in
  on)
    install -d -m 755 -o root -g root /run/uvmc-ssh || exit 1
    : > "$KEYS.new" && chmod 644 "$KEYS.new" && mv -f "$KEYS.new" "$KEYS" || exit 1
    # Open sessions end too (an open connection could carry new commands), except shared folders.
    for pid in $(pgrep -u "$U" -f "^sshd: $U@"); do
      serves_folder "$pid" || kill -TERM "$pid" 2>/dev/null
    done ;;
  off)
    install -d -m 755 -o root -g root /run/uvmc-ssh || exit 1
    install -m 644 -o root -g root "$SRC" "$KEYS.new" && mv -f "$KEYS.new" "$KEYS" || exit 1 ;;
  status)
    if [ ! -e "$KEYS" ]; then echo unavailable; elif [ -s "$KEYS" ]; then echo open; else echo locked; fi ;;
  *) echo "usage: uvmc-lock on|off|status" >&2; exit 2 ;;
esac
LEOF
chmod 755 /usr/local/sbin/uvmc-lock
# Fill the file now (open), and only point SSH at it once it holds the key and the config passes.
systemd-tmpfiles --create /etc/tmpfiles.d/uvmc-ssh.conf >/dev/null 2>&1 || true
[ -s "/run/uvmc-ssh/$LOCK_USER.keys" ] || /usr/local/sbin/uvmc-lock off
SSHD_LOCK=/etc/ssh/sshd_config.d/10-uvmc-lock.conf
if [ -s "/run/uvmc-ssh/$LOCK_USER.keys" ] && cmp -s "/run/uvmc-ssh/$LOCK_USER.keys" "/home/$LOCK_USER/.ssh/authorized_keys"; then
  cat > "$SSHD_LOCK.new" <<EOF
# Written by UVMC setup: the lock (supervisor/uvmc/lock.py). No root login; $LOCK_USER's keys
# come from a file in memory that the lock empties.
PermitRootLogin no
Match User $LOCK_USER
    AuthorizedKeysFile /run/uvmc-ssh/%u.keys
EOF
  mv -f "$SSHD_LOCK.new" "$SSHD_LOCK"
  if sshd -t 2>/dev/null; then
    systemctl reload ssh >/dev/null 2>&1 || systemctl reload ssh.service >/dev/null 2>&1 || true
  else
    rm -f "$SSHD_LOCK"
    say "Warning: SSH didn't accept the lock's settings, so the lock is off (the panel says so)."
  fi
else
  rm -f "$SSHD_LOCK"
  say "Warning: the lock's key file couldn't be made, so the lock is off (the panel says so)."
fi

# --- start -------------------------------------------------------------------------
if [ -d /run/systemd/system ]; then
  systemctl enable uvmc.service >/dev/null 2>&1
  systemctl restart uvmc.service
fi

echo "UVMC supervisor $(PYTHONPATH=/opt/uvmc /opt/uvmc/venv/bin/python -c 'import uvmc; print(uvmc.__version__)') installed."
