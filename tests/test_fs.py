import json

from long import fs
from long.agent import Agent, Approval
from long.llm.base import AssistantTurn, ToolCall
from test_agent import FakeBackend, FakeUI


def tc(name, id="t1", **args):
    return ToolCall(id=id, name=name, arguments=json.dumps(args))


def run(agent_root, turns, answers=()):
    ui = FakeUI(answers=answers)
    be = FakeBackend(turns + [AssistantTurn(text="ok")])
    agent = Agent(be, agent_root.resolve(), ui)
    agent.run_turn("go")
    return agent, ui, be


def tool_outputs(be):
    return [m["content"] for m in be.seen[-1] if m["role"] == "tool"]


def test_ls_basic_and_flags(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "b.txt").write_text("hello")
    (tmp_path / ".hidden").write_text("")
    _, _, be = run(tmp_path, [AssistantTurn(tool_calls=[tc("ls"), tc("ls", id="t2", all=True, long=True)])])
    plain, full = tool_outputs(be)
    assert "sub/" in plain and "b.txt" in plain and ".hidden" not in plain
    assert plain.index("sub/") < plain.index("b.txt")   # dirs first
    assert ".hidden" in full and "rw" in full


def test_pwd_and_cd_inside_project(tmp_path):
    (tmp_path / "sub").mkdir()
    agent, ui, be = run(tmp_path, [AssistantTurn(tool_calls=[tc("cd", path="sub"), tc("pwd", id="t2")])])
    assert agent.cwd == (tmp_path / "sub").resolve() and agent.root == tmp_path.resolve()
    assert not any(x[0] == "confirm" for x in ui.log)
    assert str(agent.cwd) in tool_outputs(be)[1]


def test_git_runs_in_cwd_after_cd(tmp_path):
    import subprocess
    (tmp_path / "sub").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "sub" / "f").write_text("x")
    _, _, be = run(tmp_path, [AssistantTurn(tool_calls=[
        tc("cd", path="sub"),
        ToolCall(id="g", name="git", arguments=json.dumps({"args": ["status", "--short", "."]}))])])
    assert "?? ./" in tool_outputs(be)[1]  # relative to sub/, not the repo root


def test_ls_outside_project_refused(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _, _, be = run(proj, [AssistantTurn(tool_calls=[tc("ls", path="..")])])
    assert "outside the project" in tool_outputs(be)[0]


def test_ls_symlink_escape_refused(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (tmp_path / "secret").mkdir()
    (proj / "link").symlink_to(tmp_path / "secret")
    _, _, be = run(proj, [AssistantTurn(tool_calls=[tc("ls", path="link")])])
    assert "outside the project" in tool_outputs(be)[0]


def test_cd_outside_asks_and_switches_project(tmp_path):
    proj, other = tmp_path / "proj", tmp_path / "other"
    proj.mkdir(); other.mkdir()
    agent, ui, _ = run(proj, [AssistantTurn(tool_calls=[tc("cd", path=str(other))])], answers=[Approval.YES])
    assert any(x[0] == "confirm" for x in ui.log)
    assert agent.cwd == other.resolve() and agent.root == other.resolve()


def test_cd_outside_denied(tmp_path):
    proj, other = tmp_path / "proj", tmp_path / "other"
    proj.mkdir(); other.mkdir()
    agent, _, be = run(proj, [AssistantTurn(tool_calls=[tc("cd", path=str(other))])], answers=[Approval.NO])
    assert agent.cwd == proj.resolve()
    assert "denied" in tool_outputs(be)[0]


def test_cd_to_missing_dir(tmp_path):
    agent, _, be = run(tmp_path, [AssistantTurn(tool_calls=[tc("cd", path="nope")])])
    assert agent.cwd == tmp_path.resolve() and "not a directory" in tool_outputs(be)[0]


def test_ls_sent_to_git_tool_is_redirected(tmp_path):
    ls_via_git = ToolCall(id="x", name="git", arguments=json.dumps({"args": ["ls"]}))
    _, ui, be = run(tmp_path, [AssistantTurn(tool_calls=[ls_via_git])])
    assert "Call the `ls` tool" in tool_outputs(be)[0] and not any(x[0] == "confirm" for x in ui.log)


def test_fs_tools_advertised():
    assert {t["function"]["name"] for t in fs.TOOLS} == {"ls", "pwd", "cd"}


def test_json_text_fs_calls_are_recovered(tmp_path):
    from long import tools
    p = tools.parse_json_text_tool_call
    assert p('{"name": "ls", "arguments": {}}') == ("ls", {})
    assert p('{"name": "ls", "arguments": {"path": "src", "all": true}}') == ("ls", {"path": "src", "all": True})
    assert p('Sure.\n{"name": "cd", "arguments": "{\\"path\\": \\"x\\"}"}') == ("cd", {"path": "x"})
    assert p('{"name": "pwd", "arguments": {}}') == ("pwd", {})
    (tmp_path / "a.txt").write_text("")
    ui = FakeUI()
    be = FakeBackend([AssistantTurn(text='{"name": "ls", "arguments": {}}'), AssistantTurn(text="")])
    Agent(be, tmp_path.resolve(), ui).run_turn("ls")
    assert ("auto", "ls") in ui.log
    assert not any(x[0] == "text" and x[1].startswith("{") for x in ui.log)


def test_silent_model_after_ls_gets_done_note(tmp_path):
    ui = FakeUI()
    be = FakeBackend([AssistantTurn(tool_calls=[tc("ls")]), AssistantTurn(text="")])
    Agent(be, tmp_path.resolve(), ui).run_turn("ls")
    assert ("text", "Done - the output is shown above.") in ui.log
