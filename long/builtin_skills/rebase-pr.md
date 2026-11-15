---
name: rebase-pr
description: Update a PR branch by rebasing it onto its base branch and pushing it, only when it is behind
action: true
workflow: rebase-pr
triggers: [rebase my pr, rebase the pr, rebase this pr, rebase my pull request, rebase this pull request, rebase my branch, rebase this branch, update my pr, update my pull request, update my branch, bring my branch up to date, bring my pr up to date, catch up with main, "re:\brebase\b.*\b(onto|on top of)\b.*\b(main|master|base)\b"]
---
Goal: bring a PR branch up to date with its base branch (usually main) by replaying its commits on top of the base.

When to rebase:
- The base has new commits the branch lacks (check `git rev-list --count HEAD..origin/BASE`), and the user asked to update, catch up or rebase, or the pull request shows conflicts or "branch is out of date".
- If the branch is already up to date, say so and do nothing.
- Only a feature or PR branch you own. Never rebase main, master or a branch other people have pushed commits to.

How:
1. Refuse if there are uncommitted changes (commit or stash first).
2. `git fetch origin`, then `git rebase origin/BASE` (BASE is the pull request's base, from `gh pr view --json baseRefName`).
3. If the rebase conflicts, run `git rebase --abort` so nothing is left half-done, and report which files conflicted. Do not guess at conflict resolutions.
4. Push with `git push --force-with-lease origin BRANCH` (never plain `--force`): a rebase rewrites the branch's commits, so the push is a force-push and the user must approve it. A branch that was never pushed gets a plain `git push -u origin BRANCH`.
5. Report how many commits were replayed and the pull request URL.
