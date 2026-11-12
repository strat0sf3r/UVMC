"""The agent loop. The model proposes actions; the supervisor decides, runs and logs them.

The loop doesn't know which model it talks to: it goes through a Provider
(see providers.py), and keeps the conversation in the provider-neutral format.
"""

from __future__ import annotations

import threading
from typing import Callable

from . import compact
from .config import Config
from .log import RunLog
from .providers import AnthropicProvider, Provider, ToolCall
from .rules import Decision, Rules
from .tools import Tools

Approver = Callable[[str, dict, str, str], bool]  # tool, args, reason, call id


class Session:
    """One conversation with the agent; several tasks can follow each other."""

    def __init__(self, cfg: Config, provider, approve: Approver, log: RunLog, out=print,
                 cancel: threading.Event | None = None, tools: Tools | None = None):
        self.cfg, self.approve, self.log, self.out = cfg, approve, log, out
        self.provider = provider
        self.cancel = cancel or threading.Event()  # set it to stop the current task
        self.rules = Rules(cfg)
        self.tools = tools or Tools(cfg)
        self.messages: list[dict] = []
        self.current_call: str | None = None  # the tool call running right now, for linking activity
        # Per-session setup, used for orchestrators and workers:
        self.tool_names: list[str] | None = None   # None: every enabled tool
        self.extra_tools: list[dict] = []           # definitions of tools handled below
        self.handlers: dict[str, Callable[[dict, str], tuple[str, bool]]] = {}
        self.parallel_tools: set[str] = set()       # calls of these in one reply run at the same time
        self.system: str | None = None              # None: the house rules
        self.max_steps: int | None = None
        self.owns_cancel = True                     # a worker leaves the shared stop signal alone
        self.last_text = ""                         # the model's latest message, e.g. a worker's report
        self.brain = None                           # the Claude Code process, when that is the brain
        self.briefing = None                        # (settings, live text) fixed at the first task
        self.context_tokens = 0                     # what the last request held (in + out), or an estimate
        self.context_estimated = False              # True after compacting, until the next request
        self.compacted = False                      # something was trimmed or summarized: recall is offered

    @property
    def provider(self) -> Provider | None:
        return self._provider

    @provider.setter
    def provider(self, value) -> None:
        # Anything with .messages.create (an Anthropic client, or a test double) gets the Anthropic adapter.
        if value is not None and not isinstance(value, Provider):
            value = AnthropicProvider(self.cfg.profile(None), client=value)
        self._provider = value

    def run_task(self, task: str) -> str:
        """Runs one task to completion. Returns why it ended."""
        if getattr(self.provider, "drives_loop", False):  # Claude Code on your plan runs its own loop
            from . import brain
            return brain.run_task(self, task)
        rec = self.log.event("task", text=task, model=self.provider.profile.name, **self._task_by())
        self.messages.append({"role": "user", "text": task, "seq": _seq(rec)})
        self.last_text = ""
        used = 0
        max_steps = self.max_steps or self.cfg.max_steps
        system = self.system if self.system is not None else self.cfg.house_rules
        tools = self.tools.definitions(self.tool_names) + self.extra_tools
        if self.compacted:   # the agent can read what compacting shortened (compact.py)
            tools = tools + [compact.RECALL_SPEC]
            self.handlers = {**self.handlers, "recall": lambda args, call_id: compact.recall(self.log, args)}
        try:
            for step in range(1, max_steps + 1):
                if self.cancel.is_set():
                    raise KeyboardInterrupt
                reply = self.provider.complete(system, self.messages, tools, self.cfg.max_tokens)
                used += reply.input_tokens + reply.output_tokens
                if reply.input_tokens:
                    self.context_tokens, self.context_estimated = reply.input_tokens + reply.output_tokens, False
                rec = self.log.event("reply", step=step, stop=reply.stop, model=self.provider.profile.name,
                                     usage={"in": reply.input_tokens, "out": reply.output_tokens,
                                            **({"cost": reply.cost} if reply.cost is not None else {})},
                                     content=reply.blocks(), **reply.extra)
                self.messages.append({"role": "assistant", "text": reply.text, "tool_calls": reply.tool_calls,
                                      "native": {self.provider.kind: reply.native} if reply.native is not None else {},
                                      "seq": _seq(rec)})
                if reply.text:
                    self.last_text = reply.text
                    self.out(f"\n{reply.text}")

                if reply.stop != "tool_use" or not reply.tool_calls:
                    if reply.stop == "max_tokens":
                        return self._end("reply cut off at the length limit", used)
                    return self._end("done", used)

                results = self._handle_all(reply.tool_calls)
                self.messages.append({"role": "tool_results", "results": results})
                if self.cancel.is_set():
                    raise KeyboardInterrupt

                if used >= self.cfg.max_tokens_per_task:
                    return self._end("token budget reached", used)
            return self._end(f"step limit ({max_steps}) reached", used)
        except KeyboardInterrupt:
            self._close_open_calls("Stopped by the user.")
            return self._end("stopped by you", used)

    def _task_by(self) -> dict:
        """Who wrote the task, when it wasn't you (e.g. UVMC asking the agent to resolve a conflict)."""
        by, self.task_by = getattr(self, "task_by", None), None
        return {"by": by} if by else {}

    def close(self) -> None:
        """Ends a Claude Code process if one belongs to this conversation."""
        if self.brain is not None:
            self.brain.close()
            self.brain = None

    # -----------------------------------------------------------------------

    def _handle_all(self, calls: list[ToolCall]) -> list[dict]:
        """Every call of a reply, in order; the ones in parallel_tools (a coordinator's delegate
        calls) all at once, next to the others. The results keep the calls' order."""
        results: list[dict | None] = [None] * len(calls)

        def one(i: int) -> None:
            c = calls[i]
            if self.cancel.is_set():
                results[i] = {"id": c.id, "content": "Stopped by the user.", "is_error": True}
            else:
                results[i] = self._handle(c)

        def in_thread(i: int) -> None:
            try:
                one(i)
            except Exception as e:  # a bug in one job must not lose the others' results
                c = calls[i]
                self.log.event("result", id=c.id, tool=c.name, is_error=True, text=f"{type(e).__name__}: {e}")
                results[i] = {"id": c.id, "content": f"It failed: {type(e).__name__}: {e}", "is_error": True}

        together = [i for i, c in enumerate(calls) if c.name in self.parallel_tools]
        if len(together) < 2:
            together = []
        threads = [threading.Thread(target=in_thread, args=(i,), daemon=True) for i in together]
        for t in threads:
            t.start()
        for i in range(len(calls)):
            if i not in together:
                one(i)
        for t in threads:
            t.join()
        return results  # type: ignore[return-value]

    def _handle(self, call: ToolCall) -> dict:
        args = call.input if isinstance(call.input, dict) else {}
        if call.name in self.handlers:
            decision = Decision(self.cfg.tool_defaults.get(call.name, "allow"), f"default rule for {call.name}")
        elif self.tool_names is not None and call.name not in self.tool_names:
            decision = Decision("deny", f"tool '{call.name}' is not available here")
        else:
            decision = self.rules.check(call.name, args)
        self.out(f"\n> {call.name}: {_summary(call.name, args)}")
        verdict = decision.verdict
        if verdict == "ask":
            verdict = "allow" if self.approve(call.name, args, decision.reason, call.id) else "deny"
            decision.reason += " -> you " + ("approved" if verdict == "allow" else "refused")
        self.log.event("decision", id=call.id, tool=call.name, args=args, verdict=verdict, reason=decision.reason)

        if verdict != "allow":
            self.out(f"  denied ({decision.reason})")
            text, is_error = f"Denied by the supervisor: {decision.reason}.", True
        else:
            self.current_call = call.id
            try:
                if call.name in self.handlers:
                    text, is_error = self.handlers[call.name](args, call.id)
                else:
                    result = self.tools.run(call.name, args)
                    text, is_error = result.text, result.is_error
            finally:
                self.current_call = None
            first = text.splitlines()[0] if text else ""
            self.out(f"  {'error' if is_error else 'ok'}: {first[:100]}")
        rec = self.log.event("result", id=call.id, tool=call.name, is_error=is_error, text=text)
        return {"id": call.id, "content": text, "is_error": is_error, "seq": _seq(rec)}

    def _close_open_calls(self, reason: str) -> None:
        """Every tool call needs a result before the conversation can go on."""
        last = self.messages[-1] if self.messages else None
        if last and last["role"] == "assistant" and last.get("tool_calls"):
            self.messages.append({"role": "tool_results", "results": [
                {"id": c.id, "content": reason, "is_error": True} for c in last["tool_calls"]]})

    def _end(self, why: str, used: int) -> str:
        if self.owns_cancel:
            self.cancel.clear()
        self.log.event("end", reason=why, tokens=used)
        self.out(f"\n[{why}; {used:,} tokens]")
        return why


def _seq(record) -> int | None:
    """An event's number in the conversation's log: what compacting refers to, and recall reads."""
    return record.get("seq") if isinstance(record, dict) else None


def _summary(name: str, args: dict) -> str:
    if name == "run_command":
        text = str(args.get("command", ""))
    elif name == "install_system_package":
        text = " ".join(map(str, args.get("packages", [])))
    elif name == "write_file":
        text = f"{args.get('path')} ({len(str(args.get('content', '')))} chars)"
    elif name == "propose_tool":
        text = str(args.get("folder", ""))
    elif name == "delegate":
        text = f"{args.get('model', 'worker')}: {args.get('instructions', '')}"
    elif name in ("bring_in_worker", "discard_worker"):
        text = str(args.get("job", ""))
    else:
        text = str(args.get("path", "."))
    text = text.replace("\n", " ")
    return text if len(text) <= 120 else text[:117] + "..."
