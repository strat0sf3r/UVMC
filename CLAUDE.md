# Notes for Claude

UVMC: an Ubuntu VM (Multipass) where an AI agent works only through our supervisor, with a web panel. Read [README.md](README.md), then [docs/guide.md](docs/guide.md) (how everything works) and the "Planned work" section of [docs/design.md](docs/design.md) (what's done, what's next, and why).

## Working here

- Test each step before going on, and explain what you're about to do before building something big. Ask when a request is unclear instead of guessing.
- Try things with test data (`make-testdata` makes `testdata/`), never with the user's real projects.
- The user handles git. Commits use the address 147395095+strat0sf3r@users.noreply.github.com, never a personal email.
- After changing `supervisor/`, the user runs `windows\update.cmd` to push it into the VM. Bump `supervisor/uvmc/__init__.py` so the panel shows which version is running.

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
- `windows/`, `unix/`: the scripts on the PC.

## State (0.12.5, 2026-11-08)

Done: steps 1-5, 7 (tools the agent builds), 7b (several conversations), 7c (each conversation isolated, bring-in, agent-resolved conflicts), a model on the PC's graphics card (`local-model`, llama.cpp, not Ollama: the user doesn't want Ollama's subscription side). Step 6 (Codex on a ChatGPT plan) is parked: no plan to test with.

Not yet confirmed on the VM: the proposal card and transcripts (0.12.2-0.12.3), the handed-file fix and folded UVMC tasks (0.12.4), the model on the PC end to end and `httpx` now in requirements.txt (0.12.5; anthropic 2.x stopped bringing it, which broke every OpenAI-compatible model). `unix/local-model.sh` is untested (no Mac or Linux PC here).

Next: step 8, a proper installer (`UVMC-Setup.exe` with Inno Setup, Multipass bundled). Needs the user's decision on code signing first.

Later, when asked: agents talking to each other through the supervisor; tools reaching email protocols (IMAP/SMTP); approved tools offered to the models and the PC's Claude apps; "propose again with another model" on a resolution.
