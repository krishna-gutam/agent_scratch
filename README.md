# cli-chatbot

A multi-provider **AI agent chatbot** with two frontends over one core: an interactive
**CLI** and a full-featured **Streamlit web app**. Talk to OpenAI, OpenRouter, Groq or
Google Gemini models, give them tools (file editing, shell, web search), approve what
they do, and organize your work into per-project conversations, skills and prompts.

No SDKs — all provider calls go through their OpenAI-compatible HTTP endpoints using
Python's standard library.

## Features

- **4 providers, 2 frontends** — switch between any model from any provider mid-project;
  the model catalog is auto-discovered from each provider's `/models` endpoint.
- **Agent tool calling** with an explicit approval gate: inspect every call, approve,
  deny, or reject with written feedback.
- **Per-workspace memory** — conversations, notes and settings live inside the project
  directory you're working in (`<project>/.chatbot/`), so opening another project gives
  you *its* history, not a global pile of chats.
- **Thread management** — multiple named conversations per project, rename/delete,
  undo first/last turn, delete individual messages, estimated token count.
- **Skills & prompts** — reusable Markdown instruction packs loaded on demand with
  `/skill <name> [task]` and `/prompt <name> [task]`, plus an in-app authoring studio
  with live validation.
- **In-browser file editor** with syntax highlighting (optional `streamlit-ace`),
  quick-notes scratchpad, and a sidebar shell runner.
- **Fuzzy file patching** — `apply_patch` matches code across whitespace, indentation
  and unicode differences instead of failing on exact-match misses.

## Quick start

Requires **Python 3.10+** (the code uses `X | Y` type unions).

```bash
pip install -r requirements.txt        # python-dotenv, tavily-python, streamlit
cp .env.example .env                   # if you have one; otherwise create .env manually
```

Add at least one provider key to `.env`:

```dotenv
OPENAI_API_KEY=sk-...
# and/or
OPENROUTER_API_KEY=sk-or-...
GROQ_API_KEY=gsk_...
GOOGLE_API_KEY=...
TAVILY_API_KEY=tvly-...      # only needed for web search
```

Run it:

```bash
streamlit run app.py     # web UI — pick a model in the Models tab, then chat
python main.py           # CLI — discovers models, search & pick, chat
```

## The two frontends

### Web app (`streamlit run app.py`)

| Tab | What it does |
|---|---|
| 💬 Chat Interface | The conversation. Shows the tool-approval gate when the agent wants to act. |
| 🧠 Models | Search/filter the discovered catalog across providers, see key status, re-discover. |
| 📝 Editor | Browse, edit and save files in the active workspace. |
| 🧩 Skills / 📜 Prompts | Browse installed skills/prompts and load one with an optional task. |
| ✍️ Create & Edit | Full authoring studio: create/edit/delete skill and prompt entries, bundled files, preview exactly what the model receives. |
| 🗒️ Message Logs / 🕒 Manage History | Inspect/delete raw messages; rename, delete and review threads. |

Sidebar: workspace switcher (with native OS folder picker), conversation switcher,
model status, tools/auto-approve toggles, undo/clear buttons, shell runner, quick notes.

### CLI (`python main.py`)

```
You: /skills                     list skills          You: !ls -la       run + add output to context
You: /skill review backend.py    load a skill         You: !!git status  run quietly (no context)
You: /prompts                    list prompts         /switch            change model
                                                      /clear             wipe history
```

The CLI executes requested tools automatically (no approval gate) — use the web app
when you want a say in what runs.

## Tools available to the agent

| Tool | Purpose |
|---|---|
| `read_file` | Read up to 1000 lines of a workspace file, resumable by line number. |
| `grep_search` | Regex search across the workspace (max 50 matches shown). |
| `apply_patch` | Replace code via a 9-tier fuzzy matching chain (exact → trimmed → whitespace/indentation-flexible → unicode-normalized → block-anchor → similarity-based), with escape-drift detection and "did you mean" hints. |
| `run_bash` | Execute shell commands pinned to the project's `.venv` (auto-created), 600 s timeout, truncated output. |
| `web_search` | Tavily advanced web search. |

Tools are self-registering: drop a new module into `tools/`, decorate a function with
`@tool("description")`, and it appears in the schema automatically (parameter types are
derived from Python annotations).

## Skills & prompts

Both are Markdown instruction files you load explicitly — nothing is auto-triggered.

```
skills/
  commit-message.md            # single-file entry
  code-review/
    SKILL.md                   # directory entry (+ optional bundled resource files)
    checklist.md
```

Optional frontmatter (flat `key: value`, not YAML):

```markdown
---
name: code-review
description: Review a file or diff for bugs and risky changes.
---
```

Then: `/skills` lists them, `/skill code-review refactor auth module` loads one with a task.
The same layout works for prompts (`PROMPT.md`, `/prompt ...`). Bundled files are listed
to the model so it can read them when instructions call for it.

The authoring tab validates before saving and refuses anything that would silently fail
to load later (empty body, unclosed frontmatter fence, name collisions, path escapes).

## Workspaces

Your **current directory is the project**. Per-project state lives in `<project>/.chatbot/`
(conversation threads, notes, last-used model/thread); global state (model catalog,
recent projects) lives beside the source code. Switch projects from the sidebar —
conversations follow the project. File operations are confined to the active workspace.

## Project structure

```
main.py            core API client + original CLI REPL (imported unchanged by backend)
backend.py         ChatSession: threads, persistence, interruptible agent loop, no UI imports
app.py             Streamlit frontend only — swap it out without touching the core
workspace.py       cwd-as-project abstraction, safe file ops, per-project state
authoring.py       write-side for skills/prompts (validation, frontmatter composing)
skills.py          skill loader (/skill commands)
prompts.py         prompt loader (/prompt commands)
tools/
  __init__.py      plugin loader + JSON-schema generation from type hints
  decorator.py     @tool registration decorator
  read_file.py     grep_search.py  smart_replace.py  shell_exec.py  web_search.py
context.md         dense internal map of the codebase (for agents/maintainers)
requirements.txt   dependencies (reconstructed from imports)
```

## Security notes

- Tool calls in the web app require explicit approval unless you tick **Auto-Approve**.
- All file access is path-confined to the active workspace (traversal-safe resolution).
- `run_bash` executes arbitrary commands by design — keep the approval gate on for
  untrusted conversations, and mind what's in your environment variables.

## Known limitations

- No streaming responses; replies arrive whole (one model call per step).
- Token counts are estimates (chars ÷ 4).
- Provider quirks are handled per-endpoint but not exhaustively tested; error bodies are
  surfaced verbatim.
- Debug logging prints full request/response payloads in CLI mode.
