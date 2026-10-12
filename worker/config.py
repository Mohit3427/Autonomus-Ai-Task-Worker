"""Runtime settings (env-driven) and the environment directory."""
from __future__ import annotations

import json
import os
from datetime import date
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# Provider presets for OpenAI-compatible APIs. "gemini" is the free option (Google AI Studio key).
PROVIDERS = {
    # Free-tier quotas are per model and small (e.g. 20 requests/day for gemini-3.8-flash), so the
    # client walks this chain - best model first - when a model's daily quota runs out.
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "key_env": "GEMINI_API_KEY", "model": "gemini-3.8-flash", "rpm": 8,
               "fallbacks": ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite",
                             "gemini-3.5-flash-lite"]},
    "openai_compat": {"base_url": os.environ.get("LLM_BASE_URL", ""), "key_env": "LLM_API_KEY",
                      "model": "", "rpm": 0},
}


def _default_provider() -> str:
    if os.environ.get("LLM_PROVIDER"):
        return os.environ["LLM_PROVIDER"].strip().lower()
    if os.environ.get("GEMINI_API_KEY") and not os.environ.get("ANTHROPIC_API_KEY"):
        return "gemini"
    return "anthropic"


@dataclass
class Settings:
    provider: str = field(default_factory=_default_provider)  # anthropic | gemini | openai_compat
    model: str = os.environ.get("WORKER_MODEL", "")  # empty -> provider default
    llm_rpm: float = float(os.environ.get("LLM_RPM", "-1"))  # requests/min cap; -1 -> provider default
    fallback_models: list[str] = field(default_factory=lambda: [
        m.strip() for m in os.environ.get("LLM_FALLBACK_MODELS", "").split(",") if m.strip()])
    effort: str = os.environ.get("WORKER_EFFORT", "medium")
    verifier_effort: str = os.environ.get("VERIFIER_EFFORT", "medium")
    max_turns: int = int(os.environ.get("WORKER_MAX_TURNS", "60"))
    verifier_max_turns: int = int(os.environ.get("VERIFIER_MAX_TURNS", "25"))
    max_repair_rounds: int = int(os.environ.get("MAX_REPAIR_ROUNDS", "2"))
    user_response_timeout: float = float(os.environ.get("USER_RESPONSE_TIMEOUT", "600"))
    tool_timeout: float = float(os.environ.get("TOOL_TIMEOUT", "60"))
    headless: bool = _bool("HEADLESS", True)
    use_fallbacks: bool = _bool("USE_REFUSAL_FALLBACKS", True)
    runs_dir: Path = Path(os.environ.get("RUNS_DIR", ROOT / "runs"))
    environment_file: Path = Path(os.environ.get("ENVIRONMENT_FILE", ROOT / "environment.json"))
    # The simulated apps pin "today" via SIM_TODAY; the agent must agree with them.
    today: str = os.environ.get("SIM_TODAY") or date.today().isoformat()

    def __post_init__(self):
        preset = PROVIDERS.get(self.provider, {})
        if not self.model:
            self.model = preset.get("model") or "claude-opus-5-5"
        if self.llm_rpm < 0:
            self.llm_rpm = preset.get("rpm", 0)
        if not self.fallback_models and not os.environ.get("WORKER_MODEL"):
            self.fallback_models = list(preset.get("fallbacks", []))

    @property
    def api_key(self) -> str:
        env = PROVIDERS.get(self.provider, {}).get("key_env", "ANTHROPIC_API_KEY")
        return os.environ.get(env, "")


@dataclass
class Environment:
    """What the worker is allowed to touch: apps, their docs, and secret *names*.

    This file is the main generalization point - pointing the worker at a different
    set of internal tools means editing this JSON, not the agent code.
    """

    organization: str
    operator: str
    apps: list[dict]
    secrets: dict[str, str] = field(repr=False)
    example_tasks: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "Environment":
        raw = json.loads(Path(path).read_text())
        return cls(raw["organization"], raw.get("operator", ""), raw["apps"], raw.get("secrets", {}),
                   raw.get("example_tasks", []))

    @property
    def allowed_hosts(self) -> set[str]:
        return {urlparse(a["url"]).netloc for a in self.apps}

    def is_allowed_url(self, url: str) -> bool:
        p = urlparse(url)
        return p.scheme in ("http", "https") and p.netloc in self.allowed_hosts

    def secret(self, name: str) -> str | None:
        return self.secrets.get(name)

    def describe(self) -> str:
        lines = []
        for a in self.apps:
            lines.append(f"- {a['name']} — {a['url']}\n  {a['description']}\n  Auth: {a['auth']}")
        return "\n".join(lines)
