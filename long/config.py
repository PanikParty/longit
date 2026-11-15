from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "long" / "config.toml"


@dataclass
class Config:
    backend: str = "openai"
    openai: dict = field(default_factory=lambda: {
        "base_url": "http://localhost:11434/v1", "model": "qwen2.5-coder:14b", "api_key_env": "OPENAI_API_KEY"})
    llamacpp: dict = field(default_factory=lambda: {"model_path": "", "n_ctx": 8192, "n_gpu_layers": -1})
    skills: dict = field(default_factory=lambda: {"match": "triggers"})  # match: off | triggers | model
    max_steps: int = 12
    mode: str = "manual"          # manual | auto | plan
    trace: bool = False           # show the full trajectory: thinking vs tool calls and results


def load(path: Path | None = None) -> Config:
    cfg = Config()
    p = path or CONFIG_PATH
    if p.is_file():
        data = tomllib.loads(p.read_text())
        cfg.backend = data.get("backend", cfg.backend)
        cfg.max_steps = int(data.get("max_steps", cfg.max_steps))
        cfg.mode = str(data.get("mode", cfg.mode))
        cfg.trace = bool(data.get("trace", cfg.trace))
        cfg.openai.update(data.get("openai", {}))
        cfg.llamacpp.update(data.get("llamacpp", {}))
        cfg.skills.update(data.get("skills", {}))
    return cfg
