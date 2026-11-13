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
- Works in directories that aren't git repositories yet; `git init` is just another approved step.

## Requirements

- [uv](https://docs.astral.sh/uv/) (no `pip install` needed) and Python 3.11+
- `git`
- [`gh`](https://cli.github.com) (optional, only for creating GitHub repositories; run `gh auth login` first)
- A model: for example [Ollama](https://ollama.com) with `ollama pull qwen2.5-coder:7b`

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
| `/model` | list the server's models and pick one (`/model NAME` or `/model N` to switch directly) |
| `/backend` | show the active backend and model |
| `/allowed` | subcommands auto-approved this session |
| `/clear` | forget the conversation |
| `/exit` | quit (Ctrl-D also works) |

Ctrl-C cancels the current step.

### Approvals

| Risk | Behaviour | Examples |
|---|---|---|
| READ | runs automatically | `status`, `log`, `diff`, `show`, `branch` (list) |
| MUTATE | shows the command and asks `[y/N/a]`; `a` allows that subcommand for the session | `add`, `commit`, `checkout`, `merge`, `push`, `gh repo create --private` |
| DESTRUCTIVE | shows a warning and asks every time | `reset --hard`, `clean -f`, `push --force`, `branch -D`, `gh repo create --public` |
| BLOCKED | refused, and the model is told why | `-c` config overrides, `--exec`, aliases, `-C` outside the repo |

Unknown subcommands are treated as mutating. There is no "approve everything" mode.

## Configuration

Flags override `~/.config/long/config.toml`:

```toml
backend = "openai"            # or "llamacpp"
max_steps = 12

[openai]
base_url = "http://localhost:11434/v1"
model = "qwen2.5-coder:7b"
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
| `--max-steps N` | cap on model/tool round trips per request |

Small models are less reliable at tool calling. If `long` picks the wrong tool or garbles a call,
try a larger model (`/model`).

## Development

```bash
uv run pytest
```

See [DESIGN.md](DESIGN.md) for the architecture and safety model.

## License

[MIT](LICENSE)
