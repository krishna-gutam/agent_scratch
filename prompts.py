"""Command-invoked prompts for the CLI chatbot.

A prompt is a Markdown file of instructions that the user explicitly loads into
the conversation with a slash command. Nothing is auto-triggered.

Layout (next to this file):

    prompts/
        code-review/
            PROMPT.md          <- required
            checklist.md      <- optional bundled resource
        commit-message.md     <- single-file prompt, name comes from filename

PROMPT.md may open with a frontmatter block:

    ---
    name: code-review
    description: Review a file or diff for bugs and risky changes.
    ---

Commands (see handle_prompt_command):
    /prompts            list available prompts
    /prompts reload     re-scan the prompts directory
    /prompt <name>      load a prompt, no task yet
    /prompt <name> ...  load a prompt and give it a task in one go
"""

import os
from difflib import get_close_matches

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
PROJECT_ROOT = os.path.dirname(PROMPTS_DIR)
PROMPT_FILE = "PROMPT.md"

_CACHE = None


# --------------------------------------------------------------------------
# parsing / discovery
# --------------------------------------------------------------------------

def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _split_frontmatter(text):
    """Return (meta, body). Frontmatter is optional and parsed as flat key: value."""
    meta = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if ":" in line and not line.strip().startswith("#"):
                    key, value = line.split(":", 1)
                    meta[key.strip().lower()] = value.strip().strip("\"'")
            body = text[end + 4:]
    return meta, body.strip()


def _bundled_files(prompt_path):
    """Every file that ships alongside PROMPT.md, as paths relative to the project root."""
    if os.path.basename(prompt_path) != PROMPT_FILE:
        return []

    base = os.path.dirname(prompt_path)
    found = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith((".", "__"))]
        for filename in filenames:
            if dirpath == base and filename == PROMPT_FILE:
                continue
            if filename.startswith("."):
                continue
            full = os.path.join(dirpath, filename)
            found.append(os.path.relpath(full, PROJECT_ROOT))
    return sorted(found)


def discover_prompts(force=False):
    """Scan PROMPTS_DIR once and cache the result. Returns {lowercase_name: prompt}."""
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE

    prompts = {}
    if os.path.isdir(PROMPTS_DIR):
        for entry in sorted(os.listdir(PROMPTS_DIR)):
            if entry.startswith((".", "__")):
                continue

            path = os.path.join(PROMPTS_DIR, entry)
            if os.path.isdir(path):
                prompt_path = os.path.join(path, PROMPT_FILE)
                if not os.path.isfile(prompt_path):
                    continue
                default_name = entry
            elif entry.lower().endswith(".md"):
                prompt_path = path
                default_name = os.path.splitext(entry)[0]
            else:
                continue

            try:
                meta, body = _split_frontmatter(_read(prompt_path))
            except Exception as e:
                print(f"[prompts] skipped {prompt_path}: {e}")
                continue

            if not body:
                continue

            name = (meta.get("name") or default_name).strip()
            prompts[name.lower()] = {
                "name": name,
                "description": meta.get("description", "").strip(),
                "path": prompt_path,
                "body": body,
                "files": _bundled_files(prompt_path),
            }

    _CACHE = prompts
    return prompts


def resolve(query):
    """Return (prompt, candidates). Exactly one of the two is meaningful."""
    prompts = discover_prompts()
    q = query.strip().lower()

    if q in prompts:
        return prompts[q], []

    for match_fn in (lambda k: k.startswith(q), lambda k: q in k):
        hits = [s for k, s in prompts.items() if match_fn(k)]
        if len(hits) == 1:
            return hits[0], []
        if len(hits) > 1:
            return None, hits

    close = get_close_matches(q, list(prompts), n=3, cutoff=0.6)
    if len(close) == 1:
        return prompts[close[0]], []
    return None, [prompts[c] for c in close]


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def format_catalog():
    prompts = discover_prompts()
    if not prompts:
        return (
            "No prompts found.\n"
            f"Add one at {os.path.join(PROMPTS_DIR, '<name>', PROMPT_FILE)} "
            f"(or {os.path.join(PROMPTS_DIR, '<name>.md')})."
        )

    width = max(len(s["name"]) for s in prompts.values())
    lines = ["Available prompts:"]
    for key in sorted(prompts):
        prompt = prompts[key]
        lines.append(f"  {prompt['name']:<{width}}  {prompt['description'] or '(no description)'}")
    lines.append("")
    lines.append("Load one with: /prompt <name> [task]")
    return "\n".join(lines)


def render(prompt, task=""):
    """Turn a prompt into the user-turn text that gets sent to the model."""
    rel_path = os.path.relpath(prompt["path"], PROJECT_ROOT)

    parts = [
        # f"[prompt: {prompt['name']}]",
        # f"The instructions below were loaded from {rel_path} at the user's explicit request. "
        # "Follow them for this task and for the rest of this conversation, unless the user says otherwise.",
        # "--- BEGIN PROMPT ---",
        prompt["body"],
        # "--- END PROMPT ---",
    ]

    # if prompt["files"]:
    #     listing = "\n".join("  - " + f for f in prompt["files"])
    #     parts.append(
    #         "Files bundled with this prompt (read them with run_powershell tool only if the "
    #         f"instructions above call for it):\n{listing}"
    #     )

    if task:
        parts.append(f"Task: {task}")
    # else:
    #     parts.append(
    #         "No task was given yet. Confirm in one short line that the prompt is loaded, "
    #         "and state what you need from the user to start."
    #     )

    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# command entry point
# --------------------------------------------------------------------------

def handle_prompt_command(user_input):
    """Handle '/prompt...' input.

    Returns the expanded prompt text to send as a normal user turn, or None if
    the command was fully handled locally (listing, reload, error).
    """
    parts = user_input.strip().split(maxsplit=1)
    command = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if rest.split(" ")[0].lower() in ("reload", "refresh") and rest:
        discover_prompts(force=True)
        print("[prompts reloaded]")
        print(format_catalog() + "\n")
        return None

    if command == "/prompts" or not rest:
        print(format_catalog() + "\n")
        return None

    name, _, task = rest.partition(" ")
    prompt, candidates = resolve(name)

    if prompt is None:
        if candidates:
            print(f"'{name}' is ambiguous. Did you mean: "
                  + ", ".join(c["name"] for c in candidates) + "\n")
        else:
            print(f"No prompt named '{name}'.\n")
            print(format_catalog() + "\n")
        return None

    print(f"[prompt loaded: {prompt['name']} <- {os.path.relpath(prompt['path'], PROJECT_ROOT)}]")
    return render(prompt, task.strip())
