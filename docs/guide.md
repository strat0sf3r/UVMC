# UVMC guide

Everything about installing, using and changing UVMC. For a short introduction, see the [README](../README.md); for the reasoning behind it and the roadmap, [design.md](design.md).

## Install

UVMC runs as a real Ubuntu 24.04 VM through [Multipass](https://canonical.com/multipass). The installer sets up everything, including Multipass itself.

| | Windows (Pro / Enterprise / Education) | macOS / Linux |
| --- | --- | --- |
| Install | double-click `windows\install.cmd` | `unix/install.sh` |
| **Open the control panel** (asks which folders to share first) | `UVMC Panel` shortcut, or `windows\panel.cmd` | `unix/panel.sh` |
| Choose shared folders again | hold Shift on the shortcut, or `windows\panel.cmd --folders` | `unix/panel.sh --folders` |
| The UVMC helper on your PC (see "The helper on your PC") | `windows\helper.cmd` (`-AutoStart`, `-NoAutoStart`, `-Stop`) | `python3 unix/helper` (`--background`, `--autostart`, `--no-autostart`, `--stop`) |
| Plain shell in the VM | `windows\shell.cmd` | `unix/shell.sh` |
| Sign in with your Claude plan (for the "my plan" models; easier: panel › Settings) | `windows\sign-in-claude.cmd` | `unix/sign-in-claude.sh` |
| Let Claude Code / Claude Desktop on your PC use UVMC (see below) | `windows\connect-claude-apps.cmd` | `unix/connect-claude-apps.sh` |
| Run a model on your graphics card (see "A model on your PC") | `windows\local-model.cmd` | `unix/local-model.sh` |
| Start / stop that model (double-click) | `windows\start-local-model.cmd`, `windows\stop-local-model.cmd` | `unix/start-local-model.sh`, `unix/stop-local-model.sh` |
| Remove that model (asks first) | `windows\uninstall-local-model.cmd` | `unix/uninstall-local-model.sh` |
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
2. Windows only: turns on Virtual Machine Platform if it's off (every edition has it, Home included). This needs one restart; the installer continues by itself after you log back in.
3. Installs Multipass if it's missing (on Windows the pinned version, from its GitHub release, checked against its hash; Homebrew on macOS, snap on Linux) and remembers that it did. On Windows, an older Multipass is upgraded in place (the machine is kept), and a machine still on Multipass's old Hyper-V driver is moved to the HCS driver; once the moved machine answers, it offers to remove the old Hyper-V copy (it takes as much disk as the machine). Running `install.cmd` again is how you upgrade Multipass for UVMC. On Windows it also adds a scheduled task, `UVMC - start Multipass after Windows starts`: Multipass 1.17.0's service dies on its first starts after each boot on Windows 10, and the task starts it again for two minutes after Windows starts (the uninstaller removes it).
4. Creates the VM (2 CPUs, 15 GB disk, and 3 GB of memory if your computer has 12 GB or more, else 2 GB; set `UVMC_MEMORY`, e.g. `4G`, before installing to choose) and runs [cloud-init.yaml](../cloud-init.yaml) inside it: an `agent` user with no sudo and basic tools. On Windows it first asks where to store the machine: next to UVMC (the repo's `machine` folder, kept out of git), Multipass's usual place (`C:\ProgramData\Multipass`), or another folder you type. The folder is given to the Multipass service directly as well, so it's used at once, not only after a restart of Windows. It takes about 5 GB at first and can grow to about 16 GB.
5. Installs the supervisor (see below).
6. Doesn't ask for an API key any more (since 0.12.6): add keys, or sign in with your Claude plan, in the panel's Settings tab. They're stored in the VM where the agent can't read them.
7. Takes a snapshot called `clean`: the factory reset point.
8. Asks three optional questions, once each: whether your PC's Claude apps may use UVMC (default no); whether to start the UVMC helper each time you log in (default no, since 0.21.1; the panel shortcut starts it anyway; see "The helper on your PC"); and, if no model is set up on the PC yet, whether to set one up now (default no; `local-model` then runs in a window of its own, without administrator rights, and asks for them once for its firewall rule).
9. Windows: adds a `UVMC Panel` shortcut to the desktop and Start menu.

First install takes about 5-10 minutes, mostly downloads.

**What the uninstaller does**

Asks first, copies the run logs into the repo's `logs` folder, offers to save the agent's `work` folder to your Documents (Windows) or home folder (macOS/Linux), deletes the VM with all its snapshots, removes the shortcuts, stops the UVMC helper and takes it out of your login (with its key and log), asks before removing the model on your PC with its downloaded models (default no; `uninstall-local-model` does it later), and removes Multipass only if UVMC installed it. It also removes a Hyper-V copy Multipass kept from the move to its HCS driver. Virtual Machine Platform stays on; the uninstaller prints the command to turn it off. If the machine was stored next to UVMC, restart Windows before installing again (until then a new Multipass would still use the removed folder; the uninstaller says so).

## Driving the agent

Everything goes through the supervisor, from the control panel (`panel`): the model only proposes actions, and the supervisor checks each one against your rules, runs it as `agent`, and logs it. That holds for every model, including Claude Code on your plan. There's no other way to run an AI agent in the VM; `shell` only gives you a plain shell to look around.

## The control panel

A web page served by the supervisor inside the VM, opened in your normal browser by the `UVMC Panel` shortcut or the helper's tray icon. The shortcut goes through the UVMC helper on your PC (it starts it if needed; see below), which finds the VM's current address and signs you in; if the helper doesn't answer, the shortcut does that itself, as before 0.20.

- **Conversation:** type a task and press Enter. Every step appears on the timeline as it happens: the agent's messages, each tool call (click it to see the output), and the result. Follow-up tasks continue the same conversation. **Stop** ends the current task at any moment.
- **Several conversations at once:** **New conversation** opens another one in a tab next to the others, and they can work at the same time (3 at most by default). Each tab's dot shows what it's doing: grey idle, green working, amber waiting for you. Each conversation has its own model, timeline, approvals and Stop, and runs as its own Linux user (agent1, agent2, ...) in its own copy of the work folder, through its own proxy. They share the machine's limits and the allowed sites. Tasks from scripts and from your PC's Claude apps get their own tab ("from a script", "from a Claude app"). Close a tab with ×; its run stays under Runs. `[conversations]` in config.toml sets the limits.
- **The main work folder:** `/home/agent/work` is the main version; conversations can read it but not change it. A conversation gets its own copy at its first task, and the bar under the tabs says how its copy stands: its changes not in the main folder yet, newer files in the main folder, files changed on both sides. **Bring into the main folder** shows what would happen first, then applies it both ways: its changes go into the main folder (an undo point is made first, and **Undo this bring-in** puts it back), and the main folder's newer files come into its copy. A text file changed on both sides in different places is merged line by line. Real conflicts (the same lines, a file that isn't text, or removed on one side and changed on the other) go to this conversation's agent, which resolves them like Claude Code resolves a git conflict: it works out what each side was doing (both versions, the other conversation's whole log as a readable transcript, and the main folder and the other conversations' folders, which it can read) and keeps both sides' work. Nothing of it goes into the main folder yet: you get **The agent's proposed resolution**, with its explanation and, per file, the changes it proposes. Per file you accept it, keep this conversation's version, keep the main folder's, or decide later. A beta option lets the conversation whose version is in the main folder resolve it instead; until you answer its proposal, the file stays out of this conversation's bring-in. You can also tell the agent in the chat what to change, and review its new proposal. Closing a conversation with changes not brought in asks first. After a restart, conversations with a copy come back, so their work isn't lost (the agent starts afresh).
- **Approvals:** an amber card appears, the header (and the conversation's tab) turns amber, and the agent waits for you. Two kinds: a tool your rules mark "ask" (such as a system package install) gets Allow / Deny; a site that isn't on the allowed list gets **Allow for this task / Always allow / Deny** (unanswered site requests are refused after 2 minutes). Each conversation has its own proxy, so the question appears in the conversation that asked, on the step that asked.
- **What each step did:** every tool call shows which programs it started and which sites it contacted; click it to see them with the output.
- **Undo this task:** under each of your tasks, puts this conversation's copy of the work folder back to how it was before it. Other conversations and the main folder aren't touched.
- **Files:** the agent's work folder, updated as it works. Click a file to read it. **Delete** beside a file or folder (or above an open file) asks first. In **This conversation**, it deletes from the conversation's copy only: that counts as one of its changes, and the main folder loses the file when you bring them in (Undo this task doesn't cover it). In **Main folder**, it deletes there straight away, after an undo point: **Undo** right after puts the main folder back (along with anything brought in since). Conversations get the change at their next bring-in. A project's own top folder, `.uvmc-merge` and `.uvmc-context` can't be deleted this way, and neither can anything under the shared folders from your PC (they're read-only).
- **Activity:** every program the agent started and every site it contacted in this conversation.
- **Runs:** every past conversation, replayable step by step. **Delete** beside a closed one removes its log for good, after you confirm. A conversation that's still open is closed first with its tab's ×. Copies made elsewhere stay: the PC's `logs` folder (from stop or reset), transcripts handed to another conversation, and the audit log's record of commands.
- **Machine:** processor, memory and disk; the agent's own limits; the programs it's running, each with a Stop button.
- **Settings:** models (add, edit, make default), API keys (paste one; it's never shown again, only its last characters), allowed sites, and a snippet to copy panel changes into config.toml. Panel changes are kept in the VM on top of config.toml, so `update` doesn't undo them.

Machine controls (start, stop, snapshot, reset) stay on your PC as scripts, so they keep working if the VM hangs. Closing the browser never stops the agent; the supervisor runs as a background service and the panel catches up when you come back.

Access needs a token that's created at install and that the agent can't read, so the agent can't approve its own requests. The shortcut and the helper open the panel with it in the address (`?token=`); the panel answers with a small page that sets its sign-in cookie (`SameSite=Strict`, `HttpOnly`) and moves on by itself to the same page without the token. A redirect would lose that cookie when the visit starts at the helper (`127.0.0.1`), since browsers count the whole chain as coming from another site. It only ever moves on to a page of the panel: `//host` and similar tricks become a path on the panel ([service.py](../supervisor/uvmc/service.py) `signin_target`).

## The helper on your PC

A small program on your PC that lets the panel do what only the PC can do. The VM can't see your PC, so without it the panel can only show you commands to run there. On Windows it's [helper.ps1](../windows/helper.ps1), a tray icon; on Mac/Linux [unix/helper](../unix/helper), in Python with nothing to install, and without a tray icon (notifications come from the system).

- **What it does:** under **Settings › Models on your PC**, the panel gets **Download** on every size not downloaded (it picks that size and downloads it, with a progress bar; UVMC switches to it when it's there), **Delete** on downloaded sizes and **Forget** on your own files, **Start / Stop the model server**, **Test the models**, **Add your own GGUF file** (you choose the file in a window on the PC), **Get the newest llama.cpp** (with the version you have), and switches for starting the model server at login and for its normal log. Each runs `local-model` hidden on the PC; you get a notification when it's done, even with the panel closed. It also opens the panel at the VM's current address, signed in (`/panel`): Windows can give the VM a new address while it runs, and an open panel tab then stops answering; open it again from the shortcut or the tray.
- **Starting it:** the installer asks whether to start it at each login (default no); the `UVMC Panel` shortcut starts it if it isn't running; `helper` starts it by hand (a second start just opens the panel). The tray icon's menu has **Start the helper when I log in**, with a checkmark when it's on: click it to switch. `helper -AutoStart` / `-NoAutoStart` does the same from a terminal (a shortcut in your Startup folder, started through Explorer so it never runs with administrator rights); `helper -Stop` quits it. On Mac/Linux: `python3 unix/helper --background`, `--autostart` / `--no-autostart` (a LaunchAgent on a Mac, a systemd user service on Linux), `--stop`.

  **Mac and Linux:** the helper has no tray icon there, so starting at login is switched in Terminal, from the UVMC folder: `python3 unix/helper --no-autostart` stops it starting at login (on a Mac it unloads and deletes `~/Library/LaunchAgents/com.uvmc.helper.plist`; on Linux it disables and deletes the systemd user service `uvmc-helper`), and `python3 unix/helper --autostart` turns it back on. The installer asks once, default no.
- **The tray icon (Windows):** double-click opens the panel; the menu has Open the UVMC panel, Model on this PC › Start / Stop, Stop UVMC (asks first; saves the run logs like `stop`), and Quit the helper.
- **Walls.** It listens on `127.0.0.1:8092` only, on a plain socket, so the VM and your home network can't reach it (Windows' own web service would listen on every address). Every call needs its key, kept in `%LOCALAPPDATA%\UVMC\helper.json` (`~/.config/uvmc/helper.json`, readable by you only, on Mac/Linux), and from a browser it must come from the panel's own page (one of the VM's current addresses, port 8080); the Host header must name `127.0.0.1` or `localhost`, so other websites can't reach it through DNS tricks. It does a fixed set of things, with model names checked against [local_models.json](../supervisor/uvmc/local_models.json); deleting a model and adding a file of your own are confirmed in a window on the PC. The panel gets the key in the part of the address after `#`, which browsers never send to the VM, keeps it for that tab only, and cleans the address. The VM never talks to the helper: the panel's page in your browser does, for your clicks. Its log (`helper.log` next to the key) holds what was asked and the answer's code, never keys or what conversations say.
- **Browsers.** Firefox lets the panel reach it with no prompt. Chrome and Edge may ask once to allow the site to reach devices on your local network. If the helper doesn't answer (stopped, or the browser keeps the page from reaching it), the panel says so and shows the `local-model` commands instead.
- **Mac/Linux windows:** confirming and choosing a file use `osascript` on a Mac, `zenity` or `kdialog` on Linux; without one of them, Delete and Add your own file are refused with the command to run instead, never done without asking.
- **Limits:** while it reads from the VM (up to about 25 s when the VM doesn't answer), its tray and other calls wait. It's a PowerShell script for now (about 85 MB of memory); one Go program for all three systems, with a tray icon everywhere, is a later goal (design.md).

## Models

The supervisor isn't tied to Claude. The loop talks to a **provider interface**, and an **adapter** per API style translates to and from it ([providers.py](../supervisor/uvmc/providers.py)):

| Adapter | Covers |
| --- | --- |
| `anthropic` | Claude, and Anthropic-compatible endpoints |
| `openai` | OpenAI and any OpenAI-compatible API: DeepSeek, Gemini (through Google's compatibility endpoint, beta), Mistral, Groq, OpenRouter, and llama.cpp's server on your PC (`local`, below) |

Each **model profile** in `config.toml` (`[models.<name>]`) names a provider, a URL, a model and a key name. Ready to use: `claude`, `claude-opus`, `claude-haiku`, `deepseek`, and `local` once `local-model` has run. Prepared, just fill in a model name: `gpt`, `gemini`, `openrouter` (below).

- Add keys in the panel: **Settings › API keys** (the profile's `key` name, e.g. `deepseek`). Keys stay in the VM where the agent can't read them. (The `set-key` scripts were removed in 0.12.6: piping a key into `multipass exec` hangs with Multipass on Windows (1.16, still in 1.17.0); the installer no longer asks for one either.)
- Pick the model per conversation in the panel's header, even mid-conversation; from a terminal, `uvmc ask --model deepseek "..."`.
- Tools, rules, walls, logs and the panel are the same whatever the model. How well a model uses the tools varies a lot, so try a new one on small tasks first.

### OpenRouter

[OpenRouter](https://openrouter.ai) gives one key for hundreds of models (Claude, GPT, Gemini, DeepSeek, Qwen, Llama and more) and passes each request to one of the providers that serve the model. It speaks the OpenAI-compatible API, so it goes through the `openai` adapter; what is particular to it is in [openrouter.py](../supervisor/uvmc/openrouter.py).

- **Key:** make one at openrouter.ai/settings/keys and paste it in **Settings › API keys** as `openrouter` (the key's row then shows its credit and, for free models, today's requests).
- **Model:** edit the `openrouter` profile (or **Add a model › Start from OpenRouter**) and click **Choose...** next to Model name: a searchable list of the models that can use tools, with price per million tokens and context. Ids look like `deepseek/deepseek-v4-pro`. For several OpenRouter models at once, add one profile each with the same key.
- **Privacy:** the profile starts `open`. Set it to `paid` and every request asks OpenRouter for providers that keep and collect nothing (`provider: {zdr: true, data_collection: "deny"}`); a model with no such provider is refused rather than sent elsewhere. OpenRouter's own account settings (openrouter.ai/settings/privacy) apply as well; read them before choosing `paid`. Every request with tools also asks only for providers that support every parameter (`require_parameters`), so a request never lands where tools are ignored.
- **Free models** (ids ending `:free`) can only be `open`: their providers may keep or train on what they get. They allow 20 requests a minute, and 50 a day until you have bought 10 dollars of credit (1000 after). A task can take up to 40 model turns, so a few tasks use a day's allowance.
- **Cost:** OpenRouter says what each reply cost; the header adds it up per conversation next to the tokens.
- **Reasoning models:** their reasoning blocks are sent back to the same profile on the next turn (some models, Claude among them, need that with tool calls), and kept in memory only, never in the log. Another model gets the plain conversation.
- **Errors** say what to do: no credit (402), the limit and when to try again (429), no provider that keeps nothing (paid profiles).
- Not sent: attribution headers (`HTTP-Referer`, `X-OpenRouter-Title`); UVMC doesn't announce itself. While UVMC is locked, the model list and the credit check aren't asked either.

### A model on your PC

`local-model` (`windows\local-model.cmd`, `unix/local-model.sh`) runs models on the PC's graphics card with [llama.cpp](https://github.com/ggml-org/llama.cpp)'s own server: no account, no subscription, nothing to pay. It runs on the PC, not in the VM, which has no graphics card.

- **Run it once.** It downloads the newest llama.cpp (Vulkan on Windows and Linux: NVIDIA, AMD including older cards like the RX 5000 series, Intel; Metal on a Mac, checked against its published checksum) and the largest size of the recommended Qwen that fits the card (on an 8 GB card, Qwen3.5 9B). It starts the server and gives UVMC the server's key with `uvmc-setkey local`. It does **not** start by itself when you log in unless you ask: `-AutoStart` (a shortcut in your Startup folder; a LaunchAgent or systemd user service on Mac/Linux), `-NoAutoStart` to undo.
- **Several models, one list.** [local_models.json](../supervisor/uvmc/local_models.json) lists what can be added, grouped by model: a short description, strengths and weaknesses for each model, then its sizes, each in its newest version (Qwen: 2B, 4B and 9B are 3.5, 27B is 3.8, 35B-A3B is 3.6), with the download size and the memory it needs on a graphics card and on a Mac (with an 8,000-token conversation). On the list: Qwen (recommended), Qwen uncensored by HauhauCS (recommended) and by huihui-ai, Gemma 4 and Gemma 4 uncensored by huihui-ai, Ministral 3, Devstral Small 2 (Mistral's coding model) and gpt-oss 20B (OpenAI); only sizes for gaming cards (up to 32 GB) and Macs (up to 64 GB). `local-model -List` shows it with whether each size fits your card (fits, tight, too big), `-Add <name>` downloads one (several are kept side by side), `-Delete <name>` deletes one. Each file's SHA-256 is pinned in the list: a file that changes on Hugging Face is refused until it is checked again. Official files come from the maker's page; "converted" ones are the maker's model turned into GGUF by Unsloth; "unofficial" ones are said so before downloading and marked in the panel.
- **Router mode.** The server starts with no model of its own and a `models.ini` that `local-model` writes (the card's settings, then one section per downloaded model). The model a request names is loaded on the card, one at a time (`--models-max 1`); switching takes a few seconds.
- **Your own models.** `local-model -AddFile <file.gguf> [-Name <name>]` uses a GGUF file you have, where it is, with the card's settings. UVMC doesn't check it (no pinned checksum); the panel lists it under "Your own models". `-Delete <name>` only forgets it: your file stays.
- **Which one UVMC uses** is picked in the panel (Settings › Models on your PC › Select on a size, for every conversation, from the next message on). A size not downloaded yet: with the UVMC helper running, **Download** fetches it from the panel; without it, Select picks it and the panel shows the command to run on the PC (`local-model -Add <name>`). Either way UVMC switches to it as soon as it's there. While a model loads on the card, the list looks again every 3 seconds until it's there. Nothing picked: the one on the card, else the first. A picked model that was deleted falls back the same way.
- **Checking a model.** `local-model -Test [name]` sends it UVMC's own kind of request (a task and the `run_command` tool), says whether it called the tool right, and measures its speed. Sizes that haven't been through this on the RX 5700 XT are marked "untested".
- **Commands.** `-Update` gets the newest llama.cpp; `-Status`, `-Start`, `-Stop`, `-AutoStart`, `-NoAutoStart`, `-Remove`. `start-local-model`, `stop-local-model` and `uninstall-local-model` do `-Start`, `-Stop` and `-Remove` with a double-click. On Mac/Linux the same with `--list`, `--add`, `--test`, `--update`, ... `-Model <name>` (from before 0.16) now does what `-Add` does. A model from an older `local-model` that is no longer on the list stays on disk, unused, until you delete it; the script says where.
- **Walls.** The server listens only on the PC itself and on the network to the VM, never the home network; on Windows two firewall rules (asked once, with administrator rights): one lets the local subnet in, on its port only (only the VM's network reaches the server, since it listens nowhere else; the rule isn't tied to that network card, which Multipass makes anew at each start of Windows), the other stops llama-server from opening any connection itself (it never needs to; downloads are done by the script). Not on Mac/Linux yet: a Mac's firewall only filters what comes in, and Linux would need llama-server as its own user. It answers only with its key, which lives in the VM where the agent can't read it. The model only writes text: what it asks for is done by the supervisor in the VM, with the same rules. The agent can't reach the PC at all: its firewall only lets it talk to the proxy, and the proxy refuses the PC's address whatever the port or site list says.
- **Finding the PC.** The profile's address is `http://{pc}:8091/v1`; the supervisor fills in `{pc}` with its network gateway each time ([pc.py](../supervisor/uvmc/pc.py)), since Windows gives the VM's network a new range at each restart.
- **Settings › Models on your PC** shows whether it runs, which model UVMC uses and how long a conversation it holds, then the models with a size on the PC, then the others under "More models you can add" (closed; each model's card opens on a click), each with what it's good and bad at, an "uncensored" label on the unofficial ones, and a row per size: download size, memory needed on a card and on a Mac, downloaded or on the card, and Select. When `local-model` has told UVMC the PC's card (it sends the card's name and memory next to the key, to `/etc/uvmc/pc-hardware.json`; nothing is detected in the VM), each size gets a dot: green fits, orange tight (may spill and slow down), grey too big. On a Mac the memory is the part the graphics may use, as llama.cpp reports it. A server from before 0.16 (one model) is still shown, with a note to run `local-model` again.
- **Settings chosen for the card** (measured on an RX 5700 XT, 8 GB): flash attention off on AMD and Intel through Vulkan (it fell back to the processor, 10x slower on long prompts), and on cards under 11 GB an 8k-token context with a smaller work buffer (`ubatch-size = 256`, which kept the model off shared memory: a 7,000-token prompt read in 18 s instead of 32 s). Thinking is off, so the agent gets straight answers. On that card Qwen3 8B writes 30-55 tokens/s and reads about 400.
- **Limits.** A small model is much weaker than Claude with tools: give it small tasks. A conversation longer than its context is refused by the server; the error says to start a new conversation, or compact this one.
- **Compacting a conversation** ([compact.py](../supervisor/uvmc/compact.py)). With the model on your PC, the header shows how much of its context the conversation takes ("context 5,200 of 8,192", orange from 70%, red from 90%), and **Compact** next to Run makes it smaller. It runs only when you click it, never by itself. Other models don't get it: Claude and the big services hold far longer conversations.
  - **Trim old outputs** (at once, no model call): long command outputs and file contents the agent wrote become a short note with their first lines. The latest 3 outputs stay whole.
  - **Summarize**: the model writes a summary that replaces the history. Your last task and the agent's last answer stay word for word. If the history doesn't fit the model's context, it's summarized in pieces. "Check the summary once more" gives the model the entries its summary doesn't mention, to add what's still needed (slower). Stop leaves the conversation as it was, and so does a failed summary.
  - **Nothing is lost.** Every entry of the conversation has a number in its log. Shortened outputs say theirs, and the summary cites them. After compacting, the agent gets a `recall` tool to read an entry in full or search the conversation. The panel keeps showing the whole conversation; only what the model gets is shorter.
  - In a private conversation the summary stays in its own history and log, and is erased with them.

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
- **Limits:** usage counts against your plan's limits, shared with claude.ai. With a "my plan" model selected, the header shows how much of each usage window is used and when it resets (for example "Plan: 5h 81% (resets 16:00), week 30%"; hover for details), amber when Claude Code warns you're close, red when the limit is reached. These numbers come from Claude Code as it works, so they appear after the first reply. A plan model can coordinate workers and be one: a plan worker runs its own Claude Code for each job, closed when the job ends.
- **Terms:** this is for your own use with your own plan. Anthropic doesn't allow third-party products to offer claude.ai login to others without approval, so don't run UVMC as a service for other people on your plan.

### Coordinator and worker

Pick a **Worker** next to the model in the panel's header, and the conversation splits into two roles (the orchestrator–worker pattern):

- The model you talk to becomes the **coordinator**. It discusses with you, plans, and hands work over with a `delegate` tool. It can read files to check results, but can't change the machine itself. Its instructions are in [orchestrator_rules.md](../supervisor/orchestrator_rules.md).
- The **worker** gets each job as self-contained instructions, with no memory of your conversation, and does it with the normal tools inside all the same walls. It ends with a report, which goes back to the coordinator.
- **Each job has its own sandbox**, like a conversation: its own Linux user (agent9, agent10, ... after the conversations' users), its own copy of the coordinator's folder, its own proxy and its own box. Its changes stay in its copy: the coordinator checks them (it can read the job's folder) and brings them into its own folder with `bring_in_worker`, or drops them with `discard_worker`. Bringing in works like bringing a conversation into the main folder: files only the worker changed are copied, changes to different lines of a text file are merged, and when the same lines changed (another job got there first) the coordinator says which version to keep, or hands the combining to another job. When a job ends, its folder is locked and its programs are stopped, since its user does the next job.
- **Several jobs at once:** jobs the coordinator hands over in one reply run at the same time, each in its own copy (3 at most per coordinator, `parallel`). A job that needs another's result waits until that one is brought in, since each job starts from the coordinator's folder as it is then.
- In the panel, each delegation shows the instructions, the job's folder and the worker's steps live, nested under its own `delegate` step, side by side when several run. Approvals still come to you, whoever asks. Stop stops the coordinator and every job.
- Workers can't delegate further. `[delegation]` in config.toml sets which profiles may be workers, their step limit, the jobs at once (`parallel`) and how many users setup makes for jobs (`worker_users`, 6: all conversations' jobs together; more jobs wait for a free one). Choose "None" to go back to one model doing everything; jobs not brought in yet can still be brought in.

### What the agent is told

At the start of each conversation the agent gets three clearly separate parts, all visible and editable in **Settings › What the agent is told**:

1. **House rules:** the fixed rules ([house_rules.md](../supervisor/house_rules.md)). Edit them in the panel (kept in the VM; **Reset** returns to the repo version) or in the repo and push with `update`.
2. **Live briefing:** written by the supervisor for that conversation: UVMC's version and which model it is (coordinator or worker), the folders shared right now, the allowed sites, its limits, what's in the work folder, the date, and what you can do in the panel. Switch it off, or any part of it.
3. **Your notes:** free text every conversation gets, e.g. "Answer in French".

A preview shows the exact text. The whole text stays the same for a conversation (Claude Code would otherwise restart on every change), so changes apply from the next new conversation. A coordinator gets its role's rules ([orchestrator_rules.md](../supervisor/orchestrator_rules.md)) instead of the house rules; its workers get the house rules, a briefing written for a worker, and your notes.

## Tools the agent builds

Ask the agent for something that should keep working after the conversation, for example *"every morning, check this page and note what changed"*. It writes a small program in its work folder, tests it, and proposes it with `propose_tool`. You get an approval card showing everything the tool would be: what it does, when it runs, the sites it reaches, the settings and secrets it will ask you for, and its full code (or only the changes, for a new version). Nothing is installed unless you approve.

Approved tools are in the **Tools** tab: **Run now**, **Turn on** (for the ones on a schedule), their settings form, their code (read-only), and every run with its result. They start switched off, so you can fill in their settings first. A secret you type there is handed to the tool only when it runs; the agent never sees it.

Each tool runs as its own Linux user, from a frozen copy the agent can't change (a change is a new proposal), inside the tools' own resource box (512 MB), with a time limit per run. It can reach only the sites it declared, through its own proxy; the firewall gives it nothing else. Its own data folder (`HOME`) is kept between runs.

**Results are in Reports.** The **Reports** button next to **New conversation** opens a Reports tab beside the conversations (close it with ×; the button opens it again). It starts with a card per tool: how its last run went (its status, or that it failed), its first few numbers, when it ran, **Open report** and **Run now**. **Open report** shows the whole report at full width, with the earlier runs to look back at and **← All reports** to go back. A run counts as new until you open its report: the button and the tool's card show how many. A run in the Tools tab's list opens in Reports.

**Work on this tool** (on a tool's report, and in the Tools tab) opens a new conversation for it: UVMC copies the approved version's code into `tools/<name>` in that conversation's folder (code only, not its data or secrets), and the message box starts with what it is and its latest report. Say what to change and press Run; the agent changes the copy, tests it, and proposes a new version, which you approve as usual. Always a new conversation, so the work doesn't land in one about something else. In any conversation, the agent can also fetch a tool's code itself with `get_tool_code`; if its folder already has `tools/<name>`, the copy goes next to it as `tools/<name>-v<version>`, so nothing of its own is overwritten.

**Notifications:** each tool's page has **Notify me**: *when it asks, fails or reports a problem* (the default), *after every run*, or *never*. A tool asks by putting `"notify": "what happened"` in its report; a problem is a failed run or a report whose status is bad. The notification is the browser's (a pop-up from Windows, like the panel's out-of-memory alert), shown only when you aren't looking at the panel, and only if you allowed notifications for it when the browser asked; clicking it opens the report.

A tool's report is data, not a web page: the tool writes JSON to the file named by `UVMC_REPORT`, built from a fixed set of blocks, and the panel draws them with its own code. The blocks are heading, text (Markdown), stats (big numbers with a trend), progress, table, list, chart (line or bar, with a table view), diff, image (PNG, JPEG, GIF or WebP), link, code, and two for layout: columns (2 or 3, stacked on a phone) and section (can be folded). The format is in [reports.py](../supervisor/uvmc/reports.py). Nothing a tool writes runs in the panel: text is escaped, links are only http(s), and images are copied out after the run, read as the tool's own user (so a link it plants can't make the supervisor read other files), and checked by their first bytes (no SVG, which can hold scripts). A part that's wrong is left out and listed under the card. A tool without a report shows what it printed, as Markdown, or its last lines if it failed. When the agent tests a tool it leaves a sample report in the folder; `propose_tool` checks it and sends any problem back to the agent, and the approval card shows it, so you see what the results will look like before you approve.

For now tools reach sites over HTTP(S) only (no email protocols like IMAP), use the programs already on the machine, and can't be called by the models or your PC's Claude apps. `[made_tools]` in config.toml has the settings.

## Programs that use UVMC

A **program** is your own software that runs on its own (in the VM next to UVMC, on your PC, ...) and uses UVMC's features with a key of its own. It differs from a tool: a tool is code an agent wrote that UVMC runs, contains and holds the secrets of; UVMC doesn't run a program, contain it or see its code, and the program keeps its own secrets. What a program may do is what its key allows. For now that is **sending reports**; waking an agent, asking you questions and offering actions to agents come later (design.md 7s).

**Adding one:** Settings › Programs that use UVMC › **Add a program**: a name (like `training-dispatcher`) and what it is. UVMC shows its key once: copy it into a file only the program can read. **New key** makes another (the old one stops working at once), **Remove** deletes the program, its key and its reports. Per program: what it may do (Send reports) and **Tell me if it's silent for more than N minutes**: a notification (and a red card) when it hasn't called UVMC for that long, and another when it calls again.

**Its reports** appear in the Reports tab next to the tools' (marked "program"), with the same report page, earlier reports (the last 30), "new" counts and Notify choice. The format is the tools' ([reports.py](../supervisor/uvmc/reports.py)), with one difference: a program sends its pictures inside the report (`{"type": "image", "data": "<base64>"}`), since it shares no folder with UVMC.

**The client library:** [client/python/uvmc_client.py](../client/python/uvmc_client.py), one file with nothing but Python's standard library, to copy into a program:

```python
from uvmc_client import Uvmc
uvmc = Uvmc()     # reads UVMC_URL, UVMC_KEY_FILE (or UVMC_KEY, or a systemd credential uvmc-key), UVMC_DATA
uvmc.report(title="Queue", status=("ok", "3 running"),
            blocks=[uvmc.stats({"Running": 3}), uvmc.table(["Job", "State"], rows)],
            notify=None)   # a text asks for a notification
uvmc.heartbeat()   # "still alive", for programs with nothing to report for a while
```

It never makes the program fail because of UVMC: a call that doesn't get through returns `None` (the reason is in `.last_error`), and a report UVMC couldn't take (unreachable, locked, busy) is kept in `UVMC_DATA` and sent with the next call that gets through. Without `UVMC_URL` it still works: running as a UVMC tool (`UVMC_REPORT` set) it writes the tool's report file, and otherwise it writes `UVMC_DATA/reports/latest.json` (and the few before it). So the same program runs connected to UVMC, as a tool, or on its own. `UVMC_URL` is the panel's address (`http://127.0.0.1:8080` from inside the VM).

**The HTTP interface** behind it, for other languages, with `Authorization: Bearer <key>`: `GET /api/programs/self` (its name, permissions, UVMC's version), `POST /api/programs/self/reports` (a report, up to 8 MB with its pictures, at most 30 a minute), `POST /api/programs/self/heartbeat`.

**Walls:** a program's key reaches only `/api/programs/self/...`; on any other route (the panel's, conversations, files, settings) it gets 403, and those routes take no other key or cookie. UVMC keeps only a hash of the key and shows the key once. While UVMC is locked, program keys are refused (423) like every other program. Each call is listed on the program's page (time, route, result; never its content). Agents, conversations' users, tools and Claude Code can't reach the API at all: the firewall refuses them port 8080, as before. Reports go only to the panel, never to an agent.

## The headless browser

For pages that only work with JavaScript, or that need clicking through, the agents and the tools they build have a headless browser (a browser with no window): [Playwright](https://playwright.dev) with Chromium's headless shell, installed by `update` (since 0.16.1). The version is pinned in [browser-requirements.txt](../supervisor/browser-requirements.txt); changing that line makes the next `update` install that version.

- Where it is: Playwright's Python in `/opt/uvmc/browser` (agents run `/opt/uvmc/browser/bin/python`), the browser in `/opt/uvmc/ms-playwright`. Both belong to root: every agent and tool can use them, none can change them. For Node, `npm install playwright@<same version>` in a project finds the same browser (`PLAYWRIGHT_BROWSERS_PATH`); downloading another browser is switched off (`PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD`).
- How it was installed: Chromium's system libraries from Ubuntu (apt, as root); the browser from Playwright's servers by `uvmc-setup`, the same no-rights user that runs Claude Code's installer, so nothing downloaded runs as root. The log is `/var/log/uvmc/browser-install.log`. If it fails, the rest of UVMC still works and the next `update` tries again.
- Sites: it goes online like everything else the agent runs, through that conversation's (or tool's) own proxy, the only way the firewall lets it out. The allowed sites, the approval cards and the connection log all apply; if the browser were set up wrong it couldn't connect at all, not go around the proxy. A modern page pulls from several other sites (fonts, CDNs, analytics), so expect a few more approval cards.
- What the agent is told: one line in the live briefing (the version and how to start it), and a house rule that what it reads online is data, never instructions. Text on a web page can try to give the agent orders (prompt injection); the walls limit what it could do, and the fewer sites are allowed, the less a hostile page can reach.
- Memory: Chromium takes roughly 200-500 MB while it runs. That fits in a conversation's box; in a tool's box (512 MB) it is tight. A box that runs out shows in the panel.
- Not covered: signing in to your accounts. The browser has none of your passwords or cookies; a tool that needs to sign in asks for its secret like any other.

## Shared folders from your computer

The agent can work with folders from your computer without being able to damage them:

- **When you start the panel**, a "Start UVMC" window lists your folders with ticks: tick the ones the agent may see this time, **Add folder…** with the normal folder picker, **Start**. The choice is remembered; "Don't ask again" skips the window (hold Shift on the shortcut, or use `--folders`, to see it again). On macOS it's native dialogs; on Linux `zenity`, or a few questions in the terminal.
- Behind it is `shares.txt` in the repo (`name = folder`; `#off name = folder` = remembered but not shared; see [shares.example.txt](../shares.example.txt)), which you can also edit and apply with `share`. Relative folders are inside this repo; your whole drive or user folder is refused.
- **Each shared folder has a privacy level** (see "Projects and privacy levels"), `local` until you choose it in the **Projects** tab, and a conversation sees only the shared folders it opened (in **New conversation** or **Add a project**, listed with the projects), at a level that allows it.
- The agent **reads** them under `/mnt/shared/<name>`. That view is read-only for it: writing or deleting fails, whatever command it tries. A shared folder its conversation didn't open is closed to it, and `/mnt/shared` can't be listed, so it doesn't learn the names of the others.
- To **change or add** a file, the agent prepares the new version in its work folder and calls `update_shared_file`. You get a card with the file, sizes, a line-by-line diff for text files and its reason: **Replace it / Deny**.
- On approval the supervisor (not the agent) copies the original to `<folder>/_uvmc_backups/<time>/…`, writes the new file, and checks it arrived intact. **Restore original** in the panel undoes it.
- **Deleting** files on your computer isn't offered at all; the agent can only tell you.
- A coordinator can't write to shared folders; workers can, with the same approval.

To try it with sample files instead of a real project: run `make-testdata` (it creates `testdata/` inside this repo: text, JSON, CSV and 1 MB / 60 MB binaries, ignored by git), then `share`.

How it works inside the VM: each folder is mounted at `/srv/uvmc-host/<name>`, which only the supervisor can reach, and given a read-only view at `/srv/uvmc-views/<name>/files`, which `/mnt/shared/<name>` links to (refreshed every 30 seconds). A view is made only while the folder is actually mounted, as one read-only mount kept apart from later mounts; anything else found there (a writable mount on top, several stacked) is taken down and made again on the next refresh. If a mount can't be shown to the agents directly, the view becomes a read-only copy instead. `/srv/uvmc-views/<name>` belongs to the supervisor (mode 700): it lets in, by an ACL entry, only the users of the conversations that opened that share (and their running workers), and the supervisor changes that entry as conversations open and close and as levels change. Before 0.16 every agent could read every shared folder; after `update`, none can until a conversation opens it.

Platform notes: on Windows, Multipass needs folder sharing switched on once (`share` asks for admin for that). On Linux with the snap version of Multipass, folders outside your home folder need `sudo snap connect multipass:removable-media`.

## The agent's walls

The agent is free inside its own space: commands run without asking, it can install Python and npm packages for itself, and it can start servers and scripts. The walls are enforced by Linux, not by guessing what commands do:

| Wall | What it does |
| --- | --- |
| No root | The `agent` user has no sudo, so it can't change the system, the supervisor, the rules or the logs. System packages go through the `install_system_package` tool, which you approve. |
| Firewall | The kernel blocks all outgoing traffic from `agent` except to the supervisor's proxy on 127.0.0.1. No DNS, no other ports, no direct internet. |
| Own network | Every command of a conversation or job runs in its user's own network: nothing in it but its own loopback, with its proxy at the usual address (127.0.0.1 and its port). A server it starts on 127.0.0.1 (or `localhost`) is reachable by that conversation alone, across its commands, the headless browser included; another conversation, a job, the VM's own programs (the panel, a dashboard someone runs in the VM) and DNS are out of reach. Programs that look up a name themselves instead of handing it to the proxy fail; nearly everything (pip, npm, git over https, curl, Python's `requests`, the browser) hands it to the proxy. If its network can't be entered, the command doesn't run. |
| Watched proxy | Allowed sites (`[network]` in config.toml, plus the ones you approve) go through; you're asked about others; every connection is logged with site and bytes. It sees which sites, not what's inside HTTPS. |
| Resource box | Everything the agent runs shares one memory, CPU and process limit (`[limits]`). A runaway program hits the box, not the VM. Each conversation runs in its own smaller box inside it, so Stop ends that conversation's programs and leaves the others running. |
| One user per conversation | Each conversation runs as its own user (agent1, agent2, ...), in its own copy of the work folder (only the projects it works on, at its privacy level), and the firewall lets that user reach only its own proxy. Its files, programs and sites are its own; no agent can open the main work folder or another's copy. |
| Fixed-size home | `/home/agent` is its own disk (6 GB by default), and `/tmp` and `/var/tmp` are size-limited memory disks, so filling a disk can't take the VM down. |
| Undo | A hard-linked copy of the work folder is saved before every task (last 20 kept). |
| Private view | Every command of a conversation, job or tool runs in a view of its own: a `/proc` that shows only its own user's processes (no other conversation's commands or arguments), and its own `/tmp`, `/var/tmp` and `/dev/shm` (whatever path a program writes to, the files are its alone). The supervisor and the system keep the normal view. If the view can't be set up, the command doesn't run. |
| One user per job | A coordinator's jobs run as their own users (agent9, agent10, ...), in their own copies of the coordinator's folder, through their own proxies. A finished job's folder is locked. |
| Activity log | Linux's audit system records every program the agent starts, even inside scripts. |
| Factory reset | The `clean` snapshot on your PC, for anything else. |

**Memory budget.** The VM's memory is shared by Linux and the supervisor (about 600 MB), the agent's box (`[limits] memory`, 1200 MB) and Claude Code's own box when you use a "my plan" model (`[subscription] memory`, 1000 MB; one Claude Code uses about 200-400 MB, and each "my plan" conversation that works runs its own, as does a "my plan" worker, so two or three fit). When a box is full and the system has to stop a process in it, the panel says so: a red banner (and a desktop notification if the panel isn't in front and your browser allows it), and a note in the conversation it happened in, naming the box and the setting to raise. If they don't fit, `update` shrinks the agent's box and says so. On a 2 GB VM the agent gets about 700 MB; 3 GB gives everyone their full share. Claude Code is closed after 15 unused minutes (`idle_minutes`) to free its memory; your next message restarts it with the conversation so far. To give an existing VM more memory: `multipass stop uvmc`, `multipass set local.uvmc.memory=3G`, `multipass start uvmc`, then `update`.

**Name lookups (since 0.25).** An agent's own network has no DNS: the proxy looks up every site's name. Nearly every program hands the name to the proxy (pip, npm, git over https, curl, Python's `requests` and `urllib`, the headless browser with its proxy set), so nothing changes for them. A program that looks up the name itself first, or ignores `HTTPS_PROXY`, now fails with a name-resolution error ("Temporary failure in name resolution", `getaddrinfo` failed, `ENOTFOUND`), even for an allowed site. The agent is told this in its house rules. If it happens, the fix is to give that program the proxy (most have a proxy option); there is deliberately no DNS to turn back on, since lookups were a way out past the proxy.

Not covered by the walls: the agent can still change things in its own home outside the work folder (undo covers only the work folder), and it can use its CPU and memory share fully. Changing `home_size` later doesn't resize an existing home disk. Since 0.15 no agent can open the main work folder or another agent's copy (they belong to the `uvmc-files` group, which only the supervisor is in); a conflict's other versions and the other side's transcript are handed over in `.uvmc-merge/`, and a coordinator can still read its own finished jobs. Since 0.16 a folder shared from your PC is open only to the conversations that opened it. An agent can loosen the permissions of its own copy, which only exposes what it already has; a stronger per-agent sandbox is step 7h.

## Projects and privacy levels

Which model may see which files ([privacy.py](../supervisor/uvmc/privacy.py), step 7k in design.md). Three levels, for models and files alike:

| Level | Models | Meaning |
| --- | --- | --- |
| `local` | the model on your PC | the files never leave your PC |
| `paid` | the Anthropic API, your Claude plan, other paid APIs you trust | trusted by contract: UVMC can't check it, read the provider's terms |
| `open` | free tiers, anything else | it may log or train on what it gets |

- **Models:** each profile has `privacy = "..."` (config.toml or Settings). Without one it counts as open. A model may see files at its level or below.
- **Projects:** the main work folder's top-level folders. Everything at the top level that no project claims is the **General** project. It is `local` until you choose its level (`[privacy] general`), so nothing there reaches a paid or open model without your say.
- **Zones and marks:** a subfolder can have its own level (`Website/drafts` open, `Website/keys` local); a single file can be marked stricter than where it sits ("sensitive" is local), never looser. Levels are kept by the supervisor, so agents can't change them.
- **A conversation** opens one or more projects and has a level: its model's, or lower. Its copy holds only the files of its projects at its level or below; it's told how many are hidden, never which. With no project chosen it gets General if its level allows, else a scratch project of its own (removed with it if left empty). A folder it makes at the top level becomes a new project at its level.
- **Data only flows down through you.** A conversation's "seen" level is the most private file it has had in its copy. Bringing in a change to a less private place than that is **held**: the bring-in card shows the change, and you tick what may go in. Changes where it may not look are **not brought in** at all.
- **Switching model** to a less safe one asks first, naming the projects; if the conversation has seen files the new model may not, it's refused: start a new conversation instead. A worker, or another conversation resolving a conflict, must be allowed everything the conversation has seen.
- Making a project, zone or file less private asks first and names the models that gain access. The API: `GET /api/projects`; `POST /api/projects` (name, level), `/api/projects/claim` (a General folder becomes a project), `/api/projects/level`, `/api/projects/zone`, `/api/projects/mark` (send `"confirm": true` after a 409 with `"confirm"`); `POST /api/conversations` takes `projects`, `level` and `like` (the same projects as another conversation); `POST /api/conversations/project` adds one; `GET /api/conversations/<id>/held?path=` shows a held change.
- **In the panel:** **New conversation** asks for the model, the level (its model's or lower) and the projects (the ones its level can't open are greyed, with the level they need), and can start with the current conversation's whole transcript (only if the new one may see everything that one has seen; the agent finds it in `.uvmc-context/`). The **Projects** tab sets each project's level, makes new projects (at the model's level unless you choose), and lists folders and files with their own level. In **Files**, every entry shows its level, and what the conversation on screen can't see is marked; opening a folder lets you give it its own level or, at the top of General, make it a project; opening a file lets you mark it sensitive or remove the mark, and **Give it to this conversation**: a one-off, the file keeps its level, and the conversation's changes into less private places are then held for your review. The folder bar shows the conversation's projects and level, with **Add a project**. Model profiles in Settings have a privacy field, with UVMC's suggestion.
- **Folders shared from your PC** are projects too, with one level for the whole folder (they stay live, so nothing inside is filtered: share a stricter subfolder on its own). A shared folder is `local` until you set its level in the Projects tab. Opening one counts as having seen its level. When the agent wants to write into a shared folder less private than what its conversation has seen, the card says so.
- **Sites:** a conversation that has seen paid or local files could send them out through a site. It's asked about every site not on your allowed list, each time (the card has no **Always allow**, and says what it has seen); the allowed list still works.
- **Tools the agent builds** get a level when you approve them: the card suggests the most private level its conversation has seen (its code may carry those files), and you can change it there or later in the Tools tab (lowering asks first). Only a conversation at that level or higher may work on its code (**Work on this tool**, `get_tool_code`), and getting the code counts as having seen that level.
- **The PC's Claude apps** count as paid: they read only what a paid model may, a conversation they start is at most paid, and they get no reply from a conversation that has seen local files.

## Private conversations

A conversation that leaves nothing behind in UVMC once you close it ([private.py](../supervisor/uvmc/private.py), step 7n in design.md). Tick **Private** on the **New conversation** card. Three restrictions appear under it, all ticked; untick the ones you don't want (they can't change once it has started, and its tab's **private** badge lists them):

- **Only the model on my PC** (a profile with `{pc}` in its address, level `local`), its workers too, and you can't switch it to another. Its level is `local`, so it can open your local projects. Unticked (or when no model on your PC is set up), any model: its level follows the model as for any conversation, and **that model's provider receives and keeps the whole conversation**; UVMC erases only its own copy.
- **No internet:** its proxy refuses every site, the allowed ones too. Nothing can be looked up, downloaded or installed from online, and the headless browser can't load pages. Unticked: sites not on your allowed list are asked about each time, never **Always allow**.
- **Nothing reaches the main folder:** it can't bring files in (the button is greyed), write into folders shared from your PC, or install a tool it built. When you close it with changed files, the card asks: **Keep its files** (you review and bring them in as usual, then close it again to erase the rest), **Erase everything**, or keep it open. Unticked: it brings files in like any conversation (held for review when they'd go somewhere less private).
- **While it's open** it works like any other: its steps, approvals and the Activity view. Approval cards for a tool it built or a file written on your PC say that this part stays after it's erased. Sites never get **Always allow**.
- **Closing it erases it,** after you confirm (the tab's × asks): its messages and steps (kept in memory under `/run/uvmc/private`, never under Runs), its copy of the work folder, its user's home folder and `/tmp` files, its workers' copies and homes, and its lines in the recent sites and programs. A restart of UVMC (an update, a crash, the VM restarting) erases it too: it never comes back. It is never closed by UVMC to make room for another.
- **Other conversations learn nothing about it:** no transcript (not as a starting transcript, not for a conflict), and files it brought in say "brought in by a private conversation", without its tasks.
- **The audit log** keeps which program it ran, never the arguments, folder or files named (rules added for its user while it runs, `audit.set_quiet`). Setup also leaves the supervisor's own commands out of the audit log, and drops sudo's audit records of them (`USER_CMD`, which hold the whole command line), for every conversation. **The system log** keeps no agent commands or conversation file names, for every conversation: setup tells sudo not to log the few commands that carry them (`systemd-run`, `setpriv`, `env` and the supervisor's file commands such as `install`, `rm`, `rsync`: `/etc/sudoers.d/uvmc-quiet`), and commands start without their text in systemd's log. **The cost, until the supervisor gets its own user (design.md 7o):** the supervisor runs as `ubuntu`, your admin account in the VM, so your own `sudo` use of those few commands isn't logged either, and none of `ubuntu`'s sudo use is in the audit log. Every other sudo use (package installs, editors, `sudo -i`, services) stays in the system log, and refused sudo attempts are always logged.
- **Checked at every start:** the supervisor runs a harmless command with a marker text the way a private conversation's run, then looks for the marker in the system log and the audit log. If it is found, private conversations are off and the card says why (usually: run `update`). If closing one couldn't remove something, the panel says what and where, and UVMC tries again at its next start.
- **The model's server on your PC** logs only warnings and errors (never what's written), with its `/slots` view off. `local-model -Logs on` (Unix: `--logs on`) turns its normal log back on for testing (requests and timings, still not the text); `-Logs off` again. A llama.cpp too old for these options won't start: run `local-model -Update`.
- **What stays anyway:** files you bring into the main folder (and the main folder's undo points, the last 20), tools you approve, files it writes into a folder shared from your PC once you allow it, and deleted data on the disk until it is overwritten (UVMC deletes files; it doesn't shred them). Files it brings into a less private place than local are held for your review, as for any conversation that has seen local files.
- The API: `POST /api/conversations` with `"private": true` and optionally `"private_options": {"offline": false, "pc_only": false, "sealed": false}` (each true unless given false); `POST /api/conversations/close` answers 409 with `"confirm"` until you send `"erase": true`, and with `"unsaved"` and `"sealed"` when it keeps changed files to itself (send `"discard": true, "erase": true` to erase everything, or `POST /api/conversations/keep` to keep them and bring them in). A private conversation takes tasks from the panel only: not from `/api/run`, never from the Claude apps.

## The lock

The **Lock** button in the panel's header closes the VM to every program on your PC and keeps every conversation on the model on your PC ([lock.py](../supervisor/uvmc/lock.py), step 7q in design.md). Use it with private conversations when nothing should get in or out.

- **No PIN: it keeps programs out, not you.** Locking gives the panel a new key (token) and hands it only to the browser that clicked Lock, in its cookie. Every key a program on the PC read before (the Claude apps, scripts, anything that copied it) stops working at once, for the whole panel API, and open event streams end. So while locked only that panel can do anything, unlocking included; the new key is written to `/etc/uvmc/panel_token`, which the PC can't read until SSH is open again. After unlocking (or a restart) the PC's programs read the new key the way they always do; the Claude apps' relays look it up again by themselves.
- **While locked, no program on your PC gets in:** Multipass (`exec`, `shell`, `transfer`, new `mount`s), `update` (it says the VM is locked), the PC's other scripts, the Claude apps' UVMC tools, any AI agent, and you in a terminal. Open sessions from the PC are ended. Shared folders already mounted stay. How: everything from the PC comes in by SSH as `ubuntu` with Multipass's key, and SSH reads that key from a file in memory (`/run/uvmc-ssh/ubuntu.keys`) that the lock empties (`uvmc-lock`); root can't log in by SSH at all.
- **While locked, only the model on your PC works,** for every conversation, normal or private, and their workers: conversations on another model show "waits for unlock" and can't start a task; a task running on one is stopped when you lock; Claude Code's idle processes are closed. The panel and the model on your PC keep working (the VM calls the PC, not the other way). The agents' internet isn't changed by the lock: each conversation keeps its own rules.
- **Restarting the VM always unlocks it** (`multipass restart uvmc`): your way back if the panel can't be reached. Any program on the PC can restart the VM, so the lock isn't proof against that: a restart erases private conversations, normal ones come back readable. The panel tells you when a restart released the lock.
- **What it isn't:** a wall against what is already inside the VM as `ubuntu` or root (until step 7o), or against an administrator on the PC, who can read the VM's disk and memory. Nor against a program on the PC that reads your browser's saved cookies: it gets the new key with them, and could unlock. (A program running as you can usually also log your keystrokes, so a PIN wouldn't stop it either.)
- The API: `GET /api/lock`; `POST /api/lock` with `{"on": true|false}`. Locking answers with the new key in a `Set-Cookie`; 409 if the VM couldn't be locked or unlocked, 500 if the key couldn't be changed (then it doesn't lock).

## Keeping UVMC up to date

UVMC is built from parts made by others. None of them updates by itself into something untested: each version is pinned, and you choose when to move. **Settings > Updates** in the panel shows what is behind (since 0.16.2).

### What the panel checks

Once a day, and when you press **Check now**, the supervisor compares what is installed with the newest release of each part ([updates.py](../supervisor/uvmc/updates.py)). It only reads version numbers from pypi.org, registry.npmjs.org, api.github.com and changelogs.ubuntu.com, as the supervisor's own user, never an agent's; it installs nothing. A part with a newer version gets an amber dot on the Settings tab and, if your browser allows the panel's notifications, one notice per new version (not every day). `[updates] check = false` in config.toml stops the daily check; **Check now** still works.

### How to update each part

| Part | Where its version is set | How to update | Test afterwards |
|---|---|---|---|
| The supervisor's Python packages (anthropic, fastapi, uvicorn, httpx) | `supervisor/requirements.txt`, exact pins (`name==version`) | Raise the pin, run the tests (below), run `update` | The tests; a task with each kind of model you use (API, plan, PC) |
| Playwright and Chromium (the headless browser) | `supervisor/browser-requirements.txt` | Raise the pin, run `update`: it installs that Playwright and the Chromium that goes with it (`/var/log/uvmc/browser-install.log`) | Ask the agent to open a page with JavaScript; the panel's briefing shows the new version |
| Claude Code (the "my plan" models) | Not pinned: always the newest | Settings > Your Claude plan > **Update Claude Code**, or `update-claude` on your PC | A short task with a "my plan" model |
| Ubuntu's packages in the VM | Ubuntu | Security updates install by themselves every day. The rest: `multipass exec uvmc -- sudo apt-get upgrade -y` in a terminal on your PC. When the panel says **restart needed**: `stop`, then open the panel again | Open the panel; run a task |
| The Ubuntu release (24.04 now) | `install.ps1` / `install.sh` (`multipass launch 24.04`) | Not yet: moving to 26.04 is step 7f in design.md, tested on a copy first. 24.04 gets security updates until 2029 | |
| Multipass (on your PC) | Pinned in `windows/multipass.ps1` and `check-download.cmd` (1.17.0) | When UVMC moves to a new version: run `install.cmd` again (it upgrades in place and keeps the machine), then `update`. `update` says when your PC's version isn't the pinned one | `update`, the panel, a share, `shell`, the CPU of an exec without a console, piped input (design.md 7e) |
| llama.cpp (a model on your PC) | Not pinned: the newest release | `local-model -Update` (Mac/Linux: `--update`) | A short task with the PC's model |

### The routine

1. Look at **Settings > Updates** (or press **Check now**).
2. For a pinned part, change its line in the repo, e.g. `anthropic==2.6.0` to `anthropic==2.7.0` in `supervisor/requirements.txt`. One part at a time, so a problem has one cause.
3. Run the tests in the VM (see CLAUDE.md, "Tests"; they run offline).
4. Run `windows\update.cmd` (`unix/update.sh`). It installs exactly the pinned versions.
5. Check the panel shows what you expect (Settings > Updates, the version at the bottom of Settings), and try the test from the table.
6. Commit the changed pin, so every machine built from the repo gets the same version.

If something breaks, put the old pin back and run `update` again.

### What the VM can't see

Multipass and llama.cpp run on your PC, not in the VM. `update` tells the VM which Multipass your PC has, so the panel shows the version from your last update, not a live one. With the UVMC helper running, the panel shows the llama.cpp version on the PC and gets the newest with a click, but doesn't yet say when a newer one is out. Checking both directly and telling you even with the panel closed is for the helper later (design.md, 7m).

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

### Time limits on the PC's commands to the VM

**What this fixes:** with Multipass on Windows (1.16, and still in 1.17.0), a `multipass exec` keeps one CPU core at 100% for as long as it runs whenever its input isn't a console window, as with the Claude apps' relay and the extension, which run hidden. Also, an exec ends only when everything it started in the VM has let go of its output. So a command that never answered left a `multipass.exe` on the PC spinning a core until someone ended it in Task Manager (found 2026-11-09: clients that had used hours of CPU), and left the Claude app waiting forever for an answer.

**What happens now:** the quick commands the scripts send get `timeout` inside the VM, which stops the command and everything it started, so the exec ends. Then the script says so, in its window or as the error the Claude app gets:

| Where | Command | Limit |
|---|---|---|
| relay (`uvmc mcp`, `uvmc`), extension | address and token lookup | 20 s in the VM; the client is stopped on the PC after 30 s if Multipass itself doesn't answer |
| panel | address and token | 20 s |
| local-model | the VM's route, storing the key | 20 s |
| install | checking setup finished, switching on the Claude apps | 30 s |
| diagnose | each command | 60 s |
| save-logs | packing the run logs | 300 s |

A lookup normally takes about a second, so a limit only fires when the machine or Multipass is stuck, and then it would never have answered. The relay tries a second time before giving up, so a Claude app waits about 40 s at most and then gets a message such as "Could not reach the UVMC machine: The UVMC machine did not answer within 20 seconds (hostname -I); the command was stopped." Nothing is left running on the PC or in the VM.

Left without a limit on purpose: `shell` and `sign-in-claude` (interactive), `update` (`setup.sh`), `update-claude`, install's wait for setup to finish, and `share` (copying a big shared folder can take long). These are long by nature, always run in a window, and stopping one halfway would do more harm than waiting. Uninstall's saving of the work folder is never cut short either.

**The log:** each timeout adds a line to `%LOCALAPPDATA%\UVMC\timeouts.log` (Windows) or `~/.config/uvmc/timeouts.log` (Mac/Linux): when, what ran it, the command, and which limit fired. "stopped in the VM" means the VM was too slow; "no answer from Multipass" means the client didn't return at all. The newest 500 lines are kept, and `diagnose` includes the last 20. The helpers are [windows/timeouts.ps1](../windows/timeouts.ps1) and [unix/timeouts.sh](../unix/timeouts.sh).

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
| [supervisor/uvmc/browser.py](../supervisor/uvmc/browser.py) | The headless browser: where agents and tools find it, and its line in the briefing |
| [supervisor/uvmc/updates.py](../supervisor/uvmc/updates.py) | Settings > Updates: which parts have a newer version out (it never installs anything) |
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
| [supervisor/uvmc/reports.py](../supervisor/uvmc/reports.py) | The report format tools write (the blocks), checked before the panel draws it |
| [supervisor/uvmc/programs.py](../supervisor/uvmc/programs.py) | Programs that use UVMC: their keys, their reports and the silent check |
| [supervisor/uvmc/shares.py](../supervisor/uvmc/shares.py) | Shared folders: approved writes, backups, restore |
| [supervisor/uvmc/settings.py](../supervisor/uvmc/settings.py) | Panel settings: model profiles and API keys, on top of config.toml |
| [supervisor/setup.sh](../supervisor/setup.sh) | Installs all of this inside the VM; `update` runs it again |
| [supervisor/uvmc/rules.py](../supervisor/uvmc/rules.py) | Decides allow / ask / deny per call |
| [supervisor/tests/](../supervisor/tests/) | Offline tests with a fake Claude |

- In the VM it runs as the `uvmc` systemd service (`uvmc serve`), with the panel on port 8080.
- From a terminal or a script, use `uvmc ask "..."` on your PC ([Using UVMC from a terminal or scripts](#using-uvmc-from-a-terminal-or-scripts)): it talks to this service, so its tasks show up in the panel too. (The old `supervisor` scripts, which started a second supervisor inside the VM, were removed in 0.12.9.)
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
- `uvmc serve` is the supervisor; the `uvmc` systemd service runs it (as `ubuntu`; it refuses to run as `agent`). Plain `uvmc` is an old terminal mode that starts a second supervisor next to the service; nothing uses it, so use the panel or `uvmc ask` on your PC instead.
- `uvmc-agent shell` opens a shell as the agent (what `shell` on your PC does).
- `uvmc-setkey [name]` replaces a stored API key (reads it from stdin; run with sudo).
- `uvmc-install-claude` installs or updates Claude Code (run with sudo; `update-claude` on your PC does it).
- `/opt/uvmc/browser` and `/opt/uvmc/ms-playwright` are the headless browser (Playwright and Chromium), installed by `update`.
- `uvmc-claude-signin` signs Claude Code in with your Claude plan (run with sudo; `sign-in-claude` on your PC does it).
