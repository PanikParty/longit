"""Scripted skills: fixed command sequences that the harness runs itself.

A skill whose frontmatter says `workflow: NAME` is not handed to the model. The harness walks the steps below in a
fixed order. The model is used only to write text (a branch slug, a commit message, a pull request description), the
same separate tool-less calls used elsewhere. Every command still goes through `Agent.run_tool`, so classification,
confinement, the current mode, session approvals and the approval prompts apply exactly as they do to the model's own
tool calls. Workflows are registered here, in code: a skill file can only name one, never contain one.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from . import commitmsg, safety, tools
from .llm.base import BackendError

if TYPE_CHECKING:  # pragma: no cover
    from .agent import Agent


@dataclass
class StepResult:
    ok: bool        # exit code 0
    status: str     # "ok" | "failed" | "refused" (blocked, declined, skipped by mode)
    output: str     # command output without the "exit code" header
    content: str    # exactly what a model would have received


# ---- helpers (harness-internal reads; their output is never shown to a model) ---------------------------
def changed_paths(cwd: Path) -> list[str]:
    """Every modified, staged, deleted and untracked (not ignored) path."""
    r = tools.run_git(["status", "--porcelain=v1", "-uall", "-z"], cwd, timeout=30, max_chars=None)
    if r.exit_code != 0:
        return []
    parts, paths, i = r.output.split("\0"), [], 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        if "R" in entry[:2] or "C" in entry[:2]:
            i += 1  # the next field is the original path
        paths.append(entry[3:])
    return paths


_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
_SAFE_ENV_SUFFIXES = (".example", ".sample", ".template", ".dist")


def secret_like(path: str) -> bool:
    base = path.rsplit("/", 1)[-1].lower()
    if base == ".env" or (base.startswith(".env.") and not base.endswith(_SAFE_ENV_SUFFIXES)):
        return True
    return (base.endswith(_SECRET_SUFFIXES) or base.startswith("credentials")
            or base in ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"))


def branch_free(cwd: Path, name: str) -> bool:
    r = tools.run_git(["for-each-ref", "--format=%(refname)", f"refs/heads/{name}", f"refs/remotes/origin/{name}"],
                      cwd, timeout=15)
    return r.exit_code == 0 and not r.output.strip()


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


def _stopped(r: StepResult, command: str, done: list[str]) -> str:
    if r.content.startswith("denied"):
        why = f"You declined `{command}`, so I stopped there."
    elif r.status == "refused":
        why = f"`{command}` was refused: {_first_line(r.content)}"
    else:
        why = f"`{command}` failed: {_first_line(r.output) or 'no output'}."
    return why + (" Done so far: " + "; ".join(done) + "." if done else " Nothing was changed.")


# ---- the pr-branch workflow ---------------------------------------------------------------------------
def pr_branch(a: "Agent") -> str:
    """All changes -> new pr/<slug> branch -> commit -> push to origin -> pull request. Returns the report."""
    label, n, done = "workflow pr-branch", 0, []

    def step(argv: list[str], tool: str = "git") -> StepResult:
        nonlocal n
        n += 1
        return a.run_tool(tool, argv, f"{label} - step {n}")

    if not step(["status", "--short", "--branch"]).ok:
        return "This isn't a git repository, so there is nothing to put on a branch."
    paths = changed_paths(a.cwd)
    if not paths:
        return "Nothing to commit: the working tree has no changes."
    if not step(["remote", "get-url", "origin"]).ok:
        return "There is no `origin` remote, so I can't push. Add one (or ask me to create a GitHub repository) and try again."

    slug = None
    try:
        slug = commitmsg.generate_branch_slug(a.harness_backend, a.cwd, paths)
    except BackendError as e:
        a.ui.notice(f"could not name the branch with the model ({e}); using a timestamp", error=True)
    base = "pr/" + (slug or time.strftime("changes-%Y%m%d-%H%M"))
    name, k = base, 2
    while not branch_free(a.cwd, name) and k < 50:
        name, k = f"{base}-{k}", k + 1
    a._trace("harness", "separate model call: branch name", name)

    secrets = [p for p in paths if secret_like(p)]
    add = ["add", "-A"] + (["--", "."] + [f":(exclude,literal){p}" for p in secrets] if secrets else [])
    switch = ["switch", "-c", name]
    if a.mode.value == "plan":
        return ("Plan (nothing was changed):\n"
                f"1. {safety.display(switch)}\n2. {safety.display(add)}\n"
                "3. git commit  (message written by the model from the staged diff)\n"
                f"4. git push -u origin {name}\n5. gh pr create  (title and description written by the model from the commits)"
                + (f"\nLeaving out as secret-looking: {', '.join(secrets)}" if secrets else ""))

    r = step(switch)
    if not r.ok:
        return _stopped(r, safety.display(switch), done)
    done.append(f"created branch `{name}`")

    r = step(add)
    if not r.ok:
        return _stopped(r, safety.display(add), done)
    done.append("staged all changes" + (f" except {', '.join(secrets)}" if secrets else ""))
    if not tools.run_git(["diff", "--cached", "--name-only"], a.cwd, timeout=20, max_chars=None).output.strip():
        return f"Created branch `{name}`, but there was nothing to stage" + (
            f" (only secret-looking files changed: {', '.join(secrets)})." if secrets else ".")

    r = step(["commit"])
    if not r.ok:
        return _stopped(r, "git commit", done)
    subject = tools.run_git(["log", "-1", "--format=%s"], a.cwd, timeout=10).output.strip()
    done.append(f'committed "{subject}"')

    push = ["push", "-u", "origin", name]
    r = step(push)
    if not r.ok:
        return _stopped(r, safety.display(push), done)
    done.append("pushed to origin")

    r = step(["pr", "create"], tool="gh")
    left_out = f" Left out as secret-looking: {', '.join(secrets)}." if secrets else ""
    if not r.ok:
        return (f"Branch `{name}` is committed (\"{subject}\") and pushed to origin, but opening the pull request "
                + ("was declined." if r.content.startswith("denied") else f"failed: {_first_line(r.output) or r.status}.")
                + left_out)
    url = next((m.group(0) for m in re.finditer(r"https://\S+", r.output)), "")
    return (f"Opened a pull request{(': ' + url) if url else ''}\n"
            f"Branch `{name}`, one commit (\"{subject}\"), pushed to origin.{left_out}")


# ---- the rebase-pr workflow --------------------------------------------------------------------------
_PROTECTED_BRANCHES = {"main", "master", "develop", "trunk"}


def rebase_pr(a: "Agent") -> str:
    """Bring the checked-out PR branch up to date with its base branch by rebasing, then push it.

    When: only if the base has commits the branch lacks (behind > 0); otherwise there is nothing to do.
    Never on a protected branch, never over uncommitted work, and a conflicted rebase is aborted rather than
    left half-done. The push is `--force-with-lease` (DESTRUCTIVE, so it always asks) because a rebase rewrites
    the branch's commits; a branch that was never pushed gets a plain `push -u`."""
    label, n, done = "workflow rebase-pr", 0, []

    def step(argv: list[str], tool: str = "git") -> StepResult:
        nonlocal n
        n += 1
        return a.run_tool(tool, argv, f"{label} - step {n}")

    def q(*argv: str) -> "tools.RunResult":  # quiet harness-internal read
        return tools.run_git(list(argv), a.cwd, timeout=30, max_chars=None)

    def count(rng: str) -> int:
        out = q("rev-list", "--count", rng).output.strip()
        return int(out) if out.isdigit() else 0

    if not step(["status", "--short", "--branch"]).ok:
        return "This isn't a git repository, so there is nothing to rebase."
    branch = q("branch", "--show-current").output.strip()
    if not branch:
        return "HEAD is detached, so there is no branch to rebase. Switch to your PR branch first."
    if branch in _PROTECTED_BRANCHES:
        return (f"You're on `{branch}`. A rebase updates a feature or PR branch, never `{branch}` itself; "
                "switch to the PR branch first.")
    if q("status", "--porcelain", "--untracked-files=no").output.strip():
        return "There are uncommitted changes. Commit or stash them first so the rebase can't disturb them."
    if not step(["remote", "get-url", "origin"]).ok:
        return "There is no `origin` remote, so there is nothing to rebase onto or push to."

    base = None
    r = step(["pr", "view", "--json", "baseRefName", "--jq", ".baseRefName"], tool="gh")   # the PR's own base, if any
    if r.ok and r.output.strip():
        base = r.output.strip().splitlines()[-1].strip()
    if not base:
        head = q("symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD").output.strip()
        base = head.removeprefix("origin/") if head else None
    if not base:
        base = next((c for c in ("main", "master") if q("rev-parse", "--verify", "-q", f"origin/{c}").exit_code == 0), None)
    if not base:
        return "I couldn't work out the base branch (no pull request for this branch and no origin/main or origin/master)."
    target = f"origin/{base}"

    plan = a.mode.value == "plan"
    if not plan:
        r = step(["fetch", "origin"])
        if not r.ok:
            return _stopped(r, "git fetch origin", done)
    if q("rev-parse", "--verify", "-q", target).exit_code != 0:
        return f"`{target}` doesn't exist, so there is nothing to rebase onto."
    behind, ahead = count(f"HEAD..{target}"), count(f"{target}..HEAD")
    if behind == 0:
        return (f"`{branch}` is already up to date with `{target}` ({ahead} commit(s) ahead of it), "
                "so no rebase is needed." + (" (As of the last fetch; plan mode doesn't fetch.)" if plan else ""))

    pushed = q("rev-parse", "--verify", "-q", f"origin/{branch}").exit_code == 0
    push = (["push", "--force-with-lease", "origin", branch] if pushed else ["push", "-u", "origin", branch])
    if plan:
        return ("Plan (nothing was changed; counts are from the last fetch):\n"
                f"1. git fetch origin\n2. git rebase {target}  (`{branch}` is {behind} commit(s) behind it, {ahead} ahead)\n"
                "3. if the rebase conflicts: git rebase --abort, and tell you which files conflict\n"
                f"4. {safety.display(push)}" + ("  (a force-push: it will ask you)" if pushed else ""))

    r = step(["rebase", target])
    if not r.ok:
        conflicts = [p for p in q("diff", "--name-only", "--diff-filter=U").output.splitlines() if p]
        aborted = step(["rebase", "--abort"]).ok
        return (f"Rebasing `{branch}` onto `{target}` hit conflicts in {', '.join(conflicts) or 'some files'}. "
                + ("I aborted the rebase, so the branch is exactly as it was. " if aborted else
                   "I could not abort it automatically: run `git rebase --abort`. ")
                + f"To resolve them yourself: `git rebase {target}`, fix the files, `git add` them, "
                  "`git rebase --continue`, then ask me to push.")
    done.append(f"rebased {ahead} commit(s) onto {target} ({behind} new upstream commit(s))")

    r = step(push)
    if not r.ok:
        return _stopped(r, safety.display(push), done) + " The rebased branch is local only; the pull request on GitHub is unchanged."
    r = step(["pr", "view", "--json", "url", "--jq", ".url"], tool="gh")
    url = next((m.group(0) for m in re.finditer(r"https://\S+", r.output)), "") if r.ok else ""
    return (f"Rebased `{branch}` onto `{target}` ({ahead} commit(s) replayed over {behind} new upstream commit(s)) and "
            + ("force-pushed with --force-with-lease." if pushed else "pushed it.")
            + (f"\nPull request: {url}" if url else ""))


REGISTRY: dict[str, Callable[["Agent"], str]] = {"pr-branch": pr_branch, "rebase-pr": rebase_pr}
