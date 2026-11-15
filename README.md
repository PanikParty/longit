# long

A natural-language interface to git. Type what you want ("squash my last 3 commits",
"what changed on main since Tuesday?", "prepare a pull request"), and `long` works out the git commands, runs the
safe ones, and asks you before it changes anything.

```
› prepare a pull request from all outstanding changes
skill: pr-branch (matched trigger 'prepare a pull request')
  $ git status --short --branch
  $ git remote get-url origin
  $ git switch -c pr/add-skill-matching                      [changes repo]  run? [y/N/a]
  $ git add -A                                               [changes repo]  run? [y/N/a]
  $ git commit -m 'Add skill matching'                       [changes repo]  run? [y/N/a]
  $ git push -u origin pr/add-skill-matching                 [changes repo]  run? [y/N/a]
  $ gh pr create --title 'Add skill matching' --body '...'   [changes repo]  run? [y/N/a]
Opened a pull request: https://github.com/you/repo/pull/3
```

- **Local or remote model.** Any OpenAI-compatible server (Ollama, llama-server, LM Studio, vLLM, OpenAI), or an
  in-process GGUF model through llama.cpp.
- **No shell for the model.** Its tools are `git`, a small allow-listed `gh` (create a repo, open and read pull
  requests), and `ls`, `pwd` and `cd` (confined to the project; leaving it needs your approval).
- **You stay in control.** Read-only commands run immediately. Anything that changes something is shown and needs
  your approval; destructive commands always ask. Three modes: `manual`, `auto`, `plan`.
- **The harness writes the boilerplate.** A `git commit` with no message and a `gh pr create` with no title or
  description get them written by the model from the diff, and you see the real text in the approval prompt.
- **Skills.** Stored instructions for kinds of request, matched to your prompt. Some are scripted workflows that the
  harness runs step by step itself, so a small model can't wander off.
- **See everything.** `--trace` shows the model's thinking, every tool call and its complete result, and everything
  the harness did on its own.
- Works in directories that aren't git repositories yet; `git init` is just another approved step.

## Requirements

- [uv](https://docs.astral.sh/uv/) (no `pip install` needed) and Python 3.11+
- `git`
- [`gh`](https://cli.github.com), only for creating GitHub repositories and pull requests (run `gh auth login` first)
- A model: for example [Ollama](https://ollama.com) with `ollama pull qwen2.5-coder:14b`

## Install and run

The quickest way is the helper script, which runs `uv sync` and then launches `long`
(arguments are passed through, and it works on the directory you run it from):

```bash
./run.sh
```

Or do it by hand:

```bash
uv sync                    # create .venv and install dependencies
uv run long                # interactive mode in the current directory
uv run long "show me the last 5 commits"     # one-shot request
uv run long -C ~/code/app  # work in another directory
```

To install `long` as a command on your PATH:

```bash
uv tool install .
```

For the in-process llama.cpp backend, add the extra: `uv sync --extra local`.

## Usage

Describe what you want in plain English. In the interactive prompt these slash commands are available:

| Command | What it does |
|---|---|
| `/help` | show help |
| `/status` | `git status` of the current directory |
| `/mode` | show or set the mode: `manual`, `auto` or `plan` (shortcuts `/manual`, `/auto`, `/plan`) |
| `/skills`, `/skill NAME [request]` | list stored skills; run one explicitly |
| `/trace` | toggle the trajectory trace (`/trace on`, `/trace off`) |
| `/model` | list the server's models and pick one (`/model NAME` or `/model N` to switch directly) |
| `/backend` | show the active backend and model |
| `/allowed` | subcommands auto-approved this session |
| `/clear` | forget the conversation |
| `/exit` | quit (Ctrl-D also works) |

Ctrl-C cancels the current step. A bare "yes", "go" or "continue" is treated as approval of what the model just
proposed.

## Safety

### Approvals

| Risk | Behaviour | Examples |
|---|---|---|
| READ | runs automatically | `status`, `log`, `diff`, `show`, `branch` (list), `gh pr view` |
| MUTATE | shows the command and asks `[y/N/a]`; `a` allows that subcommand for the session | `add`, `commit`, `checkout`, `merge`, `push`, `gh repo create --private`, `gh pr create` |
| DESTRUCTIVE | shows a warning and asks every time | `reset --hard`, `clean -f`, `push --force`, `branch -D`, `gh repo create --public` |
| BLOCKED | refused, and the model is told why | `-c` config overrides, `--exec`, aliases, `-C` outside the project, any `gh` command not listed below |

Unknown git subcommands are treated as mutating. Even in `auto` mode, destructive commands still ask and blocked
ones stay blocked; there is no "approve everything" mode. With no terminal to answer (for example `< /dev/null`),
a prompt counts as "no".

### Modes

| Mode | Behaviour |
|---|---|
| `manual` (default) | read-only commands run; every change asks first |
| `auto` | reads and ordinary changes (`add`, `commit`, `push`, `gh pr create`, private `gh repo create`, a generated `.gitignore`) run without asking; DESTRUCTIVE commands, leaving the project with `cd`, and anything BLOCKED behave as before |
| `plan` | read-only: the model investigates and replies with a plan; any change it attempts is skipped and nothing is modified. Scripted workflows print their plan |

Switch inside the REPL with `/mode manual|auto|plan` (or `/manual`, `/auto`, `/plan`). Start in a mode with
`--mode`, or set `mode = "auto"` in the config. A typical flow is to ask in `plan` mode, read the plan, switch to
`manual` or `auto`, and say "go".

### GitHub access (`gh`)

The `gh` tool is deliberately tiny. Only these are allowed; everything else is blocked:

| Command | Risk | Notes |
|---|---|---|
| `gh auth status`, `gh repo view` | READ | no `--web` |
| `gh pr view`, `gh pr list`, `gh pr status` | READ | no `--web`, no `--repo/-R` |
| `gh repo create NAME --private\|--public` | MUTATE / DESTRUCTIVE | a visibility flag is required; `--public` is DESTRUCTIVE; `--source`, `--remote`, `--description` allowed |
| `gh pr create` | MUTATE | opens a pull request for the pushed branch. Allowed: `--title`, `--body`, `--base`, `--head`, `--fill`, `--draft`. No `--body-file`, `--template`, `--web`, `--repo` |

### Other safeguards

- **`.gitignore` guard.** Before `git add -A` (or `--all`, `.`) in a repo with no `.gitignore`, `long` looks for files that
  shouldn't be committed (`.venv/`, `__pycache__/`, `node_modules/`, `.env`, `.DS_Store`, ...) and asks whether to create
  a `.gitignore` for them first.
- **Secrets.** The `pr-branch` workflow leaves out files that look like secrets (`.env`, `*.pem`, `*.key`,
  `credentials*`, SSH keys) and tells you which.
- **Filesystem confinement.** `ls` and `cd` stay inside the project (symlinks that point outside are refused).
  `cd` out of it asks for approval and starts a fresh project context.
- **Invented output and loops.** Replies containing made-up tool results, or a copy of the model's own instructions,
  are rejected; a model that only describes a plan, asks permission, or repeats itself is nudged to actually call a
  tool, and `long` gives up with a message after a couple of tries.
- **Project skills are untrusted** (see below).

## Skills

A skill is a stored, named set of instructions for a kind of request. When your prompt matches one, `long` adds its
instructions to the model's context for that turn and says so (`skill: undo-last-commit (matched trigger ...)`).
Skills only add guidance: every command still goes through the safety tiers, the current mode and your approvals.

Built in:

| Skill | What it does |
|---|---|
| `pr-branch` | scripted workflow: put every change on a new `pr/...` branch, commit, push to `origin` and open the pull request, asking you only for approvals. Try "prepare a pull request" |
| `github-repo` | publish the current project to a new private GitHub repo and push |
| `undo-last-commit` | undo the latest commit but keep its changes (offers `revert` if it was already pushed) |
| `whats-new` | summarize recent changes (since a tag, a date, or the last commits) |

Add your own as markdown files in `~/.config/long/skills/` (yours everywhere) or `.long/skills/` inside a repo
(project skills). Later layers override earlier ones by name: built-in, then yours, then the project's.

```markdown
---
name: tidy-branches
description: Delete local branches that are already merged
triggers: [tidy branches, clean up branches, "re:\bprune (old )?branches\b"]
---
1. `git branch --merged main` and show the list.
2. Never touch main, master or the current branch.
3. Delete the rest with `git branch -d`, one per call.
```

| Header field | Meaning |
|---|---|
| `name` | lowercase letters, digits, `-` and `_` (defaults to the file name) |
| `description` | one line; used by `model` matching and shown by `/skills` |
| `triggers` | phrases matched as whole words, case-insensitively; a `re:` prefix makes one a regex |
| `action` | `true` if the skill must end by changing something: replies that only talk are nudged to keep going |
| `workflow` | name of a scripted workflow (below) |

- **Scripted skills.** `workflow: NAME` makes the harness run a fixed command sequence itself and use the model only
  to write text (a branch name, a commit message, a PR description). This is how `pr-branch` works, so it doesn't
  depend on a small model staying on task. Every command still goes through classification, the mode and the
  approval prompts. Workflows live in the code (`long/workflows.py`); a skill file can name one but can't define one.
- **Matching** (`[skills] match` in the config, or `--skill-match`): `triggers` (default) uses the `triggers:` phrases;
  the longest match wins. `model` also asks the model to pick from the descriptions when no trigger matches (one extra
  model call per request). `off` never auto-matches. `--no-skills` doesn't load any.
- **Explicit:** `/skills` lists them; `/skill NAME [request]` runs one regardless of matching.
- **Project skills are untrusted.** They come from files in the repository you opened, so `long` shows what the skill
  says and asks once per session before using one.

## Trajectory trace

`long --trace` (or `/trace on` in the REPL, or `trace = true` in the config) shows what the model and the harness
actually did, step by step, with nothing truncated:

```
-- step 1 - model call                      how long the model took
-- step 1 - thinking (model-stated)         narration or reasoning the model emitted alongside its calls
-- step 1 - tool call - git                 the exact call, as JSON
-- step 1 - result (what the model received) [ok]   the complete output, exactly what went back to the model
-- step 2 - answer                          the final reply
-- harness - ...                            things `long` did itself: recovered a call written as text, rejected
                                            invented output, nudged the model, created a .gitignore, wrote a commit
                                            message, PR text or branch name
-- workflow pr-branch - step 3 - tool call  scripted workflow steps, labelled by number
-- trajectory: 2 model call(s) - tool calls: git x1
```

"Thinking" is what the model says about its own reasoning (including `<think>` blocks and the `reasoning` field some
servers return). It is not verified; the tool call and result lines are the ground truth of what ran.

## Configuration

Flags override `~/.config/long/config.toml`:

```toml
backend = "openai"            # or "llamacpp"
max_steps = 12
mode = "manual"               # manual | auto | plan
trace = false                 # show the full trajectory

[skills]
match = "triggers"            # off | triggers | model

[openai]
base_url = "http://localhost:11434/v1"
model = "qwen2.5-coder:14b"
api_key_env = "OPENAI_API_KEY"   # name of the env var holding the key, if the server needs one

[llamacpp]
model_path = "~/models/qwen2.5-7b-instruct-q4_k_m.gguf"
n_ctx = 8192
n_gpu_layers = -1
```

| Flag | Meaning |
|---|---|
| `-C, --repo PATH` | directory to work in (default: current) |
| `--config FILE` | alternate config file (skills are read from `skills/` next to it) |
| `--backend {openai,llamacpp}` | choose the backend |
| `--base-url URL`, `--model NAME`, `--api-key-env VAR` | OpenAI-compatible backend |
| `--model-path FILE`, `--n-ctx N` | llama.cpp backend |
| `--mode {manual,auto,plan}` | approval mode (default `manual`) |
| `--no-skills`, `--skill-match {off,triggers,model}` | skills (see above) |
| `--trace` | show the full trajectory (thinking, tool calls, complete results) |
| `--max-steps N` | cap on model/tool round trips per request |
| `--version` | print the version |

## Using a model on a remote host

`long` runs on your machine, in your repository; only the model lives elsewhere. It talks to any
server that speaks the OpenAI chat API (`POST {base_url}/chat/completions`), so point `base_url` at the host.

What the server needs:

- **Tool calling.** The model must support function calls (Qwen2.5-Coder, Llama 3.1+ and similar do).
- **`GET {base_url}/models`** (optional). It powers `/model` and the "available models" hint on a 404.
- Reachable from your machine, with enough GPU or RAM for the model you pick.

### Ollama on another machine

On the remote host, make Ollama listen on the network, then pull a model:

```bash
OLLAMA_HOST=0.0.0.0:11434 ollama serve      # or set OLLAMA_HOST in the service's environment
ollama pull qwen2.5-coder:14b
```

On your machine, point `long` at it (replace `gpu-box` with the host name or IP):

```bash
uv run long --base-url http://gpu-box:11434/v1 --model qwen2.5-coder:14b
```

or make it permanent in `~/.config/long/config.toml`:

```toml
[openai]
base_url = "http://gpu-box:11434/v1"
model = "qwen2.5-coder:14b"
```

Ollama has no authentication, so don't expose it to the internet. Use a trusted LAN, a VPN, or an SSH
tunnel (below).

### Over an SSH tunnel (recommended for anything not on a trusted LAN)

Keep the server bound to `localhost` on the remote host and forward the port:

```bash
ssh -N -L 11434:localhost:11434 you@gpu-box     # leave running in another terminal
uv run long                                     # the default base_url (localhost:11434) now reaches gpu-box
```

If something is already using local port 11434, forward a different one
(`-L 11500:localhost:11434`) and pass `--base-url http://localhost:11500/v1`.

### A hosted or authenticated API

For OpenAI, a vLLM or LiteLLM gateway, or any server that wants a bearer token, put the key in an
environment variable and tell `long` its name. The key itself is never stored in the config file.

```bash
export OPENAI_API_KEY=...        # or any variable name you like
uv run long --base-url https://api.openai.com/v1 --model <model-name>
# custom variable name:
export MY_GATEWAY_KEY=...
uv run long --base-url https://llm.example.com/v1 --model <model-name> --api-key-env MY_GATEWAY_KEY
```

### Things to know

- **Your repo context goes to that host.** Each request includes the repository snapshot (branch, `git status`,
  recent commits, remotes) and the output of every command the model runs, such as diffs and file listings. The
  separate calls that write commit messages and PR text include the relevant diff.
  Use a host you trust, and prefer `https://` or an SSH tunnel over plain `http://` across untrusted networks.
- Requests time out after 5 minutes. A cold model on a slow host can take a while to load on the first request.
- Use `/model` to see what the server offers and switch without restarting.

## Troubleshooting

| Symptom | What to do |
|---|---|
| `request to … failed` / connection refused | the model server isn't running or `base_url` is wrong |
| `404 … model not found` | the error lists the models the server has; pick one with `/model` or `--model` |
| the model writes JSON instead of acting, ends with an empty reply, or loops | `long` recovers or nudges where it can. A larger model (`/model`) helps most; `--trace` shows exactly what happened, and `/clear` resets the conversation |
| `gh executable not found` or a PR step fails | install [`gh`](https://cli.github.com) and run `gh auth login`; the branch is already pushed by then |
| prompts never get answered in a script or CI | approvals need a terminal. Use `--mode auto` (destructive commands are still declined) |
| a project skill is ignored | you declined it at the prompt; it won't be asked again this session |

## Development

```bash
uv run pytest
```

| Path | What lives there |
|---|---|
| `long/agent.py` | the model/tool loop, modes, guards, skill selection, tracing |
| `long/safety.py`, `long/tools.py`, `long/fs.py` | command classification, the `git`/`gh` runners, `ls`/`pwd`/`cd` |
| `long/commitmsg.py`, `long/gitignore.py` | harness-written commit, PR and branch text; the `.gitignore` guard |
| `long/skills.py`, `long/workflows.py`, `long/builtin_skills/` | skills, scripted workflows, the built-in skills |
| `long/llm/` | model backends (OpenAI-compatible, llama.cpp) |
| `long/ui.py`, `long/cli.py`, `long/config.py` | terminal UI and slash commands, flags, config |

See [DESIGN.md](DESIGN.md) for the architecture and safety model.

## License

[MIT](LICENSE)
