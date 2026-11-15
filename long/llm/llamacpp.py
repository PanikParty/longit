"""In-process inference through llama-cpp-python (optional dependency)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import AssistantTurn, BackendError, parse_choice_message


class LlamaCppBackend:
    def __init__(self, model_path: str, n_ctx: int = 8192, n_gpu_layers: int = -1,
                 chat_format: str | None = None, temperature: float = 0.2, verbose: bool = False):
        try:
            from llama_cpp import Llama  # type: ignore
        except ImportError as e:  # pragma: no cover - depends on optional install
            raise BackendError("llama-cpp-python is not installed: uv sync --extra local (or: uv tool install 'long[local]')") from e
        path = Path(model_path).expanduser()
        if not path.is_file():
            raise BackendError(f"model file not found: {path}")
        kwargs: dict[str, Any] = dict(model_path=str(path), n_ctx=n_ctx, n_gpu_layers=n_gpu_layers, verbose=verbose)
        if chat_format:
            kwargs["chat_format"] = chat_format  # e.g. "chatml-function-calling" for models w/o a tool template
        self._llm = Llama(**kwargs)
        self.temperature = temperature
        self.name = f"llamacpp:{path.name}"

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AssistantTurn:
        try:
            extra: dict[str, Any] = {"tools": tools, "tool_choice": "auto"} if tools else {}
            out = self._llm.create_chat_completion(messages=messages, temperature=self.temperature, **extra)
            return parse_choice_message(out["choices"][0]["message"])
        except Exception as e:  # llama.cpp raises ValueError/RuntimeError for context overflow etc.
            raise BackendError(f"llama.cpp error: {e}") from e
