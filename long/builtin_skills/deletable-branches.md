---
name: deletable-branches
description: List the branches that are safe to delete (merged, not current, no open PR), without deleting anything
workflow: deletable-branches
triggers: ["re:\b(which|what|list|give me|show)\b[^.?!]*\bbranch(es)?\b[^.?!]*\b(can|could|should|safe to|may|ok to|okay to) (i |we |you )?(safely )?(delete|remove|prune)\b", "re:\bbranches (to|i can|we can|that can be|that could be|that should be) (delete|remove|prune|deleted|removed|pruned)\b", "re:\b(safe|ok|okay) to (delete|remove|prune)\b[^.?!]*\bbranch", "re:\b(stale|old|dead|leftover|obsolete) branches\b"]
---
Goal: tell the user which branches they can safely delete, and show the commands, without deleting anything.

A branch is safe to delete only if ALL of these hold:
- It is merged into the base branch: its tip is in the base's history, or GitHub shows a merged pull request from that branch whose head commit is exactly that tip (squash and rebase merges).
- It is not the base branch, main, master or another long-lived branch, and not checked out here or in another worktree.
- It does not have an open pull request.

Branches that are not merged, or still have an open PR, are kept and listed with the reason. Never suggest deleting main, the current branch, or anything with unmerged work. Local branches are removed with `git branch -d` (`-D` for squash-merged ones); remote ones with `git push origin --delete`.
