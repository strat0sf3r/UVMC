"""Signing in with your Claude plan from the panel, through Claude Code's own `claude setup-token`.

The supervisor runs setup-token in a pseudo-terminal (very wide, so nothing wraps), reads the
sign-in link out of it for the panel, types in the code you paste back, and picks up the
one-year token it prints. The token is checked with one tiny request, then stored as the key
"claude-subscription". It never goes to the browser.

The terminal output is Claude Code's screen drawing (colours, cursor moves, links in escape
codes), so the link and the token are read from the raw bytes: any escape code ends a match,
which keeps the token from running into the text drawn after it.
"""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from typing import Callable

URL_RE = re.compile(rb"https://[^\s\x07\x1b\"']*oauth/authorize\?[^\s\x07\x1b\"']+")
TOKEN_RE = re.compile(rb"sk-ant-oat\d+-[A-Za-z0-9_-]{20,}")
COLOUR_RE = re.compile(rb"\x1b\[[0-9;]*m")
ESCAPES_RE = re.compile(r"\x1b\[[0-9;?<>=]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]|[\x00-\x08\x0e-\x1f]")
KEY_NAME = "claude-subscription"


def reach(url: str = "https://platform.claude.com/") -> str:
    """Whether this machine reaches the sign-in server (any HTTP answer counts)."""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return f"yes (HTTP {r.status})"
    except urllib.error.HTTPError as e:
        return f"yes (HTTP {e.code})"
    except Exception as e:   # noqa: BLE001 - any failure is the answer here
        return f"NO ({e})"


class SignInError(Exception):
    pass


def screen_text(raw: bytes) -> str:
    """Readable text from the terminal output, for error messages."""
    text = raw.decode("utf-8", "replace")
    text = re.sub(r"\x1b\[\d*G", " ", text)        # Claude Code draws spaces as cursor moves
    text = re.sub(r"\x1b\[\d*[ABEFK]", "\n", text)   # moves to other lines end a line
    text = ESCAPES_RE.sub("", text).replace("\r", "")
    return re.sub(r"[ \t]+", " ", text)


class SignIn:
    """One sign-in at a time: start() gives the link, submit(code) finishes it."""

    def __init__(self, claude_bin: str, work_root: str, save: Callable[[str], None],
                 verify: Callable[[str], tuple[bool, str]] | None = None, timeout: float = 600):
        self.claude_bin, self.work_root, self.save = claude_bin, work_root, save
        self.verify = verify or self._verify
        self.timeout = timeout
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.master: int | None = None
        self.home: str | None = None
        self.out = b""
        self.last_out = b""
        self.mark = 0          # where the output stood when the code was typed
        self.started = 0.0
        self.url = ""

    # --- the steps ---------------------------------------------------------------

    def start(self, wait: float = 45) -> str:
        with self.lock:
            self._stop()
            if not os.path.exists(self.claude_bin):
                raise SignInError("Claude Code is not installed in the machine yet. Run update on your PC first.")
            os.makedirs(self.work_root, exist_ok=True)
            self.home = tempfile.mkdtemp(prefix="signin-", dir=self.work_root)
            os.chmod(self.home, 0o700)
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 1000, 0, 0))  # wide: nothing wraps
            env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": self.home, "TERM": "xterm-256color",
                   "LANG": "C.UTF-8", "DISABLE_AUTOUPDATER": "1", "BROWSER": "/bin/true",
                   "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
            self.proc = subprocess.Popen([self.claude_bin, "setup-token"], stdin=slave, stdout=slave, stderr=slave,
                                         env=env, cwd=self.home, start_new_session=True, close_fds=True)
            os.close(slave)
            self.master, self.out, self.last_out, self.mark, self.url = master, b"", b"", 0, ""
            self.started = time.time()
        found = self._wait(lambda: URL_RE.search(self.out), wait)
        if not found:
            text = screen_text(self.out)[-400:].strip()
            self.cancel()
            self.note("no sign-in link")
            raise SignInError("Claude Code didn't show a sign-in link." + (f" It said: {text}" if text else ""))
        self.url = found.group(0).decode()
        return self.url

    def submit(self, code: str, wait: float = 150) -> str:
        try:
            message = self._submit(code, wait)
        except Exception as e:
            self.note(f"failed: {e}")
            raise
        self.note("signed in")
        return message

    def note(self, outcome: str) -> None:
        """What the last sign-in showed and how it ended, in <work_root>/last.txt (tokens masked),
        and one line in the supervisor's log, so a failure can be looked at afterwards."""
        mask = lambda t: re.sub(r"(sk-ant-[a-z]+\d*-)[A-Za-z0-9_-]+", r"\1(masked)", t)
        outcome = mask(outcome)
        print(f"UVMC sign-in: {outcome}", file=sys.stderr, flush=True)
        try:
            os.makedirs(self.work_root, exist_ok=True)
            def lines(raw: bytes) -> str:
                return "\n".join(l.strip() for l in screen_text(raw).splitlines() if l.strip())
            path = os.path.join(self.work_root, "last.txt")
            with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {outcome}\n")
                f.write(f"Claude Code: {self.claude_version()}. Reaching Claude from here: {reach()}\n")
                out = self.out or self.last_out
                f.write("\n--- what Claude Code showed before the code ---\n" + mask(lines(out[:self.mark]))[-3000:])
                f.write("\n\n--- what it showed after the code ---\n" + mask(lines(out[self.mark:]))[-3000:])
                f.write("\n\n--- the end of its raw output ---\n" + mask(repr(out[-1500:])))
        except OSError:
            pass

    def claude_version(self) -> str:
        try:
            return subprocess.run([self.claude_bin, "--version"], capture_output=True, text=True,
                                  timeout=20).stdout.strip() or "?"
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"? ({e})"

    def _submit(self, code: str, wait: float) -> str:
        code = "".join(code.split())
        if not code:
            raise SignInError("Paste the code the browser showed you.")
        if not self.running():
            raise SignInError("The sign-in has ended. Start it again.")
        self._read(0.2)
        self.mark = len(self.out)
        # The code, then Enter on its own: typed in one go, newer Claude Code takes it all as a
        # paste and the Enter never counts.
        os.write(self.master, code.encode())
        time.sleep(0.5)
        self._read(0.2)
        os.write(self.master, b"\r")

        def outcome():
            after = self.out[self.mark:]
            token = TOKEN_RE.search(COLOUR_RE.sub(b"", after))
            if token:
                return ("token", token.group(0).decode())
            text = screen_text(after)
            m = re.search(r"(OAuth error[^\n]*|[Ee]rror[^\n]*|[Ii]nvalid[^\n]*)", text)
            if m:
                return ("error", m.group(1).strip())
            if self.proc.poll() is not None:
                return ("error", text.strip()[-300:] or "Claude Code stopped.")
            return None

        result = self._wait(outcome, wait)
        if result is None:
            self.cancel()
            raise SignInError(f"Timed out: Claude Code gave no answer within {int(wait)} seconds after the code. "
                              "The sign-in server may be slow right now. Start the sign-in again.")
        kind, value = result
        if kind == "error":
            self.cancel()
            value = re.sub(r"\s*Press Enter to retry\.?", "", value).strip().rstrip(".")
            raise SignInError(f"Claude Code said: {value}. Start the sign-in again and use the newest code.")
        token = value
        self.cancel()
        ok, why = self.verify(token)
        if not ok:
            raise SignInError(f"Claude Code gave a token, but it didn't work: {why}")
        self.save(token)
        return f"Signed in with your Claude plan (token ends in {token[-4:]})."

    def cancel(self) -> None:
        with self.lock:
            self._stop()

    def running(self) -> bool:
        if self.proc is None or self.proc.poll() is not None:
            return False
        if time.time() - self.started > self.timeout:
            self.cancel()
            return False
        return True

    # --- internals -----------------------------------------------------------------

    def _wait(self, check, wait: float):
        end = time.time() + wait
        while time.time() < end:
            self._read(0.3)
            hit = check()
            if hit:
                return hit
            if self.proc is None or (self.proc.poll() is not None and not self._read(0.1)):
                return check()
        return None

    def _read(self, timeout: float) -> bool:
        if self.master is None:
            return False
        try:
            ready, _, _ = select.select([self.master], [], [], timeout)
            if not ready:
                return False
            data = os.read(self.master, 65536)
        except OSError:
            return False
        if data:
            self.out += data
            if len(self.out) > 2_000_000:
                self.out = self.out[-1_000_000:]
                self.mark = 0
        return bool(data)

    def _stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, 9)
            except OSError:
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        self.proc = None
        if self.master is not None:
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = None
        if self.home:
            shutil.rmtree(self.home, ignore_errors=True)
            self.home = None
        if self.out:
            self.last_out = self.out   # kept for note(), which runs after the stop
        self.out = b""

    def _verify(self, token: str) -> tuple[bool, str]:
        """One tiny request with the token (a few tokens of your plan)."""
        home = tempfile.mkdtemp(prefix="check-", dir=self.work_root)
        try:
            env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": home, "LANG": "C.UTF-8",
                   "CLAUDE_CODE_OAUTH_TOKEN": token, "DISABLE_AUTOUPDATER": "1",
                   "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
            p = subprocess.run([self.claude_bin, "-p", "Reply with the single word OK.", "--model", "haiku",
                                "--tools", "", "--max-turns", "1"],
                               capture_output=True, text=True, env=env, cwd=home, timeout=90,
                               stdin=subprocess.DEVNULL)
            answer = (p.stdout + p.stderr).strip()
            if re.search(r"\bok\b", answer, re.I):
                return True, ""
            return False, answer.splitlines()[-1] if answer else f"exit code {p.returncode}"
        except subprocess.TimeoutExpired:
            return False, "timed out: checking it took more than 90 seconds. Start the sign-in again"
        except OSError as e:
            return False, str(e)
        finally:
            shutil.rmtree(home, ignore_errors=True)
