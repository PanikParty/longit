"""Rich-based scrolling transcript + REPL with slash commands."""
from __future__ import annotations

from rich.console import Console
from rich.text import Text

from . import tools
from .agent import Agent, Approval, Mode
from .llm.base import BackendError
from .safety import Risk, Verdict

HELP = """\
/help       show this help
/status     git status of the current repo
/mode       show or set the mode: /mode manual|auto|plan (also /manual /auto /plan)
/skills     list stored skills; /skill NAME [request] runs one explicitly
/trace      show or set trajectory tracing: /trace on|off (thinking, tool calls and full results)
/backend    show the active model backend
/model      pick a model (or: /model NAME, /model N)
/allowed    subcommands auto-approved this session
/clear      forget the conversation
/exit       quit (Ctrl-D also works)
Anything else is a request in plain English."""


class ConsoleUI:
    def __init__(self, console: Console | None = None):
        self.console = console or Console()
        self.trace_enabled = False

    # ---- Frontend protocol -----------------------------------------
    def assistant_text(self, text: str) -> None:
        self.console.print(Text(text))

    def tool_call(self, command: str, verdict: Verdict) -> None:
        self.console.print(Text(f"  $ {command}", style="dim"))

    def confirm(self, command: str, verdict: Verdict) -> Approval:
        destructive = verdict.risk is Risk.DESTRUCTIVE
        style = "bold red" if destructive else "yellow"
        label = "DESTRUCTIVE" if destructive else "changes repo"
        note = f" - {verdict.reason}" if verdict.reason else ""
        self.console.print(Text(f"  $ {command}", style=style))
        self.console.print(Text(f"    [{label}]{note}", style=style))
        prompt = "    run? [y/N] " if destructive else "    run? [y/N/a=always for `%s`] " % verdict.subcommand
        while True:
            try:
                ans = self.console.input(Text(prompt)).strip().lower()
            except (EOFError, KeyboardInterrupt):
                self.console.print()
                return Approval.NO
            if ans in ("", "n", "no"):
                return Approval.NO
            if ans in ("y", "yes"):
                return Approval.YES
            if ans in ("a", "always") and not destructive:
                return Approval.ALWAYS

    _TRACE_STYLES = {"model": "dim", "thinking": "magenta", "answer": "bold green", "tool_call": "cyan",
                     "tool_result": "green", "harness": "yellow", "summary": "bold"}

    def trace_event(self, kind: str, title: str, body: str = "", status: str = "") -> None:
        """One labelled step of the trajectory. Tool calls and results are shown in full."""
        style = self._TRACE_STYLES.get(kind, "")
        if kind == "tool_result" and status in ("failed", "refused"):
            style = "red"
        self.console.print(Text(f"-- {title}" + (f" [{status}]" if status else ""), style=style))
        for ln in body.splitlines():
            self.console.print(Text("   " + ln, style="dim" if kind in ("model", "thinking") else ""))

    def tool_result(self, result: tools.RunResult) -> None:
        if self.trace_enabled:  # the trace already shows the complete result
            return
        lines = result.output.strip().splitlines()
        shown = lines[:12]
        for ln in shown:
            self.console.print(Text("    " + ln, style="dim"))
        if len(lines) > len(shown):
            self.console.print(Text(f"    ... {len(lines) - len(shown)} more lines", style="dim italic"))
        if result.exit_code != 0:
            self.console.print(Text(f"    (exit {result.exit_code})", style="red"))

    def notice(self, message: str, error: bool = False) -> None:
        self.console.print(Text(message, style="red" if error else "cyan"))

    # ---- REPL -------------------------------------------------------
    def repl(self, agent: Agent) -> None:
        self.console.print(Text(f"long  {agent.root}  [{agent.backend.name}]  mode: {agent.mode.value}"
                                  + ("  trace: on" if self.trace_enabled else ""), style="bold"))
        self.console.print(Text("Describe what you want. /help for commands.", style="dim"))
        while True:
            try:
                line = self.console.input("[bold green]›[/] ").strip()
            except EOFError:
                self.console.print()
                return
            except KeyboardInterrupt:
                self.console.print()
                continue
            if not line:
                continue
            if line.startswith("/"):
                if not self._slash(line, agent):
                    return
                continue
            try:
                self._run(agent, line)
            except KeyboardInterrupt:
                self.notice("\ninterrupted", error=True)
                agent.messages.append({"role": "user", "content": "(the user interrupted the previous step)"})
                agent.messages.append({"role": "assistant", "content": "Understood, stopping."})

    def _run(self, agent: Agent, text: str, skill=None) -> None:
        agent.run_turn(text, skill=skill)
        if agent.mode is Mode.PLAN:
            self.notice("plan mode: nothing was changed. /manual or /auto, then say \"go\" to run it.")

    def _slash(self, line: str, agent: Agent) -> bool:
        cmd = line.split()[0]
        if cmd in ("/exit", "/quit"):
            return False
        if cmd == "/help":
            self.console.print(Text(HELP))
        elif cmd == "/status":
            self.tool_result(tools.run_git(["status", "--short", "--branch"], agent.cwd))
        elif cmd in ("/mode", "/manual", "/auto", "/plan"):
            arg = cmd[1:] if cmd != "/mode" else (line.split(maxsplit=1)[1].strip().lower() if " " in line else "")
            self._set_mode(agent, arg)
        elif cmd == "/skills":
            self._list_skills(agent)
        elif cmd == "/skill":
            parts = line.split(maxsplit=2)
            sk = agent.skills.get(parts[1]) if len(parts) > 1 and agent.skills else None
            if sk is None:
                self.notice("usage: /skill NAME [request]  (see /skills)", error=True)
            else:
                self._run(agent, parts[2] if len(parts) > 2 else f"Run the {sk.name} skill.", skill=sk)
        elif cmd == "/trace":
            arg = line.split(maxsplit=1)[1].strip().lower() if " " in line else ""
            if arg in ("on", "off", ""):
                self.trace_enabled = (arg == "on") if arg else not self.trace_enabled
                self.notice("trace: " + ("on - showing thinking, tool calls and full results"
                                         if self.trace_enabled else "off"))
            else:
                self.notice("usage: /trace [on|off]", error=True)
        elif cmd in ("/model", "/models"):
            self._pick_model(agent, line.split(maxsplit=1)[1].strip() if " " in line else "")
        elif cmd == "/backend":
            self.console.print(Text(agent.backend.name))
        elif cmd == "/allowed":
            self.console.print(Text(", ".join(sorted(agent.allowed)) or "(none)"))
        elif cmd == "/clear":
            agent.reset()
            self.notice("conversation cleared")
        else:
            self.notice(f"unknown command {cmd}; try /help", error=True)
        return True

    def _pick_model(self, agent: Agent, choice: str) -> None:
        be = agent.backend
        if not (hasattr(be, "list_models") and hasattr(be, "set_model")):
            self.notice("this backend loads a single model file; restart with --model-path to change it", error=True)
            return
        try:
            models = be.list_models()
        except BackendError as e:
            self.notice(str(e), error=True)
            return
        if not models:
            self.notice("the server reports no models", error=True)
            return
        if not choice:
            for n, m in enumerate(models, 1):
                mark = "*" if m == be.model else " "
                self.console.print(Text(f"  {mark} {n}. {m}", style="bold" if m == be.model else ""))
            try:
                choice = self.console.input(Text("  model number or name (blank to keep current): ")).strip()
            except (EOFError, KeyboardInterrupt):
                self.console.print()
                return
            if not choice:
                return
        if choice.isdigit() and 1 <= int(choice) <= len(models):
            choice = models[int(choice) - 1]
        if choice not in models:
            self.notice(f"'{choice}' is not one of the server's models", error=True)
            return
        be.set_model(choice)
        self.notice(f"model set to {choice} (this session only; set [openai].model in the config to keep it)")

    def _set_mode(self, agent: Agent, name: str) -> None:
        if not name:
            self.console.print(Text(f"mode: {agent.mode.value}  (manual | auto | plan)"))
            return
        try:
            agent.mode = Mode(name)
        except ValueError:
            self.notice(f"unknown mode '{name}'; choose manual, auto or plan", error=True)
            return
        notes = {Mode.MANUAL: "every change asks first",
                 Mode.AUTO: "ordinary changes run without asking; destructive ones still ask",
                 Mode.PLAN: "read-only: the model investigates and writes a plan, nothing changes"}
        self.notice(f"mode: {name} - {notes[agent.mode]}")

    def _list_skills(self, agent: Agent) -> None:
        if agent.skills is None or not agent.skills.all():
            self.console.print(Text("no skills loaded"))
            return
        self.console.print(Text(f"skill matching: {agent.skill_match}", style="dim"))
        for sk in agent.skills.all():
            self.console.print(Text(f"  {sk.name}  [{sk.source}]  {sk.description}"))
            if sk.triggers:
                self.console.print(Text("      triggers: " + ", ".join(sk.triggers), style="dim"))
        for w in agent.skills.warnings:
            self.notice(w, error=True)
