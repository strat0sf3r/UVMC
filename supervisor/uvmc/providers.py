"""The brain is swappable: one provider interface, one adapter per API style.

The loop speaks one internal format; adapters translate it to and from each API.

Internal conversation format (a list of dicts):
    {"role": "user", "text": "..."}
    {"role": "assistant", "text": "...", "tool_calls": [ToolCall...], "native": {...}}
    {"role": "tool_results", "results": [{"id": ..., "content": "...", "is_error": bool}]}

"native" keeps the provider's own form of a reply so the same provider gets it back
exactly (some send extra parts, like thinking, that must be returned unchanged).
Another provider gets the generic form instead, so you can switch models mid-conversation.

Two adapters cover most of the market:
    anthropic  Claude (and Anthropic-compatible endpoints)
    openai     OpenAI and every OpenAI-compatible API: DeepSeek, Gemini's compatibility
               endpoint, Mistral, Groq, OpenRouter, and local servers such as Ollama or vLLM
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field


class ProviderError(Exception):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class Reply:
    text: str
    tool_calls: list[ToolCall]
    stop: str                      # "tool_use", "end" or "max_tokens"
    input_tokens: int = 0
    output_tokens: int = 0
    native: object = None          # the provider's own form, for exact replay
    extra: dict = field(default_factory=dict)

    def blocks(self) -> list[dict]:
        """The reply as content blocks, the shape the log and the panel use."""
        out = [{"type": "text", "text": self.text}] if self.text else []
        out += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in self.tool_calls]
        return out


@dataclass
class Profile:
    """A named model setup from config.toml, e.g. [models.deepseek]."""
    name: str
    provider: str                  # "anthropic" or "openai"
    model: str
    key: str = ""                  # key name for set-key; "" = no key needed (local servers)
    base_url: str = ""
    max_tokens_param: str = "max_tokens"   # OpenAI's newer models want "max_completion_tokens"
    label: str = ""


class Provider:
    kind = "base"

    def __init__(self, profile: Profile):
        self.profile = profile

    def complete(self, system: str, messages: list[dict], tools: list[dict], max_tokens: int) -> Reply:
        raise NotImplementedError


# --- Anthropic -------------------------------------------------------------------

class AnthropicProvider(Provider):
    kind = "anthropic"

    def __init__(self, profile: Profile, client=None, api_key: str | None = None):
        super().__init__(profile)
        if client is None:
            import anthropic
            kwargs = {"api_key": api_key}
            if profile.base_url:
                kwargs["base_url"] = profile.base_url
            client = anthropic.Anthropic(**kwargs)
        self.client = client

    def to_native(self, messages: list[dict]) -> list[dict]:
        out = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["text"]})
            elif m["role"] == "assistant":
                native = (m.get("native") or {}).get(self.kind)
                if native is not None:
                    out.append({"role": "assistant", "content": native})
                    continue
                content = [{"type": "text", "text": m["text"]}] if m.get("text") else []
                content += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input}
                            for c in m.get("tool_calls", [])]
                out.append({"role": "assistant", "content": content or [{"type": "text", "text": "(no reply)"}]})
            elif m["role"] == "tool_results":
                out.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"], "is_error": r["is_error"]}
                    for r in m["results"]]})
        return out

    def complete(self, system, messages, tools, max_tokens) -> Reply:
        try:
            r = self.client.messages.create(model=self.profile.model, max_tokens=max_tokens, system=system,
                                            tools=tools, messages=self.to_native(messages))
        except Exception as e:
            raise ProviderError(f"{self.profile.name}: {e}") from e
        text = "\n\n".join(b.text for b in r.content if b.type == "text" and b.text.strip())
        calls = [ToolCall(b.id, b.name, b.input if isinstance(b.input, dict) else {})
                 for b in r.content if b.type == "tool_use"]
        stop = {"tool_use": "tool_use", "max_tokens": "max_tokens"}.get(r.stop_reason, "end")
        return Reply(text, calls, stop, r.usage.input_tokens, r.usage.output_tokens, native=r.content)


# --- OpenAI-compatible --------------------------------------------------------------

class OpenAIProvider(Provider):
    """Chat Completions with tools, over plain HTTP (no extra library needed)."""
    kind = "openai"

    def __init__(self, profile: Profile, api_key: str | None = None, timeout: float = 600):
        super().__init__(profile)
        import httpx  # already installed: the anthropic package depends on it
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        base = (profile.base_url or "https://api.openai.com/v1").rstrip("/")
        self.http = httpx.Client(base_url=base, headers=headers, timeout=timeout)

    @staticmethod
    def tools(tools: list[dict]) -> list[dict]:
        return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                  "parameters": t.get("input_schema", {"type": "object"})}}
                for t in tools]

    def to_native(self, system: str, messages: list[dict]) -> list[dict]:
        out = [{"role": "system", "content": system}] if system else []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["text"]})
            elif m["role"] == "assistant":
                msg = {"role": "assistant", "content": m.get("text") or None}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [{"id": c.id, "type": "function",
                                          "function": {"name": c.name, "arguments": json.dumps(c.input)}}
                                         for c in m["tool_calls"]]
                elif not msg["content"]:
                    msg["content"] = "(no reply)"
                out.append(msg)
            elif m["role"] == "tool_results":
                for r in m["results"]:
                    content = f"Error: {r['content']}" if r["is_error"] else r["content"]
                    out.append({"role": "tool", "tool_call_id": r["id"], "content": content})
        return out

    def complete(self, system, messages, tools, max_tokens) -> Reply:
        body = {"model": self.profile.model, "messages": self.to_native(system, messages)}
        if tools:
            body["tools"] = self.tools(tools)
            body["tool_choice"] = "auto"
        body[self.profile.max_tokens_param or "max_tokens"] = max_tokens
        try:
            resp = self.http.post("/chat/completions", json=body)
        except Exception as e:
            raise ProviderError(f"{self.profile.name}: could not reach {self.http.base_url}: {e}") from e
        if resp.status_code >= 400:
            detail = resp.text[:500]
            try:
                err = resp.json().get("error")
                detail = err.get("message", detail) if isinstance(err, dict) else (err or detail)
            except ValueError:
                pass
            raise ProviderError(f"{self.profile.name}: HTTP {resp.status_code}: {detail}")
        try:
            data = resp.json()
            choice = data["choices"][0]
        except (ValueError, KeyError, IndexError) as e:
            raise ProviderError(f"{self.profile.name}: unexpected answer: {resp.text[:300]}") from e
        msg = choice.get("message") or {}
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
                if not isinstance(args, dict):
                    args = {"_raw": raw}
            except (ValueError, TypeError):
                args = {"_raw": raw}  # the tool will report bad arguments back to the model
            calls.append(ToolCall(tc.get("id") or f"call_{uuid.uuid4().hex[:12]}", fn.get("name", ""), args))
        content = msg.get("content")
        if isinstance(content, list):  # some servers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        finish = choice.get("finish_reason")
        stop = "tool_use" if calls else ("max_tokens" if finish == "length" else "end")
        usage = data.get("usage") or {}
        extra = {"reasoning": msg["reasoning_content"]} if msg.get("reasoning_content") else {}
        return Reply((content or "").strip(), calls, stop, int(usage.get("prompt_tokens") or 0),
                     int(usage.get("completion_tokens") or 0), native=None, extra=extra)


# --- Claude Code on your Claude plan ------------------------------------------------

class ClaudeCodeProvider(Provider):
    """Claude Code, signed in with your Claude plan, as the brain.

    It runs its own loop (see brain.py), so complete() is never called: the session
    hands the whole task to brain.run_task. The key is the long-lived token from
    `claude setup-token`; it only ever goes into the brain process's environment.
    """
    kind = "claude-code"
    drives_loop = True

    def __init__(self, profile: Profile, api_key: str | None = None):
        super().__init__(profile)
        self.token = api_key or ""

    def complete(self, system, messages, tools, max_tokens) -> Reply:
        raise ProviderError("Claude Code runs its own loop; it doesn't answer single requests.")


ADAPTERS = {"anthropic": AnthropicProvider, "openai": OpenAIProvider, "claude-code": ClaudeCodeProvider}


def make(profile: Profile, api_key: str | None) -> Provider:
    if profile.provider not in ADAPTERS:
        raise ProviderError(f"Unknown provider '{profile.provider}' in [models.{profile.name}]: "
                            f"use one of {', '.join(ADAPTERS)}.")
    if profile.key and not api_key:
        if profile.provider == "claude-code":
            raise ProviderError(f"Claude Code is not signed in with your plan yet. Run sign-in-claude on your PC "
                                f"(or paste the token under Settings, key '{profile.key}').")
        raise ProviderError(f"No API key for '{profile.name}'. Run set-key {profile.key} on your PC.")
    return ADAPTERS[profile.provider](profile, api_key=api_key)
