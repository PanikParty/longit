import pytest
from rich.console import Console

from long import config
from long.agent import Agent, Approval, Mode
from long.cli import main
from long.llm.base import AssistantTurn
from long.ui import ConsoleUI
from test_agent import FakeBackend, FakeUI, call, git, repo  # noqa: F401  (repo is a fixture)


def run(root, mode, turns, answers=()):
    ui = FakeUI(answers=answers)
    be = FakeBackend(turns + [AssistantTurn(text="ok")])
    agent = Agent(be, root, ui, mode=mode)
    agent.run_turn("go")
    return agent, ui, be


def confirms(ui):
    return [x[1] for x in ui.log if x[0] == "confirm"]


def test_manual_asks_for_mutations(repo):
    (repo / "n.txt").write_text("n")
    _, ui, _ = run(repo, Mode.MANUAL, [AssistantTurn(tool_calls=[call("add", "n.txt")])], answers=[Approval.YES])
    assert confirms(ui) == ["git add n.txt"]


def test_auto_runs_mutations_without_asking(repo):
    (repo / "n.txt").write_text("n")
    _, ui, _ = run(repo, Mode.AUTO, [AssistantTurn(tool_calls=[call("add", "n.txt"), call("commit", "-m", "n", id="c2")])])
    assert confirms(ui) == []
    assert "n" in git(repo, "log", "--oneline", "-1")


def test_auto_still_asks_for_destructive_and_blocks(repo):
    (repo / "a.txt").write_text("dirty")
    _, ui, be = run(repo, Mode.AUTO, [AssistantTurn(tool_calls=[
        call("reset", "--hard"), call("-c", "core.pager=x", "status", id="c2")])], answers=[Approval.NO])
    assert confirms(ui) == ["git reset --hard"]
    assert (repo / "a.txt").read_text() == "dirty"
    assert any("blocked by safety policy" in m["content"] for m in be.seen[-1] if m["role"] == "tool")


def test_plan_mode_runs_reads_but_never_mutations(repo):
    (repo / "n.txt").write_text("n")
    agent, ui, be = run(repo, Mode.PLAN, [AssistantTurn(tool_calls=[
        call("status", "--short"), call("add", "n.txt", id="c2"), call("commit", "-m", "x", id="c3")])])
    assert confirms(ui) == []
    assert ("auto", "git status --short") in ui.log
    assert git(repo, "diff", "--cached", "--name-only") == ""
    tool_msgs = [m["content"] for m in be.seen[-1] if m["role"] == "tool"]
    assert sum("plan mode" in t for t in tool_msgs) == 2
    assert "Mode: PLAN" in be.seen[0][0]["content"]


def test_plan_mode_text_only_reply_is_not_treated_as_stuck(repo):
    plan = "1. git add -A\n2. git commit\n\nWould you like to proceed?"
    ui = FakeUI()
    be = FakeBackend([AssistantTurn(text=plan)])
    Agent(be, repo, ui, mode=Mode.PLAN).run_turn("commit everything")
    assert [x[1] for x in ui.log if x[0] == "text"] == [plan]


def test_plan_mode_summary_when_silent(repo):
    ui = FakeUI()
    be = FakeBackend([AssistantTurn(tool_calls=[call("push")]), AssistantTurn(text="")])
    Agent(be, repo, ui, mode=Mode.PLAN).run_turn("push")
    assert ("text", "`git push` was not run (plan mode).") in ui.log


def test_plan_mode_blocks_leaving_project(tmp_path):
    from test_fs import tc
    proj, other = tmp_path / "p", tmp_path / "o"
    proj.mkdir(); other.mkdir()
    agent, ui, _ = run(proj.resolve(), Mode.PLAN, [AssistantTurn(tool_calls=[tc("cd", path=str(other))])])
    assert agent.cwd == proj.resolve() and confirms(ui) == []


def test_auto_creates_gitignore_without_asking(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "a.py").write_text("x")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "c").write_text("x")
    _, ui, _ = run(tmp_path.resolve(), Mode.AUTO, [AssistantTurn(tool_calls=[call("add", "-A")])])
    assert confirms(ui) == [] and (tmp_path / ".gitignore").read_text().strip() == ".venv/"


def test_slash_mode_commands(tmp_path):
    console = Console(record=True, width=100)
    ui = ConsoleUI(console)
    agent = Agent(FakeBackend([]), tmp_path, ui)
    ui._slash("/plan", agent)
    assert agent.mode is Mode.PLAN
    ui._slash("/mode auto", agent)
    assert agent.mode is Mode.AUTO
    ui._slash("/mode bogus", agent)
    assert agent.mode is Mode.AUTO and "unknown mode" in console.export_text()
    ui._slash("/mode", agent)
    assert "mode: auto" in console.export_text()
    ui._slash("/manual", agent)
    assert agent.mode is Mode.MANUAL


def test_config_and_cli_mode(tmp_path, capsys):
    cfgfile = tmp_path / "c.toml"
    cfgfile.write_text('mode = "plan"\n')
    assert config.load(cfgfile).mode == "plan"
    cfgfile.write_text('mode = "yolo"\n')
    assert main(["--config", str(cfgfile), "-C", str(tmp_path), "x"]) == 2
    assert "unknown mode" in capsys.readouterr().err
