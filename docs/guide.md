# UVMC guide

Everything about installing, using and changing UVMC. For a short introduction, see the [README](../README.md); for the reasoning behind it and the roadmap, [design.md](design.md).

## Install

UVMC runs as a real Ubuntu 24.04 VM through [Multipass](https://canonical.com/multipass). The installer sets up everything, including Multipass itself.

| | Windows (Pro / Enterprise / Education) | macOS / Linux |
| --- | --- | --- |
| Install | double-click `windows\install.cmd` | `unix/install.sh` |
| **Open the control panel** (asks which folders to share first) | `UVMC Panel` shortcut, or `windows\panel.cmd` | `unix/panel.sh` |
| Choose shared folders again | hold Shift on the shortcut, or `windows\panel.cmd --folders` | `unix/panel.sh --folders` |
| Supervisor in the terminal | `windows\supervisor.cmd` | `unix/supervisor.sh` |
| Plain shell in the VM | `windows\shell.cmd` | `unix/shell.sh` |
| Set / replace an API key (or use the panel's Settings tab) | `windows\set-key.cmd [name]` | `unix/set-key.sh [name]` |
| Sign in with your Claude plan (for the "my plan" models; easier: panel › Settings) | `windows\sign-in-claude.cmd` | `unix/sign-in-claude.sh` |
| Let Claude Code / Claude Desktop on your PC use UVMC (see below) | `windows\connect-claude-apps.cmd` | `unix/connect-claude-apps.sh` |
| Use UVMC from a terminal or scripts (see below) | `windows\uvmc.cmd` | `unix/uvmc` |
| Push supervisor changes into the VM | `windows\update.cmd` | `unix/update.sh` |
| Update Claude Code in the VM (or panel › Settings › Update Claude Code) | `windows\update-claude.cmd` | `unix/update-claude.sh` |
| Share folders listed in `shares.txt` (made from `shares.example.txt`, kept out of git) | `windows\share.cmd` | `unix/share.sh` |
| Make a test folder to share | `windows\make-testdata.cmd` | `unix/make-testdata.sh` |
| Copy run logs to the repo's `logs` folder | `windows\save-logs.cmd` | `unix/save-logs.sh` |
| Stop | `windows\stop.cmd` | `unix/stop.sh` |
| Factory reset | `windows\reset.cmd` | `unix/reset.sh` |
| Uninstall | `windows\uninstall.cmd` | `unix/uninstall.sh` |

**What the installer does**

1. Asks for admin rights once.
2. Windows only: turns on Hyper-V if it's off. This needs one restart; the installer continues by itself after you log back in.
3. Installs Multipass if it's missing (winget on Windows, Homebrew on macOS, snap on Linux) and remembers that it did.
4. Creates the VM (2 CPUs, 15 GB disk, and 3 GB of memory if your computer has 12 GB or more, else 2 GB; set `UVMC_MEMORY`, e.g. `4G`, before installing to choose) and runs [cloud-init.yaml](../cloud-init.yaml) inside it: an `agent` user with no sudo and basic tools. On Windows it first asks where to store the machine: next to UVMC (the repo's `machine` folder, kept out of git), Multipass's usual place (`C:\ProgramData\Multipass`), or another folder you type. It takes about 5 GB at first and can grow to about 16 GB.
5. Installs the supervisor (see below).
6. Asks for an Anthropic API key (optional: you can use other models, or your Claude plan through `sign-in-claude`). It's stored in the VM where the agent can't read it.
7. Takes a snapshot called `clean`: the factory reset point.
8. Windows: adds a `UVMC Panel` shortcut to the desktop and Start menu.

First install takes about 5-10 minutes, mostly downloads.

**What the uninstaller does**

Asks first, copies the run logs into the repo's `logs` folder, offers to save the agent's `work` folder to your Documents (Windows) or home folder (macOS/Linux), deletes the VM with all its snapshots, removes the shortcuts, and removes Multipass only if UVMC installed it. Hyper-V stays on; the uninstaller prints the command to turn it off.

## Driving the agent

Everything goes through the supervisor, from the control panel (`panel`): the model only proposes actions, and the supervisor checks each one against your rules, runs it as `agent`, and logs it. That holds for every model, including Claude Code on your plan. There's no other way to run an AI agent in the VM; `shell` only gives you a plain shell to look around.

## The control panel

A web page served by the supervisor inside the VM, opened in your normal browser by the `UVMC Panel` shortcut (it looks up the VM's address and signs you in).

- **Conversation:** type a task and press Enter. Every step appears on the timeline as it happens: the agent's messages, each tool call (click it to see the output), and the result. Follow-up tasks continue the same conversation. **Stop** ends the current task at any moment.
- **Several conversations at once:** **New conversation** opens another one in a tab next to the others, and they can work at the same time (3 at most by default). Each tab's dot shows what it's doing: grey idle, green working, amber waiting for you. Each conversation has its own model, timeline, approvals and Stop, and runs as its own Linux user (agent1, agent2, ...) in its own copy of the work folder, through its own proxy. They share the machine's limits and the allowed sites. Tasks from scripts and from your PC's Claude apps get their own tab ("from a script", "from a Claude app"). Close a tab with ×; its run stays under Runs. `[conversations]` in config.toml sets the limits.
- **The main work folder:** `/home/agent/work` is the main version; conversations can read it but not change it. A conversation gets its own copy at its first task, and the bar under the tabs says how its copy stands: its changes not in the main folder yet, newer files in the main folder, files changed on both sides. **Bring into the main folder** shows what would happen first, then applies it both ways: its changes go into the main folder (an undo point is made first, and **Undo this bring-in** puts it back), and the main folder's newer files come into its copy. A text file changed on both sides in different places is merged line by line. Real conflicts (the same lines, a file that isn't text, or removed on one side and changed on the other) go to this conversation's agent, which resolves them like Claude Code resolves a git conflict: it works out what each side was doing (both versions, the other conversation's whole log as a readable transcript, and the main folder and the other conversations' folders, which it can read) and keeps both sides' work. Nothing of it goes into the main folder yet: you get **The agent's proposed resolution**, with its explanation and, per file, the changes it proposes. Per file you accept it, keep this conversation's version, keep the main folder's, or decide later. A beta option lets the conversation whose version is in the main folder resolve it instead; until you answer its proposal, the file stays out of this conversation's bring-in. You can also tell the agent in the chat what to change, and review its new proposal. Closing a conversation with changes not brought in asks first. After a restart, conversations with a copy come back, so their work isn't lost (the agent starts afresh).
- **Approvals:** an amber card appears, the header (and the conversation's tab) turns amber, and the agent waits for you. Two kinds: a tool your rules mark "ask" (such as a system package install) gets Allow / Deny; a site that isn't on the allowed list gets **Allow for this task / Always allow / Deny** (unanswered site requests are refused after 2 minutes). Each conversation has its own proxy, so the question appears in the conversation that asked, on the step that asked.
- **What each step did:** every tool call shows which programs it started and which sites it contacted; click it to see them with the output.
- **Undo this task:** under each of your tasks, puts this conversation's copy of the work folder back to how it was before it. Other conversations and the main folder aren't touched.
- **Files:** the agent's work folder, updated as it works. Click a file to read it.
- **Activity:** every program the agent started and every site it contacted in this conversation.
- **Runs:** every past conversation, replayable step by step.
- **Machine:** processor, memory and disk; the agent's own limits; the programs it's running, each with a Stop button.
- **Settings:** models (add, edit, make default), API keys (paste one; it's never shown again, only its last characters), allowed sites, and a snippet to copy panel changes into config.toml. Panel changes are kept in the VM on top of config.toml, so `update` doesn't undo them.

Machine controls (start, stop, snapshot, reset) stay on your PC as scripts, so they keep working if the VM hangs. Closing the browser never stops the agent; the supervisor runs as a background service and the panel catches up when you come back.

Access needs a token that's created at install and that the agent can't read, so the agent can't approve its own requests.

## Models

The supervisor isn't tied to Claude. The loop talks to a **provider interface**, and an **adapter** per API style translates to and from it ([providers.py](../supervisor/uvmc/providers.py)):

| Adapter | Covers |
| --- | --- |
| `anthropic` | Claude, and Anthropic-compatible endpoints |
| `openai` | OpenAI and any OpenAI-compatible API: DeepSeek, Gemini (through Google's compatibility endpoint, beta), Mistral, Groq, OpenRouter, and local servers like Ollama, LM Studio or vLLM on your PC |

Each **model profile** in `config.toml` (`[models.<name>]`) names a provider, a URL, a model and a key name. Ready to use: `claude`, `claude-opus`, `claude-haiku`, `deepseek`. Prepared, just fill in a model name: `gpt`, `gemini`, `local`.

- Store a key with `set-key` (Anthropic) or `set-key deepseek` (the profile's `key`). Keys stay in the VM where the agent can't read them; local models need none.
- Pick the model per conversation in the panel's header, even mid-conversation. The supervisor runs `uvmc --model deepseek` in the terminal.
- Tools, rules, walls, logs and the panel are the same whatever the model. How well a model uses the tools varies a lot, so try a new one on small tasks first.

### Claude on your plan instead of the API

A Claude Pro or Max plan is often much cheaper than API use. The **"my plan"** profiles (`claude-plan`, `claude-plan-opus`, `claude-plan-haiku`) use it: Claude Code, signed in with your plan, becomes the brain, and the supervisor stays its only pair of hands.

- **Sign in once** in the panel: **Settings › Your Claude plan › Sign in with your Claude plan**. Open the sign-in page it shows, approve, and paste the code the page gives you. Behind it, the supervisor runs Claude Code's own `claude setup-token`, checks the one-year token with one tiny request, and stores it like an API key (key name `claude-subscription`), readable only by the supervisor; the token is never shown. `sign-in-claude` on your PC does the same in a terminal, as a fallback.
- **Same chat, same panel.** Pick "Claude Sonnet (my plan)" in the header. Replies, tool rows, approvals, Stop, Undo and history work as with the API models. You can switch between plan and API models mid-conversation; the earlier messages are passed along.
- **How it's kept in its box** ([brain.py](../supervisor/uvmc/brain.py)):
  - Claude Code runs as its own user, `uvmc-brain`, with all its built-in tools switched off (`--tools ""`). Its only tools come from our MCP server ([mcp_server.py](../supervisor/uvmc/mcp_server.py)), which forwards each call to the supervisor. There, rules, approval cards and logs apply as for any model, and the action runs as `agent`.
  - The login is only in the brain process's environment: never on a command line or in a file the agent can read. Nothing the agent runs can read another user's process.
  - Its network goes through a second proxy that only allows Anthropic's sites (`[subscription] sites`), and the firewall gives that user nothing else. Its connections are listed under Activity › Sites as "Claude Code itself".
  - The tool bridge (a unix socket) only answers the `uvmc-brain` user.
- **Updates:** Claude Code in the VM never updates itself, so a new version can't change behind your back. Update it when you choose: panel › Settings › Your Claude plan › **Update Claude Code**, or `update-claude` on your PC. It installs the latest version and the next message uses it.
- **Limits:** usage counts against your plan's limits, shared with claude.ai. With a "my plan" model selected, the header shows how much of each usage window is used and when it resets (for example "Plan: 5h 81% (resets 16:00), week 30%"; hover for details), amber when Claude Code warns you're close, red when the limit is reached. These numbers come from Claude Code as it works, so they appear after the first reply. A plan model can coordinate workers, but can't be a worker itself.
- **Terms:** this is for your own use with your own plan. Anthropic doesn't allow third-party products to offer claude.ai login to others without approval, so don't run UVMC as a service for other people on your plan.

### Coordinator and worker

Pick a **Worker** next to the model in the panel's header, and the conversation splits into two roles (the orchestrator–worker pattern):

- The model you talk to becomes the **coordinator**. It discusses with you, plans, and hands work over with a `delegate` tool. It can read files to check results, but can't change the machine itself. Its instructions are in [orchestrator_rules.md](../supervisor/orchestrator_rules.md).
- The **worker** gets each job as self-contained instructions, with no memory of your conversation, and does it with the normal tools inside all the same walls. It ends with a report, which goes back to the coordinator.
- In the panel, each delegation shows the instructions and the worker's steps live, nested under the `delegate` step. Approvals still come to you, whoever asks. Stop stops both.
- Workers can't delegate further, and work one at a time. `[delegation]` in config.toml sets which profiles may be workers and their step limit. Choose "None" to go back to one model doing everything.

### What the agent is told

At the start of each conversation the agent gets three clearly separate parts, all visible and editable in **Settings › What the agent is told**:

1. **House rules:** the fixed rules ([house_rules.md](../supervisor/house_rules.md)). Edit them in the panel (kept in the VM; **Reset** returns to the repo version) or in the repo and push with `update`.
2. **Live briefing:** written by the supervisor for that conversation: UVMC's version and which model it is (coordinator or worker), the folders shared right now, the allowed sites, its limits, what's in the work folder, the date, and what you can do in the panel. Switch it off, or any part of it.
3. **Your notes:** free text every conversation gets, e.g. "Answer in French".

A preview shows the exact text. The whole text stays the same for a conversation (Claude Code would otherwise restart on every change), so changes apply from the next new conversation. A coordinator gets its role's rules ([orchestrator_rules.md](../supervisor/orchestrator_rules.md)) instead of the house rules; its workers get the house rules, a briefing written for a worker, and your notes.

## Tools the agent builds

Ask the agent for something that should keep working after the conversation, for example *"every morning, check this page and note what changed"*. It writes a small program in its work folder, tests it, and proposes it with `propose_tool`. You get an approval card showing everything the tool would be: what it does, when it runs, the sites it reaches, the settings and secrets it will ask you for, and its full code (or only the changes, for a new version). Nothing is installed unless you approve.

Approved tools are in the **Tools** tab: **Run now**, **Turn on** (for the ones on a schedule), their settings form, their code (read-only), and every run with its output. They start switched off, so you can fill in their settings first. A secret you type there is handed to the tool only when it runs; the agent never sees it.

Each tool runs as its own Linux user, from a frozen copy the agent can't change (a change is a new proposal), inside the tools' own resource box (512 MB), with a time limit per run. It can reach only the sites it declared, through its own proxy; the firewall gives it nothing else. Its own data folder (`HOME`) is kept between runs.

For now tools reach sites over HTTP(S) only (no email protocols like IMAP), use the programs already on the machine, and can't be called by the models or your PC's Claude apps. `[made_tools]` in config.toml has the settings.

## Shared folders from your computer

The agent can work with folders from your computer without being able to damage them:

- **When you start the panel**, a "Start UVMC" window lists your folders with ticks: tick the ones the agent may see this time, **Add folder…** with the normal folder picker, **Start**. The choice is remembered; "Don't ask again" skips the window (hold Shift on the shortcut, or use `--folders`, to see it again). On macOS it's native dialogs; on Linux `zenity`, or a few questions in the terminal.
- Behind it is `shares.txt` in the repo (`name = folder`; `#off name = folder` = remembered but not shared; see [shares.example.txt](../shares.example.txt)), which you can also edit and apply with `share`. Relative folders are inside this repo; your whole drive or user folder is refused.
- The agent **reads** them under `/mnt/shared/<name>`. That view is read-only for it: writing or deleting fails, whatever command it tries.
- To **change or add** a file, the agent prepares the new version in its work folder and calls `update_shared_file`. You get a card with the file, sizes, a line-by-line diff for text files and its reason: **Replace it / Deny**.
- On approval the supervisor (not the agent) copies the original to `<folder>/_uvmc_backups/<time>/…`, writes the new file, and checks it arrived intact. **Restore original** in the panel undoes it.
- **Deleting** files on your computer isn't offered at all; the agent can only tell you.
- A coordinator can't write to shared folders; workers can, with the same approval.

To try it with sample files instead of a real project: run `make-testdata` (it creates `testdata/` inside this repo: text, JSON, CSV and 1 MB / 60 MB binaries, ignored by git), then `share`.

How it works inside the VM: each folder is mounted at `/srv/uvmc-host/<name>`, which only the supervisor can reach, and given to the agent as a read-only view at `/mnt/shared/<name>` (refreshed every 30 seconds). If a mount can't be shown to the agent directly, the view becomes a read-only copy instead.

Platform notes: on Windows, Multipass needs folder sharing switched on once (`share` asks for admin for that). On Linux with the snap version of Multipass, folders outside your home folder need `sudo snap connect multipass:removable-media`.

## The agent's walls

The agent is free inside its own space: commands run without asking, it can install Python and npm packages for itself, and it can start servers and scripts. The walls are enforced by Linux, not by guessing what commands do:

| Wall | What it does |
| --- | --- |
| No root | The `agent` user has no sudo, so it can't change the system, the supervisor, the rules or the logs. System packages go through the `install_system_package` tool, which you approve. |
| Firewall | The kernel blocks all outgoing traffic from `agent` except to the supervisor's proxy on 127.0.0.1. No DNS, no other ports, no direct internet. |
| Watched proxy | Allowed sites (`[network]` in config.toml, plus the ones you approve) go through; you're asked about others; every connection is logged with site and bytes. It sees which sites, not what's inside HTTPS. |
| Resource box | Everything the agent runs shares one memory, CPU and process limit (`[limits]`). A runaway program hits the box, not the VM. Each conversation runs in its own smaller box inside it, so Stop ends that conversation's programs and leaves the others running. |
| One user per conversation | Each conversation runs as its own user (agent1, agent2, ...), in its own copy of the work folder, and the firewall lets that user reach only its own proxy. Its files, programs and sites are its own; the main work folder is read-only for all of them. |
| Fixed-size home | `/home/agent` is its own disk (6 GB by default), and `/tmp` and `/var/tmp` are size-limited memory disks, so filling a disk can't take the VM down. |
| Undo | A hard-linked copy of the work folder is saved before every task (last 20 kept). |
| Activity log | Linux's audit system records every program the agent starts, even inside scripts. |
| Factory reset | The `clean` snapshot on your PC, for anything else. |

**Memory budget.** The VM's memory is shared by Linux and the supervisor (about 600 MB), the agent's box (`[limits] memory`, 1200 MB) and Claude Code's own box when you use a "my plan" model (`[subscription] memory`, 700 MB; one Claude Code uses about 200-400 MB, and each "my plan" conversation that works runs its own, so two at once may need a bigger box). If they don't fit, `update` shrinks the agent's box and says so. On a 2 GB VM the agent gets about 700 MB; 3 GB gives everyone their full share. Claude Code is closed after 15 unused minutes (`idle_minutes`) to free its memory; your next message restarts it with the conversation so far. To give an existing VM more memory: `multipass stop uvmc`, `multipass set local.uvmc.memory=3G`, `multipass start uvmc`, then `update`.

Not covered by the walls: the agent can still change things in its own home outside the work folder (undo covers only the work folder), and it can use its CPU and memory share fully. Changing `home_size` later doesn't resize an existing home disk.

## Using UVMC from a terminal or scripts

**The `uvmc` command** (`windows\uvmc.cmd` on Windows, `unix/uvmc` on macOS and Linux, which needs python3):

```
uvmc ask "clean up the csv in testdata"     runs a task, shows its steps live, asks approvals here
uvmc ask --new --model claude-plan "..."    in a new conversation (a new tab in the panel), with a given model
uvmc ask --no-ask "..."                     refuses anything needing approval instead of asking
uvmc ask --json "..."                       waits, then prints the result as JSON (exit code 0 = done)
uvmc status | stop | new | models | model <name> | open | url
```

On Windows the options are written `-New -Model claude-plan -NoAsk -Json`. Add the folder to your PATH to type `uvmc` from anywhere. Approvals you don't answer in the terminal can still be answered in the panel.

**The HTTP API** is what the panel itself uses. `uvmc url` prints the address and the panel token; send the token as `Authorization: Bearer <token>`. The full reference, where you can try every call, is at `http://<vm-ip>:8080/docs`. The main calls:

```
POST /api/run  {"text": "...", "model": ..., "new": true, "no_ask": true, "timeout": 600}
               runs one task and answers when it ends: the final reply, how it ended, each
               tool step, approvals, errors, and files changed in shared folders
               ("new": in a conversation of its own, next to the panel's)
POST /api/task {"text": "...", "conv": ...}   starts a task and returns at once
               ("after": its first event, "conv": its conversation)
GET  /api/events                     live events of every conversation (server-sent events;
                                     each names its "conv")
POST /api/approve {"id": ..., "answer": "allow" | "always" | "deny"}
POST /api/stop {"conv": ...}   POST /api/new   POST /api/model {"name": ..., "conv": ...}
GET  /api/conversations   POST /api/conversations   POST /api/conversations/close {"conv": ...}
GET  /api/state?conv=...  /api/history  /api/files  /api/activity  /api/machine
Calls that take "conv" use the current conversation (the last one opened or picked) without it.
```

Set `UVMC_URL` and `UVMC_TOKEN` to make `uvmc` skip looking them up through Multipass.

### Your PC's Claude apps using UVMC (optional)

Claude Code and Claude Desktop on your PC can hand tasks to the agent in the VM, as a tool. It's off until you switch it on:

1. Say yes when the installer offers it, or run `connect-claude-apps` (`windows\connect-claude-apps.cmd`, `unix/connect-claude-apps.sh`) once. It adds UVMC to Claude Code (for all your projects) and opens the UVMC extension ([extension/uvmc.mcpb](../extension/uvmc.mcpb)) in Claude Desktop, which asks you to install it. `-Remove` (`--remove`) takes it out of Claude Code; remove the extension in Claude Desktop's settings.
2. In the panel, **Settings › Claude apps on your PC**, switch it on (the installer does this too when you say yes). Uninstalling takes UVMC out of the apps again.

The apps then get five tools: `run_task` (waits up to a few minutes for the result), `task_status`, `stop_task`, `list_files` and `read_file` in the work folder. Behind them the apps start `uvmc mcp`, a small relay that finds the machine through Multipass each time, so nothing goes stale when its address changes. Tasks from your apps run with the same rules and walls and show in the panel; anything needing approval waits for you there, and the apps can't approve anything. `run_task` starts a new conversation by default, in its own tab ("from a Claude app"), so it can work while you use the panel; its result names the conversation, which `task_status`, `stop_task` and a follow-up `run_task` can take.

## The supervisor

| File | What it is |
| --- | --- |
| [supervisor/config.toml](../supervisor/config.toml) | Model, limits, enabled tools, allow / ask / deny rules |
| [supervisor/house_rules.md](../supervisor/house_rules.md) | The agent's system prompt (also the worker's) |
| [supervisor/orchestrator_rules.md](../supervisor/orchestrator_rules.md) | The coordinator's system prompt |
| [supervisor/uvmc/agent.py](../supervisor/uvmc/agent.py) | The loop: ask the model, check rules, run tools, log, repeat |
| [supervisor/uvmc/providers.py](../supervisor/uvmc/providers.py) | The provider interface and the Anthropic and OpenAI-compatible adapters |
| [supervisor/uvmc/brain.py](../supervisor/uvmc/brain.py) | Claude Code on your plan as the brain: its process, the stream-json conversation, the tool bridge |
| [supervisor/uvmc/mcp_http.py](../supervisor/uvmc/mcp_http.py) | UVMC as a tool for the Claude apps on your PC (off until switched on) |
| [supervisor/uvmc/briefing.py](../supervisor/uvmc/briefing.py) | What the agent is told: house rules, the live briefing and your notes |
| [supervisor/uvmc/signin.py](../supervisor/uvmc/signin.py) | Signing in with your Claude plan from the panel, through Claude Code's own `setup-token` |
| [supervisor/uvmc/mcp_server.py](../supervisor/uvmc/mcp_server.py) | The MCP server Claude Code gets as its only tools; forwards every call to the supervisor |
| [supervisor/uvmc/service.py](../supervisor/uvmc/service.py) | The background service: conversations, approvals, stop, bring-in, and the HTTP API |
| [supervisor/uvmc/static/index.html](../supervisor/uvmc/static/index.html) | The control panel (one file, no build step) |
| [supervisor/uvmc/executor.py](../supervisor/uvmc/executor.py) | Where commands run: as `agent`, in the resource box, with the proxy. Swap it to move the supervisor to the host later |
| [supervisor/uvmc/tools.py](../supervisor/uvmc/tools.py) | `run_command`, `read_file`, `write_file`, `list_dir`, `install_system_package` |
| [supervisor/uvmc/netproxy.py](../supervisor/uvmc/netproxy.py) | The agent's only way online: allowed sites, approvals, connection log |
| [supervisor/uvmc/audit.py](../supervisor/uvmc/audit.py) | Reads the audit log into the activity log |
| [supervisor/uvmc/checkpoints.py](../supervisor/uvmc/checkpoints.py) | Work-folder copies for Undo |
| [supervisor/uvmc/workcopies.py](../supervisor/uvmc/workcopies.py) | Each conversation's own copy of the work folder; bringing work into the main folder; resolutions waiting for you |
| [supervisor/uvmc/transcript.py](../supervisor/uvmc/transcript.py) | A conversation's log as a readable transcript, for an agent resolving a conflict |
| [supervisor/uvmc/made_tools.py](../supervisor/uvmc/made_tools.py) | Tools the agent builds: proposals, frozen copies, their users, proxies and schedule |
| [supervisor/uvmc/shares.py](../supervisor/uvmc/shares.py) | Shared folders: approved writes, backups, restore |
| [supervisor/uvmc/settings.py](../supervisor/uvmc/settings.py) | Panel settings: model profiles and API keys, on top of config.toml |
| [supervisor/setup.sh](../supervisor/setup.sh) | Installs all of this inside the VM; `update` runs it again |
| [supervisor/uvmc/rules.py](../supervisor/uvmc/rules.py) | Decides allow / ask / deny per call |
| [supervisor/tests/](../supervisor/tests/) | Offline tests with a fake Claude |

- In the VM it runs as the `uvmc` systemd service (`uvmc serve`), with the panel on port 8080.
- The terminal version still works for quick tests: `supervisor.cmd` gives a `task>` prompt with y/N approvals; `supervisor "do X"` runs one task and exits; `--no-ask` refuses everything that would need approval.
- Every conversation is logged as JSON Lines in `/var/log/uvmc/runs/` inside the VM. `stop`, `reset` and `uninstall` first copy them to the repo's `logs/runs` folder, so history survives a reset.
- After editing anything in `supervisor/`, run `update` to push it into the VM. A factory reset returns to the version installed at setup, so run `update` after a reset too.

**Who can do what inside the VM**

| | `ubuntu` (supervisor) | `agent` | `uvmc-brain` (Claude Code on your plan) |
| --- | --- | --- | --- |
| Run tools in `/home/agent/work` | via sudo, in the resource box | yes | only by asking the supervisor |
| Reach the internet | yes | only through the proxy | only Anthropic's sites, through its own proxy |
| sudo | yes | no | no |
| Read the API keys (`/etc/uvmc/api_key`, `/etc/uvmc/keys/`) | yes | no | no; it gets only its own login, in its environment |
| Change config, rules, supervisor code | yes (as root) | no | no |
| Read the logs, checkpoints, activity log | yes | no | no |
| Read the panel token (`/etc/uvmc/panel_token`) | yes | no | no |

Run the tests on any machine with Python 3.11+ and the packages in `requirements.txt` plus `httpx`: `cd supervisor && UVMC_NO_SUDO=1 python3 -m unittest discover tests`.

## Inside the VM

- The agent works as user `agent` in `/home/agent/work`.
- `uvmc` starts the supervisor (as `ubuntu`; it refuses to run as `agent`).
- `uvmc-agent shell` opens a shell as the agent (what `shell` on your PC does).
- `uvmc-setkey [name]` replaces a stored API key (reads it from stdin; run with sudo).
- `uvmc-install-claude` installs or updates Claude Code (run with sudo; `update-claude` on your PC does it).
- `uvmc-claude-signin` signs Claude Code in with your Claude plan (run with sudo; `sign-in-claude` on your PC does it).
