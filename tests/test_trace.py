import json

from rich.console import Console

from long import config
from long.agent import Agent, Approval
from long.cli import main
from long.llm.base import AssistantTurn, parse_choice_message
from long.ui import ConsoleUI
from test_agent import FakeBackend, FakeUI, call, git, repo  # noqa: F401


class TraceUI(FakeUI):
    trace_enabled = True

    def __init__(self, answers=()):
        super().__init__(answers)
        self.events = []

    def trace_event(self, kind, title, body="", status=""):
        self.events.append((kind, title, body, status))


def kinds(ui):
    return [e[0] for e in ui.events]


def test_trajectory_separates_thinking_tool_calls_and_answer(repo):
    ui = TraceUI()
    be = FakeBackend([AssistantTurn(text="Let me look at the history.", tool_calls=[call("log", "--oneline")]),
                      AssistantTurn(text="One commit.")])
    Agent(be, repo, ui).run_turn("what is in the log?")
    assert kinds(ui) == ["model", "thinking", "tool_call", "tool_result", "model", "answer", "summary"]
    ev = {e[0]: e for e in ui.events}
    assert ev["thinking"][2] == "Let me look at the history."
    assert json.loads(ev["tool_call"][2]) == {"args": ["log", "--oneline"]}        # full call, pretty-printed
    assert "init" in ev["tool_result"][2] and ev["tool_result"][3] == "ok"          # full result, as the model saw it
    assert "tool calls: git x1" in ev["summary"][1] and "2 model call" in ev["summary"][1]
    # narration is not printed as an answer; only the final reply is
    assert [x[1] for x in ui.log if x[0] == "text"] == ["One commit."]


def test_result_status_and_denials_are_traced(repo):
    (repo / "n.txt").write_text("n")
    ui = TraceUI(answers=[Approval.NO])
    be = FakeBackend([AssistantTurn(tool_calls=[call("add", "n.txt"), call("show", "nope", id="c2")]),
                      AssistantTurn(text="ok")])
    Agent(be, repo, ui).run_turn("go")
    results = [e for e in ui.events if e[0] == "tool_result"]
    assert [r[3] for r in results] == ["refused", "failed"]
    assert "denied" in results[0][2] and "exit code:" in results[1][2]
    assert "1 denied/blocked/skipped" in ui.events[-1][1]


def test_harness_actions_are_labelled(repo):
    (repo / "b.txt").write_text("x\n")
    git(repo, "add", "b.txt")
    ui = TraceUI(answers=[Approval.YES])
    be = FakeBackend([AssistantTurn(text="<tool_response>exit code: 0</tool_response>"),   # invented output
                      AssistantTurn(text='{"name": "git", "arguments": {"args": ["commit"]}}'),  # call as text
                      AssistantTurn(text="Add b.txt"),                                       # commit-message call
                      AssistantTurn(text="done")])
    Agent(be, repo, ui).run_turn("commit")
    titles = [e[1] for e in ui.events if e[0] == "harness"]
    assert any("invented tool output" in t for t in titles)
    assert any("nudged" in t for t in titles)
    assert any("recovered tool call" in t for t in titles)
    assert any("commit message" in t for t in titles)


def test_tracing_off_emits_nothing(repo):
    ui = TraceUI()
    ui.trace_enabled = False
    be = FakeBackend([AssistantTurn(tool_calls=[call("status")]), AssistantTurn(text="clean")])
    Agent(be, repo, ui).run_turn("status")
    assert ui.events == [] and [x[1] for x in ui.log if x[0] == "text"] == ["clean"]


def test_reasoning_fields_are_parsed_and_not_replayed():
    t = parse_choice_message({"content": "<think>weigh options</think>Run status.", "reasoning": "server side"})
    assert t.text == "Run status." and "weigh options" in t.thinking and "server side" in t.thinking
    assert "thinking" not in t.as_message() and "think" not in json.dumps(t.as_message())


def test_reasoning_is_traced_as_thinking(repo):
    ui = TraceUI()
    be = FakeBackend([AssistantTurn(text="done", thinking="the repo is clean")])
    Agent(be, repo, ui).run_turn("hi")
    assert ("thinking", "step 1 - thinking (model-stated)", "the repo is clean", "") in ui.events


def test_console_trace_shows_full_results_without_truncation(tmp_path):
    for i in range(30):
        (tmp_path / f"f{i:02}.txt").write_text("")
    from test_fs import tc
    console = Console(record=True, width=100)
    ui = ConsoleUI(console)
    ui.trace_enabled = True
    be = FakeBackend([AssistantTurn(text="Listing.", tool_calls=[tc("ls")]), AssistantTurn(text="Thirty files.")])
    Agent(be, tmp_path.resolve(), ui).run_turn("ls")
    out = console.export_text()
    assert "f00.txt" in out and "f29.txt" in out and "more lines" not in out
    assert "-- step 1 - thinking" in out and "-- step 1 - tool call - ls" in out and "-- step 2 - answer" in out
    assert "trajectory:" in out


def test_slash_trace_and_banner(tmp_path):
    console = Console(record=True, width=100)
    ui = ConsoleUI(console)
    agent = Agent(FakeBackend([]), tmp_path, ui)
    ui._slash("/trace on", agent)
    assert ui.trace_enabled
    ui._slash("/trace", agent)
    assert not ui.trace_enabled
    ui._slash("/trace maybe", agent)
    assert "usage: /trace" in console.export_text()


def test_config_and_flag(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text("trace = true\n")
    assert config.load(f).trace is True
    assert config.load(tmp_path / "none.toml").trace is False
