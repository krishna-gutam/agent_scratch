"""
workspace.py
------------
Everything that depends on *where* you are: the active project directory, the
files in it, its notes, and the per-project state directory that holds its
conversation threads.

Layout inside whatever directory you're working in:

    <project>/
        .chatbot/
            chats/<thread>.json     conversations, scoped to this project
            notes.md                quick notes, scoped to this project
            state.json              last thread + last model used here

The recents list and the model catalog are *not* per-project — they live next
to this file, so switching workspace never loses them.
"""

import contextlib
import json
import os

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIRNAME = ".chatbot"
RECENTS_FILE = os.path.join(APP_DIR, ".recent_projects.json")
MAX_RECENTS = 12

IGNORED_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".idea", ".vscode", STATE_DIRNAME,
}
IGNORED_EXT = {
    ".pyc", ".pyo", ".so", ".dll", ".dylib", ".exe", ".bin", ".o", ".a",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf", ".zip",
    ".tar", ".gz", ".7z", ".mp3", ".mp4", ".mov", ".sqlite", ".db",
}


# --- LOCATIONS --------------------------------------------------------------


def current() -> str:
    return os.getcwd()


def state_dir(root: str | None = None) -> str:
    path = os.path.join(root or current(), STATE_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def chats_dir(root: str | None = None) -> str:
    path = os.path.join(state_dir(root), "chats")
    os.makedirs(path, exist_ok=True)
    return path


def notes_path(root: str | None = None) -> str:
    return os.path.join(state_dir(root), "notes.md")


def state_path(root: str | None = None) -> str:
    return os.path.join(state_dir(root), "state.json")


@contextlib.contextmanager
def app_directory():
    """Run a block from the app's own directory, then come back.

    `main.discover_models()` writes `discovered_models.json` to a relative
    path. Without this the catalog would be re-downloaded into every project
    you ever open, so we pin it beside main.py instead.
    """
    previous = os.getcwd()
    os.chdir(APP_DIR)
    try:
        yield
    finally:
        os.chdir(previous)


# --- PER-PROJECT STATE ------------------------------------------------------


def read_state(root: str | None = None) -> dict:
    try:
        with open(state_path(root), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_state(patch: dict, root: str | None = None) -> None:
    state = read_state(root)
    state.update(patch)
    try:
        with open(state_path(root), "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def remember_model(provider: str, model: str, root: str | None = None) -> None:
    write_state({"provider": provider, "model": model}, root)


def last_model(root: str | None = None) -> tuple[str | None, str | None]:
    state = read_state(root)
    return state.get("provider"), state.get("model")


# --- RECENT PROJECTS --------------------------------------------------------


def load_recent_projects() -> list[str]:
    """Most recently opened first. Directories that vanished are dropped."""
    try:
        with open(RECENTS_FILE, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except Exception:
        return []
    return [p for p in entries if isinstance(p, str) and os.path.isdir(p)]


def save_recent_project(path: str) -> None:
    path = os.path.abspath(path)
    entries = [p for p in load_recent_projects() if os.path.abspath(p) != path]
    entries.insert(0, path)
    try:
        with open(RECENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(entries[:MAX_RECENTS], f, indent=2)
    except Exception:
        pass


def forget_project(path: str) -> None:
    path = os.path.abspath(path)
    entries = [p for p in load_recent_projects() if os.path.abspath(p) != path]
    try:
        with open(RECENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2)
    except Exception:
        pass


def switch_to(path: str) -> str | None:
    """chdir into an existing project. Returns an error string, or None on success."""
    if not path:
        return "Error: no path given."
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        return f"Error: {path} is not a directory."
    if os.path.abspath(current()) == path:
        return None
    try:
        os.chdir(path)
    except OSError as e:
        return f"Error: {e}"
    save_recent_project(path)
    return None


def create_project(path: str) -> str | None:
    """Create a directory (parents included) and switch into it."""
    if not path:
        return "Error: no path given."
    path = os.path.abspath(os.path.expanduser(path))
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        return f"Error: {e}"
    return switch_to(path)


# --- NOTES ------------------------------------------------------------------


def read_notes(root: str | None = None) -> str:
    try:
        with open(notes_path(root), "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def write_notes(text: str, root: str | None = None) -> str:
    try:
        with open(notes_path(root), "w", encoding="utf-8") as f:
            f.write(text or "")
        return "Notes saved."
    except Exception as e:
        return f"Error: {e}"


# --- FILES ------------------------------------------------------------------


def resolve(rel_path: str, root: str | None = None) -> str | None:
    """Absolute path for a project-relative path, or None if it escapes the project."""
    root_abs = os.path.abspath(root or current())
    full = os.path.abspath(os.path.join(root_abs, rel_path))
    if full != root_abs and not full.startswith(root_abs + os.sep):
        return None
    return full


def list_project_files(root: str | None = None, limit: int = 800) -> list[str]:
    """Project-relative paths of editable text files, shallow-first."""
    root_abs = os.path.abspath(root or current())
    found = []
    for dirpath, dirnames, filenames in os.walk(root_abs):
        dirnames[:] = sorted(
            d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")
        )
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            if os.path.splitext(filename)[1].lower() in IGNORED_EXT:
                continue
            found.append(os.path.relpath(os.path.join(dirpath, filename), root_abs))
            if len(found) >= limit:
                return found
    return found


def read_file(rel_path: str, root: str | None = None) -> str:
    full = resolve(rel_path, root)
    if full is None:
        return "Error: path is outside the active workspace."
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception as e:
        return f"Error: {e}"


def write_file(rel_path: str, content: str, root: str | None = None) -> str:
    full = resolve(rel_path, root)
    if full is None:
        return "Error: path is outside the active workspace."
    try:
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Saved {rel_path}"
    except Exception as e:
        return f"Error: {e}"


def language_for(rel_path: str) -> str:
    """Editor syntax mode from the file extension."""
    return {
        ".py": "python", ".js": "javascript", ".jsx": "jsx", ".ts": "typescript",
        ".tsx": "tsx", ".json": "json", ".md": "markdown", ".html": "html",
        ".css": "css", ".sh": "sh", ".yml": "yaml", ".yaml": "yaml",
        ".toml": "toml", ".sql": "sql", ".xml": "xml",
    }.get(os.path.splitext(rel_path)[1].lower(), "text")
