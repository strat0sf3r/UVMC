"""Command line: `uvmc` for an interactive session, `uvmc "task"` for a single task."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, config
from .agent import Session
from .log import RunLog


def terminal_approver(no_ask: bool):
    def approve(tool: str, args: dict, reason: str, call_id: str = "") -> bool:
        if no_ask:
            return False
        print(f"\n  The agent wants to use {tool} ({reason}):")
        for key, value in args.items():
            text = str(value)
            if len(text) > 400:
                text = text[:400] + " ..."
            print(f"    {key}: {text}")
        try:
            return input("  Allow? [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            return False
    return approve


def serve_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="uvmc serve", description="Run the supervisor as a service with the panel.")
    ap.add_argument("--config", type=Path, default=config.DEFAULT_PATH)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    opts = ap.parse_args(argv)
    from .service import serve
    serve(config.load(opts.config), opts.host, opts.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["serve"]:
        return serve_main(argv[1:])
    ap = argparse.ArgumentParser(prog="uvmc", description="UVMC supervisor: run tasks with the agent in this terminal. "
                                 "`uvmc serve` runs it as a service with the panel.")
    ap.add_argument("task", nargs="?", help="run this one task, then exit")
    ap.add_argument("--config", type=Path, default=config.DEFAULT_PATH)
    ap.add_argument("--no-ask", action="store_true", help="refuse everything that would need approval")
    ap.add_argument("--model", help="model profile to use (default: [agent] model in config.toml)")
    ap.add_argument("--version", action="version", version=f"uvmc {__version__}")
    opts = ap.parse_args(argv)

    cfg = config.load(opts.config)
    from .providers import ProviderError, make  # imported late so --help works without dependencies
    try:
        profile = cfg.profile(opts.model)
        if profile.provider == "claude-code":
            print(f"{profile.label} works through the panel only (the service hands it its tools). "
                  f"Use the panel, or pick another model with --model.", file=sys.stderr)
            return 1
        provider = make(profile, config.api_key(profile.key))
    except (KeyError, ProviderError) as e:
        print(e.args[0] if e.args else e, file=sys.stderr)
        print(f"Profiles: {', '.join(cfg.models)}", file=sys.stderr)
        return 1

    log = RunLog(cfg.log_dir)
    session = Session(cfg, provider, terminal_approver(opts.no_ask), log)

    def run(task: str) -> None:
        try:
            session.run_task(task)
        except ProviderError as e:
            print(f"\nAPI error: {e}", file=sys.stderr)
            log.event("error", text=str(e))
            session.messages.clear()  # start clean after a failed request

    if opts.task:
        run(opts.task)
        return 0

    print(f"UVMC supervisor {__version__} | model {profile.label} ({profile.model}) | works as '{cfg.user}' in {cfg.workdir}")
    print("Type a task. Ctrl+C stops the current task; an empty line or Ctrl+D quits.")
    if log.path:
        print(f"Log: {log.path}")
    while True:
        try:
            task = input("\ntask> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not task:
            return 0
        run(task)


if __name__ == "__main__":
    sys.exit(main())
