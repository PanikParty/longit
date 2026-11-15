"""The single `git` tool: schema, argv normalisation, confinement, and subprocess runner."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

GIT_TOOL = {
    "type": "function",
    "function": {
        "name": "git",
        "description": (
            "Run one git command in the user's repository. Provide the arguments WITHOUT the leading "
            "'git', as a list of strings, e.g. [\"log\", \"--oneline\", \"-5\"]. No shell is involved: "
            "no pipes, redirects, globbing or &&. Read-only commands run immediately; commands that "
            "change the repo are shown to the user for approval first."
        ),
        "parameters": {
            "type": "object",
            "properties": {"args": {"type": "array", "items": {"type": "string"},
                                    "description": "git arguments, one list element per argv entry"}},
            "required": ["args"],
        },
    },
}
GH_TOOL = {
    "type": "function",
    "function": {
        "name": "gh",
        "description": (
            "Run one GitHub CLI command. Only these are available: [\"auth\", \"status\"], "
            "[\"repo\", \"view\"], [\"pr\", \"view\"|\"list\"|\"status\"], "
            "[\"repo\", \"create\", NAME, \"--private\"|\"--public\", \"--source=.\", \"--remote=origin\"] "
            "(a visibility flag is required; it does not push, use the git tool for `push -u origin HEAD`), and "
            "[\"pr\", \"create\"] to open a pull request for the current, already pushed, branch. Give `pr create` no "
            "arguments: the harness writes the title and description from the commits and diff "
            "(optional: --base BRANCH, --draft). Arguments exclude the leading 'gh'. The user approves every creation."
        ),
        "parameters": {
            "type": "object",
            "properties": {"args": {"type": "array", "items": {"type": "string"},
                                    "description": "gh arguments, one list element per argv entry"}},
            "required": ["args"],
        },
    },
}
TOOLS = [GIT_TOOL, GH_TOOL]

MAX_OUTPUT_CHARS = 8000


@dataclass
class RunResult:
    exit_code: int
    output: str

    def for_model(self) -> str:
        body = self.output if self.output.strip() else "(no output)"
        return f"exit code: {self.exit_code}\n{body}"


def parse_args(raw: str) -> list[str]:
    """Parse a tool call's JSON arguments into an argv list. Raises ValueError with a model-facing message."""
    try:
        obj = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        raise ValueError(f"arguments were not valid JSON ({e}); send {{\"args\": [\"status\"]}}") from e
    args = obj.get("args") if isinstance(obj, dict) else None
    if isinstance(args, str):
        try:
            args = shlex.split(args)
        except ValueError as e:
            raise ValueError(f"could not split args string: {e}") from e
    if not isinstance(args, list) or not args or not all(isinstance(a, str) for a in args):
        raise ValueError("'args' must be a non-empty list of strings")
    if args[0] == "git":
        args = args[1:]
    if not args:
        raise ValueError("'args' contained only 'git'")
    return args


_FENCE = re.compile(r"```(?:git|bash|sh)?\s*\n(.*?)```", re.S)


def parse_fallback_calls(text: str) -> list[list[str]]:
    """For models without native tool calling: take the first fenced block of `git ...` lines."""
    for m in _FENCE.finditer(text):
        cmds = []
        for line in m.group(1).splitlines():
            line = line.strip().lstrip("$ ").strip()
            if not line.startswith("git "):
                continue
            if any(tok in line for tok in ("|", ";", "&&", "||", ">", "<", "`", "$(")):
                continue  # refuse anything shell-shaped rather than guess
            try:
                cmds.append(shlex.split(line)[1:])
            except ValueError:
                continue
        if cmds:
            return cmds[:1]
    return []


_JSON_FENCE = re.compile(r"^```(?:json)?\s*\n(.*)\n```$", re.S)


def _argv_from_payload(obj: dict) -> list[str] | None:
    payload = obj.get("arguments", obj.get("parameters", obj.get("args")))
    if isinstance(payload, dict):
        payload = payload.get("args")
    if isinstance(payload, str):
        try:
            payload = shlex.split(payload)
        except ValueError:
            return None
    if not isinstance(payload, list) or not payload or not all(isinstance(a, str) for a in payload):
        return None
    return payload


def _call_from_obj(obj: dict) -> tuple[str, list[str] | dict] | None:
    name = str(obj.get("name", "")).lower()
    if name in ("ls", "pwd", "cd"):
        args = obj.get("arguments", obj.get("parameters", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                return None
        return (name, args) if isinstance(args, dict) else None
    argv = _argv_from_payload(obj)
    if not argv:
        return None
    tool = "git"
    if argv[0] in ("git", "gh"):
        tool, argv = argv[0], argv[1:]
    elif name == "gh":
        tool = "gh"
    return (tool, argv) if argv else None


MAX_TEXT_CALLS = 5


def parse_json_text_tool_calls(text: str) -> list[tuple[str, list[str] | dict]]:
    """Some small models emit tool calls as JSON *text* instead of structured calls, e.g.
    {"name": "Run", "arguments": ["init"]}, sometimes after prose or several in a row.
    Returns [(tool, argv)] for git/gh or [(tool, args_dict)] for ls/pwd/cd, in order; [] if none.
    A reply that is exactly one JSON object may use any shape; JSON embedded in prose (or several
    objects) must each look like a call ("name" plus "arguments"). If any candidate is malformed the
    whole reply is treated as plain text."""
    t = text.strip()
    m = _JSON_FENCE.match(t)
    if m:
        t = m.group(1).strip()
    decoder = json.JSONDecoder()
    found: list[dict] = []
    pos = 0
    while (i := t.find("{", pos)) != -1:
        try:
            obj, end = decoder.raw_decode(t, i)
        except json.JSONDecodeError:
            pos = i + 1
            continue
        if isinstance(obj, dict) and ((i == 0 and end == len(t)) or ("name" in obj and "arguments" in obj)):
            found.append(obj)
        pos = end
    if not found or len(found) > MAX_TEXT_CALLS:
        return []
    calls = [_call_from_obj(o) for o in found]
    return [] if any(c is None for c in calls) else calls  # type: ignore[misc]


def parse_json_text_tool_call(text: str) -> tuple[str, list[str] | dict] | None:
    """Single-call convenience wrapper: None unless the reply holds exactly one call."""
    calls = parse_json_text_tool_calls(text)
    return calls[0] if len(calls) == 1 else None


def parse_json_text_call(text: str) -> list[str] | None:
    r = parse_json_text_tool_call(text)
    return r[1] if r else None


def repo_root(cwd: str | Path) -> Path | None:
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 else None


def resolve_workdir(path: str | Path) -> tuple[Path, bool] | None:
    """Return (directory, is_repo). Falls back to the plain directory when it is not in a git repo.
    None if the path is not an existing directory."""
    p = Path(path).expanduser()
    if not p.is_dir():
        return None
    root = repo_root(p)
    return (root, True) if root is not None else (p.resolve(), False)


def check_confined(argv: list[str], root: Path, cwd: Path) -> str | None:
    """Return an error string if -C/--git-dir/--work-tree point outside the repo."""
    root = root.resolve()
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        opt, _, val = argv[i].partition("=")
        if opt in ("-C", "--git-dir", "--work-tree"):
            if not val:
                i += 1
                val = argv[i] if i < len(argv) else ""
            p = (cwd / val).resolve() if val else cwd
            if not (p == root or root in p.parents):
                return f"{opt} {val} points outside the repository ({root})"
        elif opt in ("-c", "--config-env", "--exec-path", "--namespace") and "=" not in argv[i]:
            i += 1
        i += 1
    return None


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head, tail = text[: limit * 2 // 3], text[-limit // 3:]
    return f"{head}\n... [{len(text) - len(head) - len(tail)} characters omitted] ...\n{tail}"


def _run(prog: str, argv: list[str], cwd: Path, timeout: float, extra_env: dict[str, str]) -> RunResult:
    env = dict(os.environ)
    env.update(extra_env)
    try:
        p = subprocess.run([prog, *argv], cwd=cwd, env=env, capture_output=True, text=True,
                           errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return RunResult(124, f"timed out after {timeout:.0f}s")
    except FileNotFoundError:
        return RunResult(127, f"{prog} executable not found" + (" (install it from https://cli.github.com)" if prog == "gh" else ""))
    out = p.stdout + (("\n" if p.stdout and p.stderr else "") + p.stderr if p.stderr else "")
    return RunResult(p.returncode, truncate(out))


def run_git(argv: list[str], cwd: Path, timeout: float = 60.0) -> RunResult:
    return _run("git", argv, cwd, timeout, dict(
        GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat", PAGER="cat", GIT_EDITOR="true",
        GIT_SEQUENCE_EDITOR="true", LC_ALL="C.UTF-8", GIT_OPTIONAL_LOCKS="0"))


def run_gh(argv: list[str], cwd: Path, timeout: float = 120.0) -> RunResult:
    return _run("gh", argv, cwd, timeout, dict(
        GH_PROMPT_DISABLED="1", GH_PAGER="cat", PAGER="cat", NO_COLOR="1", GIT_TERMINAL_PROMPT="0"))


def check_gh_confined(argv: list[str], root: Path, cwd: Path | None = None) -> str | None:
    """`--source` must stay inside the repo."""
    root = root.resolve()
    cwd = (cwd or root).resolve()
    for i, a in enumerate(argv):
        val = None
        if a.startswith("--source="):
            val = a.split("=", 1)[1]
        elif a == "--source" and i + 1 < len(argv):
            val = argv[i + 1]
        if val is not None:
            p = (cwd / val).resolve()
            if not (p == root or root in p.parents):
                return f"--source {val} points outside the repository ({root})"
    return None
