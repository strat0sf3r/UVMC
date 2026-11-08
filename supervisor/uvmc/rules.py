"""Decides allow / ask / deny for every tool call, before anything runs."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Config


@dataclass
class Decision:
    verdict: str  # "allow", "ask" or "deny"
    reason: str


class Rules:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.deny = [re.compile(p) for p in cfg.deny_patterns]
        self.ask = [re.compile(p) for p in cfg.ask_patterns]

    def check(self, tool: str, args: dict) -> Decision:
        if tool not in self.cfg.tools_enabled:
            return Decision("deny", f"tool '{tool}' is not enabled")
        if tool == "run_command":
            command = str(args.get("command", ""))
            for p in self.deny:
                if p.search(command):
                    return Decision("deny", f"command matches deny rule /{p.pattern}/")
            for p in self.ask:
                if p.search(command):
                    return Decision("ask", f"command matches ask rule /{p.pattern}/")
        verdict = self.cfg.tool_defaults.get(tool, "ask")
        return Decision(verdict, f"default rule for {tool}")
