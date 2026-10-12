"""Client for OpenAI-compatible chat APIs (Gemini, OpenRouter, Ollama, ...).

The agent loop speaks one message format (Anthropic-style content blocks). This adapter
translates that history to the OpenAI chat format on each call and translates the reply back
into the same block shapes, so the loop, tools, policy and verifier need no changes.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

import httpx

from .llm import LLMError, Usage

EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


class _RateLimiter:
    """Spaces requests to stay under a requests-per-minute cap (shared by worker and verifier)."""

    def __init__(self):
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self, rpm: float) -> None:
        if rpm <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            if now < self._next:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + 60.0 / rpm


_limiter = _RateLimiter()

# model -> unix time until which its daily free quota is known to be exhausted (shared across runs)
_exhausted_until: dict[str, float] = {}


def _retry_delay(body: str) -> float | None:
    """Parse Google's RetryInfo ("retryDelay": "26156s") from an error body."""
    m = re.search(r'"retryDelay":\s*"([\d.]+)s"', body)
    return float(m.group(1)) if m else None


def _is_daily_quota(body: str) -> bool:
    return "PerDay" in body or "per day" in body.lower()


def _clean_schema(schema: Any) -> Any:
    """Drop JSON-Schema keywords some OpenAI-compatible backends reject."""
    if isinstance(schema, dict):
        return {k: _clean_schema(v) for k, v in schema.items() if k != "additionalProperties"}
    if isinstance(schema, list):
        return [_clean_schema(v) for v in schema]
    return schema


def _get(block: Any, key: str, default=None):
    return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)


def to_openai_messages(system: str, messages: list) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        role, content = m["role"], m["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            raw = next((_get(b, "raw") for b in content if _get(b, "type") == "provider_raw"), None)
            if raw is not None:
                # Replay the provider's own message verbatim - this keeps fields such as Gemini
                # thought signatures on tool calls, which must come back unchanged.
                out.append(raw)
                continue
            text = "".join(_get(b, "text", "") for b in content if _get(b, "type") == "text")
            calls = [{"id": _get(b, "id"), "type": "function",
                      "function": {"name": _get(b, "name"), "arguments": json.dumps(_get(b, "input") or {})}}
                     for b in content if _get(b, "type") == "tool_use"]
            msg: dict = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        # user turn: tool results first (they must directly follow the assistant tool calls), then text
        texts = []
        for b in content:
            t = _get(b, "type")
            if t == "tool_result":
                body = _get(b, "content")
                if isinstance(body, list):
                    body = "\n".join(_get(x, "text", "") for x in body)
                if _get(b, "is_error"):
                    body = f"ERROR: {body}"
                out.append({"role": "tool", "tool_call_id": _get(b, "tool_use_id"), "content": body})
            elif t == "text":
                texts.append(_get(b, "text", ""))
        if texts:
            out.append({"role": "user", "content": "\n\n".join(texts)})
    return out


def from_openai_response(data: dict) -> SimpleNamespace:
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    blocks: list = []
    reasoning = msg.get("reasoning_content") or msg.get("reasoning")
    if reasoning:
        blocks.append(SimpleNamespace(type="thinking", thinking=reasoning))
    if msg.get("content"):
        blocks.append(SimpleNamespace(type="text", text=msg["content"]))
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments") or "{}"
        parse_error = None
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as e:
            args, parse_error = {}, f"{e}: {str(raw_args)[:200]}"
        if not tc.get("id"):
            tc["id"] = f"call_{int(time.time() * 1000)}_{i}"
        blocks.append(SimpleNamespace(type="tool_use", id=tc["id"], name=fn.get("name", ""), input=args,
                                      parse_error=parse_error))
    replay = {k: v for k, v in msg.items() if k in ("role", "content", "tool_calls", "extra_content")}
    replay["role"] = "assistant"
    blocks.append(SimpleNamespace(type="provider_raw", raw=replay, model=data.get("model", "")))

    finish = choice.get("finish_reason")
    if any(b.type == "tool_use" for b in blocks):
        stop = "tool_use"
    elif finish == "length":
        stop = "max_tokens"
    elif finish == "content_filter":
        stop = "refusal"
    else:
        stop = "end_turn"
    u = data.get("usage") or {}
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    usage = SimpleNamespace(input_tokens=(u.get("prompt_tokens") or 0) - cached,
                            output_tokens=u.get("completion_tokens") or 0,
                            cache_read_input_tokens=cached, cache_creation_input_tokens=0)
    return SimpleNamespace(content=blocks, stop_reason=stop, usage=usage, stop_details=None)


@dataclass
class OpenAICompatClient:
    """OpenAI-compatible chat client with a model fallback chain.

    `model` is tried first; `fallback_models` are used, in order, when the current model's daily
    quota is exhausted or it stays overloaded. Switching mid-run is safe for Gemini: the next model
    accepts the previous model's thought signatures (verified), and history is replayed verbatim.
    """

    model: str
    base_url: str
    api_key: str
    rpm: float = 0  # requests/minute cap; 0 = unlimited
    send_reasoning_effort: bool = True
    fallback_models: list[str] = field(default_factory=list)
    max_tokens: int = 16000
    on_retry: Callable[[str], None] | None = None
    usage: Usage = field(default_factory=Usage)
    transport: httpx.AsyncBaseTransport | None = None  # injectable for tests

    def __post_init__(self):
        self._chain = [self.model, *[m for m in self.fallback_models if m != self.model]]
        self._advance_past_exhausted()

    def _advance_past_exhausted(self) -> None:
        now = time.time()
        usable = [m for m in self._chain if _exhausted_until.get(m, 0) <= now]
        if usable:
            self.model = usable[0]

    def _switch_model(self, reason: str) -> bool:
        idx = self._chain.index(self.model) if self.model in self._chain else -1
        now = time.time()
        for m in self._chain[idx + 1:]:
            if _exhausted_until.get(m, 0) <= now:
                if self.on_retry:
                    self.on_retry(f"{self.model}: {reason}; switching to {m}")
                self.model = m
                return True
        return False

    def _payload(self, system: str, tools: list[dict], messages: list, effort: str) -> dict:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_messages(system, messages),
            "tools": [{"type": "function", "function": {
                "name": t["name"], "description": t["description"],
                **({"parameters": _clean_schema(t["input_schema"])} if t["input_schema"].get("properties") else {}),
            }} for t in tools],
            "tool_choice": "auto",
            "max_tokens": self.max_tokens,
        }
        if self.send_reasoning_effort:
            payload["reasoning_effort"] = EFFORT_MAP.get(effort, "medium")
        return payload

    async def complete(self, *, system: str, tools: list[dict], messages: list, effort: str):
        url = self.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        attempts, overloaded = 0, 0
        while True:
            attempts += 1
            await _limiter.wait(self.rpm)
            try:
                async with httpx.AsyncClient(timeout=180, transport=self.transport) as client:
                    resp = await client.post(url, json=self._payload(system, tools, messages, effort),
                                             headers=headers)
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                status, body = 0, f"network error: {e!r}"
            else:
                status, body = resp.status_code, resp.text
                if status == 200:
                    out = from_openai_response(resp.json())
                    self.usage.add(out.usage)
                    return out
                if status in (401, 403):
                    raise LLMError(f"API key rejected (HTTP {status}): {body[:300]}. Check the key in .env.")
                if status not in (408, 429) and status < 500:
                    raise LLMError(f"Model API error - HTTP {status}: {body[:400]}")

            delay = None
            if status == 429 and _is_daily_quota(body):
                wait = _retry_delay(body) or 3600.0
                _exhausted_until[self.model] = time.time() + wait
                if self._switch_model("daily free quota used up"):
                    attempts = overloaded = 0
                    continue
                raise LLMError(f"Daily free-tier quota is used up for every configured model "
                               f"({', '.join(self._chain)}). It resets in about {wait / 3600:.1f}h.")
            if status == 429:
                delay = _retry_delay(body)
            if status in (500, 502, 503, 504):
                overloaded += 1
                if overloaded >= 3 and self._switch_model(f"overloaded (HTTP {status})"):
                    attempts = overloaded = 0
                    continue
            if attempts >= 6:
                raise LLMError(f"Model API unavailable after {attempts} attempts: HTTP {status} {body[:300]}")
            delay = min(delay if delay is not None else 2 ** attempts * 2 + random.random(), 90.0)
            if self.on_retry:
                self.on_retry(f"{self.model}: HTTP {status or 'network'}; retrying in {delay:.0f}s "
                              f"(attempt {attempts}/6)")
            await asyncio.sleep(delay)
