"""Compacting a conversation, for models with a small context (the model on the PC).

You start it in the panel; it never runs by itself. Two ways:

    trim       long tool outputs and file contents the agent wrote, except the latest few, are
               replaced by a short note. No model call.
    summary    the same model writes a summary of the history, which replaces it. Run in pieces
               when the history doesn't fit the model's context; optionally checked once more.

Nothing is lost: every entry of the history is an event in the conversation's log, with a
number (seq). Whatever is shortened says its number, the summary cites numbers, and the agent
gets the recall tool to read an entry in full or search the log. The panel keeps showing the
whole conversation; only what goes to the model gets shorter.

PRIVATE CONVERSATIONS: the summary goes into the conversation's own history and log only, which
are erased with it (tests/test_private.py::test_nothing_left compacts one).
"""

from __future__ import annotations

import json

KEEP_OUTPUTS = 3          # the latest tool outputs stay whole
TRIM_OVER = 1200          # characters; shorter outputs stay as they are
HEAD = 300                # characters of a trimmed output kept, so the agent remembers what it was
CHARS_PER_TOKEN = 3.5     # for estimates; llama.cpp counts exactly on the next request
ENTRY_CHARS = 1500        # a tool output in the text the summary is made from
SUMMARY_TOKENS = 3000     # the summary's length limit
RECALL_CHARS = 6000       # one entry per recall call, continued with offset
SUMMARY_TAG = "Summary of this conversation so far"

RECALL_SPEC = {
    "name": "recall",
    "description": ("Reads entries of this conversation that were shortened or summarized to save room. "
                    "Every entry has a number (#12). Give id to read one in full (offset continues a long one), "
                    "or search to find entries containing a word or phrase."),
    "input_schema": {"type": "object", "properties": {
        "id": {"type": "integer", "description": "An entry's number, e.g. 12 for #12."},
        "search": {"type": "string", "description": "A word or phrase to look for."},
        "offset": {"type": "integer", "description": "For a long entry: the character to start from."}}},
}

SYSTEM = """You compact a conversation between a user and an AI agent that works in a Linux machine, so the agent can go on with less context. The agent will read your summary instead of the history.

Write down: what the user wants and asked for, what was done and decided, files created or changed (with paths), commands that matter and what they gave, problems and how they were solved, and what is still open or unfinished. Keep exact names, paths, numbers and error messages. Leave out what no longer matters.

Every entry has a number like #12. After each fact, give the numbers it comes from, e.g. (#12, #15), so the agent can read them again. End with a section "Left out:" listing, with their numbers, details you left out that might still be needed.

Write plain text. No introduction, no closing remarks."""


def estimate(session) -> int:
    """Tokens the conversation would take now, roughly: history, instructions and tool definitions."""
    chars = len(session.system or session.cfg.house_rules or "")
    chars += len(json.dumps(session.extra_tools, default=str)) + 600 * len(session.tool_names or [])
    for m in session.messages:
        chars += len(m.get("text") or "")
        for c in m.get("tool_calls") or []:
            chars += len(json.dumps(c.input, default=str)) + 40
        for r in m.get("results") or []:
            chars += len(r.get("content") or "") + 20
    return int(chars / CHARS_PER_TOKEN)


def _note(n: int, seq, what: str) -> str:
    where = f" recall(id={seq}) shows them" if seq else ""
    return f"[{n:,} characters of {what} removed to save room;{where}]"


def trim(session, keep: int = KEEP_OUTPUTS, over: int = TRIM_OVER) -> dict:
    """Shortens long tool outputs and long tool inputs (a file the agent wrote), except the latest
    `keep` outputs and the calls that made them. Returns how many were shortened and the characters saved."""
    results = [(m, r) for m in session.messages if m["role"] == "tool_results" for r in m["results"]]
    recent = {id(r) for _, r in results[-keep:]} if keep else set()
    recent_calls = {r["id"] for _, r in results[-keep:]} if keep else set()
    count = saved = 0
    for m, r in results:
        text = r.get("content") or ""
        if id(r) in recent or r.get("trimmed") or len(text) <= over:
            continue
        head = text[:HEAD].rstrip()
        r["content"] = f"{head}\n...\n{_note(len(text) - len(head), r.get('seq'), 'this output')}"
        r["trimmed"] = True
        count += 1
        saved += len(text) - len(r["content"])
    for m in session.messages:
        if m["role"] != "assistant":
            continue
        changed = False
        for c in m.get("tool_calls") or []:
            if c.id in recent_calls or not isinstance(c.input, dict):
                continue
            for k, v in list(c.input.items()):
                if isinstance(v, str) and len(v) > over:
                    c.input = {**c.input, k: _note(len(v), m.get("seq"), f"'{k}'")}
                    count += 1
                    saved += len(v)
                    changed = True
        if changed:
            m["native"] = {}   # the provider's own copy still holds the long input: use the generic form
    if count:
        session.compacted = True
        session.context_tokens, session.context_estimated = estimate(session), True
    return {"shortened": count, "chars": saved}


# --- summary ---------------------------------------------------------------------------------

def entries(messages: list[dict]) -> list[str]:
    """The history as numbered text entries, tool outputs cut to ENTRY_CHARS (recall has them whole)."""
    names, out = {}, []
    for m in messages:
        ref = f"#{m['seq']}" if m.get("seq") else "#-"
        if m["role"] == "user":
            who = "Earlier summary" if m.get("summary") else "User"
            out.append(f"{ref} {who}: {m.get('text') or ''}")
        elif m["role"] == "assistant":
            parts = [m.get("text") or ""]
            for c in m.get("tool_calls") or []:
                names[c.id] = c.name
                args = json.dumps(c.input, ensure_ascii=False, default=str)
                parts.append(f"[calls {c.name} {args[:400]}{'...' if len(args) > 400 else ''}]")
            out.append(f"{ref} Agent: " + "\n".join(p for p in parts if p))
        elif m["role"] == "tool_results":
            for r in m["results"]:
                text = r.get("content") or ""
                cut = text[:ENTRY_CHARS] + (f"\n... ({len(text) - ENTRY_CHARS:,} more characters)" if len(text) > ENTRY_CHARS else "")
                rref = f"#{r['seq']}" if r.get("seq") else ref
                out.append(f"{rref} Result of {names.get(r['id'], 'a tool')}{' (error)' if r.get('is_error') else ''}: {cut}")
    return out


def chunks(items: list[str], budget: int) -> list[str]:
    """Entries joined into pieces of at most `budget` characters (an entry longer than that is cut)."""
    out, cur = [], ""
    for e in items:
        e = e if len(e) <= budget else e[:budget - 20] + "\n... (cut)"
        if cur and len(cur) + len(e) + 2 > budget:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n\n{e}" if cur else e
    return out + ([cur] if cur else [])


def budget_for(context: int) -> int:
    """Characters of history per request, for a model holding `context` tokens (0: unknown)."""
    if context <= 0:
        return 24_000   # fits a 16k context
    room = context - SUMMARY_TOKENS - 2_000 - 1_500   # the summary, the summary so far, instructions
    return max(4_000, int(room * CHARS_PER_TOKEN * 0.85))


def summarize(session, context: int = 0, check: bool = False) -> str:
    """The model's summary of the history, made in pieces if needed. Raises KeyboardInterrupt if stopped."""
    provider = session.provider
    pieces = chunks(entries(session.messages), budget_for(context))
    if not pieces:
        raise ValueError("There is nothing to summarize.")
    summary = ""
    for i, piece in enumerate(pieces):
        if session.cancel.is_set():
            raise KeyboardInterrupt
        prompt = (f"The summary of the earlier entries:\n\n{summary}\n\nThe next entries:\n\n" if summary
                  else "The entries:\n\n") + piece + (
            "\n\nWrite the summary of everything so far." if i == len(pieces) - 1 or summary
            else "\n\nWrite the summary of these entries (more follow).")
        summary = _ask(provider, prompt)
    if check and not session.cancel.is_set():
        cited = set()
        for word in summary.replace(",", " ").replace("(", " ").replace(")", " ").split():
            if word.startswith("#") and word[1:].isdigit():
                cited.add(word)
        left = [e.split("\n", 1)[0][:160] for e in entries(session.messages)
                if e.split(" ", 1)[0] not in cited and not e.startswith("#-")]
        if left:
            prompt = (f"A summary of a conversation:\n\n{summary}\n\nThese entries are not cited in it (first line of each):\n\n"
                      + "\n".join(left)[:budget_for(context)]
                      + "\n\nIf any of them holds something the agent still needs, add it with its number. "
                        "Write the whole summary again, completed.")
            summary = _ask(provider, prompt)
    return summary


def _ask(provider, prompt: str) -> str:
    reply = provider.complete(SYSTEM, [{"role": "user", "text": prompt}], [], SUMMARY_TOKENS)
    text = (reply.text or "").strip()
    if not text:
        raise ValueError("The model wrote an empty summary.")
    return text


def apply_summary(session, summary: str, seq: int | None = None) -> None:
    """The summary replaces the history; your last task and the agent's last answer stay word for word."""
    last_task = next((m for m in reversed(session.messages) if m["role"] == "user" and not m.get("summary")), None)
    last_text = session.last_text or next(
        (m.get("text") for m in reversed(session.messages) if m["role"] == "assistant" and m.get("text")), "")
    msgs = [{"role": "user", "summary": True, "seq": seq,
             "text": f"{SUMMARY_TAG} (it replaces the earlier history to save room; #numbers are entries "
                     f"you can read in full with the recall tool):\n\n{summary}"},
            {"role": "assistant", "text": "Understood. I'll go on from this summary, and use recall when I need an entry in full.",
             "tool_calls": [], "native": {}}]
    if last_task is not None:
        msgs.append({"role": "user", "text": last_task["text"], "seq": last_task.get("seq")})
        msgs.append({"role": "assistant", "text": last_text or "(no reply)", "tool_calls": [], "native": {}})
    session.messages[:] = msgs
    session.compacted = True
    session.context_tokens, session.context_estimated = estimate(session), True


# --- recall ----------------------------------------------------------------------------------

def _entry_text(e: dict) -> str | None:
    kind = e.get("kind")
    if kind == "task":
        return f"User: {e.get('text', '')}"
    if kind == "reply":
        parts = []
        for b in e.get("content") or []:
            if b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif b.get("type") == "tool_use":
                parts.append(f"[calls {b.get('name')} {json.dumps(b.get('input'), ensure_ascii=False, default=str)}]")
        return "Agent: " + "\n".join(parts)
    if kind == "result":
        return f"Result of {e.get('tool', 'a tool')}{' (error)' if e.get('is_error') else ''}: {e.get('text', '')}"
    return None


def recall(log, args: dict) -> tuple[str, bool]:
    """The recall tool: one entry of this conversation's log, or a search through it.
    Only this conversation's own entries (a worker's inner steps are left out)."""
    found = {}
    for e in log.read():
        if e.get("parent"):
            continue
        text = _entry_text(e)
        if text is not None and isinstance(e.get("seq"), int):
            found[e["seq"]] = text
    if args.get("id") is not None:
        try:
            seq = int(args["id"])
            offset = max(0, int(args.get("offset") or 0))
        except (TypeError, ValueError):
            return "id and offset must be numbers.", True
        if seq not in found:
            return f"There is no entry #{seq} (entries are your tasks, replies and tool results).", True
        text = found[seq]
        part = text[offset:offset + RECALL_CHARS]
        more = len(text) - offset - len(part)
        tail = f"\n\n({more:,} more characters: recall(id={seq}, offset={offset + len(part)}))" if more > 0 else ""
        return f"#{seq} {part}{tail}", False
    query = str(args.get("search") or "").strip().lower()
    if not query:
        return "Give id (an entry's number) or search (a word or phrase).", True
    hits = []
    for seq, text in found.items():
        at = text.lower().find(query)
        if at >= 0:
            start = max(0, at - 100)
            snippet = text[start:at + len(query) + 150].replace("\n", " ")
            hits.append(f"#{seq} {'...' if start else ''}{snippet}...")
    if not hits:
        return f"No entry contains '{args.get('search')}'.", False
    shown = hits[-10:]
    return (f"{len(hits)} entries contain it" + (", the latest 10:" if len(hits) > 10 else ":") + "\n"
            + "\n".join(shown) + "\n\nrecall(id=N) shows one in full."), False
