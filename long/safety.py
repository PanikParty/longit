"""Classify a git argv into a risk tier. Pure functions, no I/O."""
from __future__ import annotations

import enum
from dataclasses import dataclass


class Risk(enum.IntEnum):
    READ = 0
    MUTATE = 1
    DESTRUCTIVE = 2
    BLOCKED = 3


@dataclass(frozen=True)
class Verdict:
    risk: Risk
    subcommand: str
    reason: str = ""


# Global options that take a separate value argument.
_GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
_BLOCKED_GLOBAL = {"-c", "--config-env", "--exec-path"}

_READ_SUBCOMMANDS = {
    "status", "log", "diff", "show", "blame", "shortlog", "describe", "rev-parse", "rev-list",
    "ls-files", "ls-tree", "ls-remote", "cat-file", "grep", "reflog", "whatchanged", "name-rev",
    "merge-base", "diff-tree", "diff-files", "diff-index", "count-objects", "fsck", "show-ref",
    "show-branch", "cherry", "range-diff", "version", "help", "check-ignore", "for-each-ref",
    "verify-commit", "verify-tag", "var", "annotate",
}
_MUTATE_SUBCOMMANDS = {
    "add", "commit", "checkout", "switch", "restore", "merge", "rebase", "cherry-pick", "revert",
    "pull", "fetch", "push", "stash", "tag", "branch", "remote", "config", "mv", "rm", "init",
    "clone", "submodule", "worktree", "apply", "am", "bisect", "notes", "gc", "prune", "reset",
    "clean", "update-ref", "symbolic-ref", "filter-branch", "format-patch", "archive", "bundle",
    "sparse-checkout", "maintenance", "pack-refs", "repack", "commit-tree", "hash-object",
    "read-tree", "write-tree", "update-index", "replace", "lfs",
}
# Flags that make an otherwise read-only command write files or spawn programs.
_PROMOTE_FLAGS = ("--output", "-O", "--ext-diff", "--textconv", "--open-files-in-pager", "--exec")
_BLOCKED_ANYWHERE = ("--upload-pack", "--receive-pack", "--exec=", "--config-env", "--ssh-command")


def _has(args: list[str], *flags: str) -> bool:
    for a in args:
        for f in flags:
            if a == f or (f.startswith("--") and a.startswith(f + "=")):
                return True
    return False


def _short_flag(args: list[str], letter: str) -> bool:
    """True if a combined short-flag cluster like -fd contains `letter`."""
    for a in args:
        if a.startswith("-") and not a.startswith("--") and letter in a[1:]:
            return True
    return False


def split_global(argv: list[str]) -> tuple[list[str], str, list[str]]:
    """Return (global_opts, subcommand, rest). Empty subcommand if none found."""
    i = 0
    glob: list[str] = []
    while i < len(argv) and argv[i].startswith("-"):
        opt = argv[i]
        name = opt.split("=", 1)[0]
        glob.append(opt)
        if name in _GLOBAL_WITH_VALUE and "=" not in opt and i + 1 < len(argv):
            i += 1
            glob.append(argv[i])
        i += 1
    if i >= len(argv):
        return glob, "", []
    return glob, argv[i], argv[i + 1:]


def classify(argv: list[str]) -> Verdict:
    if not argv or not all(isinstance(a, str) for a in argv):
        return Verdict(Risk.BLOCKED, "", "empty or non-string arguments")
    if any("\x00" in a for a in argv):
        return Verdict(Risk.BLOCKED, "", "NUL byte in arguments")

    glob, sub, rest = split_global(argv)
    for g in glob:
        if g.split("=", 1)[0] in _BLOCKED_GLOBAL or (g.startswith("-c") and not g.startswith("--")):
            return Verdict(Risk.BLOCKED, sub, f"global option {g.split('=',1)[0]} is not allowed (config/exec override)")
    if not sub:
        return Verdict(Risk.BLOCKED, "", "no git subcommand given")
    if sub.startswith("!") or "/" in sub:
        return Verdict(Risk.BLOCKED, sub, "shell or path-style subcommands are not allowed")
    for a in rest:
        if any(a.startswith(b) for b in _BLOCKED_ANYWHERE):
            return Verdict(Risk.BLOCKED, sub, f"option {a.split('=',1)[0]} can execute arbitrary programs")

    risk, reason = _classify_sub(sub, rest)
    return Verdict(risk, sub, reason)


def _classify_sub(sub: str, a: list[str]) -> tuple[Risk, str]:
    M, D, R = Risk.MUTATE, Risk.DESTRUCTIVE, Risk.READ

    if sub in _READ_SUBCOMMANDS:
        if sub == "reflog":
            if a and a[0] in ("expire", "delete"):
                return D, "reflog expire/delete discards recovery points"
            return R, ""
        if _has(a, *_PROMOTE_FLAGS):
            return M, "flag can write files or run external programs"
        return R, ""

    if sub == "branch":
        if _short_flag(a, "D") or (_has(a, "--delete", "-d") or _short_flag(a, "d")) and (_has(a, "--force", "-f") or _short_flag(a, "f")):
            return D, "force-deletes a branch"
        if _short_flag(a, "d") or _has(a, "--delete"):
            return M, "deletes a branch"
        if _has(a, "-m", "-M", "-c", "-C", "--move", "--copy", "--set-upstream-to", "-u",
                "--unset-upstream", "--edit-description") or _short_flag(a, "M") or _short_flag(a, "m"):
            return M, ""
        positional = [x for x in a if not x.startswith("-")]
        if not positional or _has(a, "-l", "--list", "--show-current"):
            return R, ""
        return M, "creates a branch"

    if sub == "tag":
        if _has(a, "-d", "--delete"):
            return M, "deletes a tag"
        positional = [x for x in a if not x.startswith("-")]
        if not positional or _has(a, "-l", "--list", "-n", "--contains", "--points-at", "--merged", "-v", "--verify"):
            return R, ""
        return M, ""

    if sub == "stash":
        s = a[0] if a else "push"
        if s in ("list", "show"):
            return R, ""
        if s in ("drop", "clear"):
            return D, "discards stashed work"
        return M, ""

    if sub == "remote":
        s = a[0] if a else ""
        if s in ("", "-v", "--verbose", "show", "get-url"):
            return R, ""
        return M, ""

    if sub == "config":
        if _has(a, "--get", "--get-all", "--get-regexp", "--list", "-l", "--get-urlmatch") :
            return R, ""
        if _has(a, "--unset", "--unset-all", "--remove-section", "--edit", "-e"):
            return M, ""
        return M, "changes git configuration"

    if sub == "worktree":
        s = a[0] if a else ""
        if s == "list":
            return R, ""
        if s == "remove" and _has(a, "--force", "-f"):
            return D, "force-removes a worktree"
        return M, ""

    if sub == "submodule":
        s = a[0] if a else "status"
        return (R, "") if s in ("status", "summary") else (M, "")

    if sub == "reset":
        if _has(a, "--hard", "--merge"):
            return D, "discards uncommitted changes"
        return M, ""

    if sub == "clean":
        if _has(a, "-n", "--dry-run") or _short_flag(a, "n"):
            return R, ""
        return D, "permanently deletes untracked files"

    if sub in ("checkout", "restore", "switch"):
        if sub == "switch" and (_has(a, "--discard-changes", "-f", "--force") or _short_flag(a, "f")):
            return D, "discards local changes"
        if sub == "checkout" and (_has(a, "-f", "--force") or _short_flag(a, "f")):
            return D, "discards local changes"
        if "--" in a:
            tail = a[a.index("--") + 1:]
            if tail:
                return D, "overwrites working-tree files from another revision"
        if sub == "restore":
            staged_only = _has(a, "--staged", "-S") and not _has(a, "--worktree", "-W")
            return (M, "") if staged_only else (D, "overwrites working-tree files")
        if sub == "checkout" and "." in a:
            return D, "overwrites working-tree files"
        return M, ""

    if sub == "push":
        if _has(a, "--force", "-f", "--delete", "-d", "--mirror", "--prune") or _short_flag(a, "f"):
            if _has(a, "--force-with-lease") and not _has(a, "--force", "-f"):
                return D, "force-pushes (with lease)"
            return D, "rewrites or deletes remote history"
        if _has(a, "--force-with-lease"):
            return D, "force-pushes (with lease)"
        if any(x.startswith("+") or x.startswith(":") for x in a if not x.startswith("-")):
            return D, "refspec forces or deletes remote ref"
        return M, "publishes commits to a remote"

    if sub == "rebase":
        return (D, "rewrites history") if _has(a, "--root") or "-i" in a or "--interactive" in a else (M, "rewrites history")

    if sub in ("filter-branch",):
        return D, "rewrites entire history"

    if sub == "gc":
        return (D, "prunes unreachable objects") if _has(a, "--prune") and not _has(a, "--prune=never") else (M, "")

    if sub == "prune":
        return D, "deletes unreachable objects"

    if sub == "update-ref":
        return (D, "deletes a ref") if _has(a, "-d") else (M, "")

    if sub == "rm":
        return (D, "removes files from the working tree") if not _has(a, "--cached") else (M, "")

    if sub in _MUTATE_SUBCOMMANDS:
        return M, ""

    return M, "unrecognised subcommand (treated as mutating)"


GH_ONLY_WORDS = {"repo", "gh", "pr", "issue", "auth", "release", "gist", "workflow"}
_GH_CREATE_VALUE_FLAGS = {"--remote", "--source", "--description", "-d", "--homepage", "-h"}
_GH_CREATE_BOOL_FLAGS = {"--private", "--public", "--internal", "--push"}


def classify_gh(argv: list[str]) -> Verdict:
    """Only a tiny allow-list of gh commands exists; everything else is BLOCKED."""
    if not argv or not all(isinstance(a, str) for a in argv) or any("\x00" in a for a in argv):
        return Verdict(Risk.BLOCKED, "", "empty or invalid arguments")
    words = argv[:2]
    if words == ["auth", "status"] and len(argv) == 2:
        return Verdict(Risk.READ, "gh auth status")
    if words == ["repo", "view"]:
        return Verdict(Risk.BLOCKED, "gh repo view", "unsupported") if any(a.startswith("--web") or a == "-w" for a in argv) \
            else Verdict(Risk.READ, "gh repo view")
    if argv[0] == "pr":
        return _classify_gh_pr(argv)
    if words != ["repo", "create"]:
        return Verdict(Risk.BLOCKED, " ".join(argv[:2]),
                       "only `auth status`, `repo view`, `repo create` and `pr create|view|list|status` are allowed")
    rest = argv[2:]
    names: list[str] = []
    flags: set[str] = set()
    i = 0
    while i < len(rest):
        a = rest[i]
        flag, eq, _ = a.partition("=")
        if flag in _GH_CREATE_VALUE_FLAGS:
            if not eq:
                i += 1
                if i >= len(rest):
                    return Verdict(Risk.BLOCKED, "gh repo create", f"{flag} needs a value")
        elif flag in _GH_CREATE_BOOL_FLAGS and not eq:
            flags.add(flag)
        elif a.startswith("-"):
            return Verdict(Risk.BLOCKED, "gh repo create", f"option {flag} is not allowed")
        else:
            names.append(a)
        i += 1
    if len(names) != 1:
        return Verdict(Risk.BLOCKED, "gh repo create", "give exactly one repository name")
    vis = flags & {"--private", "--public", "--internal"}
    if len(vis) != 1:
        return Verdict(Risk.BLOCKED, "gh repo create", "specify exactly one of --private or --public (ask the user if unsure)")
    if vis != {"--private"}:
        return Verdict(Risk.DESTRUCTIVE, "gh repo create", "creates a PUBLICLY visible GitHub repository")
    return Verdict(Risk.MUTATE, "gh repo create", "creates a private repository on GitHub")


_GH_PR_VALUE_FLAGS = {"--title", "-t", "--body", "-b", "--base", "-B", "--head", "-H"}
_GH_PR_BOOL_FLAGS = {"--fill", "--draft", "-d"}
_GH_PR_READ_BLOCKED = {"--web", "-w", "--repo", "-R"}   # no browser, and no reaching into other repositories


def _classify_gh_pr(argv: list[str]) -> Verdict:
    sub, rest = (argv[1] if len(argv) > 1 else ""), argv[2:]
    if sub in ("view", "list", "status"):
        for a in rest:
            if a.split("=", 1)[0] in _GH_PR_READ_BLOCKED:
                return Verdict(Risk.BLOCKED, f"gh pr {sub}", f"option {a.split('=', 1)[0]} is not allowed")
        return Verdict(Risk.READ, f"gh pr {sub}")
    if sub != "create":
        return Verdict(Risk.BLOCKED, f"gh pr {sub}".strip(), "only `pr create`, `pr view`, `pr list` and `pr status` are allowed")
    i = 0
    while i < len(rest):
        flag, eq, _ = rest[i].partition("=")
        if flag in _GH_PR_VALUE_FLAGS:
            if not eq:
                i += 1
                if i >= len(rest):
                    return Verdict(Risk.BLOCKED, "gh pr create", f"{flag} needs a value")
        elif not (flag in _GH_PR_BOOL_FLAGS and not eq):
            what = f"option {flag} is not allowed" if rest[i].startswith("-") else "unexpected argument"
            return Verdict(Risk.BLOCKED, "gh pr create", what)
        i += 1
    return Verdict(Risk.MUTATE, "gh pr create", "opens a pull request on GitHub")


def display(argv: list[str], prog: str = "git") -> str:
    import shlex
    return prog + " " + " ".join(shlex.quote(a) for a in argv)
