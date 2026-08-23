# context.md — project map

Read this before exploring anything else. It is a snapshot of the whole repo so an agent
does not need to re-read every file each session.

## What this is

A multi-provider LLM **agent/chatbot** with two frontends over one core:

- **CLI** — `python main.py`
- **Web UI** — `streamlit run app.py`

Providers (all via OpenAI-compatible HTTP endpoints, stdlib `urllib` only — no SDKs):
OpenAI, OpenRouter, Groq, Google Gemini. Tool calling supported on all.

Run deps: `python-dotenv`, `tavily-python`, `streamlit` (+ optional `streamlit-ace`). See `requirements.txt`.

## Environment / secrets

`.env` keys: `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`, `GOOGLE_API_KEY`,
`TAVILY_API_KEY`. Never print values; keys only. Gitignored.

## File map (read order matters less than layering)

| File | Role |
|---|---|
| `main.py` | Original CLI **and shared core**. Provider CONFIGS/PROVIDERS dicts; `discover_models()` fetches `/v1/models` from each provider into `discovered_models.json`; fuzzy `search_models()` (SequenceMatcher); `send_chat_request()`; `run_shell()`; REPL loop (`chat_loop`) with `/exit /switch /clear`, `!cmd` (output into context) and `!!cmd` (local only). Safe to import: side effect is only `load_dotenv()`. |
| `backend.py` | Session layer. `ChatSession`: threads as JSON per workspace; history editing (undo first/last turn, delete msg); interruptible agent loop (`step()` -> pending -> `approve_tools()`/`deny_tools()`/`send_tool_feedback()`); skills/prompt command handling; token estimate = json chars // 4. No UI imports. |
| `app.py` | Streamlit frontend only. Sidebar (workspace switcher w/ zenity/kdialog/tkinter picker, threads, model status, skills/prompts quick-load, shell runner, notes) + tabs: Chat, Models, Editor (st_ace fallback textarea), Skills, Prompts, Create&Edit (authoring studio), Message Logs, Manage History. One `ChatSession` per workspace in `st.session_state`; rerun-driven loop calls `session.step()` while `busy`. |
| `workspace.py` | cwd-as-project abstraction. Per-project `<project>/.chatbot/{chats/,notes.md,state.json}`; recents in `.recent_projects.json` beside the app; traversal-safe `resolve()/read_file()/write_file()`; ignore lists for listing; `app_directory()` ctx mgr to pin catalog writes next to `main.py`. |
| `tools/__init__.py` | Self-registering tool loader. `pkgutil.walk_packages` imports every submodule; `@tool`-decorated functions become TOOLS (OpenAI function-calling schema auto-generated from type hints) + TOOL_REGISTRY; `execute_tool(name, args)` returns str/JSON, never raises. |
| `tools/decorator.py` | `@tool(description)` registers wrapper into `_DECORATED_TOOLS`. |
| `tools/smart_replace.py` | `apply_patch(file_path, old_code, new_code, justification)` via 9-tier fuzzy chain: exact -> line_trimmed -> whitespace_normalized -> indentation_flexible -> escape_normalized -> trimmed_boundary -> unicode_normalized -> block_anchor (SequenceMatcher >= 0.50/0.70) -> context_aware (>= 0.80 per line, half the lines must match). Multi-match without `replace_all` = error; escape-drift detection; auto re-indent of replacement; "did you mean" snippets on miss. Also exports `find_closest_lines`. |
| `tools/read_file.py` | Workspace-confined reader, 1000-line window, truncation footer tells caller the next `start_line`. |
| `tools/grep_search.py` | Regex search, workspace-confined, max 50 matches. |
| `tools/web_search.py` | Tavily advanced search; returns url/title/content list as JSON. |
| `tools/shell_exec.py` | `run_bash(script, justification)`: auto-creates `.venv` in cwd, hard-pins python/pip to it via injected bash functions, 600 s timeout, 2000-char output truncation. Handles Windows venv layout under Git Bash. |
| `skills.py` | Slash-command skill loader. Entries: `skills/<name>.md` or `skills/<name>/SKILL.md`; optional flat frontmatter (name/description); cached discovery; fuzzy `resolve()` (exact > prefix > substring > close_matches); `render()` wraps body in BEGIN/END SKILL markers + bundled-file listing + task line. |
| `prompts.py` | Twin of skills.py for `prompts/<name>/PROMPT.md`. NOTE: its `render()` framing is commented out — sends raw body + Task only. |
| `authoring.py` | Write-side for both catalogs (the only writer). `validate()` blocks saves that would silently vanish at load time (empty body after frontmatter strip, unclosed `---`, name collisions/shadowing, traversal); `compose()` emits parser-safe frontmatter; rename/layout-change carries bundled files; paranoid `_inside()` realpath checks everywhere. |

Scratch/state dirs not worth reading: `agi/` (second .chatbot state + empty h/), `.chatbot/`.

## Agent loop (web)

1. `session.submit(text)` — handles `!`, `!!`, `/skill`, `/prompt` locally; else appends user msg, sets `busy=True`.
2. Streamlit reruns call `step()` once per rerun until it settles.
3. Reply with `tool_calls` -> stored, `pending` filled, UI shows approval gate (approve / deny / feedback).
4. `approve_tools()` executes via `execute_tool`, appends `role:"tool"` msgs, sets `busy=True` again; deny/feedback pop the assistant msg and inject a `[system]` user note instead.
5. Plain reply -> `busy=False`.

Thread JSON stores provider/model/busy/messages; `"busy": true` on disk means a model call was owed — `_rehydrate_pending()` rebuilds the gate if reload happened mid-tool-call.

## Persistence locations

```
<project>/.chatbot/
    chats/<thread_id>.json   # conversations, scoped per project
    notes.md                 # sidebar quick notes
    state.json               # last_thread + last provider/model
<app dir>/                   # NOT per-project (gitignored):
    discovered_models.json   # provider catalogs; refresh via Models tab button
    .recent_projects.json    # up to 12 recents
```

Thread ids: uuid hex[:8] default; illegal chars `/\\:*?"<>|` rejected; duplicate ids refused (no overwrite).

## Conventions & gotchas (things that cost time to rediscover)

- `backend.py` imports `main.py` unchanged; keep everything in main behind functions/guards.
- Tools and shell run with cwd == active workspace root; switching workspace chdirs the process. Catalog reads/writes must go through `workspace.app_directory()` or they leak into the open project.
- Path safety pattern used everywhere: resolve against root, reject escapes (`_inside`, `resolve`, startswith checks).
- `sanitize_content()` is currently a **no-op** despite its docstring.
- Debug prints in `main.send_chat_request` dump full request/response payloads — noisy in CLI.
- `requirements.txt` references a nonexistent `test_smoke.py`; skills render text mentions a nonexistent "run_powershell" tool (real name is `run_bash`).
- Frontmatter parser is flat `key: value` split on first colon, NOT YAML; values must stay single-line (`compose()` enforces).
- A catalog entry whose body is empty after frontmatter stripping silently disappears from discovery — hence authoring's hard error.
- Editing keys on the on-disk slug, not the catalog name (frontmatter name can differ from filename).

## Quick verification recipe

- `streamlit run app.py` -> pick model in Models tab (needs a key in .env), chat in Chat tab.
- `python main.py` -> discovers models, search+select, chat; `/skills` lists skills (skills/ dir does not exist yet — create `skills/<name>.md` to test).
