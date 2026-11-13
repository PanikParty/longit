"""The model <-> tool loop. Frontend-agnostic: all I/O goes through the Frontend protocol."""
from __future__ import annotations

import enum
import json
from pathlib import Path
from typing import Any, Protocol

from . import fs, gitignore, safety, tools
from .llm.base import AssistantTurn, Backend, BackendError, ToolCall
from .safety import Risk, Verdict

SYSTEM_PROMPT = """You are long, a careful assistant that operates git for the user through the `git` tool, and GitHub repository creation through the `gh` tool. You can also look around with `ls`, `pwd` and `cd` (confined to the project; leaving it needs the user's approval). git, gh and ls run in the current directory.

Rules:
- Translate the user's request into git commands. Prefer inspecting first (status, log, diff) when the request depends on repo state.
- One command per tool call. No shell syntax. Never include the word `git` in args.
- Commands that change the repository are shown to the user for approval; if one is denied, do not retry it unchanged - ask what they want instead.
- Never use force flags, `reset --hard`, `clean`, or history rewriting unless the user clearly asked for that outcome. Prefer safer alternatives (`revert`, `--force-with-lease`, `stash`).
- Always pass `-m` to `git commit`; write messages that describe the actual diff.
- If the request is ambiguous in a way that matters, ask a short question instead of guessing.
- To "create a GitHub repo and push everything": (1) `git init` if the directory is not a repo; (2) `git status --short` and look for secrets (.env, *.pem, keys, credentials) - if any are present, tell the user and ask before staging; (3) `git add -A` (long itself offers to create a .gitignore first if none exists and junk would be staged); (4) `git commit -m ...` if there is anything to commit; (5) `gh repo create NAME --private --source=. --remote=origin` (default NAME is the directory name; use --public only if the user said public); (6) `git push -u origin HEAD`. Skip steps already done. If a step fails, stop and explain.
- The directory name is NOT the repository name. For a repo's name, check the remotes in the snapshot or `git remote -v` (the name is the last part of the URL, without .git); if there is no remote, say so.
- Never ask the user for confirmation in text ("Do you want to...?"). Just call the tool: the user is shown every state-changing command and approves it there. Ask only when the request is genuinely ambiguous.
- When finished, reply in 1-4 plain sentences saying what happened. Do not repeat raw command output.

Current repository snapshot (the directory may not be a repository yet):
{snapshot}
"""


class Approval(enum.Enum):
    NO = "no"
    YES = "yes"
    ALWAYS = "always"  # allow this subcommand for the rest of the session (MUTATE only)


class Frontend(Protocol):
    def assistant_text(self, text: str) -> None: ...
    def tool_call(self, command: str, verdict: Verdict) -> None: ...
    def confirm(self, command: str, verdict: Verdict) -> Approval: ...
    def tool_result(self, result: tools.RunResult) -> None: ...
    def notice(self, message: str, error: bool = False) -> None: ...


def snapshot(root: Path, cwd: Path | None = None) -> str:
    cwd = cwd or root
    if tools.repo_root(cwd) is None:
        return (f"root: {root}\ncwd: {cwd}\n[status]\nThis directory is not a git repository yet. Run read-only commands "
                "only if they make sense; if the user wants version control, propose `git init` "
                "(the user will be asked to approve it).")
    parts = []
    for label, argv in (("branch", ["branch", "--show-current"]), ("status", ["status", "--short", "--branch"]),
                        ("recent", ["log", "--oneline", "-5"]), ("remotes", ["remote", "-v"])):
        r = tools.run_git(argv, cwd, timeout=10)
        text = r.output.strip() if r.exit_code == 0 else "(unavailable)"
        parts.append(f"[{label}]\n{text or '(none)'}")
    return f"root: {root}\ncwd: {cwd}\n" + "\n".join(parts)


class Agent:
    def __init__(self, backend: Backend, root: Path, frontend: Frontend, max_steps: int = 12):
        self.backend = backend
        self.root = root
        self.cwd = root
        self.ui = frontend
        self.max_steps = max_steps
        self.allowed: set[str] = set()
        self._ran_fs = False
        self._gitignore_declined: set[Path] = set()
        self._log: list[tuple[str, str, int | None, str]] = []  # (command, outcome, exit_code, detail)
        self.messages: list[dict[str, Any]] = []
        self.reset()

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": ""}]

    def _refresh_system(self) -> None:
        self.messages[0] = {"role": "system", "content": SYSTEM_PROMPT.format(snapshot=snapshot(self.root, self.cwd))}

    # ---- main entry -------------------------------------------------
    def run_turn(self, user_text: str) -> None:
        self._refresh_system()
        self.messages.append({"role": "user", "content": user_text})
        self._log = []
        self._ran_fs = False
        for _ in range(self.max_steps):
            try:
                turn = self.backend.chat(self.messages, tools.TOOLS + fs.TOOLS)
            except BackendError as e:
                self.ui.notice(str(e), error=True)
                self.messages.pop()  # drop the unanswered user message so the history stays valid
                return
            if not turn.tool_calls and turn.text:
                parsed = tools.parse_json_text_tool_calls(turn.text)
                if not parsed:
                    fallback = tools.parse_fallback_calls(turn.text)
                    parsed = [("git", fallback[0])] if fallback else []
                if parsed:
                    turn = AssistantTurn(text="", tool_calls=[
                        ToolCall(id=f"fallback_{n}", name=tool,
                                 arguments=json.dumps(payload if isinstance(payload, dict) else {"args": payload}))
                        for n, (tool, payload) in enumerate(parsed)])
            self.messages.append(turn.as_message())
            if turn.text:
                self.ui.assistant_text(turn.text)
            if not turn.tool_calls:
                if not turn.text:
                    self.ui.assistant_text(self._summary())
                return
            for call in turn.tool_calls:
                self.messages.append({"role": "tool", "tool_call_id": call.id, "content": self._execute(call)})
        self.ui.notice(f"stopped after {self.max_steps} steps without finishing", error=True)

    def _summary(self) -> str:
        """Plain-language wrap-up for when the model ends a turn without saying anything."""
        if not self._log and self._ran_fs:
            return "Done - the output is shown above."
        if not self._log:
            return "I wasn't able to turn that into a git command. Could you rephrase or give more detail?"
        parts = []
        for command, outcome, code, detail in self._log:
            if outcome == "ok":
                parts.append(f"`{command}` succeeded.")
            elif outcome == "failed":
                parts.append(f"`{command}` failed (exit {code})" + (f": {detail}" if detail else "."))
            elif outcome == "denied":
                parts.append(f"You declined `{command}`, so I didn't run it.")
            else:
                parts.append(f"`{command}` was blocked: {detail}")
        return " ".join(parts[-3:])

    def _offer_gitignore(self) -> None:
        """Staging everything with no .gitignore: offer to create one if junk or secrets would be swept in."""
        missing = gitignore.check_missing(self.cwd, self.root)
        if missing is None or missing[0] in self._gitignore_declined:
            return
        top, patterns = missing
        verdict = Verdict(Risk.MUTATE, "gitignore",
                          "no .gitignore, and these untracked files would be staged: " + ", ".join(patterns))
        if self.ui.confirm(f"create {top / '.gitignore'} ignoring: {' '.join(patterns)}", verdict) is Approval.NO:
            self._gitignore_declined.add(top)
            self.ui.notice("continuing without a .gitignore")
            return
        gitignore.write(top, patterns)
        self.ui.notice(f"created {top / '.gitignore'}")
        self._log.append((f"create .gitignore ({' '.join(patterns)})", "ok", 0, ""))

    def _execute_fs(self, call: ToolCall) -> str:
        try:
            args = fs.parse_args(call.arguments)
        except ValueError as e:
            return f"error: {e}"
        read = Verdict(Risk.READ, call.name)
        self._ran_fs = True
        if call.name == "pwd":
            self.ui.tool_call("pwd", read)
            result = tools.RunResult(0, str(self.cwd))
        elif call.name == "ls":
            self.ui.tool_call(fs.display_ls(args), read)
            result = fs.ls(self.cwd, self.root, args)
            if result.exit_code != 0:
                self._log.append((fs.display_ls(args), "failed", result.exit_code, result.output))
        else:  # cd
            path = args.get("path")
            if not isinstance(path, str) or not path:
                return "error: 'path' must be a non-empty string"
            target = fs.resolve(self.cwd, path)
            command = f"cd {target}"
            if not target.is_dir():
                self.ui.tool_call(command, read)
                self._log.append((command, "failed", 1, "not a directory"))
                return f"exit code: 1\n{target}: not a directory"
            if fs.inside(target, self.root):
                self.ui.tool_call(command, read)
                self.cwd = target
            else:
                verdict = Verdict(Risk.MUTATE, "cd", f"leaves the current project ({self.root})")
                if self.ui.confirm(command, verdict) is Approval.NO:
                    self._log.append((command, "denied", None, ""))
                    return "denied: the user declined to leave the project directory. Do not retry; ask what they want."
                self.root, _ = tools.resolve_workdir(target)
                self.cwd = target
                self.allowed.clear()  # approvals were for the previous project
            self._log.append((command, "ok", 0, ""))
            result = tools.RunResult(0, f"now in {self.cwd}")
        self.ui.tool_result(result)
        return result.for_model()

    # ---- one tool call ---------------------------------------------
    def _execute(self, call: ToolCall) -> str:
        if call.name in fs.NAMES:
            return self._execute_fs(call)
        if call.name not in ("git", "gh"):
            return f"error: unknown tool '{call.name}'. Available: git, gh, ls, pwd, cd."
        is_gh = call.name == "gh"
        try:
            argv = tools.parse_args(call.arguments)
        except ValueError as e:
            return f"error: {e}"
        if is_gh and argv and argv[0] == "gh":
            argv = argv[1:]

        if not is_gh and argv[0] in fs.NAMES:
            return f"error: '{argv[0]}' is not a git command. Call the `{argv[0]}` tool instead."
        if not is_gh and argv[0] in safety.GH_ONLY_WORDS:
            return (f"error: '{argv[0]}' is a GitHub CLI command, not a git command. "
                    "Call the `gh` tool instead, e.g. {\"args\": [\"repo\", \"create\", NAME, \"--private\", "
                    "\"--source=.\", \"--remote=origin\"]}.")
        verdict = safety.classify_gh(argv) if is_gh else safety.classify(argv)
        command = safety.display(argv, call.name)
        if verdict.risk is Risk.BLOCKED:
            self.ui.tool_call(command, verdict)
            self.ui.notice(f"blocked: {verdict.reason}", error=True)
            self._log.append((command, "blocked", None, verdict.reason))
            return f"error: blocked by safety policy: {verdict.reason}"
        err = tools.check_gh_confined(argv, self.root, self.cwd) if is_gh else tools.check_confined(argv, self.root, self.cwd)
        if err:
            self.ui.notice(f"blocked: {err}", error=True)
            self._log.append((command, "blocked", None, err))
            return f"error: blocked by safety policy: {err}"

        if not is_gh and gitignore.stages_everything(argv):
            self._offer_gitignore()

        if verdict.risk is Risk.READ:
            self.ui.tool_call(command, verdict)
        elif verdict.risk is Risk.MUTATE and verdict.subcommand in self.allowed:
            self.ui.tool_call(command, verdict)
        else:
            decision = self.ui.confirm(command, verdict)
            if decision is Approval.NO:
                self._log.append((command, "denied", None, ""))
                return "denied: the user declined to run this command. Do not retry it unchanged; ask what they would prefer."
            if decision is Approval.ALWAYS and verdict.risk is Risk.MUTATE:
                self.allowed.add(verdict.subcommand)

        result = (tools.run_gh if is_gh else tools.run_git)(argv, self.cwd)
        self.ui.tool_result(result)
        first = next((ln.strip() for ln in result.output.splitlines() if ln.strip()), "")
        self._log.append((command, "ok" if result.exit_code == 0 else "failed", result.exit_code, first))
        return result.for_model()
