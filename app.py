"""
app.py
------
The Streamlit frontend. It renders widgets and calls into `backend.ChatSession`
and `workspace`; it holds no agent logic of its own, so swapping it for a TUI
means rewriting this file only.

Run it with:  streamlit run app.py

The CLI (`python main.py`) still works untouched — this is a second frontend
over the same core, not a replacement.
"""

import base64
import io
import json
import os
import subprocess
import sys
import time
import uuid

import streamlit as st

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

import authoring
import backend
import workspace
from backend import ChatSession, sanitize_content

try:                                   # pip install streamlit-ace for the real editor
    from streamlit_ace import st_ace
except ImportError:
    st_ace = None


# --- FILE UPLOAD HELPERS ----------------------------------------------------

MAX_IMAGE_DIMENSION = 1024  # downscale long edge to this many px before sending
MAX_TEXT_FILE_CHARS = 20_000_000  # truncate text/code file contents beyond this length

IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}

TEXT_EXTENSION_LANGS = {
    "txt": "", "md": "markdown", "csv": "", "tsv": "",
    "py": "python", "js": "javascript", "jsx": "jsx", "ts": "typescript", "tsx": "tsx",
    "java": "java", "kt": "kotlin", "swift": "swift", "go": "go", "rs": "rust",
    "c": "c", "h": "c", "cpp": "cpp", "cc": "cpp", "hpp": "cpp", "cs": "csharp",
    "rb": "ruby", "php": "php", "pl": "perl", "lua": "lua", "r": "r", "m": "matlab",
    "html": "html", "css": "css", "scss": "scss",
    "json": "json", "yaml": "yaml", "yml": "yaml", "xml": "xml", "toml": "toml", "ini": "ini",
    "sql": "sql", "sh": "bash", "bash": "bash",
}
TEXT_EXTENSIONS = set(TEXT_EXTENSION_LANGS)


def file_extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def is_image_file(filename: str) -> bool:
    return file_extension(filename) in IMAGE_EXTENSIONS


def read_text_file(uploaded_file) -> tuple[str, bool]:
    """Decode an uploaded text/code file. Returns (text, was_truncated)."""
    raw = uploaded_file.getvalue()
    text = raw.decode("utf-8", errors="replace")
    truncated = len(text) > MAX_TEXT_FILE_CHARS
    if truncated:
        text = text[:MAX_TEXT_FILE_CHARS]
    return text, truncated


def format_text_file_block(filename: str, text: str, truncated: bool) -> str:
    lang = TEXT_EXTENSION_LANGS.get(file_extension(filename), "")
    note = "\n*(truncated — file exceeds the size limit)*" if truncated else ""
    return f"**📄 {filename}**\n```{lang}\n{text}\n```{note}"


def encode_image_to_data_url(uploaded_file) -> str:
    raw = uploaded_file.getvalue()

    if not HAS_PIL:
        mime = uploaded_file.type or "image/png"
        b64 = base64.b64encode(raw).decode("utf-8")
        return f"data:{mime};base64,{b64}"

    image = Image.open(io.BytesIO(raw))
    image.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION))

    buffer = io.BytesIO()
    if image.mode == "RGBA":
        image.save(buffer, format="PNG")
        mime = "image/png"
    else:
        image.convert("RGB").save(buffer, format="JPEG", quality=85)
        mime = "image/jpeg"

    b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def data_url_to_bytes(data_url: str) -> bytes:
    _, b64data = data_url.split(",", 1)
    return base64.b64decode(b64data)


# --- SESSION WIRING ---------------------------------------------------------


def get_session() -> ChatSession:
    """One ChatSession per workspace, kept across reruns."""
    session = st.session_state.get("session")
    if session is None or session.root != os.path.abspath(workspace.current()):
        session = ChatSession()
        st.session_state.session = session
    return session


def switch_workspace_environment(error: str | None = None) -> None:
    """Re-bind the session after chdir into another project."""
    if error:
        st.error(error)
        return
    # Drop the session and every workspace-scoped widget value; the next
    # get_session() rebuilds against the new cwd.
    for key in ("session", "edit_content", "edit_path", "editor_key", "flash"):
        st.session_state.pop(key, None)
    st.rerun()


def flash(note: str | None) -> None:
    if note:
        st.session_state.flash = note


# --- SIDEBAR ----------------------------------------------------------------


def _pick_folder_native() -> tuple[str | None, str | None]:
    """
    Open the operating system's native 'choose a directory' dialog.

    Order of preference:
      1. The desktop's own picker on Linux/BSD — zenity (GNOME & friends) or
         kdialog (KDE). These are what Linux users expect and need no Python
         GUI toolkit installed.
      2. A tkinter dialog in a throwaway child process (Tk insists on owning
         the main thread, which Streamlit's script runner is not).

    Returns (path, problem); at most one is truthy. `path` is None when the
    user cancels. `problem` explains why no dialog could open at all (missing
    tkinter/zenity, headless host, …) so the caller can tell the user to type
    the path manually.
    """
    # --- 1. desktop-native pickers (Linux/BSD) -----------------------------
    if os.name != "nt" and sys.platform != "darwin":
        for argv in (
            ["zenity", "--file-selection", "--directory"],
            ["kdialog", "--getexistingdirectory", os.path.expanduser("~")],
        ):
            try:
                done = subprocess.run(argv, capture_output=True, text=True, timeout=300)
            except (FileNotFoundError, PermissionError):
                continue                        # tool not installed, try the next
            except Exception as e:
                return None, f"`{argv[0]}` failed to run ({e})."
            if done.returncode == 0 and done.stdout.strip():
                return done.stdout.strip(), None
            if done.returncode != 0:
                return None, None               # user closed the dialog

    # --- 2. tkinter in a child process -------------------------------------
    code = (
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "root = tk.Tk()\n"
        "root.withdraw()\n"
        "root.attributes('-topmost', True)\n"   # don't hide behind the browser
        "print(filedialog.askdirectory())\n"
        "root.destroy()\n"
    )
    try:
        done = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=300,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception as e:
        return None, f"Couldn't open a folder dialog ({e})."

    if done.stdout.strip():
        return done.stdout.strip(), None
    if "No module named 'tkinter'" in done.stderr:
        return None, ("No folder picker found — install `python3-tk`, `zenity`, "
                      "or `kdialog` (or type the path below).")
    return None, None                           # user cancelled


def render_workspace_panel(session: ChatSession) -> None:
    with st.container(border=True):
        #st.markdown("**📂 Workspace**")

        options = ["Current Directory"] + workspace.load_recent_projects()
        selected = st.selectbox(
            "Switch Workspace", options, format_func=lambda p: os.path.basename(p) or p
        )

        if st.button("➕ Create New Project", key="new_proj_btn", use_container_width=True):
            st.session_state.show_new_project_input = True

        if st.session_state.get("show_new_project_input", False):
            if st.button("📁 Browse…", use_container_width=True,
                         help="Open your operating system's folder picker"):
                with st.spinner("Waiting for the folder dialog…"):
                    picked, problem = _pick_folder_native()
                if problem:
                    st.warning(problem)
                if picked:
                    st.session_state.new_proj_base = picked
                    # Bump the key so the Location widget below remounts with
                    # the picked value instead of ignoring `value=`.
                    st.session_state.new_proj_key = str(uuid.uuid4())
                    st.rerun()

            st.session_state.setdefault("new_proj_key", "initial")
            base = st.text_input(
                "Location", value=st.session_state.get("new_proj_base", ""),
                key=f"new_proj_base_{st.session_state.new_proj_key}",
                placeholder="Click 📁 Browse…, or paste a full path",
            )

            folder_name = st.text_input(
                "New folder name (blank = use the location above):",
                key="new_proj_name_input", placeholder="my-new-project",
            )
            target = (
                os.path.join(base.strip(), folder_name.strip())
                if base.strip() and folder_name.strip() else base.strip()
            )
            if target:
                st.caption(f"Will create / open: `{target}`")

            col1, col2 = st.columns(2)
            if col1.button("Create Project", type="primary", disabled=not base.strip()):
                st.session_state.show_new_project_input = False
                switch_workspace_environment(workspace.create_project(target))
            if col2.button("Cancel Project"):
                st.session_state.show_new_project_input = False
                st.rerun()

        if selected != "Current Directory" and os.path.abspath(selected) != session.root:
            switch_workspace_environment(workspace.switch_to(selected))

        st.caption(f"**Active:** `{workspace.current()}`")
        if os.path.abspath(workspace.current()) not in [
            os.path.abspath(p) for p in workspace.load_recent_projects()
        ]:
            if st.button("📌 Remember this directory", use_container_width=True):
                workspace.save_recent_project(workspace.current())
                st.rerun()


def render_thread_panel(session: ChatSession) -> None:
    with st.container(border=True):
        #st.markdown("**💬 Conversation**")

        threads = session.list_threads()
        index = threads.index(session.thread_id)

        selected = st.selectbox(
            "Switch Conversation", threads, index=index, format_func=lambda t: t[:24]
        )
        if selected != session.thread_id:
            session.switch_thread(selected)
            st.rerun()

        if st.button("➕ New Conversation", key="new_conv_btn", use_container_width=True):
            st.session_state.show_new_thread_input = True

        if st.session_state.get("show_new_thread_input", False):
            custom_id = st.text_input("Thread ID (optional):", key="custom_thread_id_input")
            col1, col2 = st.columns(2)
            if col1.button("Create"):
                error = session.new_thread(custom_id or None)
                if error:
                    st.error(error)
                else:
                    st.session_state.show_new_thread_input = False
                    st.rerun()
            if col2.button("Cancel"):
                st.session_state.show_new_thread_input = False
                st.rerun()

        st.caption(f"{len(threads)} thread(s) in this workspace")


def render_active_model(session: ChatSession) -> None:
    """Read-only status block. Choosing a model happens in the Models tab."""
    with st.container(border=True):
        #st.markdown("**🧠 Model**")
        if session.model:
            st.caption(f"`{session.model}`")
            st.caption(f"via {session.provider}")
            if not backend.provider_ready(session.provider):
                env = backend.core.CONFIGS[session.provider]["api_key_env"]
                st.error(f"{env} is not set.")
        else:
            st.caption("None selected — open the **🧠 Models** tab.")


def render_skills_panel(session: ChatSession) -> None:
    with st.container(border=True):
        #st.markdown("**🧩 Skills**")

        catalog = session.skill_catalog()
        if not catalog:
            st.caption("No skills found. Add one at `skills/<name>/SKILL.md`.")
            if st.button("🔄 Rescan skills", use_container_width=True):
                session.reload_skills()
                st.rerun()
            return

        names = [s["name"] for s in catalog]
        chosen = st.selectbox("Skill", names, key="skill_picker")
        description = next(s["description"] for s in catalog if s["name"] == chosen)
        if description:
            st.caption(description)

        task = st.text_area(
            "Task (optional)", height=80, placeholder="e.g. review backend.py", key="skill_task"
        )

        col1, col2 = st.columns([0.75, 0.25])
        if col1.button("▶️ Load skill", use_container_width=True, type="primary"):
            expanded = session.expand_skill(chosen, task)
            if expanded:
                session.submit(expanded)
                st.rerun()
        if col2.button("🔄", key="rescan_skills_btn", use_container_width=True,
                       help="Rescan skills directory"):
            session.reload_skills()
            st.rerun()


def render_prompts_panel(session: ChatSession) -> None:
    with st.container(border=True):
        #st.markdown("**📜 Prompts**")

        catalog = session.prompt_catalog()
        if not catalog:
            st.caption("No prompts found. Add one at `prompts/<name>/PROMPT.md`.")
            if st.button("🔄 Rescan prompts", use_container_width=True):
                session.reload_prompts()
                st.rerun()
            return

        names = [p["name"] for p in catalog]
        chosen = st.selectbox("Prompt", names, key="prompt_picker")
        description = next(p["description"] for p in catalog if p["name"] == chosen)
        if description:
            st.caption(description)

        task = st.text_area(
            "Task (optional)", height=80, placeholder="e.g. explain workspace.py",
            key="prompt_task",
        )

        col1, col2 = st.columns([0.75, 0.25])
        if col1.button("▶️ Load prompt", use_container_width=True, type="primary"):
            expanded = session.expand_prompt(chosen, task)
            if expanded:
                session.submit(expanded)
                st.rerun()
        if col2.button("🔄", key="rescan_prompts_btn", use_container_width=True,
                       help="Rescan prompts directory"):
            session.reload_prompts()
            st.rerun()


def render_sidebar(session: ChatSession) -> bool:
    """Draw the sidebar. Returns whether tools should be auto-approved."""
    with st.sidebar:
        render_workspace_panel(session)
        render_thread_panel(session)
        render_active_model(session)
        render_skills_panel(session)
        render_prompts_panel(session)

        with st.container(border=True):
            st.metric(label="Conversation Tokens (est.)", value=session.token_count)

            session.tools_enabled = st.checkbox("Enable Tools", value=session.tools_enabled)
            auto_approve = st.checkbox("Auto-Approve Tools", value=False)

            if st.button("⏮️ Undo First Turn", use_container_width=True):
                if session.undo_first_turn():
                    st.rerun()

            if st.button("↩️ Undo Last Turn", use_container_width=True):
                if session.undo_last_turn():
                    st.rerun()

            if st.button("🗑️ Clear Chat History", use_container_width=True):
                session.clear_history()
                st.rerun()

        with st.container(border=True):
            #st.markdown("**🖥️ Shell**")
            command = st.text_input("Command", key="shell_cmd", placeholder="git status")
            col1, col2 = st.columns(2)
            if col1.button("Run + share", use_container_width=True,
                           help="Output goes into the conversation"):
                if command.strip():
                    session.submit(f"!{command}")
                    st.rerun()
            if col2.button("Run quietly", use_container_width=True,
                           help="Output stays out of the conversation"):
                if command.strip():
                    flash(session.submit(f"!!{command}"))
                    st.rerun()

        with st.container(border=True):
            notes = st.text_area(
                "Quick Notes:", value=workspace.read_notes(), height=200, key="sidebar_notes"
            )
            if st.button("Save Quick Notes", use_container_width=True):
                result = workspace.write_notes(notes)
                st.error(result) if result.startswith("Error") else st.success(result)

    return auto_approve


# --- TABS -------------------------------------------------------------------


def render_history_tab(session: ChatSession) -> None:
    st.subheader("Conversation Threads")
    st.caption(f"Stored in `{workspace.chats_dir()}`")

    for tid in session.list_threads():
        summary = session.thread_summary(tid)
        col1, col2, col3, col4 = st.columns([0.7, 0.14, 0.08, 0.08])

        with col1:
            label = f"Thread: {tid}  ({summary['count']} messages)"
            if tid == session.thread_id:
                label = "▶ " + label
            with st.expander(label):
                st.write(f"**Last Human:** {summary['last_human'] or 'No human message'}")
                st.write(f"**Last AI:** {summary['last_ai'] or 'No AI message'}")

        with col2:
            new_id = st.text_input(
                "New ID", key=f"rename_input_{tid}", label_visibility="collapsed",
                placeholder="new id",
            )
        with col3:
            if st.button("R", key=f"rename_btn_{tid}", help="Rename"):
                error = session.rename_thread(tid, new_id)
                if error:
                    st.error(error)
                else:
                    st.rerun()
        with col4:
            if st.button("D", key=f"del_thread_{tid}", help="Delete"):
                session.delete_thread(tid)
                st.rerun()


def render_logs_tab(session: ChatSession) -> None:
    st.subheader("Full Message History")

    for i, msg in enumerate(session.messages):
        col1, col2 = st.columns([0.9, 0.1])
        with col1:
            label = f"{i}: {msg.get('role')}"
            if msg.get("tool_calls"):
                label += f" ({', '.join(c['function']['name'] for c in msg['tool_calls'])})"
            with st.expander(label):
                st.code(json.dumps(msg, indent=2, default=str), language="json")
        with col2:
            if st.button("🗑️", key=f"del_msg_{i}"):
                session.delete_message(i)
                st.rerun()


def render_editor_tab() -> None:
    st.subheader("File Editor")
    st.caption(f"Editing inside `{workspace.current()}`")

    files = workspace.list_project_files()
    if not files:
        st.info("No editable files found in this workspace.")
        return

    edit_path = st.selectbox("Select a file to edit:", files)

    if st.button("Load File"):
        content = workspace.read_file(edit_path)
        if content.startswith("Error"):
            st.error(content)
        else:
            st.session_state.edit_content = content
            st.session_state.edit_path = edit_path
            # A unique key forces the editor to remount with the new text
            st.session_state.editor_key = str(uuid.uuid4())

    if "edit_content" not in st.session_state:
        return

    loaded_path = st.session_state.get("edit_path", edit_path)
    if loaded_path != edit_path:
        st.warning(f"Editing `{loaded_path}`. Press Load File to open `{edit_path}`.")

    st.session_state.setdefault("editor_key", "editor_initial")
    language = workspace.language_for(loaded_path)

    if st_ace:
        new_content = st_ace(
            value=st.session_state.edit_content,
            language=language if language != "text" else "plain_text",
            theme="monokai",
            key=st.session_state.editor_key,
        )
    else:
        st.caption("`pip install streamlit-ace` for syntax highlighting.")
        new_content = st.text_area(
            "Contents", value=st.session_state.edit_content, height=520,
            key=st.session_state.editor_key,
        )

    col1, col2 = st.columns(2)

    with col1:
        if st.button("💾 Save Changes", use_container_width=True):
            result = workspace.write_file(loaded_path, new_content)
            if result.startswith("Error"):
                st.error(result)
            else:
                st.success(result)
                st.session_state.edit_content = new_content

    with col2:
        if st.button("🔄 Reset Unsaved Changes", use_container_width=True):
            st.session_state.edit_content = workspace.read_file(loaded_path)
            st.session_state.editor_key = str(uuid.uuid4())
            st.rerun()


def render_models_tab(session: ChatSession) -> None:
    st.subheader("Model Selection")

    # --- current model + catalog freshness ---
    col1, col2 = st.columns([0.65, 0.35])
    with col1:
        if session.model:
            st.success(f"**Active:** `{session.model}`  ·  {session.provider}")
        else:
            st.warning("No model selected yet. Pick one below.")
    with col2:
        if st.button("🔄 Re-discover models", use_container_width=True, type="primary"):
            with st.spinner("Querying every provider with a key set..."):
                backend.refresh_catalog()
            st.rerun()
        updated = backend.catalog_updated_at()
        st.caption(
            f"Catalog updated {time.strftime('%d %b %H:%M', time.localtime(updated))}"
            if updated else "Catalog has never been built."
        )

    # --- provider key status ---
    status = backend.provider_status()
    cols = st.columns(len(status))
    for col, entry in zip(cols, status):
        with col:
            if entry["ready"]:
                col.metric(entry["provider"], f"{entry['count']} models")
            else:
                col.metric(entry["provider"], "no key", delta=entry["env"], delta_color="off")

    if not any(entry["ready"] for entry in status):
        st.error("No API keys found. Set at least one in your .env, then re-discover.")
        return

    st.divider()

    # --- search + filter ---
    col1, col2 = st.columns([0.55, 0.45])
    query = col1.text_input("Search", placeholder="gpt, llama, gemini…", key="model_search")
    wanted = col2.multiselect(
        "Providers",
        [e["provider"] for e in status if e["count"]],
        default=[e["provider"] for e in status if e["count"]],
        key="model_provider_filter",
    )

    matches = [pair for pair in backend.search_catalog(query) if pair[0] in wanted]

    if not matches:
        st.info("Nothing matches that search.")
        return

    limit = 60
    st.caption(
        f"{len(matches)} model(s)" + (f" — showing the first {limit}" if len(matches) > limit else "")
    )

    # --- results ---
    current = (session.provider, session.model)
    for provider, model in matches[:limit]:
        is_current = (provider, model) == current
        col1, col2, col3 = st.columns([0.62, 0.22, 0.16])
        col1.markdown(f"{'✅ ' if is_current else ''}`{model}`")
        col2.caption(provider)
        if is_current:
            col3.button("In use", key=f"use_{provider}_{model}", disabled=True,
                        use_container_width=True)
        elif col3.button("Use", key=f"use_{provider}_{model}", use_container_width=True):
            session.set_model(provider, model)
            st.rerun()


def render_skills_tab(session: ChatSession) -> None:
    st.subheader("Installed Skills")

    catalog = session.skill_catalog()
    if not catalog:
        st.info("Nothing in `skills/` yet. Add `skills/<name>/SKILL.md` and rescan.")
        return

    st.caption("Skills live beside the app, so they follow you across workspaces.")
    for skill in catalog:
        with st.expander(f"{skill['name']} — {skill['description'] or 'no description'}"):
            st.caption(skill["path"])
            if skill["files"]:
                st.caption("Bundled: " + ", ".join(skill["files"]))
            st.code(skill["body"], language="markdown")


def render_prompts_tab(session: ChatSession) -> None:
    st.subheader("Installed Prompts")

    catalog = session.prompt_catalog()
    if not catalog:
        st.info("Nothing in `prompts/` yet. Add `prompts/<name>/PROMPT.md` and rescan.")
        return

    st.caption("Prompts live beside the app, so they follow you across workspaces.")
    for prompt in catalog:
        with st.expander(f"{prompt['name']} — {prompt['description'] or 'no description'}"):
            st.caption(prompt["path"])
            if prompt["files"]:
                st.caption("Bundled: " + ", ".join(prompt["files"]))
            st.code(prompt["body"], language="markdown")


# --- AUTHORING --------------------------------------------------------------

NEW_ENTRY = "➕ New…"


def _author_load(kind: str, slug: str | None) -> None:
    """
    Point the editor at `slug` (or a blank entry).

    Only plain state is written here, never a widget key: this runs from button
    callbacks, and Streamlit refuses to let you assign to the key of a widget
    that already rendered this run. The form widgets instead take their value
    from these vars and carry `author_form_key` in their own keys, so bumping
    that key remounts them with the new values.
    """
    st.session_state.author_kind = kind
    st.session_state.author_slug = slug
    st.session_state.author_form_key = str(uuid.uuid4())

    loaded = authoring.load(kind, slug) if slug else None
    st.session_state.author_name = loaded["name"] if loaded else ""
    st.session_state.author_desc = loaded["description"] if loaded else ""
    st.session_state.author_body = loaded["body"] if loaded else ""
    st.session_state.author_layout = loaded["layout"] if loaded else authoring.SINGLE_FILE
    if loaded and loaded["malformed_frontmatter"]:
        st.session_state.author_warning = (
            f"`{loaded['rel_path']}` has no closing `---` fence, so the loader was "
            "reading its frontmatter as body text. Saving here repairs it."
        )


def _on_disk_slugs(kind: str) -> list[str]:
    """
    Entry names as they exist on disk.

    Editing keys on the filename, not the catalog, because the catalog is keyed
    on the frontmatter name and the two can disagree.
    """
    cfg = authoring.KINDS[kind]
    base, entry = cfg["base"], cfg["entry"]
    if not os.path.isdir(base):
        return []
    found = []
    for item in sorted(os.listdir(base)):
        if item.startswith((".", "__")):
            continue
        if os.path.isdir(os.path.join(base, item)):
            if os.path.isfile(os.path.join(base, item, entry)):
                found.append(item)
        elif item.lower().endswith(".md"):
            found.append(os.path.splitext(item)[0])
    return found


def render_authoring_tab(session: ChatSession) -> None:
    st.subheader("Create & Edit")
    st.caption(
        "Writes into `prompts/` and `skills/` beside the app, not into the workspace. "
        "Saving refreshes this session's catalog immediately."
    )

    st.session_state.setdefault("author_form_key", "author_initial")
    st.session_state.setdefault("author_slug", None)
    st.session_state.setdefault("author_name", "")
    st.session_state.setdefault("author_desc", "")
    st.session_state.setdefault("author_body", "")
    st.session_state.setdefault("author_layout", authoring.SINGLE_FILE)

    kind = st.radio(
        "Catalog", ["prompt", "skill"], horizontal=True, key="author_kind_picker",
        format_func=lambda k: f"{'📜' if k == 'prompt' else '🧩'} {k.title()}s",
    )
    cfg = authoring.KINDS[kind]

    choices = [NEW_ENTRY] + _on_disk_slugs(kind)
    current = st.session_state.author_slug
    index = choices.index(current) if current in choices else 0

    # The form key rides along in this widget's key too, so a reset remounts the
    # selectbox at the right index instead of fighting the old selection.
    picked = st.selectbox(
        "Entry", choices, index=index,
        key=f"author_pick_{kind}_{st.session_state.author_form_key}",
    )
    wanted = None if picked == NEW_ENTRY else picked

    # Selection or catalog changed -> reload the form. Safe here: no form widget
    # has been instantiated yet this run.
    if st.session_state.get("author_kind") != kind or current != wanted:
        _author_load(kind, wanted)
        st.rerun()

    editing = st.session_state.author_slug

    notice = st.session_state.pop("author_notice", None)
    if notice:
        st.success(notice)
    warning = st.session_state.pop("author_warning", None)
    if warning:
        st.warning(warning)

    form_key = st.session_state.author_form_key

    col1, col2 = st.columns([0.42, 0.58])
    with col1:
        name = st.text_input(
            "Name", value=st.session_state.author_name,
            placeholder="kebab-case, no spaces",
            help=f"Loaded with `{cfg['command']} <name>`.",
            key=f"author_name_{form_key}",
        )
    with col2:
        layouts = [authoring.SINGLE_FILE, authoring.DIRECTORY]
        layout = st.radio(
            "Layout", layouts, horizontal=True,
            index=layouts.index(st.session_state.author_layout),
            format_func=lambda l: (
                "Single file" if l == authoring.SINGLE_FILE else "Directory + resources"
            ),
            help="Use the directory layout only when the entry ships resource files.",
            key=f"author_layout_{form_key}",
        )

    description = st.text_input(
        "Description", value=st.session_state.author_desc,
        placeholder="One line, shown in the catalog listing.",
        key=f"author_desc_{form_key}",
    )

    if name:
        destination = authoring.target_path(kind, name, layout)
        st.caption(f"→ `{os.path.relpath(destination, cfg['module'].PROJECT_ROOT)}`")

    st.caption("Body only — the fields above are written as frontmatter for you.")
    if st_ace:
        body = st_ace(
            value=st.session_state.author_body, language="markdown",
            theme="monokai", height=420, key=f"author_body_{form_key}",
        )
    else:
        body = st.text_area(
            "Body", value=st.session_state.author_body, height=420,
            label_visibility="collapsed", key=f"author_body_{form_key}",
        )
    body = body or ""

    errors, warnings = authoring.validate(kind, name, description, body, layout, editing)
    for problem in errors:
        st.error(problem)
    for note in warnings:
        st.warning(note)

    col1, col2, col3, col4 = st.columns([0.28, 0.28, 0.18, 0.26])

    if col1.button("💾 Save", type="primary", use_container_width=True,
                   disabled=bool(errors)):
        ok, message = authoring.save(kind, name, description, body, layout, editing)
        if ok:
            session.reload_prompts() if kind == "prompt" else session.reload_skills()
            _author_load(kind, name.strip())
            st.session_state.author_notice = message
            st.rerun()
        st.error(message)

    if col2.button("🧪 Insert starter body", use_container_width=True,
                   disabled=bool(body.strip())):
        st.session_state.author_body = authoring.starter_body(name or "new entry")
        st.session_state.author_form_key = str(uuid.uuid4())
        st.rerun()

    if col3.button("↩️ Revert", use_container_width=True):
        _author_load(kind, editing)
        st.rerun()

    if editing:
        with col4.popover("🗑️ Delete", use_container_width=True):
            st.caption(f"Permanently delete `{editing}`?")
            if st.session_state.author_layout == authoring.DIRECTORY:
                st.caption("Its bundled files go with it.")
            if st.button("Yes, delete it", type="primary", key="author_delete_confirm"):
                ok, message = authoring.delete(kind, editing)
                if ok:
                    session.reload_prompts() if kind == "prompt" else session.reload_skills()
                    _author_load(kind, None)
                    st.session_state.author_notice = message
                    st.rerun()
                st.error(message)

    if editing:
        _render_bundled_editor(kind, editing)
        with st.expander("👁️ Preview what the model receives"):
            rendered = authoring.preview(kind, editing, "sample task")
            st.code(rendered or "Not loadable — fix the errors above.",
                    language="markdown")


def _render_bundled_editor(kind: str, slug: str) -> None:
    saved_layout = st.session_state.author_layout

    with st.expander("📎 Bundled files"):
        if saved_layout != authoring.DIRECTORY:
            st.caption(
                "Single-file entries cannot ship resources. Switch the layout to "
                "directory and save, then add files here."
            )
            return

        st.caption(
            "These are listed to the model, not inlined. Reference a file by its "
            "path in the body or it will never be read."
        )

        # Bundled paths are recorded relative to the repo root, but
        # save_bundled wants them relative to the entry's own directory — and
        # they can be nested a level deeper.
        entry_dir = os.path.relpath(
            os.path.dirname(authoring.target_path(kind, slug, authoring.DIRECTORY)),
            authoring.KINDS[kind]["module"].PROJECT_ROOT,
        )

        for rel in authoring.bundled_files(kind, slug):
            st.markdown(f"**`{rel}`**")
            content = st.text_area(
                rel, value=authoring.read_bundled(kind, rel), height=200,
                key=f"bundle_body_{kind}_{rel}", label_visibility="collapsed",
            )
            col1, col2, _ = st.columns([0.2, 0.2, 0.6])
            if col1.button("💾 Save", key=f"bundle_save_{kind}_{rel}",
                           use_container_width=True):
                ok, message = authoring.save_bundled(
                    kind, slug, os.path.relpath(rel, entry_dir), content
                )
                (st.success if ok else st.error)(message)
            if col2.button("🗑️ Remove", key=f"bundle_del_{kind}_{rel}",
                           use_container_width=True):
                ok, message = authoring.delete_bundled(kind, rel)
                if ok:
                    st.rerun()
                st.error(message)

        st.divider()
        new_name = st.text_input("New file", placeholder="checklist.md",
                                 key=f"bundle_new_name_{kind}_{slug}")
        new_body = st.text_area("Contents", height=140,
                                key=f"bundle_new_body_{kind}_{slug}")
        if st.button("➕ Add file", key=f"bundle_add_{kind}_{slug}"):
            ok, message = authoring.save_bundled(kind, slug, new_name, new_body)
            if ok:
                st.rerun()
            st.error(message)


# --- CHAT -------------------------------------------------------------------


def render_message_content(content) -> None:
    """Render a message's content, whether it's plain text or a list of
    OpenAI-style content parts (text / image_url)."""
    if isinstance(content, list):
        for part in content:
            if part.get("type") == "text":
                st.markdown(sanitize_content(part["text"]))
            elif part.get("type") == "image_url":
                try:
                    st.image(data_url_to_bytes(part["image_url"]["url"]))
                except Exception as e:
                    st.caption(f"[Image display error: {e}]")
    else:
        st.markdown(sanitize_content(content))


def render_transcript(session: ChatSession) -> None:
    """Chat history, rendered straight from the raw message list."""
    for msg in session.messages:
        role = msg.get("role")

        if role == "user":
            with st.chat_message("user"):
                render_message_content(msg.get("content", ""))

        elif role == "assistant":
            content = msg.get("content") or ""
            # Only render if there is actual text (ignores silent tool calls)
            if isinstance(content, list) or (isinstance(content, str) and content.strip()):
                with st.chat_message("assistant"):
                    render_message_content(content)
            elif msg.get("tool_calls"):
                with st.chat_message("assistant"):
                    names = ", ".join(c["function"]["name"] for c in msg["tool_calls"])
                    st.caption(f"🔧 requested: {names}")

        elif role == "tool":
            with st.chat_message("tool", avatar="🔧"):
                with st.expander(f"Result from {msg.get('name') or 'tool'}", expanded=False):
                    st.code(msg.get("content", ""), language="markdown")


def render_tool_approval(session: ChatSession, auto_approve: bool) -> None:
    """The approval gate shown whenever the model asked for a tool."""
    with st.chat_message("assistant"):
        st.warning("⚠️ **The agent has requested to execute the following tool(s):**")

        for call in session.pending:
            with st.expander(f"Tool Call: {call.name}", expanded=True):
                for key, value in call.display_args.items():
                    st.markdown(f"**{key}:**")
                    st.code(str(value), language="python")

        if auto_approve:
            st.info("Auto-approving because the checkbox is ticked...")
            session.approve_tools()
            st.rerun()

        col1, col2, col3 = st.columns([0.4, 0.3, 0.3])

        if col1.button("✅ Approve Action"):
            with st.status("Executing tools...", expanded=True) as status:
                for result in session.approve_tools():
                    st.write(f"**{result['name']}** → {result['output'][:200]}")
                status.update(label="Action complete!", state="complete", expanded=False)
            st.rerun()

        if col2.button("❌ Deny Action"):
            session.deny_tools()
            st.rerun()

        with col3:
            with st.popover("💬 Provide Feedback"):
                feedback = st.text_area("Tell the agent what to change:")
                if st.button("Submit Feedback"):
                    if feedback.strip():
                        session.send_tool_feedback(feedback)
                        st.rerun()
                    else:
                        st.warning("Please enter some feedback.")


# --- ENTRY POINT ------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="AI Model Chat", page_icon="💬", layout="wide")

    session = get_session()
    auto_approve = render_sidebar(session)

    (
        tab_chat,
        tab_models,
        tab_edit,
        tab_skills,
        tab_prompts,
        tab_author,
        tab_logs,
        tab_history,
    ) = st.tabs(
        [
            "💬 Chat Interface",
            "🧠 Models",
            "📝 Editor",
            "🧩 Skills",
            "📜 Prompts",
            "✍️ Create & Edit",
            "🗒️ Message Logs",
            "🕒 Manage History",
        ]
    )

    with tab_models:
        render_models_tab(session)

    with tab_history:
        render_history_tab(session)

    with tab_logs:
        render_logs_tab(session)

    with tab_skills:
        render_skills_tab(session)

    with tab_prompts:
        render_prompts_tab(session)

    with tab_author:
        render_authoring_tab(session)

    with tab_edit:
        render_editor_tab()

    with tab_chat:
        if session.provider:
            st.caption(
                f"**Model:** `[{session.provider}] {session.model}` · "
                f"**Thread:** `{session.thread_id}` · **Dir:** `{os.path.basename(session.root)}`"
            )

        note = st.session_state.pop("flash", None)
        if note:
            st.code(note, language="bash")

        render_transcript(session)

        if session.last_error:
            st.error(session.last_error)

        if session.pending:
            render_tool_approval(session, auto_approve)

        # One model call per rerun; the loop settles when `busy` goes false.
        elif session.busy:
            with st.chat_message("assistant"):
                with st.spinner(f"{session.model} is thinking..."):
                    session.step()
            st.rerun()

    # --- NEW USER INPUT ---

    if not session.is_ready():
        with tab_chat:
            st.warning(
                "⚠️ **Open the 🧠 Models tab** to pick a model, and make sure its API key is "
                "set in your .env."
            )
        st.chat_input("Select a model to start chatting...", disabled=True)
        return

    user_prompt = st.chat_input(
        "Message, !shell command, /skill <name> or /prompt <name> … (use + to attach files)",
        accept_file="multiple",
        file_type=sorted(IMAGE_EXTENSIONS | TEXT_EXTENSIONS),
    )

    if user_prompt:
        user_text = user_prompt.text if hasattr(user_prompt, "text") else str(user_prompt)
        attached_files = user_prompt.files if hasattr(user_prompt, "files") else []

        image_parts = []
        text_blocks = []
        for f in attached_files:
            if is_image_file(f.name):
                image_parts.append({
                    "type": "image_url",
                    "image_url": {"url": encode_image_to_data_url(f)},
                })
            else:
                text, truncated = read_text_file(f)
                text_blocks.append(format_text_file_block(f.name, text, truncated))

        full_text = user_text
        if text_blocks:
            full_text = (full_text + "\n\n" if full_text else "") + "\n\n".join(text_blocks)

        if image_parts:
            content = []
            if full_text:
                content.append({"type": "text", "text": full_text})
            content.extend(image_parts)
        else:
            content = full_text

        # If there are image parts, session.submit expects text or we can pass structured content.
        # Let's handle structured content or submit via session.
        if isinstance(content, list) or image_parts:
            # Directly append and trigger model step if it's structured image content
            session.messages.append({"role": "user", "content": content})
            session.busy = True
            session.last_error = None
            session._save()
            st.rerun()
        else:
            flash(session.submit(full_text))
            st.rerun()


if __name__ == "__main__":
    main()
