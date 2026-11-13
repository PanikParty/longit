# long — a natural-language interface to git

A terminal agent harness: you type what you want ("squash my last 3 commits",
"what changed on main since Tuesday?"), a model decides which git commands to
run, and a safety layer decides which of those commands may run unattended.

## Setup (uv)
```bash
uv sync                     # create .venv, install deps + dev group, from uv.lock
uv sync --extra local       # also llama-cpp-python for the llamacpp backend
uv run long                # run the CLI
uv run pytest               # run tests
uv tool install .           # optional: install `long` globally
```

## Goals
- Feels like a text agentic harness: a chat loop, visible tool calls, streamed-style transcript, slash commands.
- Backend-agnostic: remote API **or** local model, chosen by config/flag.
- The model never gets a shell. It gets one tool, `git`, taking an argv list.
- Mutations are visible and approved; destructive operations are never silent.

## Architecture

```
cli.py ──► config.py          flags + ~/.config/long/config.toml
  │
  ├─► ui.py                   rich-based transcript, approval prompt, slash commands
  │
  └─► agent.py                the loop (model ⇄ tools), context assembly
         ├─► llm/base.py      Backend protocol, Message / ToolCall types
         │     ├─ llm/openai_compat.py   any /v1/chat/completions server
         │     │                         (OpenAI, Ollama, llama-server, LM Studio, vLLM)
         │     └─ llm/llamacpp.py        in-process llama-cpp-python (GGUF)
         ├─► tools.py         `git` tool schema + subprocess runner (no shell)
         └─► safety.py        argv ─► Risk {READ, MUTATE, DESTRUCTIVE, BLOCKED}
```

### Backend abstraction
One method: `chat(messages, tools) -> AssistantTurn(text, tool_calls)`.
Both backends speak OpenAI-style message/tool dicts, so the agent loop is
backend-blind. Local models that lack native tool calling are handled by the
llama.cpp backend through its chat-format handler; if a model returns no
structured calls, the loop falls back to parsing a single fenced
```` ```git ```` block (`parse_fallback_calls`). Tool-call arguments that fail
JSON parsing are returned to the model as an error so it can retry.

### Safety tiers (`safety.py`)
Classification works on the parsed argv, never on the natural-language request.

| Risk | Behaviour | Examples |
|---|---|---|
| READ | auto-run | `status`, `log`, `diff`, `show`, `branch` (list), `stash list`, `config --get` |
| MUTATE | show command, ask `[y/n/a]`; `a` allows that subcommand for the session | `add`, `commit`, `checkout`, `merge`, `rebase`, `push`, `stash push` |
| DESTRUCTIVE | show command + warning, ask, **no** session-allow | `reset --hard`, `clean -f`, `push --force/--delete`, `branch -D`, `checkout -- .`, `reflog expire`, `gc --prune` |
| BLOCKED | refused, model told why | `-c`/`--config-env` overrides, `--exec`, `--upload-pack`, alias/`!` commands, `-C` outside the repo |

Unknown subcommands default to MUTATE. Read-class commands carrying flags that
can write files or spawn programs (`--output`, `--ext-diff`, `--textconv`, `-O`)
are promoted to MUTATE. `--yes-reads-only` is the only auto mode; there is no
"approve everything" flag in v1.

Git runs with `GIT_TERMINAL_PROMPT=0`, `GIT_PAGER=cat`, `GIT_EDITOR=true`
(so a stray `commit` without `-m` cannot hang the loop), and output is capped
(head+tail) before going back to the model.

### Agent loop
1. System prompt = role + rules + a snapshot (repo root, branch, short status).
2. Send history to backend. If the turn has text, print it.
3. For each tool call: classify → (approve) → run → append `tool` message.
   A denial is returned to the model as a tool result so it can adapt.
4. Repeat until a turn has no tool calls, or `max_steps` (default 12) is hit.
5. History is kept across user turns; `/clear` resets it.

### UI
Plain scrolling transcript (rich): `›` user, dim tool-call lines
(`$ git log --oneline -5`), truncated tool output, assistant prose.
Slash commands: `/help /status /backend /model /clear /allowed /exit`. `/model` lists the server's models (GET /models) and switches for the session; unavailable for the llamacpp backend.
Ctrl-C cancels the current step; Ctrl-D exits.

### Config (`~/.config/long/config.toml`, flags override)
```toml
backend = "openai"            # or "llamacpp"
[openai]
base_url = "http://localhost:11434/v1"
model = "qwen2.5-coder:7b"
api_key_env = "OPENAI_API_KEY"
[llamacpp]
model_path = "~/models/qwen2.5-7b-instruct-q4_k_m.gguf"
n_ctx = 8192
n_gpu_layers = -1
```

### Filesystem tools (`ls`, `pwd`, `cd`)
Native Python in `fs.py`, no subprocess or shell. `ls` and `cd` are confined to the project root
(symlinks are resolved first, so a link out of the project is refused). `cd` sets the agent's `cwd`,
which `git`, `gh` and `ls` then use; moving inside the project is automatic, while a `cd` that leaves it
asks for approval, re-roots the session on the new directory, and clears the session's "always" approvals.

### .gitignore guard
When the model stages everything (`git add -A/--all/.`) in a repo with no `.gitignore`, `long` scans the
untracked files (`gitignore.py`). If it finds generated or secret files (`.venv/`, `__pycache__/`, `node_modules/`,
`.env`, `.DS_Store`, ...), it asks before the add runs whether to create a `.gitignore` with those patterns.
Declining proceeds with the add and is not asked again for that repo this session.

### GitHub repo creation (`gh` tool)
A second tool, `gh`, exists solely so "create a GitHub repo and push everything" works. Its allow-list is
`auth status`, `repo view` (READ) and `repo create` (everything else is BLOCKED). `repo create` needs
exactly one name and exactly one visibility flag; `--private` is MUTATE (approval), `--public`/`--internal`
is DESTRUCTIVE (red warning, no session-allow). Only `--private/--public/--internal/--push/--remote/--source/
--description/--homepage` are accepted (no `--clone`, `--template`); `--source` must stay inside the repo.
The model is told to push afterwards with `git push -u origin HEAD` and to check for secrets before `git add -A`.

### Non-repo directories
If the target directory isn't inside a git repo, `long` still starts: the directory itself becomes the
root, the snapshot tells the model it isn't a repository yet, and `git init` (MUTATE, so approved
first) can create one. Only a nonexistent path is an error.

## Out of scope for v1 (next steps)
Token streaming, undo via reflog snapshots, multi-repo, editing commit
messages interactively, MCP/tool plugins, persistent session history.
