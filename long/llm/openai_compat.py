"""Any server that implements POST {base_url}/chat/completions with tools."""
from __future__ import annotations

import os
from typing import Any

import httpx

from .base import AssistantTurn, BackendError, parse_choice_message


class OpenAICompatBackend:
    def __init__(self, base_url: str, model: str, api_key: str | None = None,
                 api_key_env: str = "OPENAI_API_KEY", temperature: float = 0.2, timeout: float = 300.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or os.environ.get(api_key_env) or ""
        self.temperature = temperature
        self.timeout = timeout
        self.name = f"openai-compat:{model}@{self.base_url}"

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AssistantTurn:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": self.temperature}
        if tools:  # an empty tools array is rejected by some servers
            body.update(tools=tools, tool_choice="auto")
        try:
            r = httpx.post(f"{self.base_url}/chat/completions", json=body, headers=headers, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise BackendError(f"request to {self.base_url} failed: {e}") from e
        if r.status_code >= 400:
            msg = f"{r.status_code} from backend: {r.text[:500]}"
            if r.status_code == 404 and "model" in r.text.lower():
                msg += self._available_models_hint(headers)
            raise BackendError(msg)
        try:
            return parse_choice_message(r.json()["choices"][0]["message"])
        except (KeyError, IndexError, ValueError) as e:
            raise BackendError(f"unexpected response shape: {r.text[:500]}") from e

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def list_models(self) -> list[str]:
        """Model ids the server offers (GET {base_url}/models)."""
        try:
            r = httpx.get(f"{self.base_url}/models", headers=self._headers(), timeout=10.0)
            r.raise_for_status()
            return sorted(m["id"] for m in r.json()["data"] if isinstance(m, dict) and "id" in m)
        except httpx.HTTPError as e:
            raise BackendError(f"could not list models from {self.base_url}: {e}") from e
        except (KeyError, TypeError, ValueError) as e:
            raise BackendError(f"unexpected /models response from {self.base_url}") from e

    def set_model(self, model: str) -> None:
        self.model = model
        self.name = f"openai-compat:{model}@{self.base_url}"

    def _available_models_hint(self, headers: dict[str, str]) -> str:
        """Best-effort: ask the server which models it has. Never raises."""
        try:
            ids = self.list_models()
        except BackendError:
            return ""
        if not ids:
            return "\nthe server reports no available models"
        return f"\navailable models: {', '.join(ids)}\n(use /model to pick one, --model <name>, or set [openai].model in the config)"
