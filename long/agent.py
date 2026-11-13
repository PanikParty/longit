"""The model <-> tool loop. Frontend-agnostic: all I/O goes through the Frontend protocol."""
from __future__ import annotations

import enum
import json
from pathlib import Path
from typing import Any, Protocol

from . import safety, tools
from .llm.base import AssistantTurn, Backend, BackendError, ToolCall
from .safety import Risk, Verdict

SYSTEM_PROMPT = """You are long, a careful assistant that operates git for the user through the `git` tool, and GitHub repository creation through the `gh` tool.

Rules:
- Translate the user's request into git commands. Prefer inspecting first (status, log, diff) when the request depends on repo state.
- One command per tool call. No shell syntax. Never include the word `git` in args.
- Commands that change the repository are shown to the user for approval; if one is denied, do not retry it unchanged - ask what they want instead.
- Never use force flags, `reset --hard`, `clean`, or history rewriting unless the user clearly asked for that outcome. Prefer safer alternatives (`revert`, `--force-with-lease`, `stash`).
- Always pass `-m` to `git commit`; write messages that describe the actual diff.
- If the request is ambiguous in a way that matters, ask a short question instead of guessing.
- To "create a GitHub repo and push everything": (1) `git init` if the directory is not a repo; (2) `git status --short` and look for secrets (.env, *.pem, keys, credentials) - if any are present, tell the user and ask before staging; (3) `git add -A`; (4) `git commit -m ...` if there is anything to commit; (5) `gh repo create NAME --private --source=. --remote=origin` (default NAME is the directory name; use --public only if the user said public); (6) `git push -u origin HEAD`. Skip steps already done. If a step fails, stop and explain.
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


def snapshot(root: Path) -> str:
    if tools.repo_root(root) is None:
        return (f"root: {root}\n[status]\nThis directory is not a git repository yet. Run read-only commands "
                "only if they make sense; if the user wants version control, propose `git init` "
                "(the user will be asked to approve it).")
    parts = []
    for label, argv in (("branch", ["branch", "--show-current"]), ("status", ["status", "--short", "--branch"]),
                        ("recent", ["log", "--oneline", "-5"])):
        r = tools.run_git(argv, root, timeout=10)
        text = r.output.strip() if r.exit_code == 0 else "(unavailable)"
        parts.append(f"[{label}]\n{text or '(none)'}")
    return f"root: {root}\n" + "\n".join(parts)


class Agent:
    def __init__(self, backend: Backend, root: Path, frontend: Frontend, max_steps: int = 12):
        self.backend = backend
        self.root = root
        self.ui = frontend
        self.max_steps = max_steps
        self.allowed: set[str] = set()
        self._log: list[tuple[str, str, int | None, str]] = []  # (command, outcome, exit_code, detail)
        self.messages: list[dict[str, Any]] = []
        self.reset()

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": ""}]

    def _refresh_system(self) -> None:
        self.messages[0] = {"role": "system", "content": SYSTEM_PROMPT.format(snapshot=snapshot(self.root))}

    # ---- main entry -------------------------------------------------
    def run_turn(self, user_text: str) -> None:
        self._refresh_system()
        self.messages.append({"role": "user", "content": user_text})
        self._log = []
        for _ in range(self.max_steps):
            try:
                turn = self.backend.chat(self.messages, tools.TOOLS)
            except BackendError as e:
                self.ui.notice(str(e), error=True)
                self.messages.pop()  # drop the unanswered user message so the history stays valid
                return
            if not turn.tool_calls and turn.text:
                parsed = tools.parse_json_text_tool_call(turn.text)
                if parsed is None:
                    fallback = tools.parse_fallback_calls(turn.text)
                    parsed = ("git", fallback[0]) if fallback else None
                if parsed:
                    turn = AssistantTurn(text="", tool_calls=[
                        ToolCall(id="fallback_0", name=parsed[0], arguments=json.dumps({"args": parsed[1]}))])
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

    # ---- one tool call ---------------------------------------------
    def _execute(self, call: ToolCall) -> str:
        if call.name not in ("git", "gh"):
            return f"error: unknown tool '{call.name}'. Only 'git' and 'gh' are available."
        is_gh = call.name == "gh"
        try:
            argv = tools.parse_args(call.arguments)
        except ValueError as e:
            return f"error: {e}"
        if is_gh and argv and argv[0] == "gh":
            argv = argv[1:]

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
        err = tools.check_gh_confined(argv, self.root) if is_gh else tools.check_confined(argv, self.root, self.root)
        if err:
            self.ui.notice(f"blocked: {err}", error=True)
            self._log.append((command, "blocked", None, err))
            return f"error: blocked by safety policy: {err}"

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

        result = (tools.run_gh if is_gh else tools.run_git)(argv, self.root)
        self.ui.tool_result(result)
        first = next((ln.strip() for ln in result.output.splitlines() if ln.strip()), "")
        self._log.append((command, "ok" if result.exit_code == 0 else "failed", result.exit_code, first))
        return result.for_model()
