---
name: whats-new
description: Summarize what changed recently, since a tag, a date or a branch point
triggers: [what changed, what's new, whats new, summarize the changes, summarize recent changes, release notes, changelog]
---
Goal: give the user a short, accurate summary of recent changes. This is read-only; do not change anything.

1. Decide the range from the request: since a tag (`git describe --tags --abbrev=0`), since a date (`--since=...`), or the last 10 commits by default.
2. Run `git log --oneline <range>` and `git diff --stat <range>` (use the oldest commit's parent as the base).
3. Group the changes in 3-6 bullets by theme (features, fixes, docs, refactors), naming the files or areas involved. Do not paste raw command output.
