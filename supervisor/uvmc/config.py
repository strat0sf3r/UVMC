"""Loads config.toml and the house rules into one Config object."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .providers import Profile

DEFAULT_PATH = Path(os.environ.get("UVMC_CONFIG", "/etc/uvmc/config.toml"))
KEY_FILE = Path("/etc/uvmc/api_key")          # the Anthropic key (also used by Claude Code mode)
KEY_DIR = Path(os.environ.get("UVMC_KEY_DIR", "/etc/uvmc/keys"))  # other providers' keys
KEY_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
STATE_DIR = Path(os.environ.get("UVMC_STATE_DIR", "/var/lib/uvmc"))


@dataclass
class Config:
    model: str                     # the default model profile's name
    max_tokens: int
    max_steps: int
    max_tokens_per_task: int
    house_rules: str
    user: str
    workdir: str
    tools_enabled: list[str]
    command_timeout_s: int
    max_timeout_s: int
    max_output_chars: int
    tool_defaults: dict[str, str]
    deny_patterns: list[str] = field(default_factory=list)
    ask_patterns: list[str] = field(default_factory=list)
    log_dir: Path = Path("/var/log/uvmc/runs")
    # network
    proxy_port: int = 3128
    net_allow: list[str] = field(default_factory=list)
    net_ports: list[int] = field(default_factory=lambda: [80, 443])
    net_ask_unknown: bool = True
    net_approval_timeout_s: int = 120
    # limits
    limit_memory: str = "1200M"
    limit_cpu_percent: int = 150
    limit_processes: int = 256
    home_size: str = "6G"
    tmp_size: str = "512M"
    # checkpoints
    checkpoints_keep: int = 20
    state_dir: Path = STATE_DIR
    source: Path = DEFAULT_PATH  # the config.toml this was loaded from
    models: dict[str, Profile] = field(default_factory=dict)
    # delegation
    delegate_enabled: bool = True
    delegate_workers: list[str] = field(default_factory=list)   # empty: every profile
    delegate_max_steps: int = 30
    delegate_parallel: int = 3                       # workers at once, per coordinator
    delegate_slots: int = 6                          # users for workers, all conversations together
    orchestrator_rules: str = ""
    # Claude Code on your plan (see brain.py)
    brain_user: str = "uvmc-brain"
    brain_proxy_port: int = 3129
    brain_sites: list[str] = field(default_factory=lambda: ["anthropic.com", "claude.ai", "claude.com"])
    claude_bin: str = "/usr/local/bin/claude"
    brain_home: str = "/var/lib/uvmc-brain"
    brain_idle_minutes: int = 15                     # close Claude Code after this long unused (0: never)
    source_url: str = ""                             # the repo, linked in the panel (AGPL)
    # several conversations at the same time
    conv_max_open: int = 8                           # conversations kept open in the panel
    conv_max_running: int = 3                        # working at the same moment
    conv_root: str = "/home/agent/conv"              # each conversation's own copy of the work folder
    conv_user_prefix: str = "agent"                  # its user: agent1, agent2, ... (made by setup)
    conv_proxy_port: int = 3300                      # its proxy: this port + its slot
    files_group: str = "uvmc-files"                  # the main folder and the copies: only the supervisor's group
    privacy_general: str = "local"                   # the General project's level until you choose one
    waiting_keys: tuple = ()                         # keys of [models.*] still without a model (gpt, openrouter...),
                                                     # listed in Settings › API keys so they can be added first
    waiting_profiles: tuple = ()                     # those profiles themselves, shown in Settings › Models
                                                     # with "Choose a model"
    # folders shared from the PC (shares.py): writable mounts, the agents' read-only views, and the
    # closed folder each view sits in
    shares_rw: str = os.environ.get("UVMC_SHARES_RW", "/srv/uvmc-host")
    shares_view: str = os.environ.get("UVMC_SHARES_VIEW", "/mnt/shared")
    shares_hold: str = os.environ.get("UVMC_SHARES_HOLD", "/srv/uvmc-views")
    # tools the agent builds (see made_tools.py)
    made_tools_dir: str = "/var/lib/uvmc-tools"
    made_tools_group: str = "uvmc-tools"
    made_tools_port: int = 3200                       # each tool's proxy: this port and up
    made_tools_keep_runs: int = 30
    # services you add from git (see services.py)
    services_dir: str = "/var/lib/uvmc-services"
    services_group: str = "uvmc-services"
    services_port: int = 3400                         # each service's proxy: this port and up
    services_check_minutes: int = 10                  # how often UVMC looks for new commits
    # checking for newer versions of UVMC's parts (updates.py)
    updates_check: bool = True
    updates_every_hours: float = 24
    brain_dir: str = "/opt/uvmc/brain-cwd"          # where it starts: empty, root-owned
    bridge_socket: str = "/run/uvmc/bridge.sock"     # its tools reach the supervisor here
    mcp_python: str = "/opt/uvmc/venv/bin/python"
    mcp_script: str = "/opt/uvmc/uvmc/mcp_server.py"
    # Private conversations (docs/guide.md "Private conversations"): their log lives here, in
    # memory (/run is emptied when the supervisor stops), never in log_dir.
    private_log_dir: Path = Path("/run/uvmc/private")
    # Cleaned of a conversation's (or job's) files when it ends; None: these three in the VM,
    # none outside it (tests give their own, so nobody's real /tmp is touched).
    clean_tmp: list[str] | None = None

    @property
    def homes_root(self) -> str:
        """Where the conversations' and workers' users have their home folders."""
        return os.path.join(os.path.dirname(self.workdir.rstrip("/")) or "/home/agent", "homes")

    def tmp_dirs(self) -> list[str]:
        if self.clean_tmp is not None:
            return list(self.clean_tmp)
        return [] if os.environ.get("UVMC_NO_SUDO") == "1" else ["/tmp", "/var/tmp", "/dev/shm"]

    def profile(self, name: str | None) -> Profile:
        """A model profile by name; None gives the default one."""
        name = name or self.model
        if name not in self.models:
            raise KeyError(f"No model profile called '{name}'.")
        return self.models[name]

    def default_model_id(self) -> str:
        return self.profile(None).model


def load(path: Path = DEFAULT_PATH) -> Config:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    agent, ws, tools, rules = raw["agent"], raw["workspace"], raw["tools"], raw["rules"]
    net, limits, cps = raw.get("network", {}), raw.get("limits", {}), raw.get("checkpoints", {})
    dele = raw.get("delegation", {})
    sub = raw.get("subscription", {})
    orch_file = path.parent / dele.get("orchestrator_rules", "orchestrator_rules.md")
    rules_file = path.parent / agent.get("house_rules", "house_rules.md")
    tool_defaults = {k: v for k, v in rules.items() if isinstance(v, str)}
    for name, decision in tool_defaults.items():
        if decision not in ("allow", "ask", "deny"):
            raise ValueError(f"rules.{name} must be allow, ask or deny, not {decision!r}")
    models = _load_models(raw.get("models", {}), agent)
    default = agent.get("model", "")
    if default not in models:
        default = next(iter(models))
    return Config(
        model=default,
        models=models,
        max_tokens=int(agent.get("max_tokens", 8000)),
        max_steps=int(agent.get("max_steps", 40)),
        max_tokens_per_task=int(agent.get("max_tokens_per_task", 1_000_000)),
        house_rules=rules_file.read_text(encoding="utf-8") if rules_file.exists() else "",
        user=ws["user"],
        workdir=ws["dir"],
        tools_enabled=list(tools["enabled"]),
        command_timeout_s=int(tools.get("command_timeout_s", 120)),
        max_timeout_s=int(tools.get("max_timeout_s", 900)),
        max_output_chars=int(tools.get("max_output_chars", 20000)),
        tool_defaults=tool_defaults,
        deny_patterns=list(rules.get("deny_patterns", [])),
        ask_patterns=list(rules.get("ask_patterns", [])),
        log_dir=Path(raw.get("logs", {}).get("dir", "/var/log/uvmc/runs")),
        proxy_port=int(net.get("proxy_port", 3128)),
        net_allow=[str(d).strip().lower().lstrip("*.") for d in net.get("allow", []) if str(d).strip()],
        net_ports=[int(p) for p in net.get("ports", [80, 443])],
        net_ask_unknown=bool(net.get("ask_unknown", True)),
        net_approval_timeout_s=int(net.get("approval_timeout_s", 120)),
        limit_memory=str(limits.get("memory", "1200M")),
        limit_cpu_percent=int(limits.get("cpu_percent", 150)),
        limit_processes=int(limits.get("max_processes", 256)),
        home_size=str(limits.get("home_size", "6G")),
        tmp_size=str(limits.get("tmp_size", "512M")),
        checkpoints_keep=int(cps.get("keep", 20)),
        source=path,
        delegate_enabled=bool(dele.get("enabled", True)),
        delegate_workers=[str(w) for w in dele.get("workers", [])],
        delegate_max_steps=int(dele.get("max_steps", 30)),
        delegate_parallel=max(1, int(dele.get("parallel", 3))),
        delegate_slots=max(1, int(dele.get("worker_users", 6))),
        orchestrator_rules=orch_file.read_text(encoding="utf-8") if orch_file.exists() else "",
        brain_user=str(sub.get("user", "uvmc-brain")),
        brain_proxy_port=int(sub.get("proxy_port", 3129)),
        brain_sites=[str(d).strip().lower().lstrip("*.") for d in
                     sub.get("sites", ["anthropic.com", "claude.ai", "claude.com"]) if str(d).strip()],
        claude_bin=str(sub.get("claude", "/usr/local/bin/claude")),
        brain_home=f"/var/lib/{sub.get('user', 'uvmc-brain')}",
        brain_idle_minutes=int(sub.get("idle_minutes", 15)),
        source_url=str(raw.get("about", {}).get("source", "")),
        conv_max_open=max(1, int(raw.get("conversations", {}).get("max_open", 8))),
        conv_max_running=max(1, int(raw.get("conversations", {}).get("max_running", 3))),
        conv_proxy_port=int(raw.get("conversations", {}).get("proxy_port", 3300)),
        made_tools_dir=str(raw.get("made_tools", {}).get("dir", "/var/lib/uvmc-tools")),
        made_tools_port=int(raw.get("made_tools", {}).get("proxy_port", 3200)),
        made_tools_keep_runs=max(1, int(raw.get("made_tools", {}).get("keep_runs", 30))),
        services_dir=str(raw.get("services", {}).get("dir", "/var/lib/uvmc-services")),
        services_port=int(raw.get("services", {}).get("proxy_port", 3400)),
        services_check_minutes=max(1, int(raw.get("services", {}).get("check_minutes", 10))),
        updates_check=bool(raw.get("updates", {}).get("check", True)),
        updates_every_hours=max(1.0, float(raw.get("updates", {}).get("every_hours", 24))),
        files_group=str(raw.get("privacy", {}).get("files_group", "uvmc-files")),
        privacy_general=str(raw.get("privacy", {}).get("general", "local")),
        waiting_keys=_waiting_keys(raw.get("models", {})),
        waiting_profiles=_waiting_profiles(raw.get("models", {})),
    )


def _waiting_keys(section: dict) -> tuple:
    """Key names of profiles not offered yet (no model name): the key may come before the model,
    and OpenRouter's model picker needs the key."""
    out = set()
    for name, m in section.items():
        if isinstance(m, dict) and not m.get("model") and KEY_NAME.match(str(name)):
            key = str(m.get("key", name))
            if key and KEY_NAME.match(key) and m.get("provider", "anthropic") != "claude-code":
                out.add(key)
    return tuple(sorted(out))


def _waiting_profiles(section: dict) -> tuple:
    """The [models.*] profiles without a model name, as the panel's form fills them in."""
    out = []
    for name, m in section.items():
        if isinstance(m, dict) and not m.get("model") and KEY_NAME.match(str(name)):
            provider = str(m.get("provider", "anthropic"))
            if provider == "claude-code":
                continue
            out.append({"name": str(name), "label": str(m.get("label", name)), "provider": provider,
                        "base_url": str(m.get("base_url", "")), "model": "", "key": str(m.get("key", name)),
                        "max_tokens_param": str(m.get("max_tokens_param", "max_tokens")),
                        "privacy": str(m.get("privacy", ""))})
    return tuple(out)


def _load_models(section: dict, agent: dict) -> dict[str, Profile]:
    models: dict[str, Profile] = {}
    for name, m in section.items():
        if not isinstance(m, dict) or not m.get("model"):
            continue  # an unfinished profile is simply not offered
        if not KEY_NAME.match(name):
            raise ValueError(f"[models.{name}]: use lowercase letters, digits, - and _ in profile names.")
        provider = str(m.get("provider", "anthropic"))
        models[name] = Profile(
            name=name,
            provider=provider,
            model=str(m["model"]),
            key=str(m.get("key", {"anthropic": "anthropic", "claude-code": "claude-subscription"}.get(provider, name))),
            base_url=str(m.get("base_url", "")),
            max_tokens_param=str(m.get("max_tokens_param", "max_tokens")),
            label=str(m.get("label", name)),
            privacy=str(m.get("privacy", "")),
        )
        if models[name].privacy and models[name].privacy not in ("local", "paid", "open"):
            raise ValueError(f"[models.{name}] privacy must be local, paid or open.")
        from . import openrouter
        if openrouter.free_but_private(models[name].base_url, models[name].model, models[name].privacy):
            raise ValueError(f"[models.{name}]: {openrouter.FREE_PRIVATE}")
    if not models:  # older config: [agent] model = "<Claude model id>"
        models["claude"] = Profile("claude", "anthropic", str(agent.get("model", "claude-sonnet-5-5")),
                                   key="anthropic", label="Claude")
        agent["model"] = "claude"
    return models


def api_key(name: str = "anthropic") -> str | None:
    """Keys live outside the agent's reach.

    anthropic: ANTHROPIC_API_KEY, then /etc/uvmc/api_key.
    others:    UVMC_KEY_<NAME>, then /etc/uvmc/keys/<name>.
    """
    if not name:
        return None
    env = "ANTHROPIC_API_KEY" if name == "anthropic" else f"UVMC_KEY_{name.upper().replace('-', '_')}"
    if os.environ.get(env):
        return os.environ[env].strip()
    paths = [KEY_FILE] if name == "anthropic" else []
    paths.append(KEY_DIR / name)
    for path in paths:
        try:
            key = path.read_text(encoding="utf-8").strip()
            if key:
                return key
        except OSError:
            pass
    return None


def has_key(profile: Profile) -> bool:
    return not profile.key or api_key(profile.key) is not None
