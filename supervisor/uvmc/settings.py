"""Settings made in the panel: API keys and model profiles.

config.toml (from the repo) stays the base. What you change in the panel is saved in
/var/lib/uvmc/settings.json as a layer on top, so it survives `update`. A profile made
in the panel can be removed; a config.toml profile edited in the panel can be reset.
Keys go where uvmc-setkey puts them, readable only by the supervisor, never by the agent.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import asdict
from pathlib import Path

from . import briefing as briefing_mod
from . import config, privacy
from .executor import root
from .providers import ADAPTERS, Profile

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
SETKEY = Path("/usr/local/bin/uvmc-setkey")
FIELDS = ("label", "provider", "model", "key", "base_url", "max_tokens_param", "privacy")


def setkey_path(name: str) -> Path:
    """Where uvmc-setkey (setup.sh) stores a key."""
    return Path("/etc/uvmc/api_key") if name == "anthropic" else Path("/etc/uvmc/keys") / name


class SettingsError(Exception):
    pass


class Settings:
    def __init__(self, cfg: config.Config):
        self.cfg = cfg
        self.file = cfg.state_dir / "settings.json"
        self.base = copy.deepcopy(cfg.models)       # the config.toml profiles
        self.base_default = cfg.model
        self.base_rules = cfg.house_rules           # supervisor/house_rules.md as pushed by update
        self.data = self._load()
        self._apply()

    # --- storage ----------------------------------------------------------------

    def _load(self) -> dict:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.file)

    def _apply(self) -> None:
        rules = self.data.get("house_rules")
        self.cfg.house_rules = rules if isinstance(rules, str) and rules.strip() else self.base_rules
        models = copy.deepcopy(self.base)
        for name, p in (self.data.get("models") or {}).items():
            try:
                models[name] = Profile(name=name, **{k: str(p.get(k, "")) for k in FIELDS})
            except TypeError:
                continue
        self.cfg.models.clear()
        self.cfg.models.update(models)
        default = self.data.get("default")
        self.cfg.model = default if default in models else (self.base_default if self.base_default in models
                                                            else next(iter(models)))

    # --- profiles -------------------------------------------------------------------

    def view(self) -> dict:
        panel = self.data.get("models") or {}
        profiles = []
        for name, p in self.cfg.models.items():
            source = ("panel" if name not in self.base else "changed in panel") if name in panel else "config.toml"
            profiles.append({**asdict(p), "source": source, "has_key": config.has_key(p), "level": p.level,
                             "suggested_privacy": privacy.suggest(p.provider, p.base_url)})
        return {"profiles": profiles, "default": self.cfg.model, "keys": self.keys(),
                "mcp_enabled": self.mcp_enabled(),
                "providers": list(ADAPTERS)}

    def save_profile(self, raw: dict) -> None:
        name = str(raw.get("name", "")).strip().lower()
        if not NAME.match(name):
            raise SettingsError("Profile names use lowercase letters, digits, - and _.")
        p = {k: str(raw.get(k, "") or "").strip() for k in FIELDS}
        p["label"] = p["label"] or name
        if p["provider"] not in ADAPTERS:
            raise SettingsError(f"Provider must be one of: {', '.join(ADAPTERS)}.")
        if not p["model"]:
            raise SettingsError("Fill in the model name.")
        if p["base_url"] and not re.match(r"^https?://[^\s/]+", p["base_url"]):
            raise SettingsError("The address must start with http:// or https://.")
        if p["provider"] == "openai" and not p["base_url"]:
            raise SettingsError("OpenAI-compatible profiles need an address (base URL).")
        if p["key"] and not NAME.match(p["key"]):
            raise SettingsError("Key names use lowercase letters, digits, - and _.")
        p["max_tokens_param"] = p["max_tokens_param"] or "max_tokens"
        if p["privacy"] and p["privacy"] not in privacy.LEVELS:
            raise SettingsError(f"Privacy must be one of: {', '.join(privacy.LEVELS)}.")
        self.data.setdefault("models", {})[name] = p
        self._save()
        self._apply()

    def remove_profile(self, name: str) -> str:
        panel = self.data.get("models") or {}
        if name not in panel:
            raise SettingsError(f"'{name}' comes from config.toml; change it there.")
        del panel[name]
        if self.data.get("default") == name:
            self.data.pop("default", None)
        self._save()
        self._apply()
        return "reset to config.toml" if name in self.base else "removed"

    def set_default(self, name: str) -> None:
        if name not in self.cfg.models:
            raise SettingsError(f"No model profile called '{name}'.")
        self.data["default"] = name
        self._save()
        self._apply()

    def toml(self) -> str:
        """The panel's profiles written as config.toml sections, to copy into the repo."""
        out = []
        for name, p in (self.data.get("models") or {}).items():
            out.append(f"[models.{name}]")
            for k in FIELDS:
                if p.get(k) and not (k == "max_tokens_param" and p[k] == "max_tokens"):
                    out.append(f'{k} = "{p[k]}"')
            out.append("")
        if self.data.get("default"):
            out.insert(0, f'# in [agent]:\n# model = "{self.data["default"]}"\n')
        return "\n".join(out).strip() + "\n" if out else ""

    # --- keys -------------------------------------------------------------------------

    # --- Claude apps on your PC (MCP) ---------------------------------------------------

    def mcp_enabled(self) -> bool:
        return bool(self.data.get("mcp_enabled", False))

    def set_mcp_enabled(self, on: bool) -> None:
        self.data["mcp_enabled"] = bool(on)
        self._save()

    # --- what the agent is told -------------------------------------------------------

    def briefing(self) -> dict:
        b = briefing_mod.defaults()
        stored = self.data.get("briefing") or {}
        if isinstance(stored.get("enabled"), bool):
            b["enabled"] = stored["enabled"]
        for k, v in (stored.get("parts") or {}).items():
            if k in b["parts"] and isinstance(v, bool):
                b["parts"][k] = v
        if isinstance(stored.get("notes"), str):
            b["notes"] = stored["notes"]
        return b

    def save_briefing(self, raw: dict) -> None:
        b = self.briefing()
        if "enabled" in raw:
            b["enabled"] = bool(raw["enabled"])
        for k, v in (raw.get("parts") or {}).items():
            if k in b["parts"]:
                b["parts"][k] = bool(v)
        if "notes" in raw:
            notes = str(raw.get("notes") or "").replace("\r\n", "\n").strip()
            if len(notes) > briefing_mod.NOTES_MAX:
                raise SettingsError(f"Your notes are too long (at most {briefing_mod.NOTES_MAX} characters).")
            b["notes"] = notes
        self.data["briefing"] = b
        self._save()

    def house_rules_source(self) -> str:
        return "panel" if isinstance(self.data.get("house_rules"), str) else "repo"

    def save_house_rules(self, text: str) -> None:
        text = str(text or "").replace("\r\n", "\n").strip()
        if not text:
            raise SettingsError("The house rules can't be empty. Use Reset to go back to the repo version.")
        if len(text) > briefing_mod.RULES_MAX:
            raise SettingsError(f"The house rules are too long (at most {briefing_mod.RULES_MAX} characters).")
        if text == self.base_rules.strip():
            self.data.pop("house_rules", None)      # same as the repo: no override
        else:
            self.data["house_rules"] = text
        self._save()
        self._apply()

    def reset_house_rules(self) -> None:
        self.data.pop("house_rules", None)
        self._save()
        self._apply()

    def keys(self) -> list[dict]:
        names = sorted({p.key for p in self.cfg.models.values() if p.key} | {"anthropic"})
        out = []
        for n in names:
            k = config.api_key(n)
            out.append({"name": n, "set": bool(k), "hint": f"ends in {k[-4:]}" if k and len(k) >= 8 else ("set" if k else ""),
                        "used_by": [p.label for p in self.cfg.models.values() if p.key == n]})
        return out

    def set_key(self, name: str, value: str) -> None:
        name = name.strip().lower()
        if not NAME.match(name):
            raise SettingsError("Key names use lowercase letters, digits, - and _.")
        value = "".join(value.split())
        if value and len(value) < 8:
            raise SettingsError("That doesn't look like an API key.")
        path = config.KEY_FILE if name == "anthropic" else config.KEY_DIR / name
        if SETKEY.exists() and path == setkey_path(name):
            # The helper (root) writes only there: used when that is where keys are kept, so a
            # key never lands somewhere api_key() won't look (or a test's key in the real store).
            p = root([str(SETKEY), name], stdin=(value + "\n").encode(), timeout=15)
            if p.returncode != 0:
                raise SettingsError(p.stderr.decode(errors="replace").strip() or "Saving the key failed.")
            return
        if not value:
            if path.exists():
                path.unlink()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
