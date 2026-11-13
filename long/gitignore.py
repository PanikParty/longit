"""Before staging everything, notice a missing .gitignore and offer to create one."""
from __future__ import annotations

from pathlib import Path

from . import safety, tools

_JUNK_DIRS = {".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
              ".ruff_cache", ".idea"}
_JUNK_FILES = {".DS_Store", ".env"}
_MAX_LISTED = 50_000  # characters of `ls-files` output we are willing to scan


def stages_everything(argv: list[str]) -> bool:
    """True for `git add -A`, `--all`, `.` and similar (global options already stripped by the caller)."""
    _, sub, rest = safety.split_global(argv)
    if sub != "add":
        return False
    if "--" in rest:
        rest = rest[rest.index("--") + 1:] + [a for a in rest[:rest.index("--")]]
    return any(a in ("--all", ".", ":/", ":/.", "*") or (a.startswith("-") and not a.startswith("--") and "A" in a[1:])
              for a in rest)


def toplevel(cwd: Path) -> Path | None:
    return tools.repo_root(cwd)


def junk_patterns(top: Path) -> list[str]:
    """Patterns for untracked, not-yet-ignored generated/secret files that `git add -A` would stage."""
    r = tools.run_git(["ls-files", "--others", "--exclude-standard"], top, timeout=20)
    if r.exit_code != 0:
        return []
    found: dict[str, None] = {}
    for line in r.output[:_MAX_LISTED].splitlines():
        parts = line.split("/")
        for comp in parts[:-1]:
            if comp in _JUNK_DIRS:
                found[comp + "/"] = None
            elif comp.endswith(".egg-info"):
                found["*.egg-info/"] = None
        name = parts[-1]
        if name in _JUNK_FILES or name.startswith(".env."):
            found[name if name in _JUNK_FILES else ".env.*"] = None
        elif name.endswith(".pyc") and "__pycache__" not in parts:
            found["*.pyc"] = None
    return list(found)


def check_missing(cwd: Path, root: Path) -> tuple[Path, list[str]] | None:
    """(toplevel, patterns) if there is no .gitignore and something worth ignoring is untracked."""
    top = toplevel(cwd)
    if top is None or (top / ".gitignore").exists():
        return None
    try:
        top.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    patterns = junk_patterns(top)
    return (top, patterns) if patterns else None


def write(top: Path, patterns: list[str]) -> None:
    (top / ".gitignore").write_text("\n".join(patterns) + "\n")
