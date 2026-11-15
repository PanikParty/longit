# long

A natural-language interface to git. Type what you want ("squash my last 3 commits",
"what changed on main since Tuesday?"), a model decides which git commands to run, and a
safety layer decides which of them may run unattended.

```
› create a github repo for this project and push everything
  $ git status --short
  $ git add -A                       [changes repo]  run? [y/N/a]
  $ git commit -m "Initial commit"   [changes repo]  run? [y/N/a]
  $ gh repo create long --private --source=. --remote=origin
  $ git push -u origin HEAD
```

- Works with a local or remote model: any OpenAI-compatible server (Ollama, llama-server,
  LM Studio, vLLM, OpenAI) or an in-process GGUF model through llama.cpp.
- The model never gets a shell. Its tools are `git`, a tiny `gh` (create repos), and `ls`, `pwd` and `cd` (confined to the project; leaving it needs your approval).
- Read-only commands run immediately. Anything that changes the repo is shown and needs your approval.
- `git commit` without a message, and `gh pr create` without a title and description, get them written by the model from the diff and shown to you in the approval prompt.
- Works in directories that aren't git repositories yet; `git init` is just another approved step.

## Requirements

- [uv](https://docs.astral.sh/uv/) (no `pip install` needed) and Python 3.11+
- `git`
- [`gh`](https://cli.github.com) (optional, only for creating GitHub repositories; run `gh auth login` first)
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
uv run long "show me the last 5 commits"     # one-shot
uv run long -C ~/code/app  # work in another directory
```

To install `long` as a command on your PATH:

```bash
uv tool install .
```

For the in-process llama.cpp backend, add the extra: `uv sync --extra local`.

## Usage

Describe what you want in plain English. Slash commands:

| Command | What it does |
|---|---|
| `/help` | show help |
| `/status` | `git status` of the current repo |
| `/skills`, `/skill NAME [request]` | list stored skills; run one explicitly |
| `/trace` | toggle the trajectory trace (`/trace on`, `/trace off`) |
| `/mode` | show or set the mode: `manual`, `auto` or `plan` (shortcuts `/manual`, `/auto`, `/plan`) |
| `/model` | list the server's models and pick one (`/model NAME` or `/model N` to switch directly) |
| `/backend` | show the active backend and model |
| `/allowed` | subcommands auto-approved this session |
| `/clear` | forget the conversation |
| `/exit` | quit (Ctrl-D also works) |

Ctrl-C cancels the current step.

### Skills

A skill is a stored, named set of instructions for a kind of request. When your prompt matches one, `long`
adds its instructions to the model's context for that turn and says so (`skill: undo-last-commit (matched trigger ...)`).
Skills only add guidance: every command still goes through the safety tiers, the current mode and your approvals.

Built in: `github-repo` (publish to a new GitHub repo), `pr-branch` (a scripted workflow: put every change on a new `pr/...` branch, commit, push it to origin and open the pull request, asking you only for approvals; try "prepare a pull request"), `undo-last-commit`, `whats-new` (summarize recent changes).
Add your own as markdown files in `~/.config/long/skills/` (yours everywhere) or `.long/skills/` inside a repo
(project skills); later layers override earlier ones by name: built-in, then yours, then the project's.

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

- **Scripted skills.** A skill can say `workflow: NAME` in its header instead of leaving the steps to the model. The
  harness then runs a fixed command sequence itself and uses the model only to write text (a branch name, a commit
  message, a PR description). This is how `pr-branch` works, so it doesn't depend on a small model staying on task. Every
  command still goes through the normal classification, mode and approval prompts, and plan mode only prints the plan.
  Workflows live in the code (`long/workflows.py`); a skill file can name one but can't define one.
- **Matching** (`[skills] match` in the config, or `--skill-match`): `triggers` (default) matches the phrases in
  `triggers:` as whole words, case-insensitively (`re:` prefix for a regex); the longest match wins. `model` also asks
  the model to pick from the skill descriptions when no trigger matches (one extra model call per request).
  `off` never auto-matches. `--no-skills` doesn't load any.
- **Explicit:** `/skills` lists them; `/skill NAME [request]` runs one regardless of matching.
- **Project skills are untrusted.** They come from files in the repository you opened, so `long` shows what the skill
  says and asks once per session before using one.

### Trajectory trace

`long --trace` (or `/trace on` in the REPL, or `trace = true` in the config) shows what the model and the harness
actually did, step by step, with nothing truncated:

```
-- step 1 - model call                      how long the model took
-- step 1 - thinking (model-stated)         narration or reasoning the model emitted alongside its calls
-- step 1 - tool call - git                 the exact call, as JSON
-- step 1 - result (what the model received) [ok]   the complete output, exactly what went back to the model
-- step 2 - answer                          the final reply
-- harness - ...                            things `long` did itself: recovered a call written as text, rejected
                                            invented output, nudged the model, wrote a commit message
-- trajectory: 2 model call(s) - tool calls: git x1
```

"Thinking" is what the model says about its own reasoning (including `<think>` blocks and the `reasoning` field some
servers return). It is not verified; the tool call and result lines are the ground truth of what ran.

### Approvals

| Risk | Behaviour | Examples |
|---|---|---|
| READ | runs automatically | `status`, `log`, `diff`, `show`, `branch` (list) |
| MUTATE | shows the command and asks `[y/N/a]`; `a` allows that subcommand for the session | `add`, `commit`, `checkout`, `merge`, `push`, `gh repo create --private`, `gh pr create` |
| DESTRUCTIVE | shows a warning and asks every time | `reset --hard`, `clean -f`, `push --force`, `branch -D`, `gh repo create --public` |
| BLOCKED | refused, and the model is told why | `-c` config overrides, `--exec`, aliases, `-C` outside the repo |

Unknown subcommands are treated as mutating. Even in `auto` mode, destructive commands still ask
and blocked ones stay blocked; there is no "approve everything" mode.

### Modes

| Mode | Behaviour |
|---|---|
| `manual` (default) | read-only commands run; every change asks first |
| `auto` | reads and ordinary changes (`add`, `commit`, `push`, private `gh repo create`, a generated `.gitignore`) run without asking; DESTRUCTIVE commands, leaving the project with `cd`, and anything BLOCKED behave as before |
| `plan` | read-only: the model investigates and replies with a numbered plan; any change it attempts is skipped and nothing is modified |

Switch inside the REPL with `/mode manual|auto|plan` (or `/manual`, `/auto`, `/plan`); `/mode` alone shows the
current one. Start in a mode with `--mode`, or set `mode = "auto"` in the config. A typical flow is to ask in
`plan` mode, read the plan, switch to `manual` or `auto`, and say "go".

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
| `--config FILE` | alternate config file |
| `--backend {openai,llamacpp}` | choose the backend |
| `--base-url URL`, `--model NAME`, `--api-key-env VAR` | OpenAI-compatible backend |
| `--model-path FILE`, `--n-ctx N` | llama.cpp backend |
| `--mode {manual,auto,plan}` | approval mode (default `manual`) |
| `--no-skills`, `--skill-match {off,triggers,model}` | skills (see above) |
| `--trace` | show the full trajectory (thinking, tool calls, complete results) |
| `--max-steps N` | cap on model/tool round trips per request |

Small models are less reliable at tool calling. If `long` picks the wrong tool or garbles a call,
try a larger model (`/model`).

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
  recent commits, remotes) and the output of every command the model runs, such as diffs and file listings.
  Use a host you trust, and prefer `https://` or an SSH tunnel over plain `http://` across untrusted networks.
- Requests time out after 5 minutes. A cold model on a slow host can take a while to load on the first request.
- Use `/model` to see what the server offers and switch without restarting.
- Connection errors (`request to … failed`) mean the host or port is unreachable; a `404 … model not found`
  lists the models the server actually has.

## Development

```bash
uv run pytest
```

See [DESIGN.md](DESIGN.md) for the architecture and safety model.

## License

[MIT](LICENSE)
