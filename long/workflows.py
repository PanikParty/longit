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


# ---- the check-changes workflow ----------------------------------------------------------------------
_CONFLICT_CODES = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}
_IN_PROGRESS = (("rebase-merge", "a rebase"), ("rebase-apply", "a rebase"), ("MERGE_HEAD", "a merge"),
                ("CHERRY_PICK_HEAD", "a cherry-pick"), ("REVERT_HEAD", "a revert"), ("BISECT_LOG", "a bisect"))
_LIST_LIMIT = 8


@dataclass
class TreeState:
    staged: list[tuple[str, str]]     # (status letter, path)
    unstaged: list[tuple[str, str]]
    untracked: list[str]
    conflicted: list[str]

    @property
    def dirty(self) -> bool:
        return bool(self.staged or self.unstaged or self.untracked or self.conflicted)


def tree_state(cwd: Path) -> TreeState:
    """Parse `git status --porcelain=v1 -z` into staged / unstaged / untracked / conflicted."""
    r = tools.run_git(["status", "--porcelain=v1", "-uall", "-z"], cwd, timeout=30, max_chars=None)
    st = TreeState([], [], [], [])
    parts, i = (r.output.split("\0") if r.exit_code == 0 else []), 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if "R" in xy or "C" in xy:
            i += 1  # original path
        if xy == "??":
            st.untracked.append(path)
        elif xy in _CONFLICT_CODES:
            st.conflicted.append(path)
        else:
            if xy[0] not in " ?!":
                st.staged.append((xy[0], path))
            if xy[1] not in " ?!":
                st.unstaged.append((xy[1], path))
    return st


def _listing(title: str, items: list[str]) -> list[str]:
    out = [f"{title} ({len(items)}):"] + [f"    {it}" for it in items[:_LIST_LIMIT]]
    if len(items) > _LIST_LIMIT:
        out.append(f"    ... and {len(items) - _LIST_LIMIT} more")
    return out


def check_changes(a: "Agent") -> str:
    """Answer "is there anything uncommitted/changed here?" with read-only commands only."""
    label, n = "workflow check-changes", 0

    def step(argv: list[str]) -> StepResult:
        nonlocal n
        n += 1
        return a.run_tool("git", argv, f"{label} - step {n}")

    def q(*argv: str) -> "tools.RunResult":
        return tools.run_git(list(argv), a.cwd, timeout=30, max_chars=None)

    if not step(["status", "--short", "--branch"]).ok:
        return "This isn't a git repository, so there is nothing to check."
    st = tree_state(a.cwd)
    branch = q("branch", "--show-current").output.strip()
    where = f"`{branch}`" if branch else "the detached HEAD"
    stashes = len([ln for ln in step(["stash", "list"]).output.splitlines() if ln.strip()])

    lines = ([f"Yes: {where} has uncommitted changes."] if st.dirty
             else [f"No: nothing is uncommitted, the working tree on {where} is clean."])
    if st.conflicted:
        lines += _listing("Conflicted", st.conflicted)
    if st.staged:
        lines += _listing("Staged, ready to commit", [f"{c} {p}" for c, p in st.staged])
    if st.unstaged:
        lines += _listing("Changed but not staged", [f"{c} {p}" for c, p in st.unstaged])
    if st.untracked:
        lines += _listing("Untracked", st.untracked)

    gitdir = q("rev-parse", "--git-dir").output.strip()
    ops = sorted({label_ for name, label_ in _IN_PROGRESS if gitdir and (a.cwd / gitdir / name).exists()})
    if ops:
        lines.append(f"You're in the middle of {' and '.join(ops)}.")
    upstream = q("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if branch and upstream.exit_code == 0:
        counts = q("rev-list", "--left-right", "--count", "@{u}...HEAD").output.split()
        behind, ahead = (int(counts[0]), int(counts[1])) if len(counts) == 2 else (0, 0)
        up = upstream.output.strip()
        if ahead or behind:
            bits = ([f"{ahead} commit(s) ahead of {up} (not pushed)"] if ahead else []) + (
                [f"{behind} behind it"] if behind else [])
            lines.append("Also: " + ", ".join(bits) + ".")
        else:
            lines.append(f"Up to date with {up}.")
    elif branch:
        lines.append(f"`{branch}` has no upstream branch set.")
    if stashes:
        lines.append(f"There {'is' if stashes == 1 else 'are'} {stashes} stash{'' if stashes == 1 else 'es'}.")
    return "\n".join(lines)


# ---- the merged-branches workflow ---------------------------------------------------------------------
_BRANCH_LIST_LIMIT = 15
_PR_LIST_LIMIT = 100
_PR_LIST_LIMIT_OLD_GH = 25


def _default_base(q: Callable[..., "tools.RunResult"], has_origin: bool) -> tuple[str | None, str | None]:
    """(base branch name, base ref) for 'merged into what?': origin/HEAD, else origin or local main/master."""
    if has_origin:
        head = q("symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD").output.strip()
        if head.startswith("origin/") and q("rev-parse", "--verify", "-q", head).exit_code == 0:
            return head.removeprefix("origin/"), head
        for c in ("main", "master"):
            if q("rev-parse", "--verify", "-q", f"origin/{c}").exit_code == 0:
                return c, f"origin/{c}"
    for c in ("main", "master"):
        if q("rev-parse", "--verify", "-q", f"refs/heads/{c}").exit_code == 0:
            return c, c
    return None, None


@dataclass
class BranchInfo:
    name: str
    where: str            # "local + origin" | "local only" | "origin only"
    merged: bool          # every ref of this name is in the base's history or is the exact head of a merged PR
    via_pr: str = ""      # "PR #N" when merged only through its pull request (squash/rebase merge)
    ahead: int = 0        # commits the base lacks (unmerged branches)
    open_pr: str = ""     # number of an open PR from this branch
    tail_pr: str = ""     # "PR #N was merged but this branch has newer commits"
    local_by_ancestry: bool = True   # local tip is in the base's history, so `git branch -d` accepts it
    has_local: bool = False
    has_origin: bool = False


@dataclass
class BranchScan:
    base: str
    base_ref: str
    current: str
    branches: list[BranchInfo]
    worktree_branches: set[str]
    notes: list[str]


def scan_branches(a: "Agent", label: str) -> "BranchScan | str":
    """Fetch (unless plan mode), then classify every local and origin branch against the base branch.
    Returns a BranchScan, or a ready-made explanation string when it can't proceed."""
    n = 0
    notes: list[str] = []

    def step(argv: list[str], tool: str = "git") -> StepResult:
        nonlocal n
        n += 1
        return a.run_tool(tool, argv, f"{label} - step {n}")

    def q(*argv: str) -> "tools.RunResult":
        return tools.run_git(list(argv), a.cwd, timeout=30, max_chars=None)

    if not step(["status", "--short", "--branch"]).ok:
        return "This isn't a git repository, so there are no branches to check."
    has_origin = step(["remote", "get-url", "origin"]).ok
    if has_origin:
        if a.mode.value == "plan":
            notes.append("plan mode didn't fetch, so remote information is as of the last fetch")
        else:
            r = step(["fetch", "--prune", "origin"])
            if not r.ok:
                notes.append(("you declined the fetch" if r.content.startswith("denied") else
                              f"the fetch failed ({_first_line(r.output) or r.status})")
                             + ", so remote information may be out of date")
    base, base_ref = _default_base(q, has_origin)
    if base is None or base_ref is None:
        return "I couldn't work out which branch to compare against (no main or master, local or on origin)."

    refs: dict[str, dict[str, str]] = {}
    for line in q("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/remotes/origin").output.splitlines():
        ref, _, sha = line.partition(" ")
        if ref.startswith("refs/heads/"):
            refs.setdefault(ref.removeprefix("refs/heads/"), {})["local"] = sha
        elif ref.startswith("refs/remotes/origin/") and ref != "refs/remotes/origin/HEAD":
            refs.setdefault(ref.removeprefix("refs/remotes/origin/"), {})["origin"] = sha
    refs.pop(base, None)
    current = q("branch", "--show-current").output.strip()
    worktree_branches = {ln.split("refs/heads/", 1)[1].strip() for ln in q("worktree", "list", "--porcelain").output.splitlines()
                         if ln.startswith("branch refs/heads/")}

    prs: list[tuple[str, str, str, str]] = []   # (number, state, head branch, head commit)
    gh: StepResult | None = None
    limited_to = 0
    if has_origin:
        # `headRefOid` is missing from older gh releases (e.g. 2.15). There the last of `commits` is the head commit,
        # but that query hits GitHub's node limit above ~30 PRs, so the fallback reads fewer.
        for fields, oid, limit in (("number,state,headRefName,headRefOid", ".headRefOid", _PR_LIST_LIMIT),
                                   ("number,state,headRefName,commits", ".commits[-1].oid", _PR_LIST_LIMIT_OLD_GH)):
            gh = step(["pr", "list", "--state", "all", "--limit", str(limit), "--json", fields, "--jq",
                       '.[] | "\\(.number) \\(.state) \\(.headRefName) \\(' + oid + ')"'], tool="gh")
            if gh.ok or "Unknown JSON field" not in gh.output:
                limited_to = limit if "commits" in fields else 0
                break
    if gh is None or not gh.ok:
        notes.append("GitHub wasn't checked, so squash- or rebase-merged branches show as not merged"
                     if has_origin else "there is no origin, so only local branches were compared")
    else:
        for ln in gh.output.splitlines():
            parts = ln.split()
            if len(parts) == 4:
                prs.append((parts[0], parts[1], parts[2], parts[3]))
        if "characters omitted" in gh.output:
            notes.append("the pull request list was too long to read in full")
        if limited_to and len(prs) >= limited_to:
            notes.append(f"this gh version is old, so only the {limited_to} most recent pull requests were checked")

    infos: list[BranchInfo] = []
    for name in sorted(refs):
        where = refs[name]
        info = BranchInfo(name, "local + origin" if len(where) == 2 else ("local only" if "local" in where else "origin only"),
                          merged=True, has_local="local" in where, has_origin="origin" in where)
        for kind, sha in where.items():
            by_ancestry = q("merge-base", "--is-ancestor", sha, base_ref).exit_code == 0
            if kind == "local":
                info.local_by_ancestry = by_ancestry
            if by_ancestry:
                continue
            hit = next((p for p in prs if p[2] == name and p[1] == "MERGED" and p[3] == sha), None)
            if hit:
                info.via_pr = info.via_pr or f"PR #{hit[0]}"
                continue
            info.merged = False
            cnt = q("rev-list", "--count", f"{base_ref}..{sha}").output.strip()
            info.ahead = max(info.ahead, int(cnt) if cnt.isdigit() else 0)
            info.tail_pr = info.tail_pr or next((f"PR #{p[0]} was merged but this branch has newer commits"
                                                 for p in prs if p[2] == name and p[1] == "MERGED"), "")
        info.open_pr = next((p[0] for p in prs if p[2] == name and p[1] == "OPEN"), "")
        infos.append(info)
    return BranchScan(base, base_ref, current, infos, worktree_branches, notes)


def _capped(items: list[str], limit: int = _BRANCH_LIST_LIMIT) -> str:
    return ", ".join(items[:limit]) + (f", ... and {len(items) - limit} more" if len(items) > limit else "")


def merged_branches(a: "Agent") -> str:
    """Answer "have all the branches been merged?" for every local and origin branch, read-only apart from an
    optional `git fetch --prune origin` (asks like any change). A branch counts as merged if its tip is in the base
    branch's history, or if GitHub shows a merged PR from that branch whose head is exactly that tip (which catches
    squash and rebase merges, where the commits themselves never reach the base)."""
    scan = scan_branches(a, "workflow merged-branches")
    if isinstance(scan, str):
        return scan
    merged = [b.name + (f" ({b.via_pr})" if b.via_pr else "") + (" (current)" if b.name == scan.current else "")
              for b in scan.branches if b.merged]
    unmerged = [(b.name + (" (current)" if b.name == scan.current else ""),
                 ", ".join([b.where, f"{b.ahead} commit(s) not in {scan.base_ref}",
                            f"open PR #{b.open_pr}" if b.open_pr else (b.tail_pr or "no PR")]))
                for b in scan.branches if not b.merged]
    total = len(merged) + len(unmerged)
    if total == 0:
        lines = [f"There are no other branches besides `{scan.base}`."]
    elif not unmerged:
        lines = [f"Yes: all {total} other branch(es) are merged into {scan.base_ref}.", f"Merged: {_capped(merged)}"]
    else:
        lines = [f"No: {len(unmerged)} of {total} branch(es) are not merged into {scan.base_ref}.", "Not merged:"]
        lines += [f"    {name}  ({d})" for name, d in unmerged[:_BRANCH_LIST_LIMIT]]
        if len(unmerged) > _BRANCH_LIST_LIMIT:
            lines.append(f"    ... and {len(unmerged) - _BRANCH_LIST_LIMIT} more")
        if merged:
            lines.append(f"Merged ({len(merged)}): {_capped(merged)}")
    return "\n".join(lines + [f"Note: {nt}." for nt in scan.notes])


_NEVER_DELETE = {"main", "master", "develop", "trunk", "dev", "staging", "production", "prod"}


def deletable_branches(a: "Agent") -> str:
    """List the branches that are safe to delete, and why the others are not. Deletes nothing.

    Safe means: merged into the base (by history, or by the exact head of a merged PR), not the base itself or a
    conventional long-lived branch, not checked out here or in another worktree, and not the head of an open PR."""
    scan = scan_branches(a, "workflow deletable-branches")
    if isinstance(scan, str):
        return scan
    safe: list[BranchInfo] = []
    keep: list[tuple[str, str]] = []
    blocked: list[tuple[str, str]] = []
    for b in scan.branches:
        if b.name in _NEVER_DELETE or b.name.startswith(("release/", "release-")):
            blocked.append((b.name, "long-lived branch"))
        elif b.name == scan.current:
            blocked.append((b.name, "checked out here"))
        elif b.name in scan.worktree_branches:
            blocked.append((b.name, "checked out in another worktree"))
        elif b.open_pr:
            keep.append((b.name, f"open PR #{b.open_pr}"))
        elif not b.merged:
            keep.append((b.name, f"not merged: {b.where}, {b.ahead} commit(s) not in {scan.base_ref}, "
                                 + (b.tail_pr or "no PR")))
        else:
            safe.append(b)

    lines: list[str] = []
    if safe:
        lines.append(f"You can delete {len(safe)} branch(es); each is merged into {scan.base_ref}:")
        for b in safe:
            how = f" ({b.via_pr})" if b.via_pr else ""
            force = "  needs -D: squash/rebase-merged" if b.has_local and not b.local_by_ancestry else ""
            lines.append(f"    {b.name}{how}  [{b.where}]{force}")
        local_d = [b.name for b in safe if b.has_local and b.local_by_ancestry]
        local_D = [b.name for b in safe if b.has_local and not b.local_by_ancestry]
        remote = [b.name for b in safe if b.has_origin]
        lines.append("To delete them (nothing was deleted):")
        if local_d:
            lines.append("    git branch -d " + " ".join(local_d))
        if local_D:
            lines.append("    git branch -D " + " ".join(local_D))
        if remote:
            lines.append("    git push origin --delete " + " ".join(remote))
    else:
        lines.append("Nothing is safe to delete right now.")
    if keep:
        lines.append(f"Keep ({len(keep)}):")
        lines += [f"    {name}  ({why})" for name, why in keep[:_BRANCH_LIST_LIMIT]]
        if len(keep) > _BRANCH_LIST_LIMIT:
            lines.append(f"    ... and {len(keep) - _BRANCH_LIST_LIMIT} more")
    if blocked:
        lines.append("Can't delete: " + "; ".join(f"{name} ({why})" for name, why in blocked) + ".")
    lines.append(f"Compared with {scan.base_ref}.")
    return "\n".join(lines + [f"Note: {nt}." for nt in scan.notes])


REGISTRY: dict[str, Callable[["Agent"], str]] = {"pr-branch": pr_branch, "rebase-pr": rebase_pr,
                                                 "check-changes": check_changes, "merged-branches": merged_branches,
                                                 "deletable-branches": deletable_branches}
