---
name: undo-last-commit
description: Undo the most recent commit while keeping its changes
triggers: [undo the last commit, undo last commit, uncommit, take back the last commit, revert my last commit locally]
---
Goal: remove the latest commit from the branch but keep its changes in the working tree.

1. Run `git status -sb` and `git log --oneline -3`. If the branch shows it is not ahead of its upstream (the commit was already pushed), tell the user that undoing it rewrites shared history and offer `git revert HEAD` instead; do not continue with a reset.
2. Otherwise run `git reset --soft HEAD~1` (changes stay staged). Use `--mixed` only if the user asked to unstage them. Never use `--hard`.
3. Confirm with `git status --short` and say what is now staged.
