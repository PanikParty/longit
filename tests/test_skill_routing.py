"""One table of "what the user says -> which built-in skill answers", so a new trigger can't silently steal an old phrase."""
import pytest

from long import skills as sk

ROUTES = {
    "check-changes": [
        "Is there anything uncommitted/changed here?", "are there any changes here", "is there anything uncommitted on this branch?",
        "is there anything changed in this repo?", "Is there anything to commit?", "do I have uncommitted changes",
        "is the working tree clean", "check for uncommitted changes", "git status"],
    "merged-branches": [
        "Have all the branches been merged?", "which branches are not merged", "any unmerged branches?",
        "Are there any branches left to merge?", "what branches still need merging", "is anything left to merge",
        "Are there any branches with changes to merge?", "which branches have changes to merge",
        "branches with unmerged changes", "is there a branch with changes not yet merged", "are there branches ahead of main",
        "which branch has commits waiting to merge", "show unmerged changes",
        "are there any branches that need to be merged int main?", "are there any branches that need to be merged into main?",
        "any branches that need merging", "which branches need to be merged", "are there branches to merge into main",
        "branches waiting to be merged into main", "do any branches need merging", "is there anything that needs to be merged",
        "does anything need merging", "which branches were merged last week?", "How many branches are not merged yet?"],
    "deletable-branches": [
        "Give me a list of all the branches that I can delete", "which branches can I delete", "what branches are safe to delete?",
        "show me branches to delete", "any stale branches?", "which branches can I delete once they're merged"],
    "pr-branch": ["prepare a pull request from all outstanding changes", "prepare a pull request", "make a PR branch from my changes"],
    "rebase-pr": ["rebase my PR", "update my branch", "bring my PR up to date", "catch up with main"],
    "undo-last-commit": ["undo the last commit", "uncommit"],
    "whats-new": ["what changed", "summarize the changes", "write the release notes"],
    "github-repo": ["create a github repo for this project", "publish this to github"],
}
# imperatives and other requests that must be left to the model (no skill)
NO_SKILL = ["merge my branch into main", "merge the changes from feature into main", "merge the remaining branches into main",
            "what changes are in this branch", "commit the changes in this branch", "commit all uncommitted changes",
            "stash my uncommitted changes", "delete merged branches", "delete the branch foo", "create a branch to merge later",
            "create a branch to delete the file", "squash my last 3 commits", "rebase -i HEAD~3", "what branches exist",
            "merge the branches into main", "what happens if I merge this branch", "how do I merge a branch into main",
            "should I merge this branch?", "does merging a branch rewrite history", "why was the branch not merged by the bot",
            "if I merge branches will it delete them"]


@pytest.mark.parametrize("skill,prompt", [(s, p) for s, ps in ROUTES.items() for p in ps])
def test_prompt_routes_to_its_skill(skill, prompt):
    hit = sk.SkillSet().match(prompt)
    assert hit is not None and hit[0].name == skill, f"{prompt!r} -> {hit[0].name if hit else None}, expected {skill}"


@pytest.mark.parametrize("prompt", NO_SKILL)
def test_prompt_is_left_to_the_model(prompt):
    hit = sk.SkillSet().match(prompt)
    assert hit is None, f"{prompt!r} unexpectedly matched {hit[0].name}"


def test_every_builtin_skill_is_covered_by_the_table():
    assert {s.name for s in sk.SkillSet().all()} == set(ROUTES)
