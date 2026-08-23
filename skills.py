"""Command-invoked skills for the CLI chatbot.

A skill is a Markdown file of instructions that the user explicitly loads into
the conversation with a slash command. Nothing is auto-triggered.

Layout (next to this file):

    skills/
        code-review/
            SKILL.md          <- required
            checklist.md      <- optional bundled resource
        commit-message.md     <- single-file skill, name comes from filename

SKILL.md may open with a frontmatter block:

    ---
    name: code-review
    description: Review a file or diff for bugs and risky changes.
    ---

Commands (see handle_skill_command):
    /skills            list available skills
    /skills reload     re-scan the skills directory
    /skill <name>      load a skill, no task yet
    /skill <name> ...  load a skill and give it a task in one go
"""

import os
from difflib import get_close_matches

SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")
PROJECT_ROOT = os.path.dirname(SKILLS_DIR)
SKILL_FILE = "SKILL.md"

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


def _bundled_files(skill_path):
    """Every file that ships alongside SKILL.md, as paths relative to the project root."""
    if os.path.basename(skill_path) != SKILL_FILE:
        return []

    base = os.path.dirname(skill_path)
    found = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith((".", "__"))]
        for filename in filenames:
            if dirpath == base and filename == SKILL_FILE:
                continue
            if filename.startswith("."):
                continue
            full = os.path.join(dirpath, filename)
            found.append(os.path.relpath(full, PROJECT_ROOT))
    return sorted(found)


def discover_skills(force=False):
    """Scan SKILLS_DIR once and cache the result. Returns {lowercase_name: skill}."""
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE

    skills = {}
    if os.path.isdir(SKILLS_DIR):
        for entry in sorted(os.listdir(SKILLS_DIR)):
            if entry.startswith((".", "__")):
                continue

            path = os.path.join(SKILLS_DIR, entry)
            if os.path.isdir(path):
                skill_path = os.path.join(path, SKILL_FILE)
                if not os.path.isfile(skill_path):
                    continue
                default_name = entry
            elif entry.lower().endswith(".md"):
                skill_path = path
                default_name = os.path.splitext(entry)[0]
            else:
                continue

            try:
                meta, body = _split_frontmatter(_read(skill_path))
            except Exception as e:
                print(f"[skills] skipped {skill_path}: {e}")
                continue

            if not body:
                continue

            name = (meta.get("name") or default_name).strip()
            skills[name.lower()] = {
                "name": name,
                "description": meta.get("description", "").strip(),
                "path": skill_path,
                "body": body,
                "files": _bundled_files(skill_path),
            }

    _CACHE = skills
    return skills


def resolve(query):
    """Return (skill, candidates). Exactly one of the two is meaningful."""
    skills = discover_skills()
    q = query.strip().lower()

    if q in skills:
        return skills[q], []

    for match_fn in (lambda k: k.startswith(q), lambda k: q in k):
        hits = [s for k, s in skills.items() if match_fn(k)]
        if len(hits) == 1:
            return hits[0], []
        if len(hits) > 1:
            return None, hits

    close = get_close_matches(q, list(skills), n=3, cutoff=0.6)
    if len(close) == 1:
        return skills[close[0]], []
    return None, [skills[c] for c in close]


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def format_catalog():
    skills = discover_skills()
    if not skills:
        return (
            "No skills found.\n"
            f"Add one at {os.path.join(SKILLS_DIR, '<name>', SKILL_FILE)} "
            f"(or {os.path.join(SKILLS_DIR, '<name>.md')})."
        )

    width = max(len(s["name"]) for s in skills.values())
    lines = ["Available skills:"]
    for key in sorted(skills):
        skill = skills[key]
        lines.append(f"  {skill['name']:<{width}}  {skill['description'] or '(no description)'}")
    lines.append("")
    lines.append("Load one with: /skill <name> [task]")
    return "\n".join(lines)


def render(skill, task=""):
    """Turn a skill into the user-turn text that gets sent to the model."""
    rel_path = os.path.relpath(skill["path"], PROJECT_ROOT)

    parts = [
        f"[skill: {skill['name']}]",
        f"The instructions below were loaded from {rel_path} at the user's explicit request. "
        "Follow them for this task and for the rest of this conversation, unless the user says otherwise.",
        "--- BEGIN SKILL ---",
        skill["body"],
        "--- END SKILL ---",
    ]

    if skill["files"]:
        listing = "\n".join("  - " + f for f in skill["files"])
        parts.append(
            "Files bundled with this skill (read them with run_powershell tool only if the "
            f"instructions above call for it):\n{listing}"
        )

    if task:
        parts.append(f"Task: {task}")
    else:
        parts.append(
            "No task was given yet. Confirm in one short line that the skill is loaded, "
            "and state what you need from the user to start."
        )

    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# command entry point
# --------------------------------------------------------------------------

def handle_skill_command(user_input):
    """Handle '/skill...' input.

    Returns the expanded prompt text to send as a normal user turn, or None if
    the command was fully handled locally (listing, reload, error).
    """
    parts = user_input.strip().split(maxsplit=1)
    command = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if rest.split(" ")[0].lower() in ("reload", "refresh") and rest:
        discover_skills(force=True)
        print("[skills reloaded]")
        print(format_catalog() + "\n")
        return None

    if command == "/skills" or not rest:
        print(format_catalog() + "\n")
        return None

    name, _, task = rest.partition(" ")
    skill, candidates = resolve(name)

    if skill is None:
        if candidates:
            print(f"'{name}' is ambiguous. Did you mean: "
                  + ", ".join(c["name"] for c in candidates) + "\n")
        else:
            print(f"No skill named '{name}'.\n")
            print(format_catalog() + "\n")
        return None

    print(f"[skill loaded: {skill['name']} <- {os.path.relpath(skill['path'], PROJECT_ROOT)}]")
    return render(skill, task.strip())
