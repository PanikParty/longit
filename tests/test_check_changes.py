"""The check-changes scripted skill: "Is there anything uncommitted/changed here?" answered by the harness."""
import subprocess

import pytest

from long import skills as sk
from long import workflows
from long.agent import Agent, Mode
from test_agent import FakeBackend, FakeUI, git, repo  # noqa: F401  (repo is a fixture)
from test_trace import TraceUI


def ask(root, text="Is there anything uncommitted/changed here?", mode=Mode.MANUAL, ui=None):
    ui = ui or FakeUI()
    be = FakeBackend([])
    Agent(be, root, ui, mode=mode, skills=sk.SkillSet()).run_turn(text)
    return ui, be


def answer(ui):
    return [x[1] for x in ui.log if x[0] == "text"][-1]


def test_clean_tree(repo):
    ui, be = ask(repo)
    assert answer(ui).startswith("No: nothing is uncommitted, the working tree on `") and "is clean" in answer(ui)
    assert "has no upstream branch set" in answer(ui)
    assert be.seen == [] and not any(x[0] == "confirm" for x in ui.log)      # read-only, no model, no prompts


def test_staged_unstaged_and_untracked_are_told_apart(repo):
    (repo / "both.txt").write_text("1\n")
    git(repo, "add", "both.txt")
    git(repo, "commit", "-q", "-m", "both")
    (repo / "a.txt").write_text("changed\n")                                  # modified, not staged
    (repo / "new.py").write_text("x\n")
    git(repo, "add", "new.py")                                                # staged (new file)
    (repo / "scratch.txt").write_text("s\n")                                  # untracked
    (repo / "both.txt").write_text("2\n")
    git(repo, "add", "both.txt")
    (repo / "both.txt").write_text("3\n")                                     # staged AND modified again
    ui, _ = ask(repo)
    a = answer(ui)
    assert a.startswith("Yes: `")
    assert "Staged, ready to commit (2):\n    M both.txt\n    A new.py" in a
    assert "Changed but not staged (2):\n    M a.txt\n    M both.txt" in a
    assert "Untracked (1):\n    scratch.txt" in a


def test_deletes_renames_and_long_lists(repo):
    git(repo, "mv", "a.txt", "renamed.txt")
    for i in range(12):
        (repo / f"u{i:02}.txt").write_text("")
    a = answer(ask(repo)[0])
    assert "R renamed.txt" in a and "a.txt" not in a.replace("renamed.txt", "")           # new name only
    assert "Untracked (12):" in a and "u07.txt" in a and "u08.txt" not in a and "... and 4 more" in a
    git(repo, "reset", "-q", "--hard")
    (repo / "a.txt").unlink()
    assert "Changed but not staged (1):\n    D a.txt" in answer(ask(repo)[0])


def test_upstream_ahead_behind_and_stash(tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    work = tmp_path / "work"
    work.mkdir()
    for cmd in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        git(work, *cmd)
    (work / "a.txt").write_text("a\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "init")
    git(work, "remote", "add", "origin", str(bare))
    git(work, "push", "-q", "-u", "origin", "main")
    assert "Up to date with origin/main." in answer(ask(work)[0])
    (work / "b.txt").write_text("b\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "local")
    assert "Also: 1 commit(s) ahead of origin/main (not pushed)." in answer(ask(work)[0])
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    git(other, "config", "user.email", "o@o")
    git(other, "config", "user.name", "o")
    (other / "c.txt").write_text("c\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "remote")
    git(other, "push", "-q", "origin", "main")
    git(work, "fetch", "-q")
    assert "1 commit(s) ahead of origin/main (not pushed), 1 behind it." in answer(ask(work)[0])
    (work / "a.txt").write_text("stash me\n")
    git(work, "stash", "-q")
    a = answer(ask(work)[0])
    assert a.startswith("No:") and "There is 1 stash." in a


def test_merge_in_progress_with_conflict(repo):
    git(repo, "switch", "-q", "-c", "other")
    (repo / "a.txt").write_text("other side\n")
    git(repo, "commit", "-q", "-am", "other")
    git(repo, "switch", "-q", "-")
    (repo / "a.txt").write_text("this side\n")
    git(repo, "commit", "-q", "-am", "this")
    subprocess.run(["git", "merge", "other"], cwd=repo, capture_output=True)       # conflicts, exits non-zero
    a = answer(ask(repo)[0])
    assert a.startswith("Yes:") and "Conflicted (1):\n    a.txt" in a and "in the middle of a merge" in a


def test_not_a_repo_and_plan_mode(tmp_path_factory, repo):
    ui, _ = ask(tmp_path_factory.mktemp("plain"))
    assert "isn't a git repository" in answer(ui)
    (repo / "x.txt").write_text("x")
    ui, _ = ask(repo, mode=Mode.PLAN)                                              # read-only, so plan mode is fine
    assert answer(ui).startswith("Yes:") and "Untracked (1):\n    x.txt" in answer(ui)


def test_the_commands_it_runs_are_read_only(repo, monkeypatch):
    ran = []
    from long import tools
    real = tools.run_git
    monkeypatch.setattr(tools, "run_git", lambda argv, cwd, timeout=60.0, max_chars=tools.MAX_OUTPUT_CHARS:
                        (ran.append(argv[0]), real(argv, cwd, timeout, max_chars))[1])
    (repo / "x.txt").write_text("x")
    ask(repo)
    assert set(ran) <= {"status", "stash", "branch", "rev-parse", "rev-list", "diff", "log", "remote"}, ran


@pytest.mark.parametrize("prompt", [
    "Is there anything uncommitted/changed here?", "is there anything uncommitted here", "Is there anything to commit?",
    "do I have uncommitted changes", "is the working tree clean", "Is my repo clean?", "are there any changes here",
    "check for uncommitted changes", "show me uncommitted files", "git status", "is there anything modified"])
def test_triggers(prompt):
    hit = sk.SkillSet().match(prompt)
    assert hit and hit[0].name == "check-changes" and hit[0].workflow == "check-changes", prompt


@pytest.mark.parametrize("prompt,other", [
    ("commit all uncommitted changes", None), ("commit and push the changes", None), ("stash my uncommitted changes", None),
    ("prepare a pull request from all outstanding changes", "pr-branch"), ("what changed", "whats-new"),
    ("undo the last commit", "undo-last-commit"), ("is there a pr for this", None)])
def test_does_not_hijack_other_requests(prompt, other):
    hit = sk.SkillSet().match(prompt)
    assert (hit[0].name if hit else None) == other, prompt


def test_trace_labels_the_workflow(repo):
    ui = TraceUI()
    ask(repo, ui=ui)
    titles = [e[1] for e in ui.events]
    assert any(t.startswith("workflow check-changes: a fixed command sequence") for t in titles)
    assert "workflow check-changes - step 1 - tool call - git" in titles
    assert not any(e[0] == "model" for e in ui.events) and ui.events[-1][1].startswith("trajectory: 0 model call(s)")


def test_tree_state_unit(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "sp ace.txt").write_text("x")
    st = workflows.tree_state(tmp_path)
    assert st.untracked == ["sp ace.txt"] and st.dirty and not st.staged
