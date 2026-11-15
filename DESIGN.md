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
are promoted to MUTATE. Approval is governed by the **mode** (`manual`
default, `auto`, `plan`; `/mode`, `--mode`, `mode =` in config): `auto` runs MUTATE commands unattended but DESTRUCTIVE
ones still ask and BLOCKED ones stay blocked, so there is no "approve everything" setting; `plan` runs only READ
commands and returns a "not run" result for anything else, which also disables the stuck-loop nudges since a text-only
reply is the expected output. The system prompt carries a one-line note of the active mode.

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
model = "qwen2.5-coder:14b"
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

### Commit messages written by the harness
`git commit` with no message (`-m/-F/-C/--amend/--fixup/...` all count as one) is completed by the harness, not the
agent loop: `commitmsg.py` collects `git diff --staged` (or `diff HEAD` with `-a`) plus the last few subjects, makes a
separate tool-less model call in its own context, cleans the reply into a subject (<=72 chars) and optional bullets, and
inserts `-m` flags. The user then approves the exact command, message included. If nothing is staged the model is not
called; if the call fails, the error goes back to the agent loop. Plan mode never generates one.

### Skills
`skills.py`: markdown files with a small frontmatter (`name`, `description`, `triggers`) and an instruction body, loaded
from `long/builtin_skills/`, `~/.config/long/skills/` and `<root>/.long/skills/` (later overrides earlier by name; bad files
are skipped with a warning). Per user prompt the agent selects at most one skill: explicit (`/skill`), else the longest
matching trigger (whole-word phrase or `re:` regex), else, in `model` match mode, a tool-less model call that picks a
name from the descriptions (unknown replies mean none). Bare approvals ("yes", "go") never re-trigger a skill. The
chosen body is appended to the system prompt for that turn only. Skills are text, not capabilities: classification,
mode and approvals are unchanged. Because a repo can contain `.long/skills`, a project skill is treated as untrusted
input and needs a one-time per-session approval (the model could otherwise be steered, e.g. in auto mode, by a hostile
repository). Re-rooting with an approved `cd` reloads project skills.

### Scripted skills (workflows)
`workflow: NAME` in a skill's frontmatter makes the agent skip the model loop and call `workflows.REGISTRY[NAME](agent)`,
a plain function that runs a fixed command sequence through `Agent.run_tool`. `run_tool` is the same path as a model tool call
(classify, confine, mode, gitignore guard, commit/PR text generation, approval), so a workflow can do nothing the model could
not. The model is called only for text, in the same isolated tool-less calls as elsewhere (branch slug, commit message, PR
text). Failures are handled in code (declined, refused or failed steps stop the run and the report says what was done);
the final report is written by the harness and appended to the history. `pr-branch` discovers state with read-only commands,
refuses early (no changes, no `origin`, not a repo), names a free `pr/<slug>` branch, switches to it, stages everything minus
secret-looking files via `:(exclude,literal)` pathspecs, commits, pushes `-u origin`, and runs `gh pr create`. In plan mode it
only prints the plan. Workflows are registered in code, never loaded from skill files, so an untrusted project skill can select
one but not define behaviour. An unknown workflow name falls back to the model with a warning. In the trace, each step is
labelled `workflow NAME - step N`.

### Trajectory trace
With tracing on (`--trace`, `/trace`, `trace = true`) the agent emits one labelled event per step through an optional
`trace_event(kind, title, body, status)` frontend hook: `model` (latency), `thinking`, `tool_call`, `tool_result`,
`answer`, `harness`, `summary`. Text that accompanies tool calls, `<think>` blocks and the server's `reasoning` field are
all shown as *thinking*; the final reply is the *answer*. Tool calls print their exact JSON arguments and results print the
complete string returned to the model (including refusals, denials and "plan mode" skips), replacing the usual 12-line
preview. Harness-originated actions (text-call recovery, invented-output rejection, stuck-loop nudges, `.gitignore`
creation, the separate commit-message model call) are labelled `harness` so they can't be mistaken for model actions.
Reasoning is kept apart from actions and never replayed to the model; it is shown as model-stated, not as proof of
what ran. (Design note: an analysis of coding-agent trajectories, arXiv 2606.16988, argues stated reasoning is a weak
account of behaviour while ordered, named actions are the reliable signal; the trace follows that split.)

### .gitignore guard
When the model stages everything (`git add -A/--all/.`) in a repo with no `.gitignore`, `long` scans the
untracked files (`gitignore.py`). If it finds generated or secret files (`.venv/`, `__pycache__/`, `node_modules/`,
`.env`, `.DS_Store`, ...), it asks before the add runs whether to create a `.gitignore` with those patterns.
Declining proceeds with the add and is not asked again for that repo this session.

### GitHub repo creation (`gh` tool)
A second tool, `gh`, exists solely so "create a GitHub repo and push everything" works. Its allow-list is
`auth status`, `repo view` (READ) and `repo create` (everything else is BLOCKED). `repo create` needs
exactly one name and exactly one visibility flag; `--private` is MUTATE (approval), `--public`/`--internal`
is DESTRUCTIVE (red warning, no session-allow). `pr create|view|list|status` are also allowed: the read-only ones refuse `--web` and `-R/--repo`; `pr create` accepts only
`--title/--body/--base/--head/--fill/--draft` (no `--body-file`, `--template`, `--web`, `--repo`) and is MUTATE. Like commit
messages, a bare `gh pr create` gets its title and description from a separate tool-less model call over `git log`/`git diff`
against the base branch (`commitmsg.generate_pr_text`), inserted into the command the user approves; supplied halves are kept.
Only `--private/--public/--internal/--push/--remote/--source/
--description/--homepage` are accepted (no `--clone`, `--template`); `--source` must stay inside the repo.
The model is told to push afterwards with `git push -u origin HEAD` and to check for secrets before `git add -A`.

### Non-repo directories
If the target directory isn't inside a git repo, `long` still starts: the directory itself becomes the
root, the snapshot tells the model it isn't a repository yet, and `git init` (MUTATE, so approved
first) can create one. Only a nonexistent path is an error.

## Out of scope for v1 (next steps)
Token streaming, undo via reflog snapshots, multi-repo, editing commit
messages interactively, MCP/tool plugins, persistent session history.
