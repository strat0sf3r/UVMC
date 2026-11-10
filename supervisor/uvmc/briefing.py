"""What the agent is told at the start of a conversation, in three clearly separate parts.

    1. House rules      the fixed rules (supervisor/house_rules.md, or your edit in the panel)
    2. Live briefing    written by the supervisor for each new conversation: version, model,
                        shared folders, allowed sites, limits, the work folder, the date, and
                        what you can do from the panel. You can switch it off, or parts of it.
    3. Your notes       free text you write in the panel ("answer in French", ...)

The whole text is fixed for the length of a conversation (Claude Code would otherwise have to
restart whenever it changed), so changes apply from the next new conversation.
"""

from __future__ import annotations

import datetime as dt
from typing import Callable

from . import __version__, browser

PARTS = {
    "version": "UVMC's version and the model you are",
    "shares": "The folders shared right now",
    "sites": "The allowed sites",
    "limits": "Your limits (memory, processor, disk)",
    "work": "What's in the work folder",
    "browser": "The headless browser, when it's installed",
    "made_tools": "The tools it built that you approved",
    "date": "Today's date",
    "panel": "What the user can do in the panel",
}
NOTES_MAX = 4000
RULES_MAX = 30000
SEPARATOR = "\n\n" + "=" * 72 + "\n\n"


def defaults() -> dict:
    return {"enabled": True, "parts": {k: True for k in PARTS}, "notes": ""}


def live(ctl, parts: dict, work_lister: Callable[[], list[str]] | None = None,
         worker: str | None = None) -> str:
    """The live part, from the controller's current state. worker: the profile of a worker
    being briefed (it works for the coordinator, not directly for the user)."""
    lines = []
    on = lambda name: parts.get(name, True)  # noqa: E731
    if on("version"):
        profile = ctl.cfg.profile(worker or ctl.model)
        role = ""
        if worker:
            role = (f" You are a worker: you do one job for the coordinator "
                    f"({ctl.cfg.profile(ctl.model).label}) and report back to it. Other workers may be doing "
                    "other jobs at the same time, each in its own folder.")
        elif ctl.worker_model and ctl.cfg.delegate_enabled:
            role = (f" You are the coordinator: the worker that does the work on the machine is "
                    f"{ctl.cfg.profile(ctl.worker_model).label}, up to {ctl.cfg.delegate_parallel} jobs at once.")
        lines.append(f"- This is UVMC {__version__}. You are {profile.label} ({profile.model}).{role}")
    if on("date"):
        lines.append(f"- Today is {dt.date.today().isoformat()} (the machine's clock).")
    if on("shares"):
        # Only the ones this conversation opened and may see at its level (privacy.py).
        shares = getattr(ctl, "share_names", None)
        if shares is None:
            shares = ctl.shares.names()
        if shares:
            lines.append("- Folders shared from the user's computer that you can read (read-only for you): "
                         + ", ".join(f"/mnt/shared/{n}" for n in shares) + ".")
        else:
            lines.append("- No folders are shared from the user's computer with this conversation.")
    if on("sites") and getattr(ctl, "offline", False):
        lines.append("- This conversation has no internet: every site is refused.")
    elif on("sites"):
        snap = ctl.sites.snapshot()
        sites = snap["configured"] + snap["learned"]
        ask = ("other sites need the user's approval" if ctl.cfg.net_ask_unknown
               else "other sites are refused")
        if ctl.cfg.net_ask_unknown and getattr(ctl, "seen", "open") != "open":
            ask = ("other sites need the user's approval each time, since this conversation has seen "
                   f"{ctl.seen} files")
        lines.append(f"- Allowed sites (and their subdomains): {', '.join(sites) or 'none'}; {ask}.")
    if on("limits"):
        c = ctl.cfg
        lines.append(f"- Your limits: {c.limit_memory} memory, {c.limit_cpu_percent}% of a processor core, "
                     f"{c.limit_processes} processes, a {c.home_size} home disk.")
        if getattr(c, "conv_max_running", 1) > 1:
            lines.append("- The user can run other conversations at the same time, each with its own user and "
                         "copy of the work folder. They share the machine's limits with you.")
    if on("work"):
        try:
            names = work_lister() if work_lister else []
        except Exception:  # an unreadable folder must not stop a task
            names = []
        work = getattr(ctl, "workdir", ctl.cfg.workdir)
        shared = getattr(ctl, "shared_workdir", ctl.cfg.workdir)
        if worker and getattr(ctl, "coordinator_workdir", ""):
            lines.append(f"- Your work folder is {work}: your own copy of the coordinator's folder "
                         f"({ctl.coordinator_workdir}), made for this job. Commands start there. When you report, "
                         "the coordinator checks your changes and brings them into its folder.")
        elif work != shared:
            lines.append(f"- Your work folder is {work}: this conversation's own copy of the main work folder "
                         f"({shared}, which you can't open directly). Commands start there. The user brings your "
                         "changes into the main folder when they're ready, and the latest files from it into your copy.")
        note = getattr(ctl, "privacy_note", "")
        if note:
            lines.append(note)
        if names:
            more = f" and {len(names) - 30} more" if len(names) > 30 else ""
            lines.append(f"- In your work folder: {', '.join(names[:30])}{more}.")
        else:
            lines.append("- Your work folder is empty.")
    if on("browser") and not getattr(ctl, "offline", False):
        line = browser.briefing_line(getattr(ctl, "browser_ready", None))
        if line:
            lines.append(line)
    if on("made_tools"):
        made = getattr(ctl, "made", None)
        listed = made.summary_for_agent() if made is not None else ""
        if listed:
            lines.append(f"- Tools you built that the user approved: {listed}. Their approved copies are frozen; "
                         "to change one, get its code with get_tool_code (if it isn't in your folder already), "
                         "change and test it there, and call propose_tool again.")
    if on("panel"):
        lines.append("- The user watches every step in the UVMC panel. They approve what the rules mark as "
                     "risky, can stop you at any moment, and can undo a whole task (the work folder is "
                     "copied before each task).")
    if not lines:
        return ""
    return "# Right now\n\nWritten by the UVMC supervisor for this conversation.\n\n" + "\n".join(lines)


def compose(rules: str, briefing: dict, live_text: str) -> str:
    """House rules, then the live briefing, then your notes, each under its own heading."""
    out = [rules.strip()]
    if briefing.get("enabled", True) and live_text:
        out.append(live_text)
    notes = (briefing.get("notes") or "").strip()
    if notes:
        out.append("# Notes from the user\n\n" + notes)
    return SEPARATOR.join(p for p in out if p)
