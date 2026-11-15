---
name: merged-branches
description: Say whether every branch has been merged into the main branch, and list the ones that haven't
workflow: merged-branches
triggers: ["re:\b(have|are|were|has|is) (all|any|every|each)( of)?( the| my| our)?( other)?( local| remote)? branch(es)?\b[^.?!]*\bmerged\b", "re:\bwhich( local| remote)? branch(es)? (are|have been|were|aren't|haven't been|are not|have not been)( all)?( already)? merged\b", "re:\b(any|are there|is there any|list|show|check for) (the )?unmerged (branch|branches|work)\b", branch status, merged branches status]
---
Goal: tell the user plainly whether every branch has been merged into the main branch.

1. `git fetch --prune origin` so remote information is current.
2. For each local and origin branch other than main: it is merged if its tip is an ancestor of origin/main (`git merge-base --is-ancestor`), or if GitHub shows a merged pull request from that branch whose head commit is exactly that tip (squash and rebase merges never put the branch's own commits into main).
3. List the branches that are not merged, with where they exist, how many commits they have that main lacks, and any open PR. List the merged ones briefly. Do not delete or change anything.
