"""Thin wrapper around the Anthropic Messages API: retries, refusal fallback, usage accounting."""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import anthropic

# USD per million tokens. Cache writes (5-minute TTL) bill at 1.25x the input rate.
PRICING = {
    "claude-opus-5-5": {"in": 4.00, "out": 20.00, "cache_read": 0.20},
    "claude-sonnet-5-5": {"in": 2.00, "out": 10.00, "cache_read": 0.20},
    "claude-haiku-4-5": {"in": 1.00, "out": 5.00, "cache_read": 0.10},
}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0

    def add(self, u: Any) -> None:
        self.calls += 1
        self.input_tokens += getattr(u, "input_tokens", 0) or 0
        self.output_tokens += getattr(u, "output_tokens", 0) or 0
        self.cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0
        self.cache_write_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0

    def cost(self, model: str) -> float:
        p = PRICING.get(model)
        if p is None:  # e.g. free-tier Gemini - not billed
            return 0.0
        return (self.input_tokens * p["in"] + self.cache_write_tokens * p["in"] * 1.25
                + self.cache_read_tokens * p["cache_read"] + self.output_tokens * p["out"]) / 1_000_000

    def to_dict(self, model: str) -> dict:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read_tokens, "cache_write_tokens": self.cache_write_tokens,
                "calls": self.calls, "cost_usd": round(self.cost(model), 4)}


class LLMError(Exception):
    """Non-recoverable model API failure (auth, bad request, retries exhausted)."""


class ModelClient(Protocol):
    model: str
    usage: Usage

    async def complete(self, *, system: str, tools: list[dict], messages: list, effort: str) -> Any: ...


@dataclass
class ClaudeClient:
    model: str
    use_fallbacks: bool = True
    max_tokens: int = 16000
    on_retry: Callable[[str], None] | None = None
    usage: Usage = field(default_factory=Usage)

    def __post_init__(self):
        # SDK retries 408/409/429/5xx/connection errors with backoff; we add an outer layer below.
        self.client = anthropic.AsyncAnthropic(max_retries=3, timeout=300)

    async def complete(self, *, system: str, tools: list[dict], messages: list, effort: str):
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            # Frozen system prompt + fixed tool list -> stable, cacheable prefix.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=tools,
            messages=messages,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": effort},
            cache_control={"type": "ephemeral"},  # auto-cache the growing conversation prefix
        )
        betas: list[str] = []
        if self.use_fallbacks:
            # If a safety classifier declines, the API re-runs on Anthropic's recommended fallback model.
            betas.append("server-side-fallback-2026-07-01")
            kwargs["fallbacks"] = "default"

        attempts = 4
        for attempt in range(1, attempts + 1):
            try:
                resp = await self.client.beta.messages.create(betas=betas, **kwargs)
                self.usage.add(resp.usage)
                return resp
            except anthropic.AuthenticationError as e:
                raise LLMError("Authentication failed - set ANTHROPIC_API_KEY (see README).") from e
            except anthropic.PermissionDeniedError as e:
                raise LLMError(f"Permission denied by the API: {e.message}") from e
            except anthropic.NotFoundError as e:
                raise LLMError(f"Model or endpoint not found ({self.model}): {e.message}") from e
            except anthropic.BadRequestError as e:
                raise LLMError(f"Bad request to the model API: {e.message}") from e
            except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError,
                    anthropic.APITimeoutError) as e:
                if attempt == attempts:
                    raise LLMError(f"Model API unavailable after {attempts} attempts: {e}") from e
                delay = min(30.0, 2 ** attempt + random.random())
                if self.on_retry:
                    self.on_retry(f"{type(e).__name__}; retrying in {delay:.0f}s (attempt {attempt}/{attempts})")
                await asyncio.sleep(delay)
            except anthropic.APIStatusError as e:
                raise LLMError(f"Model API error {e.status_code}: {e.message}") from e
        raise LLMError("unreachable")
