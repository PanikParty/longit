"""Scripted skills: the harness, not the model, drives the pr-branch command sequence."""
import subprocess

import pytest

from long import tools, workflows
from long import skills as sk
from long.agent import Agent, Approval, Mode
from long.llm.base import AssistantTurn, BackendError
from test_agent import FakeBackend, FakeUI, git, repo  # noqa: F401  (repo is a fixture)
from test_skills import _remote_repo, empty_set, stub_gh, write
from test_trace import TraceUI

SLUG = AssistantTurn(text="add-new-module")
COMMIT = AssistantTurn(text="Add new module and tweak a.txt\n\n- new_module.py prints a greeting")
PR = AssistantTurn(text="Add new module\n\nAdds a small module.")


def run(work, turns, answers=(), mode=Mode.MANUAL, ui=None, text="prepare a pull request from my changes"):
    ui = ui or FakeUI(answers=answers)
    be = FakeBackend(list(turns))
    agent = Agent(be, work, ui, mode=mode, skills=sk.SkillSet())
    agent.run_turn(text)
    return agent, ui, be


def confirms(ui):
    return [x[1] for x in ui.log if x[0] == "confirm"]


def reports(ui):
    return [x[1] for x in ui.log if x[0] == "text"]


def test_full_flow_is_driven_by_the_harness(tmp_path, monkeypatch):
    work, bare = _remote_repo(tmp_path)
    ran = stub_gh(monkeypatch)
    agent, ui, be = run(work, [SLUG, COMMIT, PR], answers=[Approval.YES] * 5)
    c = confirms(ui)
    assert c[:4] == ["git switch -c pr/add-new-module", "git add -A -- . ':(exclude,literal).env'",
                     "git commit -m 'Add new module and tweak a.txt' -m '- new_module.py prints a greeting'",
                     "git push -u origin pr/add-new-module"]
    assert c[4].startswith("gh pr create --title 'Add new module' --body ") and len(c) == 5   # only permissions
    assert ran == [["pr", "create", "--title", "Add new module", "--body", "Adds a small module."]]
    assert git(work, "branch", "--show-current").strip() == "pr/add-new-module"
    committed = sorted(git(work, "show", "--name-only", "--format=", "HEAD").split())
    assert committed == [".gitignore", "a.txt", "new_module.py"]                                # .env left out
    assert "pr/add-new-module" in git(bare, "branch", "--list")
    assert git(work, "rev-parse", "main").strip() == git(bare, "rev-parse", "main").strip()   # main untouched
    report = reports(ui)[-1]
    assert "Opened a pull request: https://github.com/me/long/pull/7" in report
    assert "pr/add-new-module" in report and "Left out as secret-looking: .env" in report
    assert len(be.seen) == 3 and all([m["role"] for m in s] == ["system", "user"] for s in be.seen)   # model only wrote text
    assert agent.messages[-1] == {"role": "assistant", "content": report}


def test_auto_mode_runs_without_any_prompt(tmp_path, monkeypatch):
    work, bare = _remote_repo(tmp_path)
    stub_gh(monkeypatch)
    _, ui, _ = run(work, [SLUG, COMMIT, PR], mode=Mode.AUTO)
    assert confirms(ui) == [] and "pr/add-new-module" in git(bare, "branch", "--list")


def test_plan_mode_only_describes(tmp_path):
    work, bare = _remote_repo(tmp_path)
    _, ui, be = run(work, [SLUG], mode=Mode.PLAN)
    assert reports(ui)[-1].startswith("Plan (nothing was changed):") and "git switch -c pr/add-new-module" in reports(ui)[-1]
    assert git(work, "branch", "--show-current").strip() == "main" and confirms(ui) == [] and len(be.seen) == 1
    assert "pr/" not in git(bare, "branch", "--list")


def test_preconditions_stop_early_without_calling_the_model(repo, tmp_path):
    be_turns = []
    _, ui, be = run(repo, be_turns)                                  # clean tree
    assert "Nothing to commit" in reports(ui)[-1] and be.seen == []
    (repo / "x.txt").write_text("x")                                 # changes but no origin
    _, ui, be = run(repo, be_turns)
    assert "no `origin` remote" in reports(ui)[-1] and be.seen == [] and confirms(ui) == []


def test_not_a_git_repository(tmp_path_factory):
    plain = tmp_path_factory.mktemp("plain")
    _, ui, be = run(plain, [])
    assert "isn't a git repository" in reports(ui)[-1] and be.seen == []


def test_declining_a_step_stops_and_says_where(tmp_path, monkeypatch):
    work, bare = _remote_repo(tmp_path)
    stub_gh(monkeypatch)
    _, ui, _ = run(work, [SLUG], answers=[Approval.NO])
    assert "You declined `git switch -c pr/add-new-module`" in reports(ui)[-1] and "Nothing was changed" in reports(ui)[-1]
    assert git(work, "branch", "--show-current").strip() == "main"
    work2, bare2 = _remote_repo(tmp_path / "two") if (tmp_path / "two").mkdir() is None else (None, None)
    _, ui2, _ = run(work2, [SLUG, COMMIT], answers=[Approval.YES] * 3 + [Approval.NO])
    r = reports(ui2)[-1]
    assert "You declined `git push -u origin pr/add-new-module`" in r and 'committed "Add new module and tweak a.txt"' in r
    assert "pr/add-new-module" not in git(bare2, "branch", "--list")


def test_push_failure_stops_before_the_pr(tmp_path, monkeypatch):
    work, _ = _remote_repo(tmp_path)
    ran = stub_gh(monkeypatch)
    git(work, "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))
    _, ui, _ = run(work, [SLUG, COMMIT], answers=[Approval.YES] * 4)
    assert "`git push -u origin pr/add-new-module` failed" in reports(ui)[-1] and ran == []


def test_pr_failure_reports_that_the_branch_is_pushed(tmp_path, monkeypatch):
    work, bare = _remote_repo(tmp_path)
    monkeypatch.setattr(tools, "run_gh", lambda argv, cwd, timeout=120: tools.RunResult(1, "gh: not logged in"))
    _, ui, _ = run(work, [SLUG, COMMIT, PR], answers=[Approval.YES] * 5)
    r = reports(ui)[-1]
    assert "is committed" in r and "pushed to origin, but opening the pull request failed: gh: not logged in" in r
    assert "pr/add-new-module" in git(bare, "branch", "--list")


@pytest.mark.parametrize("reply,expected", [("Add `new module`!!", "pr/add-new-module"), ("pr/Fix the Bug", "pr/fix-the-bug"),
                                            ("  \n", "pr/changes-"), ("??", "pr/changes-")])
def test_branch_name_is_sanitised_with_a_fallback(tmp_path, monkeypatch, reply, expected):
    work, _ = _remote_repo(tmp_path)
    stub_gh(monkeypatch)
    _, ui, _ = run(work, [AssistantTurn(text=reply), COMMIT, PR], mode=Mode.AUTO)
    assert git(work, "branch", "--show-current").strip().startswith(expected)


def test_branch_name_survives_a_failing_backend_and_avoids_collisions(tmp_path, monkeypatch):
    work, _ = _remote_repo(tmp_path)
    stub_gh(monkeypatch)

    class FailsOnce(FakeBackend):
        failed = False

        def chat(self, messages, tools_):
            if not self.failed:
                self.failed = True
                raise BackendError("down")
            return super().chat(messages, tools_)

    ui = FakeUI()
    Agent(FailsOnce([COMMIT, PR]), work, ui, mode=Mode.AUTO, skills=sk.SkillSet()).run_turn("prepare a pull request")
    assert git(work, "branch", "--show-current").startswith("pr/changes-")
    assert any(x[0] == "notice" and "could not name the branch" in x[1] for x in ui.log)
    work2, _ = _remote_repo(tmp_path / "b") if (tmp_path / "b").mkdir() is None else (None, None)
    git(work2, "branch", "pr/add-new-module")
    run(work2, [SLUG, COMMIT, PR], mode=Mode.AUTO)
    assert git(work2, "branch", "--show-current").strip() == "pr/add-new-module-2"


def test_only_secret_files_changed(tmp_path):
    work, bare = _remote_repo(tmp_path)
    git(work, "checkout", "--", "a.txt")
    (work / "new_module.py").unlink()
    (work / ".gitignore").unlink()
    _, ui, _ = run(work, [SLUG], answers=[Approval.YES, Approval.NO, Approval.YES])   # switch, decline .gitignore, add
    assert "nothing to stage (only secret-looking files changed: .env)" in reports(ui)[-1]


def test_gitignore_guard_still_applies_inside_the_workflow(tmp_path, monkeypatch):
    work, _ = _remote_repo(tmp_path)
    (work / ".gitignore").unlink()
    (work / ".venv").mkdir()
    (work / ".venv" / "pyvenv.cfg").write_text("x")
    stub_gh(monkeypatch)
    _, ui, _ = run(work, [SLUG, COMMIT, PR], answers=[Approval.YES] * 6)
    c = confirms(ui)
    assert c[0].startswith("git switch") and c[1].startswith("create ") and ".gitignore" in c[1] and c[2].startswith("git add -A")
    assert ".venv" not in git(work, "show", "--name-only", "--format=", "HEAD")


def test_trace_labels_workflow_steps_and_harness_calls(tmp_path, monkeypatch):
    work, _ = _remote_repo(tmp_path)
    stub_gh(monkeypatch)
    ui = TraceUI(answers=[Approval.YES] * 5)
    run(work, [SLUG, COMMIT, PR], ui=ui)
    titles = [e[1] for e in ui.events]
    assert any(t.startswith("workflow pr-branch: a fixed command sequence") for t in titles)
    assert "workflow pr-branch - step 1 - tool call - git" in titles and any("tool call - gh" in t for t in titles)
    assert any("separate model call: branch name" in t for t in titles)
    assert "model" not in [e[0] for e in ui.events]                      # the model never drove a step
    assert "tool calls: gh x1, git x" in ui.events[-1][1]


def test_helpers(tmp_path):
    g = workflows
    assert g.secret_like(".env") and g.secret_like("cfg/.env.local") and g.secret_like("keys/server.pem")
    assert g.secret_like("credentials.json") and g.secret_like("a/id_rsa")
    assert not g.secret_like(".env.example") and not g.secret_like("environment.py") and not g.secret_like("README.md")
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a b.txt").write_text("x")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "i")
    git(tmp_path, "mv", "a b.txt", "c d.txt")
    (tmp_path / "new.py").write_text("")
    assert sorted(g.changed_paths(tmp_path)) == ["c d.txt", "new.py"]
    from long.commitmsg import slugify
    assert slugify("Add `Skills`: matching, v2!") == "add-skills-matching-v2"
    assert len(slugify("word " * 30)) <= 40 and slugify("pr/fix it") == "fix-it" and slugify("!!") == ""


def test_unknown_workflow_falls_back_to_the_model(repo, tmp_path):
    f = write(tmp_path, "weird", "[do weird]", body="BODY")
    f.write_text(f.read_text().replace("---\nname", "---\nworkflow: nope\nname", 1))
    ui = FakeUI()
    be = FakeBackend([AssistantTurn(text="Handled by the model.")])
    Agent(be, repo, ui, skills=empty_set(tmp_path)).run_turn("please do weird things")
    assert any(x[0] == "notice" and "unknown workflow 'nope'" in x[1] for x in ui.log)
    assert reports(ui) == ["Handled by the model."] and "BODY" in be.seen[0][0]["content"]


def test_explicit_skill_request_runs_the_workflow_too(tmp_path, monkeypatch):
    work, _ = _remote_repo(tmp_path)
    stub_gh(monkeypatch)
    s = sk.SkillSet()
    ui = FakeUI()
    agent = Agent(FakeBackend([SLUG, COMMIT, PR]), work, ui, mode=Mode.AUTO, skills=s, skill_match="off")
    agent.run_turn("go", skill=s.get("pr-branch"))
    assert "Opened a pull request" in reports(ui)[-1]


@pytest.mark.parametrize("text,skipped", [("yes", True), ("Please do.", True), ("go ahead", True), ("continue!", True),
                                         ("please do weird things", False), ("go through the log", False)])
def test_only_a_bare_approval_suppresses_skill_matching(repo, tmp_path, text, skipped):
    write(tmp_path, "weird", "[do weird, go through, go ahead]", body="BODY")
    be = FakeBackend([AssistantTurn(text="ok")])
    Agent(be, repo, FakeUI(), skills=empty_set(tmp_path)).run_turn(text)
    assert ("BODY" in be.seen[0][0]["content"]) is (not skipped)
