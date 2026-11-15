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
                         "symbolic-ref", "branch", "log", "worktree"}, seen
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


# ---- deletable-branches --------------------------------------------------------------------------
def branches(work):
    return sorted(b.strip().lstrip("* ") for b in git(work, "branch", "--format=%(refname:short)").split())


def test_deletable_lists_only_safe_branches_and_deletes_nothing(tmp_path, monkeypatch):
    work, bare, prs = world(tmp_path)
    stub_gh(monkeypatch, prs)
    before = branches(work)
    a = answer(ask(work, "Give me a list of all the branches that I can delete")[0])
    assert a.startswith("You can delete 2 branch(es); each is merged into origin/main:")
    assert "    merged-a  [local + origin]" in a
    assert "    squashed (PR #2)  [local + origin]  needs -D: squash/rebase-merged" in a
    assert "    git branch -d merged-a\n" in a and "    git branch -D squashed\n" in a
    assert "    git push origin --delete merged-a squashed\n" in a
    assert "stale  (not merged: local + origin, 2 commit(s) not in origin/main, PR #3 was merged but this branch has newer commits)" in a
    assert "open-b  (open PR #4)" in a and "local-only  (not merged: local only, 1 commit(s) not in origin/main, no PR)" in a
    assert "To delete them (nothing was deleted):" in a and a.rstrip().endswith("Compared with origin/main.")
    assert branches(work) == before and "merged-a" in git(bare, "branch", "--list")        # nothing was touched


def test_never_offers_the_current_branch_base_or_long_lived_branches(tmp_path, monkeypatch):
    work, _, prs = world(tmp_path)
    stub_gh(monkeypatch, prs)
    for name in ("develop", "release/1.0"):
        git(work, "branch", name, "main")                                            # merged (same tip as main)
    git(work, "switch", "-q", "merged-a")
    a = answer(ask(work, "which branches can I delete")[0])
    safe_part = a.split("Keep (")[0]
    assert "merged-a" not in safe_part.split("To delete")[0] and "develop" not in safe_part and "release/1.0" not in safe_part
    assert "Can't delete: develop (long-lived branch); merged-a (checked out here); release/1.0 (long-lived branch)." in a
    assert " main " not in safe_part and "main (" not in a                              # the base is never even considered


def test_branch_checked_out_in_another_worktree_is_not_offered(tmp_path, monkeypatch):
    work, _, prs = world(tmp_path)
    stub_gh(monkeypatch, prs)
    git(work, "worktree", "add", "-q", str(tmp_path / "wt"), "merged-a")
    a = answer(ask(work, "which branches can I delete")[0])
    assert "merged-a (checked out in another worktree)" in a and "git branch -d merged-a" not in a
    assert "squashed" in a.split("Keep (")[0]                                           # the others are still offered


def test_origin_only_merged_branch_gets_only_a_remote_delete(tmp_path, monkeypatch):
    work, bare, prs = world(tmp_path)
    git(work, "branch", "gone", "main")
    git(work, "push", "-q", "origin", "gone")
    git(work, "branch", "-D", "gone")
    stub_gh(monkeypatch, prs)
    a = answer(ask(work, "which branches can I delete")[0])
    assert "    gone  [origin only]" in a and "git push origin --delete gone merged-a squashed" in a
    assert "git branch -d merged-a\n" in a and "git branch -d merged-a gone" not in a


def test_nothing_safe_to_delete(tmp_path, monkeypatch):
    work, _, prs = world(tmp_path)
    for b in ("merged-a", "squashed"):
        git(work, "branch", "-q", "-D", b)
        subprocess.run(["git", "push", "-q", "origin", "--delete", b], cwd=work, capture_output=True)
    stub_gh(monkeypatch, prs)
    a = answer(ask(work, "which branches can I delete")[0])
    assert a.startswith("Nothing is safe to delete right now.") and "Keep (3):" in a and "To delete them" not in a


def test_without_github_squash_merged_branches_are_kept_and_a_note_says_why(tmp_path, monkeypatch):
    work, _, _ = world(tmp_path)
    stub_gh(monkeypatch, fail=True)
    a = answer(ask(work, "which branches can I delete")[0])
    assert a.startswith("You can delete 1 branch(es)") and "git branch -D" not in a
    assert "squashed  (not merged:" in a and "Note: GitHub wasn't checked" in a


def test_deletable_asks_only_for_the_fetch_in_manual_mode(tmp_path, monkeypatch):
    work, _, prs = world(tmp_path)
    stub_gh(monkeypatch, prs)
    ui, _ = ask(work, "which branches can I delete", mode=Mode.MANUAL, answers=[Approval.YES])
    assert [x[1] for x in ui.log if x[0] == "confirm"] == ["git fetch --prune origin"]
    ui, _ = ask(work, "which branches can I delete", mode=Mode.PLAN)
    assert not any(x[0] == "confirm" for x in ui.log) and "plan mode didn't fetch" in answer(ui)


def test_repo_without_origin(repo):
    git(repo, "switch", "-q", "-c", "done")
    commit(repo, "d.txt")
    git(repo, "switch", "-q", "-")
    git(repo, "merge", "-q", "--no-ff", "-m", "m", "done")
    git(repo, "switch", "-q", "-c", "wip")
    commit(repo, "w.txt")
    a = answer(ask(repo, "which branches can I delete")[0])
    assert "    done  [local only]" in a and "git branch -d done" in a and "git push origin --delete" not in a
    assert "wip  (checked out here)" in a or "wip (checked out here)" in a


@pytest.mark.parametrize("prompt", [
    "Give me a list of all the branches that I can delete", "which branches can I delete", "what branches are safe to delete?",
    "list the branches I can safely delete", "show me branches to delete", "is it safe to delete these branches",
    "any stale branches?", "which branches should I remove", "what are the branches that can be deleted"])
def test_deletable_triggers(prompt):
    hit = sk.SkillSet().match(prompt)
    assert hit and hit[0].name == "deletable-branches" and hit[0].workflow == "deletable-branches", prompt


@pytest.mark.parametrize("prompt,other", [
    ("delete the branch foo", None), ("delete merged branches", None), ("create a branch to delete the file", None),
    ("what branches exist", None), ("have all the branches been merged", "merged-branches"),
    ("which branches have been merged", "merged-branches"), ("rebase my PR", "rebase-pr")])
def test_deletable_does_not_hijack_other_requests(prompt, other):
    hit = sk.SkillSet().match(prompt)
    assert (hit[0].name if hit else None) == other, prompt


@pytest.mark.parametrize("prompt", [
    "Are there any branches left to merge?", "any branches left to merge", "what branches still need merging",
    "is anything left to merge", "branches left to merge?", "Which branches are still waiting to be merged?",
    "are there any PRs left to merge", "is there any work left to merge"])
def test_left_to_merge_phrasings_reach_the_scripted_check(prompt):
    hit = sk.SkillSet().match(prompt)
    assert hit and hit[0].name == "merged-branches" and hit[0].workflow == "merged-branches", prompt


@pytest.mark.parametrize("prompt", ["merge my branch into main", "merge the remaining branches into main",
                                    "create a branch to merge later", "delete merged branches"])
def test_merge_commands_are_not_mistaken_for_the_question(prompt):
    assert sk.SkillSet().match(prompt) is None, prompt


def test_left_to_merge_answers_with_the_open_pr_and_never_asks_the_model(tmp_path, monkeypatch):
    work, _, prs = world(tmp_path)
    stub_gh(monkeypatch, prs)
    ui, be = ask(work, "Are there any branches left to merge?")
    a = answer(ui)
    assert a.startswith("No: 3 of 5 branch(es) are not merged into origin/main.") and "open PR #4" in a
    assert be.seen == []
