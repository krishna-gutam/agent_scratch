"""
authoring.py
------------
Create, edit and delete the Markdown files behind `/skill` and `/prompt`.

`skills.py` and `prompts.py` only ever read. This module writes, and it is the
only place that does. Everything here is deliberately paranoid, because every
way a skill or prompt can be malformed fails *silently* at load time:

  - a body that is empty after frontmatter is stripped -> entry vanishes
  - a missing closing `---` fence                      -> frontmatter leaks
    into the body and the description is lost
  - a duplicate name                                   -> later entry wins,
    earlier one disappears
  - `prompt.md` instead of `PROMPT.md`                 -> directory skipped

So `validate()` runs before every write and refuses the write on any error.
The UI can also call it on each keystroke to warn ahead of time.

The frontmatter parser in skills.py/prompts.py is not YAML: it splits each line
on the first colon. `compose()` emits only what that parser can read back.
"""

import os
import re
import shutil

import prompts as prompts_mod
import skills as skills_mod

# --- kinds ------------------------------------------------------------------
# The two catalogs are structurally identical, so everything below is written
# once and parameterised by kind rather than duplicated.

KINDS = {
    "prompt": {
        "label": "Prompt",
        "module": prompts_mod,
        "base": prompts_mod.PROMPTS_DIR,
        "entry": prompts_mod.PROMPT_FILE,      # "PROMPT.md"
        "command": "/prompt",
        "reload_command": "/prompts reload",
        "discover": prompts_mod.discover_prompts,
    },
    "skill": {
        "label": "Skill",
        "module": skills_mod,
        "base": skills_mod.SKILLS_DIR,
        "entry": skills_mod.SKILL_FILE,        # "SKILL.md"
        "command": "/skill",
        "reload_command": "/skills reload",
        "discover": skills_mod.discover_skills,
    },
}

SINGLE_FILE = "single"      # <base>/<name>.md
DIRECTORY = "directory"     # <base>/<name>/<ENTRY>.md

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

STARTER_BODY = """# {title}

<What this does, and to what input.>

## Output

<The shape of the response: sections, order, length.>

## Rules

- <Something the model does by default that is wrong here.>
"""


def _kind(kind: str) -> dict:
    try:
        return KINDS[kind]
    except KeyError:
        raise ValueError(f"Unknown kind {kind!r}; expected one of {sorted(KINDS)}.")


# --- paths ------------------------------------------------------------------


def _paths(kind: str, name: str) -> tuple[str, str]:
    """(single-file path, directory-entry path) for a name. No I/O."""
    cfg = _kind(kind)
    return (
        os.path.join(cfg["base"], f"{name}.md"),
        os.path.join(cfg["base"], name, cfg["entry"]),
    )


def _inside(base: str, path: str) -> bool:
    """True when `path` really sits under `base`, traversal and links resolved."""
    base = os.path.realpath(base)
    target = os.path.realpath(path)
    return target == base or target.startswith(base + os.sep)


def existing_layout(kind: str, name: str) -> str | None:
    """Which layout `name` is stored as on disk, or None if it isn't."""
    single, directory = _paths(kind, name)
    if os.path.isfile(directory):
        return DIRECTORY
    if os.path.isfile(single):
        return SINGLE_FILE
    return None


def target_path(kind: str, name: str, layout: str) -> str:
    single, directory = _paths(kind, name)
    return directory if layout == DIRECTORY else single


def slug_for_path(kind: str, path: str) -> str:
    """
    The on-disk name behind a catalog entry's path.

    Not the same as the entry's `name`: frontmatter overrides the filename, so
    `prompts/review/PROMPT.md` can be catalogued as `code-review`. Editing has
    to key on the file.
    """
    cfg = _kind(kind)
    if os.path.basename(path) == cfg["entry"]:
        return os.path.basename(os.path.dirname(path))
    return os.path.splitext(os.path.basename(path))[0]


# --- reading ----------------------------------------------------------------


def catalog(kind: str, force: bool = False) -> list[dict]:
    """Loaded entries, sorted by name — the same view `/prompts` shows."""
    found = _kind(kind)["discover"](force=force)
    return [found[key] for key in sorted(found)]


def names(kind: str, force: bool = False) -> list[str]:
    return [entry["name"] for entry in catalog(kind, force=force)]


def load(kind: str, name: str) -> dict | None:
    """
    Read one entry straight off disk for editing.

    Deliberately not read from the catalog: the catalog is keyed on the
    *frontmatter* name, which may differ from the filename, and editing needs
    to know the file it actually came from.
    """
    layout = existing_layout(kind, name)
    if layout is None:
        return None

    cfg = _kind(kind)
    path = target_path(kind, name, layout)
    meta, body = cfg["module"]._split_frontmatter(cfg["module"]._read(path))

    fenced = _has_closing_fence(cfg["module"]._read(path))
    return {
        "slug": name,                                   # the name on disk
        "name": meta.get("name", "").strip() or name,   # the name in frontmatter
        "description": meta.get("description", "").strip(),
        "body": body,
        "layout": layout,
        "path": path,
        "rel_path": os.path.relpath(path, cfg["module"].PROJECT_ROOT),
        "files": bundled_files(kind, name),
        "malformed_frontmatter": not fenced,
    }


def _has_closing_fence(text: str) -> bool:
    return not text.startswith("---") or text.find("\n---", 3) != -1


def bundled_files(kind: str, name: str) -> list[str]:
    """Resource files shipped next to a directory-layout entry."""
    if existing_layout(kind, name) != DIRECTORY:
        return []
    cfg = _kind(kind)
    return cfg["module"]._bundled_files(target_path(kind, name, DIRECTORY))


# --- composing --------------------------------------------------------------


def compose(name: str, description: str, body: str) -> str:
    """Frontmatter + body, in the only shape the loader's parser understands."""
    name = " ".join(name.split())
    # The parser splits on the first colon and stops the block at a line that
    # is exactly `---`, so newlines in either field would corrupt the file.
    description = " ".join(description.split())
    body = body.strip()
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"


def starter_body(name: str) -> str:
    title = name.replace("-", " ").replace("_", " ").strip().title()
    return STARTER_BODY.format(title=title or "New Entry")


# --- validation -------------------------------------------------------------


def validate(kind: str, name: str, description: str, body: str,
             layout: str, original: str | None = None) -> tuple[list[str], list[str]]:
    """
    Returns (errors, warnings). A non-empty errors list blocks the save.

    `original` is the on-disk slug being edited, so renaming onto a different
    existing entry is caught but resaving over yourself is not.
    """
    cfg = _kind(kind)
    errors: list[str] = []
    warnings: list[str] = []

    name = (name or "").strip()
    body = (body or "").strip()

    # --- name ---
    if not name:
        errors.append("Name is required.")
    elif not NAME_RE.match(name):
        errors.append(
            f"`{name}` is not a usable name. Use lowercase letters, digits, "
            "`-`, `_` or `.`, starting with a letter or digit. A space would "
            f"break `{cfg['command']} <name> <task>`, which splits on the "
            "first space."
        )
    elif not _inside(cfg["base"], target_path(kind, name, layout)):
        errors.append("That name resolves outside the catalog directory.")

    # --- body ---
    if not body:
        errors.append(
            "Body is empty. The loader skips entries whose body is blank once "
            "frontmatter is stripped, so this would silently not exist."
        )
    if "\n---\n" in f"\n{body}\n" and body.lstrip().startswith("---"):
        warnings.append(
            "The body starts with `---`, which the parser may read as a second "
            "frontmatter block. Start it with a heading instead."
        )
    marker = f"--- END {cfg['label'].upper()} ---"
    if marker in body:
        warnings.append(
            f"The body contains `{marker}`, the same marker used to close the "
            "block when it is sent to the model. Remove it."
        )

    # --- description ---
    if not description.strip():
        warnings.append(
            f"No description. `{cfg['command']}s` will show "
            "`(no description)`, which makes the catalog hard to use."
        )
    elif len(description) > 100:
        warnings.append(f"Description is {len(description)} chars; keep it under ~80.")

    # --- collisions ---
    if name and not errors:
        clash = existing_layout(kind, name)
        if clash and name != original:
            errors.append(f"`{name}` already exists as a {clash} entry. Pick another name.")
        elif clash and clash != layout:
            warnings.append(
                f"`{name}` is currently a {clash} entry. Saving converts it to "
                f"{layout} and moves the file."
            )

        # Frontmatter name wins over filename, so a *different* file can still
        # claim this name and shadow it.
        for entry in catalog(kind):
            slug = slug_for_path(kind, entry["path"])
            if entry["name"].lower() == name.lower() and slug not in (original, name):
                rel = os.path.relpath(entry["path"], cfg["module"].PROJECT_ROOT)
                errors.append(
                    f"`{rel}` already declares the name `{name}` in its "
                    "frontmatter. Two entries with one name means only one loads."
                )
                break

        # Prefix ambiguity: resolve() returns "ambiguous" when a query prefixes
        # more than one name.
        siblings = [n for n in names(kind) if n != original and n != name]
        shadowed = [n for n in siblings if n.startswith(name) or name.startswith(n)]
        if shadowed:
            warnings.append(
                f"`{name}` shares a prefix with {', '.join(f'`{s}`' for s in shadowed)}. "
                "Exact matches still work, but shorter abbreviations become ambiguous."
            )

    return errors, warnings


# --- writing ----------------------------------------------------------------


def save(kind: str, name: str, description: str, body: str, layout: str,
         original: str | None = None) -> tuple[bool, str]:
    """
    Write an entry and refresh the catalog.

    Renaming or changing layout moves the old file, keeping bundled resources.
    Returns (ok, message).
    """
    cfg = _kind(kind)
    errors, _warnings = validate(kind, name, description, body, layout, original)
    if errors:
        return False, "Not saved:\n- " + "\n- ".join(errors)

    name = name.strip()
    destination = target_path(kind, name, layout)
    if not _inside(cfg["base"], destination):
        return False, "Refusing to write outside the catalog directory."

    old_layout = existing_layout(kind, original) if original else None
    moving = bool(original) and (original != name or old_layout != layout)

    try:
        os.makedirs(os.path.dirname(destination), exist_ok=True)

        # Carry bundled resources across a rename or a single->directory change
        # before the old copy is removed.
        if moving and old_layout == DIRECTORY and layout == DIRECTORY:
            old_dir = os.path.dirname(target_path(kind, original, DIRECTORY))
            for item in os.listdir(old_dir):
                if item == cfg["entry"]:
                    continue
                src = os.path.join(old_dir, item)
                dst = os.path.join(os.path.dirname(destination), item)
                if not os.path.exists(dst):
                    shutil.move(src, dst)

        with open(destination, "w", encoding="utf-8") as handle:
            handle.write(compose(name, description, body))

        if moving:
            _remove(kind, original, old_layout)

    except OSError as error:
        return False, f"Write failed: {error}"

    cfg["discover"](force=True)

    rel = os.path.relpath(destination, cfg["module"].PROJECT_ROOT)
    verb = "Moved and saved" if moving else "Saved"
    return True, f"{verb} `{rel}`. Run `{cfg['reload_command']}` in any other open session."


def delete(kind: str, name: str) -> tuple[bool, str]:
    """Remove an entry. Directory layout takes its bundled files with it."""
    cfg = _kind(kind)
    layout = existing_layout(kind, name)
    if layout is None:
        return False, f"No {kind} named `{name}`."

    try:
        removed = _remove(kind, name, layout)
    except OSError as error:
        return False, f"Delete failed: {error}"

    cfg["discover"](force=True)
    return True, f"Deleted `{removed}`."


def _remove(kind: str, name: str, layout: str | None) -> str:
    """Delete one entry's file or directory. Returns the repo-relative path."""
    cfg = _kind(kind)
    if layout == DIRECTORY:
        victim = os.path.dirname(target_path(kind, name, DIRECTORY))
    else:
        victim = target_path(kind, name, SINGLE_FILE)

    if not _inside(cfg["base"], victim):
        raise OSError(f"{victim} is outside {cfg['base']}")
    # Never let a bad name take the whole catalog with it.
    if os.path.realpath(victim) == os.path.realpath(cfg["base"]):
        raise OSError("Refusing to delete the catalog directory itself.")

    rel = os.path.relpath(victim, cfg["module"].PROJECT_ROOT)
    if layout == DIRECTORY:
        shutil.rmtree(victim)
    elif os.path.isfile(victim):
        os.remove(victim)
    return rel


# --- bundled resources ------------------------------------------------------


def save_bundled(kind: str, name: str, filename: str, content: str) -> tuple[bool, str]:
    """Add or overwrite a resource file next to a directory-layout entry."""
    cfg = _kind(kind)
    if existing_layout(kind, name) != DIRECTORY:
        return False, "Bundled files need the directory layout. Convert this entry first."

    raw = (filename or "").strip()
    filename = raw.strip("/\\")
    parts = [p for p in filename.replace("\\", "/").split("/") if p]

    if not parts:
        return False, "Give the file a name."
    # isabs is checked on the raw string: stripping the leading slash first
    # would silently turn /etc/passwd into a relative path and accept it.
    if os.path.isabs(raw) or raw != filename or ".." in parts:
        return False, "Use a path relative to the entry's own directory."
    if any(p.startswith(".") for p in parts):
        # The loader skips dot-prefixed files and directories outright.
        return False, "Names starting with a dot are skipped by the loader."
    if any(p.startswith("__") for p in parts[:-1]):
        # _bundled_files prunes these, so the write would be a silent no-op.
        return False, "Directories starting with `__` are never scanned by the loader."
    if filename == cfg["entry"]:
        return False, f"`{cfg['entry']}` is the entry file itself; edit it above."

    base_dir = os.path.dirname(target_path(kind, name, DIRECTORY))
    destination = os.path.join(base_dir, filename)
    if not _inside(base_dir, destination):
        return False, "That path escapes the entry's directory."

    try:
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, "w", encoding="utf-8") as handle:
            handle.write(content)
    except OSError as error:
        return False, f"Write failed: {error}"

    cfg["discover"](force=True)
    return True, (
        f"Saved `{os.path.relpath(destination, cfg['module'].PROJECT_ROOT)}`. "
        "Bundled files are listed to the model, not inlined — reference it by "
        "path in the body or it will never be read."
    )


def read_bundled(kind: str, rel_path: str) -> str:
    """Read a bundled file by its repo-relative path, as listed in the catalog."""
    cfg = _kind(kind)
    full = os.path.join(cfg["module"].PROJECT_ROOT, rel_path)
    if not _inside(cfg["base"], full):
        return "Error: outside the catalog directory."
    return cfg["module"]._read(full)


def delete_bundled(kind: str, rel_path: str) -> tuple[bool, str]:
    cfg = _kind(kind)
    full = os.path.join(cfg["module"].PROJECT_ROOT, rel_path)
    if not _inside(cfg["base"], full) or not os.path.isfile(full):
        return False, "Not a file inside the catalog directory."
    if os.path.basename(full) == cfg["entry"]:
        return False, f"Delete the whole entry rather than just its {cfg['entry']}."
    try:
        os.remove(full)
    except OSError as error:
        return False, f"Delete failed: {error}"
    cfg["discover"](force=True)
    return True, f"Deleted `{rel_path}`."


# --- preview ----------------------------------------------------------------


def preview(kind: str, name: str, task: str = "") -> str | None:
    """Exactly what the model receives for this entry, for a sanity check."""
    entry, _candidates = _kind(kind)["module"].resolve(name)
    return _kind(kind)["module"].render(entry, task.strip()) if entry else None
