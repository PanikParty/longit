---
name: check-changes
description: Say whether anything is uncommitted or changed in the working tree (read-only)
workflow: check-changes
triggers: [git status, working tree status, "re:\b(is|are) there (any|anything|something)\b(?:(?!\bbranch)[^.?!])*\b(uncommitted|changed|changes|modified|unstaged|untracked|dirty|to commit)\b", "re:\bdo i have (any |some )?(uncommitted|unstaged|unsaved|local|pending) (changes|work|files)\b", "re:\b(is|are) (the |my |this )?(working (tree|directory)|tree|repo|repository|checkout)( \w+)? (clean|dirty)\b", "re:\b(check|show|list)( me)?( for)?( any| all| the)? (uncommitted|unstaged|local|pending) (changes|files|work)\b"]
---
Goal: tell the user plainly whether anything in the working tree is uncommitted or changed.

1. Run `git status --short --branch`. Staged, unstaged and untracked files all count; say which is which.
2. Also say whether the branch is ahead of or behind its upstream, and mention stashes and any merge or rebase in progress.
3. If nothing is uncommitted, say so in one sentence. Do not change anything: this is read-only.
