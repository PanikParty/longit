"""Backend-neutral types. Messages use the OpenAI chat format throughout."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class BackendError(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string, exactly as the model produced it


@dataclass
class AssistantTurn:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    def as_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": self.text or None}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in self.tool_calls
            ]
        return msg


class Backend(Protocol):
    name: str

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AssistantTurn: ...


def parse_choice_message(message: dict[str, Any]) -> AssistantTurn:
    """Convert an OpenAI-style `choices[0].message` dict into an AssistantTurn."""
    calls = []
    for i, tc in enumerate(message.get("tool_calls") or []):
        fn = tc.get("function", {})
        args = fn.get("arguments", "{}")
        if not isinstance(args, str):
            import json
            args = json.dumps(args)
        calls.append(ToolCall(id=tc.get("id") or f"call_{i}", name=fn.get("name", ""), arguments=args))
    return AssistantTurn(text=(message.get("content") or "").strip(), tool_calls=calls)
