# Notes for Claude

UVMC: an Ubuntu VM (Multipass) where an AI agent works only through our supervisor, with a web panel. Read [README.md](README.md), then [docs/guide.md](docs/guide.md) (how everything works) and the "Planned work" section of [docs/design.md](docs/design.md) (what's done, what's next, and why).

## Working here

- **No access outside this project folder (`D:\code\UVMC`) and the folders the user has allowed, unless the user approves it first.** This means reading as well as changing: other files and folders on the PC, app settings, the registry, event logs, services, other processes. It applies to every session and every agent or subagent. Say exactly what you want to look at or do and why, then wait for a clear yes; a yes covers only that one access. Never change anything on the user's system unless they ask for that specific change, and describing a problem is not asking. The `uvmc` VM counts as part of the project.
- Before building on a library or tool (Multipass, llama.cpp, Python packages), check its latest release and bring it up to date first, or say why not.
- Test each step before going on, and explain what you're about to do before building something big. Ask when a request is unclear instead of guessing.
- Try things with test data (`make-testdata` makes `testdata/`), never with the user's real projects.
- The user handles git. Commits use the address 147395095+strat0sf3r@users.noreply.github.com, never a personal email.
- After changing `supervisor/`, the user runs `windows\update.cmd` to push it into the VM. Bump `supervisor/uvmc/__init__.py` so the panel shows which version is running.
- Running commands in the VM from here: with Multipass 1.16 on Windows, `multipass exec` keeps one CPU core at 100% the whole time it runs unless its input is a real console window, and Claude Code's shells never give it one. If anything in the VM keeps the command's output open (a `&` job, a server, a test that leaves a process behind), the exec never returns. On 2026-11-09, clients left this way had used hours of CPU. So keep every exec short. Start anything long (the test suite, a preview server) fully detached, then read its log with short execs: `multipass exec uvmc -- bash -c "cd /tmp/x && setsid nohup CMD > run.log 2>&1 < /dev/null &"`. Never end a command with a bare `&` job that still writes to the exec's output. If the PC gets slow, look for leftover `multipass.exe` processes (not `multipassd`) and stop them.

## Tests

```
cd supervisor
UVMC_NO_SUDO=1 python3 -m pytest -q tests      # or: python3 -m unittest discover tests
```

They run offline with a stand-in model (`FakeClient`, or `Router` in `test_conversations.py`, which answers by the conversation's first task). Also check the panel's script (`node --check` on the `<script>` part of `static/index.html`), `bash -n supervisor/setup.sh`, and `node --check extension/server/index.js`.

To look at the panel without a VM, run it the way the tests do: a `Controller` with a stand-in model, `create_app(ctl, "token")` under uvicorn, then open `/?token=token` in a browser. The README screenshots (`docs/screenshots/`) were made this way, with a fake API key in `ANTHROPIC_API_KEY`.

## Where things are

- `supervisor/uvmc/service.py`: the controller (conversations, approvals, bring-in, resolutions) and every HTTP route.
- `supervisor/uvmc/workcopies.py`: each conversation's copy, the three-way bring-in, resolutions waiting for review, files handed to another conversation.
- `supervisor/setup.sh`: everything installed inside the VM (users, firewall, slices, services). `update` runs it again.
- `extension/`: the Claude Desktop extension; rebuild `uvmc.mcpb` with `npx @anthropic-ai/mcpb pack` after changing it.
- `windows/`, `unix/`: the scripts on the PC. A quick command a script sends to the VM gets `timeout` inside it and is checked with `windows/timeouts.ps1` / `unix/timeouts.sh`, which log timeouts on the PC (guide: "Time limits on the PC's commands to the VM"). Hidden runs (the relay, the extension) also get a limit on the PC (`Invoke-VmQuick`).

## State (0.14.0, 2026-11-09)

Done: steps 1-5, 7 (tools the agent builds), 7b (several conversations), 7c (each conversation isolated, bring-in, agent-resolved conflicts), a model on the PC's graphics card (`local-model`, llama.cpp, not Ollama: the user doesn't want Ollama's subscription side). Step 6 (Codex on a ChatGPT plan) is parked: no plan to test with.

Not yet confirmed on the VM: the proposal card and transcripts (0.12.2-0.12.3), the handed-file fix and folded UVMC tasks (0.12.4), the model on the PC end to end and `httpx` now in requirements.txt (0.12.5; anthropic 2.x stopped bringing it, which broke every OpenAI-compatible model). `unix/local-model.sh` is untested (no Mac or Linux PC here). 0.12.6: piped input into `multipass exec` hangs with Multipass 1.16 on Windows, so `set-key` was removed (keys go in the panel), the installers no longer ask for a key, and `local-model` sends its key as a file. 0.12.10: "my plan" models can be workers (own bridge key per conversation's worker), Claude Code's box raised to 1000M, and a full memory box (oom_kill in memory.events) is told in the panel; not yet tried on the VM. 0.13.0: tools' reports (`reports.py`, a fixed set of blocks the panel draws) in a Reports tab beside the conversations (cards, then the full report), with per-tool browser notifications; "Work on this tool" opens a new conversation with the tool's code (`get_tool_code` for agents); not yet tried on the VM. 0.14.0: several workers at once (step 7g), each job as its own user (agent9.. after the conversations' users; `update` makes them) in its own copy of the coordinator's folder, brought in by the coordinator (`bring_in_worker`, `discard_worker`); tests pass in the VM (`/tmp`, `UVMC_NO_SUDO`), not yet tried for real on it, nor with Claude Code as the coordinator.

Next: step 7e, Multipass 1.17 (UVMC is pinned to 1.16.4; the upgrade and what to test are in design.md), then 7f, Ubuntu 26.04 in the VM. Then step 8, a proper installer (`UVMC-Setup.exe` with Inno Setup, Multipass bundled). Needs the user's decision on code signing first.

Later, when asked: 7h, a stronger sandbox for every agent (each sees only its own folder); agents talking to each other through the supervisor; tools reaching email protocols (IMAP/SMTP); approved tools offered to the models and the PC's Claude apps; "propose again with another model" on a resolution.
