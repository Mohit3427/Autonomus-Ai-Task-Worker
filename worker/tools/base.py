"""Tool abstraction shared by every capability the worker has."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..context import RunContext


@dataclass
class ToolResult:
    content: str  # what the model sees
    is_error: bool = False
    data: dict = field(default_factory=dict)  # structured extras for the UI / harness (screenshot, outcome...)
    terminal: bool = False  # ends the agent loop (finish / submit_verdict)


@dataclass
class Assessment:
    """What executing a call would do - consumed by the policy before the call runs.

    kind: read | input | local_write | submit | auth | external_write | destructive | financial | control
    """

    kind: str
    target: str = ""  # human-readable description of the affected thing
    url: str = ""


class ToolError(Exception):
    """An expected, explainable failure - surfaced to the model as an error result."""


class TransientToolError(ToolError):
    """A failure worth one automatic retry (timeouts, flaky network)."""


class Tool:
    name: str = ""
    description: str = ""
    input_schema: dict = {"type": "object", "properties": {}}

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}

    async def assess(self, ctx: "RunContext", args: dict) -> Assessment:
        return Assessment("read")

    async def run(self, ctx: "RunContext", **args: Any) -> ToolResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def validate(self, args: dict) -> str | None:
        """Schema check (required keys, types, enums), recursing into arrays and nested objects.

        Models - especially small ones - sometimes ignore nested schemas; returning a precise error
        lets them correct the call instead of the tool silently doing the wrong thing.
        """
        return _check(self.input_schema, args, "input")


_TYPES = {"string": str, "boolean": bool, "integer": int, "array": list, "object": dict}


def _check(sch: dict, value, path: str) -> str | None:
    expected = _TYPES.get(sch.get("type", ""))
    if expected and not isinstance(value, expected):
        return f"'{path}' must be of type {sch['type']}, got {type(value).__name__}."
    if sch.get("enum") and value not in sch["enum"]:
        return f"'{path}' must be one of {sch['enum']}."
    if isinstance(value, dict) and "properties" in sch:
        props = sch["properties"]
        for key in sch.get("required", []):
            if key not in value:
                return f"Missing required parameter '{key}' in {path}."
        if sch.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    return f"Unknown parameter '{key}' in {path}. Allowed: {', '.join(props)}."
        for key, v in value.items():
            if key in props and (err := _check(props[key], v, key if path == "input" else f"{path}.{key}")):
                return err
    if isinstance(value, list) and "items" in sch:
        for i, item in enumerate(value):
            if err := _check(sch["items"], item, f"{path}[{i}]"):
                return err
    return None


def schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}
