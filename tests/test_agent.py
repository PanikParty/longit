import json
import subprocess
from pathlib import Path

import pytest

from long import tools
from long.agent import Agent, Approval
from long.llm.base import AssistantTurn, ToolCall


def git(root, *a):
    return subprocess.run(["git", *a], cwd=root, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("a\n")
    git(tmp_path, "add", "a.txt")
    git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


class FakeBackend:
    name = "fake"

    def __init__(self, turns):
        self.turns = list(turns)
        self.seen = []

    def chat(self, messages, tools_):
        self.seen.append(json.loads(json.dumps(messages)))
        return self.turns.pop(0)


class FakeUI:
    def __init__(self, answers=()):
        self.answers = list(answers)
        self.log = []

    def assistant_text(self, t): self.log.append(("text", t))
    def tool_call(self, c, v): self.log.append(("auto", c))
    def tool_result(self, r): self.log.append(("result", r.exit_code))
    def notice(self, m, error=False): self.log.append(("notice", m))

    def confirm(self, c, v):
        self.log.append(("confirm", c, v.risk.name))
        return self.answers.pop(0)


def call(*argv, id="c1"):
    return ToolCall(id=id, name="git", arguments=json.dumps({"args": list(argv)}))


def test_read_runs_without_prompt(repo):
    ui = FakeUI()
    be = FakeBackend([AssistantTurn(tool_calls=[call("log", "--oneline")]), AssistantTurn(text="one commit")])
    Agent(be, repo, ui).run_turn("what's in history?")
    assert not [e for e in ui.log if e[0] == "confirm"]
    tool_msg = be.seen[1][-1]
    assert tool_msg["role"] == "tool" and "init" in tool_msg["content"]


def test_mutation_needs_approval_and_runs(repo):
    (repo / "b.txt").write_text("b\n")
    ui = FakeUI([Approval.YES, Approval.YES])
    be = FakeBackend([AssistantTurn(tool_calls=[call("add", "b.txt")]),
                      AssistantTurn(tool_calls=[call("commit", "-m", "add b", id="c2")]),
                      AssistantTurn(text="done")])
    Agent(be, repo, ui).run_turn("commit b")
    assert git(repo, "log", "--oneline").count("\n") == 2
    assert [e[0] for e in ui.log].count("confirm") == 2


def test_denial_is_reported_to_model_and_nothing_runs(repo):
    (repo / "b.txt").write_text("b\n")
    ui = FakeUI([Approval.NO])
    be = FakeBackend([AssistantTurn(tool_calls=[call("add", "b.txt")]), AssistantTurn(text="ok, skipped")])
    Agent(be, repo, ui).run_turn("add b")
    assert "denied" in be.seen[1][-1]["content"]
    assert "b.txt" not in git(repo, "diff", "--cached", "--name-only")


def test_always_applies_only_to_mutate_and_to_that_subcommand(repo):
    (repo / "b.txt").write_text("b\n")
    (repo / "c.txt").write_text("c\n")
    ui = FakeUI([Approval.ALWAYS, Approval.YES])
    be = FakeBackend([AssistantTurn(tool_calls=[call("add", "b.txt")]),
                      AssistantTurn(tool_calls=[call("add", "c.txt", id="c2")]),     # auto-approved
                      AssistantTurn(tool_calls=[call("commit", "-m", "x", id="c3")]),  # different subcommand: asks
                      AssistantTurn(text="done")])
    agent = Agent(be, repo, ui)
    agent.run_turn("go")
    assert [e[0] for e in ui.log].count("confirm") == 2
    assert agent.allowed == {"add"}


def test_destructive_ignores_always(repo):
    (repo / "a.txt").write_text("dirty\n")
    ui = FakeUI([Approval.ALWAYS, Approval.NO])
    be = FakeBackend([AssistantTurn(tool_calls=[call("reset", "--hard")]),
                      AssistantTurn(tool_calls=[call("reset", "--hard", id="c2")]),
                      AssistantTurn(text="stopped")])
    agent = Agent(be, repo, ui)
    agent.run_turn("wipe")
    assert agent.allowed == set()                    # ALWAYS must not whitelist destructive commands
    assert [e[0] for e in ui.log].count("confirm") == 2
    # first answer (ALWAYS) is treated as a plain yes, so the reset ran; the second was denied
    assert (repo / "a.txt").read_text() == "a\n"
    assert "denied" in be.seen[2][-1]["content"]


def test_blocked_never_prompts_or_runs(repo):
    ui = FakeUI()
    be = FakeBackend([AssistantTurn(tool_calls=[call("-c", "core.pager=evil", "log")]), AssistantTurn(text="no")])
    Agent(be, repo, ui).run_turn("x")
    assert not [e for e in ui.log if e[0] == "confirm"]
    assert "blocked" in be.seen[1][-1]["content"]


def test_dash_C_outside_repo_blocked(repo, tmp_path_factory):
    other = tmp_path_factory.mktemp("other")
    ui = FakeUI()
    be = FakeBackend([AssistantTurn(tool_calls=[call("-C", str(other), "status")]), AssistantTurn(text="no")])
    Agent(be, repo, ui).run_turn("x")
    assert "outside the repository" in be.seen[1][-1]["content"]


def test_bad_json_args_returned_to_model(repo):
    ui = FakeUI()
    bad = ToolCall(id="c1", name="git", arguments="{not json")
    be = FakeBackend([AssistantTurn(tool_calls=[bad]), AssistantTurn(text="retry later")])
    Agent(be, repo, ui).run_turn("x")
    assert "not valid JSON" in be.seen[1][-1]["content"]


def test_fallback_fenced_block_for_models_without_tool_calling(repo):
    ui = FakeUI()
    be = FakeBackend([AssistantTurn(text="Let me check.\n```git\ngit status --short\n```"), AssistantTurn(text="clean")])
    Agent(be, repo, ui).run_turn("status?")
    assert ("auto", "git status --short") in ui.log


def test_fallback_rejects_shell_syntax():
    assert tools.parse_fallback_calls("```bash\ngit log | head\n```") == []
    assert tools.parse_fallback_calls("```bash\ngit log && rm -rf x\n```") == []


def test_max_steps_stops_loop(repo):
    ui = FakeUI()
    be = FakeBackend([AssistantTurn(tool_calls=[call("status", id=f"c{i}")]) for i in range(5)])
    Agent(be, repo, ui, max_steps=3).run_turn("loop")
    assert any(e[0] == "notice" and "stopped after 3" in e[1] for e in ui.log)


def test_backend_error_drops_user_message(repo):
    from long.llm.base import BackendError

    class Boom:
        name = "boom"
        def chat(self, m, t): raise BackendError("down")

    ui = FakeUI()
    agent = Agent(Boom(), repo, ui)
    agent.run_turn("hi")
    assert [m["role"] for m in agent.messages] == ["system"]


def test_commit_without_message_cannot_hang(repo):
    (repo / "b.txt").write_text("b\n")
    git(repo, "add", "b.txt")
    r = tools.run_git(["commit"], repo, timeout=10)
    assert r.exit_code != 124


def test_resolve_workdir_non_repo(tmp_path):
    root, is_repo = tools.resolve_workdir(tmp_path)
    assert root == tmp_path.resolve() and not is_repo
    assert tools.resolve_workdir(tmp_path / "missing") is None


def test_resolve_workdir_repo(repo):
    sub = repo / "sub"
    sub.mkdir()
    root, is_repo = tools.resolve_workdir(sub)
    assert is_repo and root.resolve() == repo.resolve()


def test_non_repo_snapshot_and_init(tmp_path):
    ui = FakeUI(answers=[Approval.YES])
    be = FakeBackend([AssistantTurn(tool_calls=[call("init", "-b", "main")]), AssistantTurn(text="initialised")])
    agent = Agent(be, tmp_path.resolve(), ui)
    agent.run_turn("make this a repo")
    assert "not a git repository" in be.seen[0][0]["content"]
    assert ("confirm", "git init -b main", "MUTATE") in ui.log
    assert (tmp_path / ".git").is_dir()
    assert tools.repo_root(tmp_path) is not None


def test_json_text_tool_call_is_executed_and_summarised(tmp_path):
    ui = FakeUI(answers=[Approval.YES])
    be = FakeBackend([AssistantTurn(text='{"name": "Run", "arguments": ["init"]}'), AssistantTurn(text="")])
    Agent(be, tmp_path.resolve(), ui).run_turn("git init this project")
    assert (tmp_path / ".git").is_dir()
    assert ("text", "`git init` succeeded.") in ui.log


def test_failure_gets_narrative(repo):
    ui = FakeUI()
    be = FakeBackend([AssistantTurn(tool_calls=[call("show", "nosuchrev")]), AssistantTurn(text="")])
    Agent(be, repo, ui).run_turn("show it")
    texts = [x[1] for x in ui.log if x[0] == "text"]
    assert len(texts) == 1 and "`git show nosuchrev` failed (exit" in texts[0]


def test_denial_gets_narrative(repo):
    ui = FakeUI(answers=[Approval.NO])
    be = FakeBackend([AssistantTurn(tool_calls=[call("add", "a.txt")]), AssistantTurn(text="")])
    Agent(be, repo, ui).run_turn("stage")
    assert ("text", "You declined `git add a.txt`, so I didn't run it.") in ui.log


def test_model_text_is_not_overridden(repo):
    ui = FakeUI()
    be = FakeBackend([AssistantTurn(tool_calls=[call("status")]), AssistantTurn(text="clean")])
    Agent(be, repo, ui).run_turn("status")
    assert [x[1] for x in ui.log if x[0] == "text"] == ["clean"]


def test_parse_json_text_call_variants():
    p = tools.parse_json_text_call
    assert p('{"name": "git", "arguments": {"args": ["status"]}}') == ["status"]
    assert p('```json\n{"args": ["git", "log"]}\n```') == ["log"]
    assert p('{"name": "x", "arguments": "commit -m hi"}') == ["commit", "-m", "hi"]
    assert p("I will run {\"args\": [\"status\"]}") is None
    assert p('{"foo": 1}') is None


def gh_call(*argv, id="g1"):
    return ToolCall(id=id, name="gh", arguments=json.dumps({"args": list(argv)}))


def test_create_github_repo_flow(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(tools, "run_gh", lambda argv, cwd, timeout=120: (ran.append(argv), tools.RunResult(0, "https://github.com/u/x"))[1])
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@t"); git(tmp_path, "config", "user.name", "t")
    (tmp_path / "f.txt").write_text("x")
    ui = FakeUI(answers=[Approval.YES] * 3)
    be = FakeBackend([
        AssistantTurn(tool_calls=[call("add", "-A")]),
        AssistantTurn(tool_calls=[call("commit", "-m", "initial")]),
        AssistantTurn(tool_calls=[gh_call("repo", "create", "x", "--private", "--source=.", "--remote=origin")]),
        AssistantTurn(text="")])
    Agent(be, tmp_path.resolve(), ui).run_turn("create a github repo and push")
    assert ran == [["repo", "create", "x", "--private", "--source=.", "--remote=origin"]]
    assert ("confirm", "gh repo create x --private --source=. --remote=origin", "MUTATE") in ui.log
    final = [x[1] for x in ui.log if x[0] == "text"][-1]
    assert "`gh repo create x --private --source=. --remote=origin` succeeded." in final


def test_gh_source_outside_repo_blocked(tmp_path):
    ui = FakeUI()
    be = FakeBackend([AssistantTurn(tool_calls=[gh_call("repo", "create", "x", "--private", "--source=../..")]),
                      AssistantTurn(text="no")])
    Agent(be, tmp_path.resolve(), ui).run_turn("go")
    assert not any(x[0] == "confirm" for x in ui.log)


def test_gh_command_sent_to_git_tool_is_redirected_without_prompting(repo):
    ui = FakeUI()
    be = FakeBackend([AssistantTurn(tool_calls=[call("repo", "create", "x", "--private")]), AssistantTurn(text="ok")])
    Agent(be, repo, ui).run_turn("make a repo")
    assert not any(x[0] == "confirm" for x in ui.log)
    assert "Call the `gh` tool" in be.seen[1][-1]["content"]


def test_pasted_session_json_after_prose_runs_gh(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(tools, "run_gh", lambda argv, cwd, timeout=120: (ran.append(argv), tools.RunResult(0, "ok"))[1])
    text = ('It seems there was a misunderstanding. Let\'s try again.\n\n{"name": "gh", "arguments": '
            '{"args":["repo","create","long","--private","--source=.","--remote=origin"]}}')
    ui = FakeUI(answers=[Approval.YES])
    be = FakeBackend([AssistantTurn(text=text), AssistantTurn(text="done")])
    Agent(be, tmp_path.resolve(), ui).run_turn("create a github repo")
    assert ran == [["repo", "create", "long", "--private", "--source=.", "--remote=origin"]]


def test_embedded_json_needs_call_shape_and_uniqueness():
    p = tools.parse_json_text_tool_call
    assert p('see {"args": ["status"]} for an example') is None
    two = '{"name":"git","arguments":["log"]} then {"name":"git","arguments":["status"]}'
    assert p(two) is None                                   # single-call wrapper
    assert [c[1] for c in tools.parse_json_text_tool_calls(two)] == [["log"], ["status"]]
    assert tools.parse_json_text_tool_calls(two + ' and {"args": ["x"]}') != []  # extra loose object is ignored
    assert tools.parse_json_text_tool_calls('{"name":"git","arguments":["log"]} {"name":"git","arguments":[]}') == []
    assert p('ok: {"name": "gh", "arguments": ["auth", "status"]}') == ("gh", ["auth", "status"])


def test_snapshot_includes_remotes(repo):
    from long.agent import snapshot
    git(repo, "remote", "add", "origin", "git@github.com:me/longit.git")
    assert "origin\tgit@github.com:me/longit.git" in snapshot(repo)


def _junk_repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "app.py").write_text("x")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "app.cpython-312.pyc").write_text("x")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("x")
    (tmp_path / ".env").write_text("SECRET=1")


def test_add_all_offers_gitignore_and_writes_it(tmp_path):
    _junk_repo(tmp_path)
    ui = FakeUI(answers=[Approval.YES, Approval.YES])  # create .gitignore, then the add itself
    be = FakeBackend([AssistantTurn(tool_calls=[call("add", "-A")]), AssistantTurn(text="staged")])
    Agent(be, tmp_path.resolve(), ui).run_turn("stage everything")
    assert sorted((tmp_path / ".gitignore").read_text().split()) == [".env", ".venv/", "__pycache__/"]
    staged = git(tmp_path, "diff", "--cached", "--name-only").split()
    assert sorted(staged) == [".gitignore", "app.py"]


def test_gitignore_declined_still_stages_and_is_not_asked_twice(tmp_path):
    _junk_repo(tmp_path)
    ui = FakeUI(answers=[Approval.NO, Approval.YES, Approval.YES])
    be = FakeBackend([AssistantTurn(tool_calls=[call("add", "-A")]),
                      AssistantTurn(tool_calls=[call("add", "--all", id="c2")]), AssistantTurn(text="ok")])
    Agent(be, tmp_path.resolve(), ui).run_turn("stage everything")
    assert not (tmp_path / ".gitignore").exists()
    confirms = [x for x in ui.log if x[0] == "confirm"]
    assert len(confirms) == 3 and "create" in confirms[0][1]


def test_no_prompt_when_gitignore_exists_or_nothing_to_ignore(repo, tmp_path):
    ui = FakeUI(answers=[Approval.YES])
    be = FakeBackend([AssistantTurn(tool_calls=[call("add", "-A")]), AssistantTurn(text="ok")])
    Agent(be, repo, ui).run_turn("stage")           # `repo` has no junk
    assert [x for x in ui.log if x[0] == "confirm"] == [("confirm", "git add -A", "MUTATE")]
    (repo / ".gitignore").write_text("x\n")
    (repo / ".env").write_text("S=1")
    ui2 = FakeUI(answers=[Approval.YES])
    be2 = FakeBackend([AssistantTurn(tool_calls=[call("add", "-A")]), AssistantTurn(text="ok")])
    Agent(be2, repo, ui2).run_turn("stage")         # .gitignore present -> no offer
    assert len([x for x in ui2.log if x[0] == "confirm"]) == 1


def test_stages_everything_detection():
    from long.gitignore import stages_everything as f
    assert f(["add", "-A"]) and f(["add", "--all"]) and f(["add", "."]) and f(["add", "-Av"])
    assert not f(["add", "a.txt"]) and not f(["commit", "-a"]) and not f(["status"])


def test_two_json_text_calls_in_one_reply_run_in_order(repo):
    (repo / "b.txt").write_text("b")
    text = ('{"name": "git", "arguments": {"args":["add","b.txt"]}}\n\n'
            '{"name": "git", "arguments": {"args":["commit","-m","Add b"]}}')
    ui = FakeUI(answers=[Approval.YES, Approval.YES])
    be = FakeBackend([AssistantTurn(text=text), AssistantTurn(text="")])
    Agent(be, repo, ui).run_turn("commit")
    assert [x[1] for x in ui.log if x[0] == "confirm"] == ["git add b.txt", "git commit -m 'Add b'"]
    assert git(repo, "log", "--oneline").count("\n") == 2


def test_fabricated_tool_output_is_rejected_and_retried(repo):
    fake = "Staged.\n<tool_response>\nexit code: 0\n(no output)\n</tool_response>\nPushing changes to GitHub."
    ui = FakeUI()
    be = FakeBackend([AssistantTurn(text=fake), AssistantTurn(tool_calls=[call("log", "--oneline")]),
                      AssistantTurn(text="one commit")])
    Agent(be, repo, ui).run_turn("what is in the log")
    texts = [x[1] for x in ui.log if x[0] == "text"]
    assert texts == ["one commit"]                       # the made-up reply never reached the user
    assert ("auto", "git log --oneline") in ui.log       # and the real command then ran
    assert "made up" in be.seen[1][-1]["content"]        # the model was told why


def test_fabrication_gives_up_after_nudges(repo):
    fake = AssistantTurn(text="<tool_response>exit code: 0</tool_response>")
    ui = FakeUI()
    be = FakeBackend([fake, fake, fake])
    Agent(be, repo, ui).run_turn("do it")
    assert not any(x[0] == "text" for x in ui.log)
    assert any(x[0] == "notice" and "larger model" in x[1] for x in ui.log)


def test_permission_asking_is_nudged_into_a_tool_call(repo):
    ask = "1. Commit the changes.\n\nWould you like to proceed with this?"
    ui = FakeUI()
    be = FakeBackend([AssistantTurn(text=ask), AssistantTurn(tool_calls=[call("log", "--oneline")]),
                      AssistantTurn(text="one commit")])
    Agent(be, repo, ui).run_turn("commit the changes")
    assert [x[1] for x in ui.log if x[0] == "text"] == ["one commit"]
    assert ("auto", "git log --oneline") in ui.log
    assert "did not call a tool" in be.seen[1][-1]["content"]


def test_repeat_loop_across_turns_is_broken(repo):
    plan = "Sure, let's commit the changes. First I will stage and commit everything."
    ui = FakeUI()
    be = FakeBackend([AssistantTurn(text=plan),                                   # turn 1: plan, nothing run
                      AssistantTurn(text=plan.replace("Sure", "Great")),          # turn 2 ("yes"): same again -> stuck
                      AssistantTurn(tool_calls=[call("log", "--oneline")]),       # after prune + nudge: acts
                      AssistantTurn(text="done")])
    agent = Agent(be, repo, ui)
    agent.run_turn("commit the changes")
    agent.run_turn("yes")
    assert "approving what you just proposed" in be.seen[1][-1]["content"]        # approval hint on the bare "yes"
    after_nudge = be.seen[2]
    assert sum(m["role"] == "assistant" for m in after_nudge) == 0                # looped exchanges were pruned
    assert after_nudge[-1]["content"].startswith("(system) You described a plan")
    assert ("auto", "git log --oneline") in ui.log
    assert [x[1] for x in ui.log if x[0] == "text"][-1] == "done"


def test_repeat_loop_gives_up_with_notice(repo):
    plan = "Sure, let's commit the changes first."
    ui = FakeUI()
    be = FakeBackend([AssistantTurn(text=plan)] * 6)
    agent = Agent(be, repo, ui)
    agent.run_turn("commit")          # first plan is shown (nothing to compare with)
    agent.run_turn("yes")
    assert any(x[0] == "notice" and "stuck repeating" in x[1] for x in ui.log)
