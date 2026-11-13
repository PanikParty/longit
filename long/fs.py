"""Read-only filesystem tools: ls, pwd, cd. Native Python (no subprocess), confined to the project root."""
from __future__ import annotations

import json
import stat
import time
from pathlib import Path
from typing import Any

from .tools import RunResult

MAX_ENTRIES = 500

LS_TOOL = {"type": "function", "function": {
    "name": "ls",
    "description": ("List a directory (default: the current directory). Directories end with '/'. "
                    "Set all=true to include dotfiles, long=true for mode, size and modified time. "
                    "Only paths inside the project are allowed."),
    "parameters": {"type": "object", "properties": {
        "path": {"type": "string", "description": "directory or file, relative to the current directory"},
        "all": {"type": "boolean"}, "long": {"type": "boolean"}}}}}
PWD_TOOL = {"type": "function", "function": {
    "name": "pwd", "description": "Print the current working directory.",
    "parameters": {"type": "object", "properties": {}}}}
CD_TOOL = {"type": "function", "function": {
    "name": "cd",
    "description": ("Change the current working directory used by git, gh and ls for the rest of the session. "
                    "Moving inside the project is automatic; leaving it asks the user for approval."),
    "parameters": {"type": "object", "properties": {
        "path": {"type": "string", "description": "target directory (relative, absolute or ~)"}},
        "required": ["path"]}}}
TOOLS = [LS_TOOL, PWD_TOOL, CD_TOOL]
NAMES = {"ls", "pwd", "cd"}


def parse_args(raw: str) -> dict[str, Any]:
    try:
        obj = json.loads(raw) if raw and raw.strip() else {}
    except json.JSONDecodeError as e:
        raise ValueError(f"arguments were not valid JSON ({e})") from e
    if not isinstance(obj, dict):
        raise ValueError("arguments must be a JSON object")
    return obj


def resolve(cwd: Path, value: str) -> Path:
    return (cwd / Path(value).expanduser()).resolve()


def inside(path: Path, root: Path) -> bool:
    root = root.resolve()
    return path == root or root in path.parents


def display_ls(args: dict[str, Any]) -> str:
    flag = ("a" if args.get("all") else "") + ("l" if args.get("long") else "")
    parts = ["ls"] + ([f"-{flag}"] if flag else []) + ([str(args["path"])] if args.get("path") else [])
    return " ".join(parts)


def ls(cwd: Path, root: Path, args: dict[str, Any]) -> RunResult:
    target = resolve(cwd, str(args.get("path") or "."))
    if not inside(target, root):
        return RunResult(1, f"{target} is outside the project ({root}); use cd (the user must approve) first")
    if not target.exists():
        return RunResult(2, f"{target}: no such file or directory")
    if target.is_file():
        entries = [target]
    else:
        try:
            entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            return RunResult(1, f"{target}: permission denied")
    if not args.get("all"):
        entries = [e for e in entries if not e.name.startswith(".")]
    omitted = max(0, len(entries) - MAX_ENTRIES)
    lines = []
    for e in entries[:MAX_ENTRIES]:
        try:
            st = e.lstat()
        except OSError:
            continue
        is_dir = stat.S_ISDIR(st.st_mode)
        name = e.name + ("/" if is_dir else "")
        if stat.S_ISLNK(st.st_mode):
            name += f" -> {e.readlink()}"
        if args.get("long"):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
            name = f"{stat.filemode(st.st_mode)} {st.st_size:>9} {when} {name}"
        lines.append(name)
    if omitted:
        lines.append(f"... {omitted} more entries omitted")
    return RunResult(0, "\n".join(lines))
