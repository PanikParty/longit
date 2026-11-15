"""The harness (not the agent loop) asks the model to write a commit message from the staged diff."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import safety, tools
from .llm.base import Backend, BackendError

SYSTEM = """You write git commit messages. Reply with ONLY the message, no preamble, quotes or code fences.
Format: a subject line in the imperative mood (e.g. "Add retry to the HTTP client"), at most 72 characters, no
trailing period; then, only if the change is not trivial, a blank line and up to 4 short "- " bullets saying what
changed and why. Describe what the diff actually does. Follow the style of the recent commits if they show one."""

MAX_DIFF_CHARS = 10_000
_NO_MESSAGE_NEEDED = {"-m", "--message", "-F", "--file", "-C", "--reuse-message", "-c", "--reedit-message",
                      "--fixup", "--squash", "--amend", "--no-edit"}


def needs_message(argv: list[str]) -> bool:
    """True for a `git commit` that would otherwise have no message (and not an amend/fixup/reuse)."""
    _, sub, rest = safety.split_global(argv)
    if sub != "commit":
        return False
    for a in rest:
        if a == "--":
            break
        flag = a.split("=", 1)[0]
        if flag in _NO_MESSAGE_NEEDED:
            return False
        if a.startswith("-") and not a.startswith("--") and any(c in a[1:] for c in "mFCc"):
            return False
    return True


def includes_all_tracked(argv: list[str]) -> bool:
    _, _, rest = safety.split_global(argv)
    return any(a == "--all" or (a.startswith("-") and not a.startswith("--") and "a" in a[1:]) for a in rest)


def insert_message(argv: list[str], subject: str, body: str) -> list[str]:
    """Return argv with -m flags placed right after the `commit` subcommand."""
    i = argv.index("commit")
    extra = ["-m", subject] + (["-m", body] if body else [])
    return argv[: i + 1] + extra + argv[i + 1:]


def _diff(cwd: Path, all_tracked: bool) -> str:
    base = ["diff", "--no-color", "--no-ext-diff", "--no-textconv"]
    for args in ([*base, "--staged"], [*base, "HEAD"] if all_tracked else None):
        if args is None:
            continue
        stat = tools.run_git([*args, "--stat"], cwd, timeout=20)
        if stat.exit_code == 0 and stat.output.strip():
            patch = tools.run_git(args, cwd, timeout=20)
            return stat.output.strip() + "\n\n" + tools.truncate(patch.output, MAX_DIFF_CHARS)
    return ""


def clean(text: str, max_body: int = 1000) -> tuple[str, str] | None:
    """Turn raw model output into (subject, body)."""
    t = re.sub(r"^```[a-z]*\n|\n?```$", "", text.strip(), flags=re.M).strip()
    lines = [ln.rstrip() for ln in t.splitlines()]
    while lines and (not lines[0].strip() or re.match(r"^(commit message|message)\s*:?\s*$", lines[0].strip(), re.I)):
        lines.pop(0)
    if not lines:
        return None
    subject = re.sub(r"^(commit message|subject)\s*:\s*", "", lines[0].strip(), flags=re.I)
    subject = subject.strip("`'\" ").lstrip("-* ").rstrip(".").strip()
    if not subject:
        return None
    if len(subject) > 72:
        subject = subject[:72].rsplit(" ", 1)[0].rstrip(" ,;:-") or subject[:72]
    body = "\n".join(lines[1:]).strip().strip("`")[:max_body].strip()
    return subject, body


def generate(backend: Backend, cwd: Path, all_tracked: bool = False) -> tuple[str, str] | None:
    """Ask the model for a commit message. None if there is no diff or the model gave nothing usable.
    Raises BackendError if the backend fails."""
    diff = _diff(cwd, all_tracked)
    if not diff:
        return None
    log = tools.run_git(["log", "--oneline", "-5"], cwd, timeout=10)
    recent = log.output.strip() if log.exit_code == 0 else ""
    user = (f"Recent commits (for style):\n{recent or '(none)'}\n\nChanges to commit:\n{diff}")
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    turn = backend.chat(messages, [])
    return clean(turn.text) if turn.text else None


# ---- pull request title + description (gh pr create) ---------------------------------------------------
PR_SYSTEM = """You write GitHub pull request titles and descriptions. Reply with ONLY the text, no preamble or code fences.
First line: the title, imperative mood, at most 72 characters, no trailing period. Then a blank line and the description in
Markdown: a one or two sentence summary of what the branch does and why, then a short bullet list of the notable changes.
Describe only what the commits and diff actually show; do not invent tests, issues or motivations."""

_PR_FLAGS_WITH_VALUE = {"--title": "title", "-t": "title", "--body": "body", "-b": "body"}


def _pr_args(argv: list[str]) -> dict[str, str]:
    """Pull --title/--body/--base values (any spelling) out of a `pr create` argv."""
    found: dict[str, str] = {}
    rest = argv[2:]
    for i, a in enumerate(rest):
        flag, eq, val = a.partition("=")
        key = _PR_FLAGS_WITH_VALUE.get(flag) or ("base" if flag in ("--base", "-B") else None)
        if key:
            if eq:
                found[key] = val
            elif i + 1 < len(rest):
                found[key] = rest[i + 1]
    return found


def needs_pr_text(argv: list[str]) -> bool:
    """`gh pr create` that is missing a title or body (and is not --fill)."""
    if argv[:2] != ["pr", "create"] or "--fill" in argv:
        return False
    given = _pr_args(argv)
    return "title" not in given or "body" not in given


def _base_ref(cwd: Path, name: str | None) -> str | None:
    candidates = []
    if name:
        candidates += [f"origin/{name}", name]
    else:
        head = tools.run_git(["symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"], cwd, timeout=10)
        if head.exit_code == 0 and head.output.strip():
            candidates.append(head.output.strip())
        candidates += ["origin/main", "origin/master", "main", "master"]
    for ref in candidates:
        if tools.run_git(["rev-parse", "--verify", "-q", ref], cwd, timeout=10).exit_code == 0:
            return ref
    return None


def generate_pr_text(backend: Backend, cwd: Path, argv: list[str]) -> tuple[str, str] | None:
    """Title and body for the branch checked out in `cwd`, from its commits and diff against the base.
    None if there is nothing ahead of the base. Raises BackendError if the backend fails."""
    base = _base_ref(cwd, _pr_args(argv).get("base"))
    if base is None:
        return None
    log = tools.run_git(["log", "--no-color", "--format=%s%n%b%n--", f"{base}..HEAD"], cwd, timeout=20)
    if log.exit_code != 0 or not log.output.strip():
        return None
    diff_args = ["diff", "--no-color", "--no-ext-diff", "--no-textconv", f"{base}...HEAD"]
    stat = tools.run_git([*diff_args, "--stat"], cwd, timeout=20)
    patch = tools.run_git(diff_args, cwd, timeout=20)
    user = (f"Commits on this branch (base {base}):\n{log.output.strip()}\n\nFiles changed:\n{stat.output.strip()}\n\n"
            f"Diff:\n{tools.truncate(patch.output, MAX_DIFF_CHARS)}")
    messages: list[dict[str, Any]] = [{"role": "system", "content": PR_SYSTEM}, {"role": "user", "content": user}]
    turn = backend.chat(messages, [])
    return clean(turn.text, max_body=3000) if turn.text else None


def insert_pr_text(argv: list[str], title: str, body: str) -> list[str]:
    """Add the generated --title/--body, keeping any the caller already supplied."""
    given = _pr_args(argv)
    extra = (["--title", title] if "title" not in given else []) + (["--body", body or title] if "body" not in given else [])
    return argv[:2] + extra + argv[2:]
