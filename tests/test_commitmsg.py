import pytest

from long import commitmsg
from long.agent import Agent, Approval, Mode
from long.llm.base import AssistantTurn, BackendError
from test_agent import FakeBackend, FakeUI, call, git, repo  # noqa: F401


@pytest.mark.parametrize("argv,expected", [
    (["commit"], True), (["commit", "-a"], True), (["commit", "-s"], True), (["-C", ".", "commit"], True),
    (["commit", "-m", "x"], False), (["commit", "-am", "x"], False), (["commit", "--message=x"], False),
    (["commit", "-F", "f"], False), (["commit", "--amend"], False), (["commit", "--amend", "--no-edit"], False),
    (["commit", "--fixup", "HEAD"], False), (["commit", "-C", "HEAD"], False), (["status"], False), (["add", "-A"], False),
])
def test_needs_message(argv, expected):
    assert commitmsg.needs_message(argv) is expected


def test_clean_variants():
    c = commitmsg.clean
    assert c("Add retry logic") == ("Add retry logic", "")
    assert c('```\nCommit message:\n"Fix the parser."\n\n- handles empty input\n```') == (
        "Fix the parser", "- handles empty input")
    s, _ = c("Update " + "very " * 30 + "long subject line")
    assert len(s) <= 72
    assert c("   \n\n") is None


def test_insert_message_placement():
    assert commitmsg.insert_message(["commit", "-s", "--", "f"], "S", "B") == [
        "commit", "-m", "S", "-m", "B", "-s", "--", "f"]


def staged_repo(repo):
    (repo / "b.txt").write_text("new file\n")
    git(repo, "add", "b.txt")
    return repo


def test_harness_generates_message_for_bare_commit(repo):
    staged_repo(repo)
    ui = FakeUI(answers=[Approval.YES])
    be = FakeBackend([AssistantTurn(tool_calls=[call("commit")]),
                      AssistantTurn(text="Add b.txt\n\n- new file with sample content"),   # harness's own call
                      AssistantTurn(text="Committed.")])
    Agent(be, repo, ui).run_turn("commit")
    assert git(repo, "log", "-1", "--format=%s") == "Add b.txt\n"
    assert "new file with sample content" in git(repo, "log", "-1", "--format=%b")
    cmd = [x[1] for x in ui.log if x[0] == "confirm"][0]
    assert "-m 'Add b.txt'" in cmd                                        # the user approves the real message
    gen_call = be.seen[1]                                                 # isolated context, no agent history
    assert [m["role"] for m in gen_call] == ["system", "user"]
    assert "b.txt" in gen_call[1]["content"] and "new file" in gen_call[1]["content"]
    assert any(x[0] == "notice" and "written by the model" in x[1] for x in ui.log)


def test_message_supplied_by_model_is_left_alone(repo):
    staged_repo(repo)
    ui = FakeUI(answers=[Approval.YES])
    be = FakeBackend([AssistantTurn(tool_calls=[call("commit", "-m", "mine")]), AssistantTurn(text="done")])
    Agent(be, repo, ui).run_turn("commit")
    assert git(repo, "log", "-1", "--format=%s") == "mine\n" and len(be.seen) == 2   # no extra model call


def test_commit_all_uses_tracked_changes(repo):
    (repo / "a.txt").write_text("changed\n")
    ui = FakeUI(answers=[Approval.YES])
    be = FakeBackend([AssistantTurn(tool_calls=[call("commit", "-a")]), AssistantTurn(text="Update a.txt"),
                      AssistantTurn(text="ok")])
    Agent(be, repo, ui).run_turn("commit")
    assert git(repo, "log", "-1", "--format=%s") == "Update a.txt\n"


def test_nothing_to_commit_does_not_call_model(repo):
    ui = FakeUI(answers=[Approval.YES])
    be = FakeBackend([AssistantTurn(tool_calls=[call("commit")]), AssistantTurn(text="nothing staged")])
    Agent(be, repo, ui).run_turn("commit")
    assert len(be.seen) == 2           # only the agent's two calls


def test_backend_failure_is_reported_to_model(repo):
    staged_repo(repo)

    class Failing(FakeBackend):
        def chat(self, messages, tools_):
            if not tools_:
                raise BackendError("boom")
            return super().chat(messages, tools_)

    ui = FakeUI()
    be = Failing([AssistantTurn(tool_calls=[call("commit")]), AssistantTurn(text="could not")])
    Agent(be, repo, ui).run_turn("commit")
    assert any(x[0] == "notice" and "could not generate" in x[1] for x in ui.log)
    assert git(repo, "log", "--oneline").count("\n") == 1
    assert not any(x[0] == "confirm" for x in ui.log)


def test_plan_mode_does_not_generate(repo):
    staged_repo(repo)
    be = FakeBackend([AssistantTurn(tool_calls=[call("commit")]), AssistantTurn(text="plan")])
    Agent(be, repo, FakeUI(), mode=Mode.PLAN).run_turn("commit")
    assert len(be.seen) == 2
