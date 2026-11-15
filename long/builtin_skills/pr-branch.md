---
name: pr-branch
action: true
description: Put every change in the working tree on a new PR branch with a commit, push it to origin and open the pull request, with no questions asked
triggers: [pr branch, pull request branch, prepare a pr, prepare a pull request, send these changes as a pr, push these changes to a new branch, commit everything to a new branch, "re:\b(branch|push)\b.*\bfor (a|the) (pr|pull request)\b"]
---
Goal: take ALL current changes (modified, staged and untracked files) and publish them on a fresh branch of this repository and open a pull request for it. Do the whole job yourself. Do NOT ask the user anything and do not offer choices: the only thing the user does is approve or decline the commands you run. If something blocks the job, stop and say plainly what and why in your final reply.

1. Inspect, read-only: `git status --short --branch`, `git remote -v`, then `git diff HEAD --stat`.
   - No changes at all: stop and say there is nothing to commit.
   - No `origin` remote: stop and say so (do not create a repository or add a remote).
2. Choose a branch name yourself from what the changes do: `pr/` + a short lowercase slug of at most 40 characters using only a-z, 0-9 and hyphens (for example `pr/add-skill-matching`). Check that `git branch --list NAME` prints nothing; if it exists, add `-2`, `-3`, ... until it is free.
3. Create it from the current commit: `git switch -c NAME`. Uncommitted changes come with you. Never commit on main or master.
4. Stage everything: `git add -A`. If step 1 showed files that look like secrets (.env, *.pem, *.key, credentials*), leave them out instead of asking: `git add -A -- . ':(exclude).env'` (one `':(exclude)PATH'` per such file), and mention them in the final reply.
5. Commit with NO message argument: `git commit`. The harness writes the commit message from the staged diff and shows it for approval. Never ask the user for a message.
6. Push the new branch: `git push -u origin NAME`. Never force-push.
7. Open the pull request with the gh tool and NO title or description arguments: `gh pr create`. The harness writes the title and description from the commits and shows them for approval. Never ask the user for them. If it fails (for example gh is not logged in, or a pull request already exists for the branch), the branch is still pushed: report the failure plainly.
8. Final reply, 2-4 sentences: the branch name, the commit subject, that it was pushed to origin, the pull request URL that `gh pr create` printed, and any files you left out.

If any command fails, stop and report the failure in plain words instead of improvising.
