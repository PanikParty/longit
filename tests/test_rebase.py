"""The rebase-pr scripted skill: when to rebase a PR branch, and how."""
import subprocess

import pytest

from long import skills as sk
from long import tools
from long.agent import Agent, Approval, Mode
from long.llm.base import AssistantTurn
from test_agent import FakeBackend, FakeUI, git, repo  # noqa: F401  (repo is a fixture)
from test_trace import TraceUI


def make(tmp_path, *, conflict=False, push_branch=True, advance_main=True):
    """origin (bare) with main; `work` on branch feat (one commit); `other` pushes new commits to main."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.email", "t@t")
    git(work, "config", "user.name", "t")
    (work / "a.txt").write_text("line one\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "init")
    git(work, "remote", "add", "origin", str(bare))
    git(work, "push", "-q", "-u", "origin", "main")
    git(work, "switch", "-q", "-c", "feat")
    if conflict:
        (work / "a.txt").write_text("line one, edited on feat\n")
    else:
        (work / "feat.txt").write_text("feat\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "Add feat work")
    if push_branch:
        git(work, "push", "-q", "-u", "origin", "feat")
    if advance_main:
        other = tmp_path / "other"
        subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
        git(other, "config", "user.email", "o@o")
        git(other, "config", "user.name", "o")
        (other / ("a.txt" if conflict else "main_new.txt")).write_text("line one, edited on main\n" if conflict else "new\n")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "Advance main")
        git(other, "push", "-q", "origin", "main")
    return work, bare


def stub_gh(monkeypatch, base="main", url="https://github.com/me/long/pull/9", fail=False):
    ran = []

    def fake(argv, cwd, timeout=120):
        ran.append(argv)
        if fail:
            return tools.RunResult(127, "gh executable not found")
        return tools.RunResult(0, base if "baseRefName" in argv else url)

    monkeypatch.setattr(tools, "run_gh", fake)
    return ran


def go(work, mode=Mode.MANUAL, answers=(), text="rebase my PR", ui=None):
    ui = ui or FakeUI(answers=answers)
    agent = Agent(FakeBackend([]), work, ui, mode=mode, skills=sk.SkillSet())
    agent.run_turn(text)
    return ui


def confirms(ui):
    return [(x[1], x[2]) for x in ui.log if x[0] == "confirm"]


def report(ui):
    return [x[1] for x in ui.log if x[0] == "text"][-1]


def test_rebases_when_behind_and_force_pushes_with_lease(tmp_path, monkeypatch):
    work, bare = make(tmp_path)
    stub_gh(monkeypatch)
    ui = go(work, answers=[Approval.YES] * 3)
    assert confirms(ui) == [("git fetch origin", "MUTATE"), ("git rebase origin/main", "MUTATE"),
                            ("git push --force-with-lease origin feat", "DESTRUCTIVE")]      # the push always asks
    assert git(work, "merge-base", "HEAD", "origin/main").strip() == git(work, "rev-parse", "origin/main").strip()
    assert git(work, "log", "--format=%s", "-3").split("\n")[:2] == ["Add feat work", "Advance main"]
    assert git(bare, "rev-parse", "feat").strip() == git(work, "rev-parse", "HEAD").strip()   # origin now has it
    r = report(ui)
    assert "Rebased `feat` onto `origin/main` (1 commit(s) replayed over 1 new upstream commit(s))" in r
    assert "force-pushed with --force-with-lease" in r and "Pull request: https://github.com/me/long/pull/9" in r


def test_auto_mode_still_asks_before_the_force_push(tmp_path, monkeypatch):
    work, bare = make(tmp_path)
    stub_gh(monkeypatch)
    before = git(bare, "rev-parse", "feat").strip()
    ui = go(work, mode=Mode.AUTO, answers=[Approval.NO])
    assert confirms(ui) == [("git push --force-with-lease origin feat", "DESTRUCTIVE")]      # fetch and rebase ran unattended
    assert "You declined `git push --force-with-lease origin feat`" in report(ui) and "local only" in report(ui)
    assert git(bare, "rev-parse", "feat").strip() == before                                  # nothing was force-pushed


def test_no_rebase_when_not_behind(tmp_path, monkeypatch):
    work, _ = make(tmp_path, advance_main=False)
    stub_gh(monkeypatch)
    head = git(work, "rev-parse", "HEAD").strip()
    ui = go(work, answers=[Approval.YES])
    assert [c[0] for c in confirms(ui)] == ["git fetch origin"]                              # only the fetch
    assert "already up to date with `origin/main` (1 commit(s) ahead of it), so no rebase is needed" in report(ui)
    assert git(work, "rev-parse", "HEAD").strip() == head


def test_conflicts_abort_the_rebase_and_report_files(tmp_path, monkeypatch):
    work, bare = make(tmp_path, conflict=True)
    stub_gh(monkeypatch)
    head = git(work, "rev-parse", "HEAD").strip()
    ui = go(work, answers=[Approval.YES] * 3)
    assert [c[0] for c in confirms(ui)] == ["git fetch origin", "git rebase origin/main", "git rebase --abort"]
    r = report(ui)
    assert "hit conflicts in a.txt" in r and "I aborted the rebase, so the branch is exactly as it was" in r
    assert git(work, "rev-parse", "HEAD").strip() == head and git(work, "branch", "--show-current").strip() == "feat"
    assert not (work / ".git" / "rebase-merge").exists() and not (work / ".git" / "rebase-apply").exists()


def test_never_pushed_branch_gets_a_plain_push(tmp_path, monkeypatch):
    work, bare = make(tmp_path, push_branch=False)
    stub_gh(monkeypatch)
    ui = go(work, answers=[Approval.YES] * 3)
    assert confirms(ui)[-1] == ("git push -u origin feat", "MUTATE")
    assert "pushed it." in report(ui) and "feat" in git(bare, "branch", "--list")


def test_refuses_protected_branch_dirty_tree_and_missing_origin(tmp_path, monkeypatch):
    work, _ = make(tmp_path)
    stub_gh(monkeypatch)
    (work / "feat.txt").write_text("dirty\n")
    ui = go(work)
    assert "uncommitted changes" in report(ui) and confirms(ui) == []
    git(work, "checkout", "--", "feat.txt")
    git(work, "switch", "-q", "main")
    ui = go(work)
    assert "never `main` itself" in report(ui) and confirms(ui) == []
    git(work, "switch", "-q", "feat")
    git(work, "remote", "remove", "origin")
    ui = go(work)
    assert "no `origin` remote" in report(ui) and confirms(ui) == []


def test_detached_head_and_not_a_repo(tmp_path, tmp_path_factory, monkeypatch):
    work, _ = make(tmp_path)
    stub_gh(monkeypatch)
    git(work, "checkout", "-q", "--detach")
    assert "HEAD is detached" in report(go(work))
    assert "isn't a git repository" in report(go(tmp_path_factory.mktemp("plain")))


def test_base_comes_from_the_pr_else_origin_head_else_main(tmp_path, monkeypatch):
    work, _ = make(tmp_path)
    ran = stub_gh(monkeypatch, base="main")
    go(work, answers=[Approval.YES] * 3)
    assert ["pr", "view", "--json", "baseRefName", "--jq", ".baseRefName"] in ran            # asked GitHub first
    work2, _ = make(tmp_path / "two") if (tmp_path / "two").mkdir() is None else (None, None)
    stub_gh(monkeypatch, fail=True)                                                          # no gh at all
    ui = go(work2, answers=[Approval.YES] * 3)
    assert ("git rebase origin/main", "MUTATE") in confirms(ui)                              # fell back to origin/main
    assert "Pull request:" not in report(ui)


def test_plan_mode_changes_nothing(tmp_path, monkeypatch):
    work, bare = make(tmp_path)
    stub_gh(monkeypatch)
    head = git(work, "rev-parse", "HEAD").strip()
    before = git(bare, "rev-parse", "feat").strip()
    stale = go(work, mode=Mode.PLAN)                                  # no fetch yet: origin/main looks unchanged
    assert "no rebase is needed. (As of the last fetch; plan mode doesn't fetch.)" in report(stale)
    git(work, "fetch", "-q")
    ui = go(work, mode=Mode.PLAN)
    r = report(ui)
    assert r.startswith("Plan (nothing was changed") and "git rebase origin/main" in r
    assert "git push --force-with-lease origin feat" in r and "it will ask you" in r
    assert confirms(ui) == [] and git(work, "rev-parse", "HEAD").strip() == head
    assert git(bare, "rev-parse", "feat").strip() == before


@pytest.mark.parametrize("prompt", ["rebase my PR", "Please rebase this pull request", "update my branch",
                                    "bring my PR up to date", "catch up with main", "rebase this branch onto main"])
def test_rebase_triggers(prompt):
    hit = sk.SkillSet().match(prompt)
    assert hit and hit[0].name == "rebase-pr" and hit[0].workflow == "rebase-pr", prompt


@pytest.mark.parametrize("prompt", ["squash my last 3 commits", "rebase -i HEAD~3", "undo the last commit",
                                    "prepare a pull request", "what changed"])
def test_rebase_skill_does_not_hijack_other_requests(prompt):
    hit = sk.SkillSet().match(prompt)
    assert not hit or hit[0].name != "rebase-pr", prompt


def test_trace_labels_the_workflow(tmp_path, monkeypatch):
    work, _ = make(tmp_path)
    stub_gh(monkeypatch)
    ui = TraceUI(answers=[Approval.YES] * 3)
    go(work, ui=ui)
    titles = [e[1] for e in ui.events]
    assert any(t.startswith("workflow rebase-pr: a fixed command sequence") for t in titles)
    assert "workflow rebase-pr - step 1 - tool call - git" in titles
    assert not any(e[0] == "model" for e in ui.events) and ui.events[-1][1].startswith("trajectory:")
