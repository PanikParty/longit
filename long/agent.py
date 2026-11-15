"""The model <-> tool loop. Frontend-agnostic: all I/O goes through the Frontend protocol."""
from __future__ import annotations

import enum
import re
import json
import time
from pathlib import Path
from typing import Any, Protocol

from . import commitmsg, fs, gitignore, safety, tools, workflows
from . import skills as skillmod
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
- To commit, you may call `git commit` WITHOUT -m: the harness writes the message from the staged diff and shows it for approval. Never ask the user for a message. (If you do pass -m, make it describe the actual diff.) To push, run `git push` (use `-u origin HEAD` if no upstream is set). Do all the steps of a request in order.
- Never write tool results yourself. Only the tool returns results; if you did not call a tool, nothing happened.
- Never ask the user for confirmation in text ("Do you want to...?"). Just call the tool: the user is shown every state-changing command and approves it there. Ask only when the request is genuinely ambiguous.
- When finished, reply in 1-4 plain sentences saying what happened. Do not repeat raw command output.

Current repository snapshot (the directory may not be a repository yet):
{snapshot}
"""


_FABRICATED = re.compile(r"</?tool_(?:response|result|output)>|^\s*exit code:\s*\d+\s*$"
                         r"|^#\s*Tools\s*$|<tools>|You may call one or more functions", re.I | re.M)
MAX_NUDGES = 2
NUDGE = ("(system) Your last reply contained tool output you made up, or a copy of your instructions, and nothing was actually run. "
         "Never write <tool_response> blocks or exit codes yourself. To run a command, call the tool; "
         "to answer, reply in plain words.")


_AFFIRM = re.compile(r"^\s*(y|yes|yep|yeah|ok|okay|sure|go|go ahead|proceed|continue|do it|please do|"
                     r"call the tool|run it|confirm(ed)?|approved?)\b", re.I)
_ASKS_PERMISSION = re.compile(
    r"(would you like|do you want|shall i|should i|want me)\b[^?]*\b(proceed|continue|go ahead|commit|push|run|stage|apply)\b[^?]*\?"
    r"|\bproceed\?", re.I)
_BARE_AFFIRM = re.compile(r"^\s*(y|yes|yep|yeah|ok|okay|sure|go|go ahead|proceed|continue|do it|please do|please|"
                          r"call the tool|run it|confirm(ed)?|approved?)\s*[.!]*\s*$", re.I)
NUDGE_STUCK = ("(system) You described a plan or asked for permission but did not call a tool. Do not describe, "
               "ask or repeat yourself: the user's request stands, and the harness asks them before anything "
               "changes. Call the appropriate tool now.")
MAX_ACTION_NUDGES = 4
NUDGE_ACTION = ("(system) The skill is not finished: nothing has been changed yet. Do not describe, summarise or repeat "
                "the steps. Call the tool for the next step now.")
NUDGE_EMPTY = ("(system) Your last reply was empty. You are following a skill with several steps: if it is not finished, "
               "call the tool for the next step now; if it is finished or cannot continue, say plainly what you did "
               "or why you stopped.")
APPROVAL_HINT = ("\n\n(system) The user is approving what you just proposed. Do not repeat the plan or ask again; "
                 "call the tool for the first step now.")


def _pretty_args(raw: str) -> str:
    try:
        return json.dumps(json.loads(raw), indent=2)
    except (json.JSONDecodeError, TypeError):
        return raw


def _result_status(content: str) -> str:
    if content.startswith("exit code: 0"):
        return "ok"
    if content.startswith("exit code:"):
        return "failed"
    return "refused" if content.startswith(("error", "denied", "plan mode")) else ""


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", text.lower()))


def _similar(a: str, b: str) -> bool:
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.7


class Mode(enum.Enum):
    MANUAL = "manual"  # read-only runs; every change is approved by the user (default)
    AUTO = "auto"      # reads and ordinary changes run unattended; destructive/outward-risky ones still ask
    PLAN = "plan"      # read-only investigation; changes are never run, the model writes a plan instead


MODE_NOTES = {
    Mode.MANUAL: "Mode: manual. The user approves every state-changing command.",
    Mode.AUTO: ("Mode: auto. Ordinary changes run without asking, so be deliberate; destructive commands "
                "still ask the user. Carry out the whole request."),
    Mode.PLAN: ("Mode: PLAN. Do NOT change anything. Investigate with read-only commands (status, log, diff, ls...), "
                "then reply with a short numbered plan of the exact commands you would run. Any state-changing "
                "command you attempt will be skipped. Do not ask whether to proceed; the user will switch modes to run it."),
}


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
    def __init__(self, backend: Backend, root: Path, frontend: Frontend, max_steps: int = 12,
                 mode: Mode = Mode.MANUAL, skills: skillmod.SkillSet | None = None, skill_match: str = "triggers"):
        self.backend = backend
        self.root = root
        self.cwd = root
        self.ui = frontend
        self.max_steps = max_steps
        self.mode = mode
        self.skills = skills
        self.skill_match = skill_match  # "off" | "triggers" | "model"
        self._skill_trust: dict[str, bool] = {}  # project skills the user has approved/declined this session
        self.allowed: set[str] = set()
        self._ran_fs = False
        self._mutated = False
        self._gitignore_declined: set[Path] = set()
        self._log: list[tuple[str, str, int | None, str]] = []  # (command, outcome, exit_code, detail)
        self.messages: list[dict[str, Any]] = []
        self.reset()

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": ""}]

    def _refresh_system(self, skill: skillmod.Skill | None = None) -> None:
        self.messages[0] = {"role": "system", "content": SYSTEM_PROMPT.format(snapshot=snapshot(self.root, self.cwd))
                                                       + "\n" + MODE_NOTES[self.mode]
                                                       + (skillmod.injection(skill) if skill else "")}

    # ---- main entry -------------------------------------------------
    # ---- trajectory tracing ------------------------------------------
    @property
    def tracing(self) -> bool:
        return bool(getattr(self.ui, "trace_enabled", False))

    def _trace(self, kind: str, title: str, body: str = "", status: str = "") -> None:
        if self.tracing and hasattr(self.ui, "trace_event"):
            self.ui.trace_event(kind, title, body, status)

    def run_turn(self, user_text: str, skill: skillmod.Skill | None = None) -> None:
        """`skill` forces a specific stored skill (an explicit /skill request); otherwise one may be matched."""
        self._model_calls = 0
        self._tool_calls: dict[str, int] = {}
        try:
            self._run_turn(user_text, skill)
        finally:
            if self.tracing and (self._model_calls or self._tool_calls):
                tc = ", ".join(f"{n} x{c}" for n, c in sorted(self._tool_calls.items())) or "none"
                bad = sum(1 for _, o, _, _ in self._log if o in ("denied", "blocked", "planned"))
                self._trace("summary", f"trajectory: {self._model_calls} model call(s) - tool calls: {tc}"
                                       + (f" - {bad} denied/blocked/skipped" if bad else ""))

    # ---- skills -------------------------------------------------------
    def _select_skill(self, user_text: str, explicit: skillmod.Skill | None) -> skillmod.Skill | None:
        if explicit is not None:
            chosen, how = explicit, "requested"
        elif (self.skills is None or self.skill_match == "off"
              or _BARE_AFFIRM.match(user_text)):
            return None
        elif (hit := self.skills.match(user_text)) is not None:
            chosen, how = hit[0], f"matched trigger '{hit[1]}'"
        elif self.skill_match == "model" and self.skills.all():
            chosen, how = self._model_pick_skill(user_text), "chosen by the model"
        else:
            return None
        if chosen is None:
            return None
        if chosen.source == "project" and explicit is None and not self._trust_project_skill(chosen):
            return None
        self.ui.notice(f"skill: {chosen.name} ({how})")
        self._trace("harness", f"skill selected: {chosen.name} ({chosen.source})", how)
        return chosen

    def _model_pick_skill(self, user_text: str) -> skillmod.Skill | None:
        assert self.skills is not None
        candidates = self.skills.all()
        try:
            reply = self.backend.chat(skillmod.model_select_messages(candidates, user_text), []).text
        except BackendError as e:
            self.ui.notice(f"skill matching failed: {e}", error=True)
            return None
        self._trace("harness", "separate model call: skill selection", reply)
        return skillmod.pick_from_reply(candidates, reply)

    def _trust_project_skill(self, skill: skillmod.Skill) -> bool:
        """Skills stored in the repository are untrusted text: ask once per session before using one."""
        key = f"{skill.name}:{skill.path}"
        if key not in self._skill_trust:
            preview = " ".join(skill.body.split())[:300]
            self.ui.notice(f"project skill '{skill.name}' ({skill.path}) says: {preview}")
            verdict = Verdict(Risk.MUTATE, "skill", "instructions come from a file in this repository")
            self._skill_trust[key] = self.ui.confirm(f"use project skill '{skill.name}'", verdict) is not Approval.NO
        return self._skill_trust[key]

    def run_tool(self, name: str, argv: list[str], label: str = "") -> workflows.StepResult:
        """Run one command exactly as if the model had called the tool: same classification, mode, approval and
        confinement. Used by scripted workflows."""
        self._tool_calls[name] = self._tool_calls.get(name, 0) + 1
        args = json.dumps({"args": argv})
        self._trace("tool_call", f"{label} - tool call - {name}", _pretty_args(args))
        content = self._execute(ToolCall(id="workflow", name=name, arguments=args))
        status = _result_status(content)
        self._trace("tool_result", f"{label} - result", content, status=status)
        out = content.split("\n", 1)[1].strip() if "\n" in content else ""
        return workflows.StepResult(content.startswith("exit code: 0"), status or "failed",
                                    "" if out == "(no output)" else out, content)

    def _run_workflow(self, skill: skillmod.Skill) -> None:
        self._trace("harness", f"workflow {skill.workflow}: a fixed command sequence run by the harness; "
                               "the model only writes text")
        report = workflows.REGISTRY[skill.workflow](self)
        self.messages.append({"role": "assistant", "content": report})
        self._trace("answer", "answer (written by the harness)")
        self.ui.assistant_text(report)

    def _run_turn(self, user_text: str, skill: skillmod.Skill | None = None) -> None:
        active = self._select_skill(user_text, skill)
        self._refresh_system(active)
        start = len(self.messages)
        approving = bool(_AFFIRM.match(user_text)) and self._last_assistant_idle()
        self.messages.append({"role": "user", "content": user_text + (APPROVAL_HINT if approving else "")})
        self._log = []
        self._ran_fs = False
        self._mutated = False
        if active is not None and active.workflow:
            if active.workflow in workflows.REGISTRY:
                self._run_workflow(active)
                return
            self.ui.notice(f"skill '{active.name}' names an unknown workflow '{active.workflow}'; "
                           "falling back to the model", error=True)
        nudges = stuck = empties = action_nudges = 0
        rejected: list[str] = []  # idle replies already thrown away this turn
        for _ in range(self.max_steps):
            t0 = time.monotonic()
            try:
                turn = self.backend.chat(self.messages, tools.TOOLS + fs.TOOLS)
            except BackendError as e:
                self.ui.notice(str(e), error=True)
                del self.messages[start:]  # drop the unanswered request (and any nudges) so history stays valid
                return
            self._model_calls += 1
            step = self._model_calls
            self._trace("model", f"step {step} - model call", f"{time.monotonic() - t0:.1f}s")
            if turn.thinking:
                self._trace("thinking", f"step {step} - thinking (model-stated)", turn.thinking)
            if not turn.tool_calls and turn.text:
                parsed = tools.parse_json_text_tool_calls(turn.text)
                if not parsed:
                    fallback = tools.parse_fallback_calls(turn.text)
                    parsed = [("git", fallback[0])] if fallback else []
                if parsed:
                    self._trace("harness", "recovered tool call(s) the model wrote as plain text", turn.text)
                    turn = AssistantTurn(text="", tool_calls=[
                        ToolCall(id=f"fallback_{n}", name=tool,
                                 arguments=json.dumps(payload if isinstance(payload, dict) else {"args": payload}))
                        for n, (tool, payload) in enumerate(parsed)])
            if (not turn.tool_calls and active is not None and active.action and not self._mutated
                    and not _FABRICATED.search(turn.text) and action_nudges < MAX_ACTION_NUDGES):
                action_nudges += 1
                self._trace("harness", "action skill, but the model stopped before changing anything", turn.text or "(empty reply)")
                self.messages.append({"role": "user", "content": NUDGE_ACTION})
                self._trace("harness", "nudged the model to continue the skill", NUDGE_ACTION)
                continue
            if not turn.tool_calls and not turn.text and active is not None and self._log:
                empties += 1
                self._trace("harness", "skill active but the model sent an empty reply", NUDGE_EMPTY)
                if empties <= MAX_NUDGES:
                    self.messages.append({"role": "user", "content": NUDGE_EMPTY})
                    continue
            if not turn.tool_calls and _FABRICATED.search(turn.text):
                self._trace("harness", "rejected a reply containing invented tool output", turn.text)
                nudges += 1
                if nudges > MAX_NUDGES:
                    self.ui.notice("the model keeps inventing command output instead of running commands; "
                                   "try a larger model (/model)", error=True)
                    return
                self.messages.append({"role": "user", "content": NUDGE})
                self._trace("harness", "nudged the model to call a tool", NUDGE)
                continue
            if (not turn.tool_calls and turn.text and not self._log and not self._ran_fs
                    and self.mode is not Mode.PLAN):
                prev = self._last_assistant_text()
                repeated = any(_similar(turn.text, t) for t in rejected + ([prev] if prev else []))
                asking = bool(_ASKS_PERMISSION.search(turn.text))
                if repeated or asking:
                    stuck += 1
                    self._trace("harness", "stuck guard: " + ("repeated reply" if repeated else "asked permission"),
                                turn.text)
                    if stuck > MAX_NUDGES:
                        if repeated:
                            self.ui.notice("the model is stuck repeating itself without running anything; "
                                           "try /clear, or a larger model (/model)", error=True)
                            return
                    else:
                        rejected.append(turn.text)
                        if repeated:
                            self._prune_idle_exchanges()
                        self.messages.append({"role": "user", "content": NUDGE_STUCK})
                        self._trace("harness", "nudged the model to call a tool", NUDGE_STUCK)
                        continue
            self.messages.append(turn.as_message())
            if turn.text:
                if self.tracing and turn.tool_calls:
                    self._trace("thinking", f"step {step} - thinking (model-stated)", turn.text)
                else:
                    self._trace("answer", f"step {step} - answer")
                    self.ui.assistant_text(turn.text)
            if not turn.tool_calls:
                if not turn.text:
                    self._trace("answer", f"step {step} - answer (written by the harness)")
                    self.ui.assistant_text(self._summary())
                return
            for call in turn.tool_calls:
                self._tool_calls[call.name] = self._tool_calls.get(call.name, 0) + 1
                self._trace("tool_call", f"step {step} - tool call - {call.name}", _pretty_args(call.arguments))
                content = self._execute(call)
                self._trace("tool_result", f"step {step} - result (what the model received)", content,
                            status=_result_status(content))
                self.messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
        self.ui.notice(f"stopped after {self.max_steps} steps without finishing", error=True)

    def _last_assistant_text(self) -> str | None:
        for m in reversed(self.messages):
            if m["role"] == "assistant":
                return None if m.get("tool_calls") else (m.get("content") or None)
        return None

    def _last_assistant_idle(self) -> bool:
        """True if the model's last word was plain text (a plan or a question), not a tool call."""
        return self._last_assistant_text() is not None

    def _prune_idle_exchanges(self) -> None:
        """Drop the trailing run of text-only assistant replies and 'yes/continue' messages, so the model
        stops imitating its own loop. The original request stays."""
        i = len(self.messages)
        while i > 1:
            m = self.messages[i - 1]
            idle_assistant = m["role"] == "assistant" and not m.get("tool_calls")
            affirm_user = m["role"] == "user" and (bool(_AFFIRM.match(m["content"] or "")) or
                                                    (m["content"] or "").startswith("(system)"))
            if not (idle_assistant or affirm_user):
                break
            i -= 1
        del self.messages[i:]

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
            elif outcome == "planned":
                parts.append(f"`{command}` was not run (plan mode).")
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
        if self.mode is not Mode.AUTO and \
                self.ui.confirm(f"create {top / '.gitignore'} ignoring: {' '.join(patterns)}", verdict) is Approval.NO:
            self._gitignore_declined.add(top)
            self.ui.notice("continuing without a .gitignore")
            return
        gitignore.write(top, patterns)
        self.ui.notice(f"created {top / '.gitignore'}")
        self._trace("harness", "created .gitignore", "\n".join(patterns))
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
            elif self.mode is Mode.PLAN:
                self.ui.notice(f"plan mode: not running `{command}`")
                return "plan mode: leaving the project is not allowed. Mention it in your plan instead."
            else:
                verdict = Verdict(Risk.MUTATE, "cd", f"leaves the current project ({self.root})")
                if self.ui.confirm(command, verdict) is Approval.NO:
                    self._log.append((command, "denied", None, ""))
                    return "denied: the user declined to leave the project directory. Do not retry; ask what they want."
                self.root, _ = tools.resolve_workdir(target)
                if self.skills:
                    self.skills.set_project(self.root)
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

        if verdict.risk is not Risk.READ and self.mode is Mode.PLAN:
            self.ui.notice(f"plan mode: not running `{command}`")
            self._log.append((command, "planned", None, ""))
            return ("plan mode: this command was NOT run and nothing changed. Include it in your plan instead, "
                    "then finish with the plan.")

        if not is_gh and gitignore.stages_everything(argv):
            self._offer_gitignore()
        if is_gh and commitmsg.needs_pr_text(argv):
            try:
                pr_text = commitmsg.generate_pr_text(self.backend, self.cwd, argv)
            except BackendError as e:
                self.ui.notice(f"could not write the pull request text: {e}", error=True)
                return (f"error: no pull request title/description was given and generating them failed ({e}). "
                        "Pass --title and --body.")
            if pr_text:
                argv = commitmsg.insert_pr_text(argv, *pr_text)
                command = safety.display(argv, call.name)
                self.ui.notice(f"pull request text written by the model: {pr_text[0]}")
                self._trace("harness", "separate model call: pull request title and description from the commits",
                            pr_text[0] + "\n\n" + pr_text[1])
        if not is_gh and commitmsg.needs_message(argv):
            try:
                generated = commitmsg.generate(self.backend, self.cwd, commitmsg.includes_all_tracked(argv))
            except BackendError as e:
                self.ui.notice(f"could not generate a commit message: {e}", error=True)
                return f"error: no commit message was given and generating one failed ({e}). Pass -m with a message."
            if generated:
                argv = commitmsg.insert_message(argv, *generated)
                command = safety.display(argv, call.name)
                self.ui.notice(f"commit message written by the model: {generated[0]}")
                self._trace("harness", "separate model call: commit message from the staged diff",
                            generated[0] + (("\n\n" + generated[1]) if generated[1] else ""))

        if verdict.risk is Risk.READ:
            self.ui.tool_call(command, verdict)
        elif verdict.risk is Risk.MUTATE and (self.mode is Mode.AUTO or verdict.subcommand in self.allowed):
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
        if verdict.risk is not Risk.READ and result.exit_code == 0:
            self._mutated = True
        first = next((ln.strip() for ln in result.output.splitlines() if ln.strip()), "")
        self._log.append((command, "ok" if result.exit_code == 0 else "failed", result.exit_code, first))
        return result.for_model()
