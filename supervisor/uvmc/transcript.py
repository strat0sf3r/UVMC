"""A conversation's log as a readable transcript, for another agent to read.

Used when an agent resolves a conflict with another conversation's work: it reads what that
conversation was asked, what it answered and the steps it took, instead of having to ask it.
Made from the log UVMC keeps for every conversation (the same one the Runs tab replays).
"""

from __future__ import annotations

import time

LIMIT = 200_000


def _when(t) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(t)))
    except (TypeError, ValueError):
        return ""


def _short(text: str, lines: int) -> str:
    rows = str(text or "").splitlines()
    if len(rows) <= lines:
        return "\n".join(rows)
    return "\n".join(rows[:lines]) + f"\n[... {len(rows) - lines} more lines]"


def _args(name: str, args: dict) -> str:
    if not isinstance(args, dict):
        return ""
    if name == "run_command":
        return str(args.get("command", ""))
    if name == "write_file":
        return f"{args.get('path')} ({len(str(args.get('content', '')))} characters)"
    if name == "delegate":
        return f"to {args.get('model', 'the worker')}: {args.get('instructions', '')}"
    return ", ".join(f"{k}={v}" for k, v in args.items() if k != "content")[:400]


def render(records: list[dict], title: str, result_lines: int = 20, reply_chars: int | None = None) -> str:
    out = [f'# Conversation "{title or "untitled"}"', "",
           "Written by UVMC from this conversation's log, for an agent resolving a conflict with its work. "
           "It may be long: search it and read the parts you need.", ""]
    tools: dict[str, str] = {}
    for r in records:
        kind, ind = r.get("kind"), "    " if r.get("parent") else ""
        if kind == "task":
            who = "UVMC, for the user" if r.get("by") == "uvmc" else ("The coordinator, to its worker" if ind else "The user")
            out += [f"{ind}## {who} ({_when(r.get('t'))})", "", ind + str(r.get("text", "")).replace("\n", "\n" + ind), ""]
        elif kind == "reply":
            for b in r.get("content") or []:
                if b.get("type") == "text" and str(b.get("text", "")).strip():
                    text = str(b["text"]).strip()
                    if reply_chars and len(text) > reply_chars:
                        text = text[:reply_chars] + " [...]"
                    out += [f"{ind}**Agent:** " + text.replace("\n", "\n" + ind), ""]
                elif b.get("type") == "tool_use":
                    tools[b.get("id", "")] = b.get("name", "")
                    out.append(f"{ind}> {b.get('name')}: {_args(b.get('name', ''), b.get('input') or {})}")
        elif kind == "result":
            status = "error" if r.get("is_error") else "ok"
            body = _short(r.get("text", ""), result_lines) if result_lines else ""
            out.append(f"{ind}  result ({status})" + (":\n" + "\n".join(ind + "    " + x for x in body.splitlines()) if body else ""))
            out.append("")
        elif kind == "approval_done":
            out += [f"{ind}- The user {'allowed' if r.get('allow') else 'refused'} it ({r.get('type')}).", ""]
        elif kind == "sync":
            brought = (r.get("brought_in") or []) + (r.get("merged") or [])
            if brought:
                out += [f"- Brought into the main folder: {', '.join(brought)}.", ""]
        elif kind == "resolved":
            if r.get("brought_in"):
                out += [f"- After resolving a conflict, brought into the main folder: {', '.join(r['brought_in'])}.", ""]
        elif kind == "error":
            out += [f"{ind}- Error: {r.get('text')}", ""]
        elif kind == "end" and not ind:
            out += [f"(task ended: {r.get('reason')})", ""]
    return "\n".join(out)


def transcript(records: list[dict], title: str, limit: int = LIMIT) -> str:
    """The transcript, shortened step by step if it's too long: results first, then the agent's
    answers, then the oldest steps (the user's tasks always stay)."""
    for result_lines, reply_chars in ((20, None), (5, None), (0, 2000), (0, 400)):
        text = render(records, title, result_lines, reply_chars)
        if len(text) <= limit:
            return text
    # Still too long: every task, and as much of the end as fits.
    tasks = [r for r in records if r.get("kind") == "task" and not r.get("parent")]
    head = render(tasks, title, 0, 400)
    tail = render(records, title, 0, 400)
    room = max(0, limit - len(head) - 200)
    return head + "\n\n[... the older steps are left out; the most recent ones follow ...]\n\n" + tail[-room:]
