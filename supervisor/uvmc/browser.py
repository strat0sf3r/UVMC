"""The headless browser setup.sh installs for the agent's conversations, workers and tools.

Playwright (pinned in browser-requirements.txt) in its own Python environment, VENV, and
Chromium's headless shell in BROWSERS. Both belong to root: every agent can use them, none can
change them. The browser goes online like everything else the agent runs: through that user's
own proxy (HTTPS_PROXY), the only way the firewall lets it out.
"""

from __future__ import annotations

from pathlib import Path

VENV = Path("/opt/uvmc/browser")
BROWSERS = Path("/opt/uvmc/ms-playwright")
READY = VENV / "READY"   # written by setup.sh once the browser is in place: the Playwright version


def env() -> dict[str, str]:
    """Added to the environment of everything the agents and their tools run: Playwright finds
    the shared browser, and `pip`/`npm install playwright` don't try to download their own."""
    return {"PLAYWRIGHT_BROWSERS_PATH": str(BROWSERS), "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1"}


def version(ready: Path | None = None) -> str:
    """The installed Playwright version, or "" when the browser isn't installed (yet)."""
    try:
        return (ready or READY).read_text().strip()
    except OSError:
        return ""


def briefing_line(ready: Path | None = None) -> str:
    v = version(ready)
    if not v:
        return ""
    return (f"- A headless browser is installed: Playwright {v} with Chromium. In Python use "
            f"{VENV}/bin/python (it has playwright), or `npm install playwright@{v}` in a project for Node. "
            "Launch Chromium headless; it goes through your proxy like everything else, so the same sites "
            "are allowed. Use it only when a page needs JavaScript or clicking; a plain download is lighter.")
