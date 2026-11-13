"""Rich-based scrolling transcript + REPL with slash commands."""
from __future__ import annotations

from rich.console import Console
from rich.text import Text

from . import tools
from .agent import Agent, Approval
from .llm.base import BackendError
from .safety import Risk, Verdict

HELP = """\
/help       show this help
/status     git status of the current repo
/backend    show the active model backend
/model      pick a model (or: /model NAME, /model N)
/allowed    subcommands auto-approved this session
/clear      forget the conversation
/exit       quit (Ctrl-D also works)
Anything else is a request in plain English."""


class ConsoleUI:
    def __init__(self, console: Console | None = None):
        self.console = console or Console()

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

    def tool_result(self, result: tools.RunResult) -> None:
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
        self.console.print(Text(f"long  {agent.root}  [{agent.backend.name}]", style="bold"))
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
                agent.run_turn(line)
            except KeyboardInterrupt:
                self.notice("\ninterrupted", error=True)
                agent.messages.append({"role": "user", "content": "(the user interrupted the previous step)"})
                agent.messages.append({"role": "assistant", "content": "Understood, stopping."})

    def _slash(self, line: str, agent: Agent) -> bool:
        cmd = line.split()[0]
        if cmd in ("/exit", "/quit"):
            return False
        if cmd == "/help":
            self.console.print(Text(HELP))
        elif cmd == "/status":
            self.tool_result(tools.run_git(["status", "--short", "--branch"], agent.root))
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
