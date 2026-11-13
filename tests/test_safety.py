import pytest
from long.safety import Risk, classify

R, M, D, B = Risk.READ, Risk.MUTATE, Risk.DESTRUCTIVE, Risk.BLOCKED

CASES = [
    (["status"], R), (["log", "--oneline", "-5"], R), (["diff", "--staged"], R), (["show", "HEAD"], R),
    (["branch"], R), (["branch", "-a"], R), (["branch", "--show-current"], R), (["branch", "feature"], M),
    (["branch", "-d", "old"], M), (["branch", "-D", "old"], D), (["branch", "-m", "new"], M),
    (["tag"], R), (["tag", "-l", "v*"], R), (["tag", "v1.0"], M), (["tag", "-d", "v1.0"], M),
    (["stash", "list"], R), (["stash"], M), (["stash", "pop"], M), (["stash", "drop"], D), (["stash", "clear"], D),
    (["remote", "-v"], R), (["remote", "add", "o", "u"], M),
    (["config", "--get", "user.name"], R), (["config", "user.name", "x"], M),
    (["add", "."], M), (["commit", "-m", "x"], M), (["commit", "--amend", "-m", "x"], M),
    (["reset", "HEAD~1"], M), (["reset", "--soft", "HEAD~1"], M), (["reset", "--hard"], D),
    (["clean", "-n"], R), (["clean", "-fd"], D),
    (["checkout", "main"], M), (["checkout", "-b", "x"], M), (["checkout", "--", "file"], D), (["checkout", "."], D),
    (["restore", "--staged", "f"], M), (["restore", "f"], D),
    (["switch", "main"], M), (["switch", "-f", "main"], D),
    (["push"], M), (["push", "origin", "main"], M), (["push", "--force"], D), (["push", "-f"], D),
    (["push", "--force-with-lease"], D), (["push", "origin", "+main"], D), (["push", "origin", ":old"], D),
    (["push", "--delete", "origin", "x"], D),
    (["rebase", "main"], M), (["rebase", "-i", "HEAD~3"], D),
    (["merge", "x"], M), (["pull"], M), (["fetch"], M),
    (["reflog"], R), (["reflog", "expire", "--all"], D), (["gc", "--prune=now"], D),
    (["rm", "f"], D), (["rm", "--cached", "f"], M),
    (["diff", "--output=/tmp/x"], M), (["log", "--ext-diff"], M),
    (["frobnicate"], M),
    (["-c", "core.sshCommand=evil", "fetch"], B), (["-ccore.x=y", "status"], B),
    (["status", "--upload-pack=evil"], B), ([], B), (["!rm -rf /"], B), (["../x"], B),
    (["-C", "sub", "status"], R),
]


@pytest.mark.parametrize("argv,expected", CASES, ids=[" ".join(c[0]) or "empty" for c in CASES])
def test_classify(argv, expected):
    got = classify(argv)
    assert got.risk is expected, f"{argv}: got {got.risk.name} ({got.reason})"


from long.safety import classify_gh


def test_gh_allowlist():
    assert classify_gh(["auth", "status"]).risk is Risk.READ
    assert classify_gh(["repo", "view"]).risk is Risk.READ
    v = classify_gh(["repo", "create", "x", "--private", "--source=.", "--remote=origin"])
    assert v.risk is Risk.MUTATE and v.subcommand == "gh repo create"
    assert classify_gh(["repo", "create", "x", "--public", "--source", "."]).risk is Risk.DESTRUCTIVE


@pytest.mark.parametrize("argv", [
    ["repo", "create", "x"],                              # no visibility
    ["repo", "create", "x", "--private", "--public"],     # ambiguous
    ["repo", "create", "x", "--private", "--clone"],      # not allowed flag
    ["repo", "create", "--private"],                      # no name
    ["repo", "delete", "x", "--yes"],
    ["api", "-X", "DELETE", "repos/a/b"],
    ["auth", "token"],
    ["repo", "view", "--web"],
])
def test_gh_blocked(argv):
    assert classify_gh(argv).risk is Risk.BLOCKED
