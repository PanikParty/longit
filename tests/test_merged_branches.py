"""The merged-branches scripted skill: "Have all the branches been merged?" answered by the harness."""
import subprocess

import pytest

from long import skills as sk
from long import tools
from long.agent import Agent, Approval, Mode
from test_agent import FakeBackend, FakeUI, git, repo  # noqa: F401  (repo is a fixture)
from test_trace import TraceUI


def commit(root, name, text="x\n", msg=None):
    (root / name).write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", msg or f"add {name}")
    return git(root, "rev-parse", "HEAD").strip()


def world(tmp_path):
    """origin with main; `work` has these branches:
       merged-a    merge-committed into main           (merged by ancestry)
       squashed    squash-merged into main             (merged only via its PR)
       stale       PR merged, then one more commit     (NOT merged)
       open-b      pushed, open PR                     (NOT merged)
       local-only  never pushed                        (NOT merged)"""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    work = tmp_path / "work"
    work.mkdir()
    for c in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        git(work, *c)
    commit(work, "base.txt")
    git(work, "remote", "add", "origin", str(bare))
    git(work, "push", "-q", "-u", "origin", "main")
    tips = {}
    for name in ("merged-a", "squashed", "stale", "open-b", "local-only"):
        git(work, "switch", "-q", "-c", name, "main")
        tips[name] = commit(work, f"{name}.txt")
        git(work, "switch", "-q", "main")
    git(work, "merge", "-q", "--no-ff", "-m", "Merge merged-a", "merged-a")
    git(work, "merge", "-q", "--squash", "squashed")                                  # content in main, commits aren't
    git(work, "commit", "-q", "-m", "Squash squashed")
    git(work, "merge", "-q", "--squash", "stale")
    git(work, "commit", "-q", "-m", "Squash stale")
    git(work, "push", "-q", "origin", "main")
    for name in ("merged-a", "squashed", "stale", "open-b"):
        git(work, "push", "-q", "origin", name)
    git(work, "switch", "-q", "stale")
    tips["stale-new"] = commit(work, "stale-more.txt")                              # commit after its PR merged
    git(work, "push", "-q", "origin", "stale")
    git(work, "switch", "-q", "main")
    prs = [f"1 MERGED merged-a {tips['merged-a']}", f"2 MERGED squashed {tips['squashed']}",
           f"3 MERGED stale {tips['stale']}", f"4 OPEN open-b {tips['open-b']}"]
    return work, bare, prs


def stub_gh(monkeypatch, prs=None, fail=False, extra=""):
    ran = []

    def fake(argv, cwd, timeout=120):
        ran.append(argv)
        if fail:
            return tools.RunResult(127, "gh executable not found")
        if argv[:2] == ["pr", "list"]:
            return tools.RunResult(0, "\n".join(prs or []) + extra)
        return tools.RunResult(1, "unexpected")

    monkeypatch.setattr(tools, "run_gh", fake)
    return ran


def ask(root, text="Have all the branches been merged?", mode=Mode.AUTO, answers=(), ui=None):
    ui = ui or FakeUI(answers=answers)
    be = FakeBackend([])
    Agent(be, root, ui, mode=mode, skills=sk.SkillSet()).run_turn(text)
    return ui, be


def answer(ui):
    return [x[1] for x in ui.log if x[0] == "text"][-1]


def test_sorts_branches_into_merged_and_not_merged(tmp_path, monkeypatch):
    work, _, prs = world(tmp_path)
    stub_gh(monkeypatch, prs)
    ui, be = ask(work)
    a = answer(ui)
    assert a.startswith("No: 3 of 5 branch(es) are not merged into origin/main.")
    assert "stale  (local + origin, 2 commit(s) not in origin/main, PR #3 was merged but this branch has newer commits)" in a
    assert "local-only  (local only, 1 commit(s) not in origin/main, no PR)" in a
    assert "Merged (2): merged-a, squashed (PR #2)" in a                        # squash-merge recognised through its PR
    assert be.seen == []                                                        # the model was never involved


def test_open_pr_branch_is_reported_with_its_pr(tmp_path, monkeypatch):
    work, _, prs = world(tmp_path)
    stub_gh(monkeypatch, prs)
    a = answer(ask(work)[0])
    assert "open-b  (local + origin, 1 commit(s) not in origin/main, open PR #4)" in a


def test_all_merged(tmp_path, monkeypatch):
    work, bare, prs = world(tmp_path)
    for b in ("stale", "open-b", "local-only"):
        git(work, "branch", "-q", "-D", b)
        subprocess.run(["git", "push", "-q", "origin", "--delete", b], cwd=work, capture_output=True)
    stub_gh(monkeypatch, prs)
    a = answer(ask(work)[0])
    assert a.startswith("Yes: all 2 other branch(es) are merged into origin/main.") and "merged-a, squashed (PR #2)" in a


def test_without_github_squash_merges_show_as_unmerged_with_a_note(tmp_path, monkeypatch):
    work, _, _ = world(tmp_path)
    stub_gh(monkeypatch, fail=True)
    a = answer(ask(work)[0])
    assert "squashed" in a.split("Merged")[0] and "Merged (1): merged-a" in a
    assert "Note: GitHub wasn't checked, so squash- or rebase-merged branches show as not merged." in a


def test_manual_mode_asks_only_for_the_fetch_and_declining_adds_a_note(tmp_path, monkeypatch):
    work, _, prs = world(tmp_path)
    stub_gh(monkeypatch, prs)
    ui, _ = ask(work, mode=Mode.MANUAL, answers=[Approval.NO])
    assert [x[1] for x in ui.log if x[0] == "confirm"] == ["git fetch --prune origin"]
    assert "Note: you declined the fetch, so remote information may be out of date." in answer(ui)
    ui, _ = ask(work, mode=Mode.MANUAL, answers=[Approval.YES])
    assert "Note:" not in answer(ui)


def test_fetch_prune_refreshes_what_it_sees(tmp_path, monkeypatch):
    work, bare, prs = world(tmp_path)
    stub_gh(monkeypatch, prs)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    for c in (["config", "user.email", "o@o"], ["config", "user.name", "o"]):
        git(other, *c)
    git(other, "push", "-q", "origin", "--delete", "open-b")               # deleted on the remote after our last fetch
    assert "origin/open-b" in git(work, "branch", "-r")
    ask(work)
    assert "origin/open-b" not in git(work, "branch", "-r")                 # --prune applied


def test_plan_mode_does_not_fetch(tmp_path, monkeypatch):
    work, _, prs = world(tmp_path)
    stub_gh(monkeypatch, prs)
    ui, _ = ask(work, mode=Mode.PLAN)
    assert not any(x[0] == "confirm" for x in ui.log)
    assert "Note: plan mode didn't fetch, so remote information is as of the last fetch." in answer(ui)


def test_repo_without_origin_compares_local_branches(repo):
    git(repo, "switch", "-q", "-c", "done")
    commit(repo, "d.txt")
    git(repo, "switch", "-q", "-")
    git(repo, "merge", "-q", "--no-ff", "-m", "m", "done")
    git(repo, "switch", "-q", "-c", "wip")
    commit(repo, "w.txt")
    a = answer(ask(repo)[0])
    base = git(repo, "branch", "--list", "main", "master").strip().lstrip("* ")
    assert a.startswith(f"No: 1 of 2 branch(es) are not merged into {base}.")
    assert "wip (current)  (local only, 1 commit(s) not in" in a and "Merged (1): done" in a
    assert "Note: there is no origin, so only local branches were compared." in a


def test_no_other_branches_and_not_a_repo(repo, tmp_path_factory, monkeypatch):
    stub_gh(monkeypatch, [])
    assert "no other branches" in answer(ask(repo)[0])
    assert "isn't a git repository" in answer(ask(tmp_path_factory.mktemp("plain"))[0])


def test_long_lists_are_capped_and_a_truncated_pr_list_is_noted(tmp_path, monkeypatch):
    work, _, prs = world(tmp_path)
    for i in range(18):
        git(work, "branch", f"m{i:02}", "main")                              # already merged (same tip as main)
    stub_gh(monkeypatch, prs, extra="\n... [3 characters omitted] ...")
    a = answer(ask(work)[0])
    assert "... and 5 more" in a and "Note: the pull request list was too long to read in full." in a


@pytest.mark.parametrize("prompt", [
    "Have all the branches been merged?", "are all branches merged", "has every branch been merged into main?",
    "which branches are not merged", "which branches have been merged", "any unmerged branches?",
    "are all of the local branches merged", "Is every branch merged?", "branch status"])
def test_triggers(prompt):
    hit = sk.SkillSet().match(prompt)
    assert hit and hit[0].name == "merged-branches" and hit[0].workflow == "merged-branches", prompt


@pytest.mark.parametrize("prompt,other", [
    ("merge my branch into main", None), ("delete merged branches", None), ("rebase my PR", "rebase-pr"),
    ("what changed", "whats-new"), ("is the working tree clean", "check-changes"),
    ("prepare a pull request", "pr-branch"), ("have you merged the PR?", None)])
def test_does_not_hijack_other_requests(prompt, other):
    hit = sk.SkillSet().match(prompt)
    assert (hit[0].name if hit else None) == other, prompt


def test_only_read_commands_besides_the_fetch(tmp_path, monkeypatch):
    work, _, prs = world(tmp_path)
    ran = stub_gh(monkeypatch, prs)
    seen = []
    real = tools.run_git
    monkeypatch.setattr(tools, "run_git", lambda argv, cwd, timeout=60.0, max_chars=tools.MAX_OUTPUT_CHARS:
                        (seen.append(argv[0]), real(argv, cwd, timeout, max_chars))[1])
    ask(work)
    assert set(seen) <= {"status", "remote", "fetch", "for-each-ref", "merge-base", "rev-list", "rev-parse",
                         "symbolic-ref", "branch", "log"}, seen
    assert ran and all(c[:2] == ["pr", "list"] for c in ran)                # gh is only asked to list PRs


def test_trace_labels_the_workflow(tmp_path, monkeypatch):
    work, _, prs = world(tmp_path)
    stub_gh(monkeypatch, prs)
    ui = TraceUI()
    ask(work, ui=ui)
    titles = [e[1] for e in ui.events]
    assert any(t.startswith("workflow merged-branches: a fixed command sequence") for t in titles)
    assert any("tool call - gh" in t for t in titles) and not any(e[0] == "model" for e in ui.events)


def test_old_gh_without_headrefoid_falls_back_to_commits(tmp_path, monkeypatch):
    work, _, prs = world(tmp_path)
    ran = []

    def old_gh(argv, cwd, timeout=120):
        ran.append(argv)
        fields = argv[argv.index("--json") + 1]
        if "headRefOid" in fields:
            return tools.RunResult(1, 'Unknown JSON field: "headRefOid"\nAvailable fields:\n  commits')
        return tools.RunResult(0, "\n".join(prs))

    monkeypatch.setattr(tools, "run_gh", old_gh)
    a = answer(ask(work)[0])
    assert len(ran) == 2 and ran[1][ran[1].index("--limit") + 1] == "25" and ".commits[-1].oid" in ran[1][-1]
    assert "Merged (2): merged-a, squashed (PR #2)" in a and "GitHub wasn't checked" not in a    # squash-merge still recognised
    assert "only the 25 most recent" not in a                                                    # 4 PRs < 25: nothing was cut off


def test_old_gh_with_a_full_page_says_older_prs_were_not_checked(tmp_path, monkeypatch):
    work, _, prs = world(tmp_path)
    many = prs + [f"{100 + i} MERGED filler{i} {'0' * 40}" for i in range(25 - len(prs))]

    def old_gh(argv, cwd, timeout=120):
        if "headRefOid" in argv[argv.index("--json") + 1]:
            return tools.RunResult(1, 'Unknown JSON field: "headRefOid"')
        return tools.RunResult(0, "\n".join(many))

    monkeypatch.setattr(tools, "run_gh", old_gh)
    assert "Note: this gh version is old, so only the 25 most recent pull requests were checked." in answer(ask(work)[0])


def test_other_gh_errors_are_not_retried(tmp_path, monkeypatch):
    work, _, _ = world(tmp_path)
    ran = []

    def broken(argv, cwd, timeout=120):
        ran.append(argv)
        return tools.RunResult(1, "gh: not logged in")

    monkeypatch.setattr(tools, "run_gh", broken)
    a = answer(ask(work)[0])
    assert len(ran) == 1 and "GitHub wasn't checked" in a
