---
name: github-repo
description: Publish the current project to a new GitHub repository and push everything
triggers: [create a github repo, create a github repository, new github repo, publish this to github, put this on github, push this project to github]
---
Goal: get this project onto GitHub in a new repository.

1. Look first: `git status --short` and `git remote -v`. If an `origin` remote already exists, stop and tell the user instead of creating another repository.
2. If the directory is not a repository, `git init -b main`.
3. If `git status --short` shows secrets (.env, *.pem, keys, credentials), tell the user and ask before staging them.
4. `git add -A`, then `git commit` with no message (the harness writes it) if there is anything to commit.
5. `gh repo create NAME --private --source=. --remote=origin`. NAME defaults to the directory name. Use `--public` only if the user said public.
6. `git push -u origin HEAD`.

Skip steps that are already done. If a step fails, stop and explain it in plain words.
