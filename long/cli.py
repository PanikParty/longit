from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, config, tools
from .agent import Agent
from .llm.base import BackendError
from .ui import ConsoleUI


def build_backend(cfg: config.Config):
    if cfg.backend == "openai":
        from .llm.openai_compat import OpenAICompatBackend
        o = cfg.openai
        return OpenAICompatBackend(o["base_url"], o["model"], api_key_env=o.get("api_key_env", "OPENAI_API_KEY"))
    if cfg.backend == "llamacpp":
        from .llm.llamacpp import LlamaCppBackend
        l = cfg.llamacpp
        if not l.get("model_path"):
            raise BackendError("llamacpp backend needs --model-path (or [llamacpp].model_path in the config)")
        return LlamaCppBackend(l["model_path"], n_ctx=int(l["n_ctx"]), n_gpu_layers=int(l["n_gpu_layers"]),
                               chat_format=l.get("chat_format"))
    raise BackendError(f"unknown backend '{cfg.backend}' (expected 'openai' or 'llamacpp')")


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="long", description="Natural-language interface to git.")
    p.add_argument("request", nargs="*", help="optional one-shot request; omit for interactive mode")
    p.add_argument("-C", "--repo", default=".", help="repository path (default: cwd)")
    p.add_argument("--config", type=Path, help="config file (default ~/.config/long/config.toml)")
    p.add_argument("--backend", choices=["openai", "llamacpp"])
    p.add_argument("--base-url", help="OpenAI-compatible endpoint, e.g. http://localhost:11434/v1")
    p.add_argument("--model", help="model name for the OpenAI-compatible backend")
    p.add_argument("--api-key-env", help="env var holding the API key")
    p.add_argument("--model-path", help="GGUF file for the llamacpp backend")
    p.add_argument("--n-ctx", type=int)
    p.add_argument("--max-steps", type=int)
    p.add_argument("--version", action="version", version=f"long {__version__}")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = parse(argv)
    cfg = config.load(a.config)
    if a.backend:
        cfg.backend = a.backend
    if a.base_url:
        cfg.openai["base_url"] = a.base_url
    if a.model:
        cfg.openai["model"] = a.model
    if a.api_key_env:
        cfg.openai["api_key_env"] = a.api_key_env
    if a.model_path:
        cfg.llamacpp["model_path"] = a.model_path
        if not a.backend:
            cfg.backend = "llamacpp"
    if a.n_ctx:
        cfg.llamacpp["n_ctx"] = a.n_ctx
    if a.max_steps:
        cfg.max_steps = a.max_steps

    resolved = tools.resolve_workdir(a.repo)
    if resolved is None:
        print(f"long: {a.repo} is not a directory", file=sys.stderr)
        return 2
    root, is_repo = resolved
    try:
        backend = build_backend(cfg)
    except BackendError as e:
        print(f"long: {e}", file=sys.stderr)
        return 2

    ui = ConsoleUI()
    if not is_repo:
        ui.notice(f"{root} is not a git repository; ask me to `git init` if you want one.")
    agent = Agent(backend, root, ui, max_steps=cfg.max_steps)
    if a.request:
        agent.run_turn(" ".join(a.request))
        return 0
    ui.repl(agent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
