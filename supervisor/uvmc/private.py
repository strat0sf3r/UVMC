"""Private conversations: the model on the PC, no internet, erased when you close them.

A private conversation works like any other while it is open (its steps, approvals and the
Activity view), but:
- only a model profile on the PC may run it (privacy.private_ok), its workers too;
- its proxy refuses every site, allowed ones too (netproxy.Proxy offline);
- its log is kept in memory (config private_log_dir, under /run), never in Runs, and other
  conversations never get its transcript, its tasks or its title (Controller._about);
- closing it (only after you confirm) erases its log, its copy of the work folder, its user's
  home folder and /tmp files, and its entries in the recent sites and programs; a restart
  erases whatever is left (Controller._sweep_private), so it never comes back;
- while it has a user, the audit system keeps which program ran but not its arguments
  (audit.set_quiet).

What stays anyway: files you bring into the main folder (and the main folder's undo points),
tools you approve, files you hand over to a share on the PC, deleted data on the disk until it
is overwritten, and whatever the model's server on the PC writes (local-model starts it
without request logs).

PRIVATE CONVERSATIONS RULE (also in CLAUDE.md): anything new that stores what a conversation
says or does (a file, a log, a list in memory that outlives it, a message to another
conversation) must leave nothing of a private one behind. tests/test_private.py runs a private
conversation through everything with a marker text and fails if the marker is found anywhere
UVMC writes; add the new place there.

self_check(), run at every start in the VM, makes sure the machine doesn't keep a private
conversation's commands where UVMC can't erase them: journald (sudo and systemd would log the
whole command) and the audit log. If anything is found, private conversations are off and the
panel says why.
"""

from __future__ import annotations

import secrets
import time

from .audit import AUDIT_LOG, set_quiet
from .executor import SLICE, make_executor, no_sudo, root

JOURNAL_FIX = ("The system log (journald) keeps the agent's commands. Run update on your PC: it sets sudo not to "
               "log the supervisor's commands (/etc/sudoers.d/uvmc-quiet).")
AUDIT_FIX = ("The audit log keeps a private conversation's commands: the rule that should leave them out had no "
             "effect.")


def _audit_tail(limit: int = 4_000_000) -> str:
    try:
        with AUDIT_LOG.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - limit))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def self_check(cfg, spare_user: str, wait: float = 6.0) -> list[str]:
    """Runs a harmless command with a marker text the way a private conversation's commands run
    (as a conversation's user, through sudo and systemd-run, with its audit rule on), then looks
    for the marker in journald and the audit log. Returns the problems (empty: all clear).
    Outside the VM there is nothing to check."""
    if no_sudo():
        return []
    marker = "uvmc-check-" + secrets.token_hex(6)
    hexed = marker.encode().hex().upper()
    since = int(time.time()) - 2
    problems = []
    err = set_quiet(spare_user, True)
    if err:
        problems.append(f"The audit log can't be told to leave out a private conversation's commands ({err}).")
    try:
        p = make_executor(cfg, SLICE, user=spare_user, workdir="/").run(f"/bin/true {marker}", timeout=20)
        if p.returncode != 0:
            problems.append("The check's test command didn't run: "
                            + (p.stderr.decode(errors="replace").strip()[:300] or f"exit code {p.returncode}"))
    except Exception as e:   # noqa: BLE001
        problems.append(f"The check's test command didn't run: {e}")
    in_journal = in_audit = False
    deadline = time.time() + wait
    while True:
        try:
            j = root(["journalctl", f"--since=@{since}", "--no-pager", "-q", "-o", "cat"], timeout=30)
            in_journal = marker in j.stdout.decode(errors="replace")
        except Exception as e:   # noqa: BLE001
            problems.append(f"The system log couldn't be read for the check: {e}")
            break
        text = _audit_tail()
        in_audit = marker in text or hexed in text
        if in_journal or in_audit or time.time() > deadline:
            break
        time.sleep(1)
    set_quiet(spare_user, False)
    if in_journal:
        problems.append(JOURNAL_FIX)
    if in_audit:
        problems.append(AUDIT_FIX)
    return problems
