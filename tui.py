"""
tui.py
------
Terminal frontend. Third frontend over the same core: `main.py` is the CLI,
`app.py` is Streamlit, this is Textual.

Like `app.py` it talks only to `backend.ChatSession`, `workspace` and the
catalog helpers, so the agent loop, persistence and tool orchestration are
shared with the other two frontends and nothing in the core changes.

Run it:

    python tui.py            # opens the current directory as the workspace
    python tui.py ~/code/foo # opens that directory instead

Two things the core does that a full-screen app cannot tolerate, both handled
here rather than by editing the core:

* `main.send_chat_request`, `discover_models` and `execute_tool` print debug
  payloads to stdout. Every call into them goes through `capture()`, which
  redirects stdout into the Logs pane (ctrl+l) instead of over the screen.
* Model calls, tool execution and `!shell` are blocking. They run on worker
  threads so keys, scrolling and cancelling stay live.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import sys
import threading

from rich.markup import escape
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Collapsible,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    OptionList,
    RichLog,
    Rule,
    Static,
    Switch,
    TextArea,
)
from textual.widgets.option_list import Option

import backend
import workspace
from backend import ChatSession

TOOL_OUTPUT_LIMIT = 4000
SUMMARY_LIMIT = 60


# --- CORE CALLS -------------------------------------------------------------


_stdout_lock = threading.Lock()


def capture(call):
    """Run a core call with its debug prints diverted. Returns (result, output)."""
    buffer = io.StringIO()
    with _stdout_lock:
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            try:
                result = call()
            except BaseException as error:  # noqa: BLE001 - a crash here must not kill the app
                return error, buffer.getvalue()
    return result, buffer.getvalue()


# --- FORMATTING -------------------------------------------------------------


def shorten(text: str, limit: int = SUMMARY_LIMIT) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def call_signature(call) -> str:
    """`apply_patch(file_path="tui.py", …)` for the approval gate and transcript."""
    args = ", ".join(f"{k}={shorten(v, 28)!r}" for k, v in (call.args or {}).items())
    return f"{call.name}({shorten(args, 90)})"


def describe_message(message: dict) -> tuple[str, str, str]:
    """Returns (css class, gutter label, body) for one stored message."""
    role = message.get("role")
    content = message.get("content") or ""

    if role == "user":
        if content.startswith("[shell]"):
            return "msg-shell", "shell", content[len("[shell]") :].strip()
        if content.startswith("[system]"):
            return "msg-system", "note", content[len("[system]") :].strip()
        return "msg-user", "you", content
    if role == "assistant":
        if message.get("tool_calls"):
            names = ", ".join(
                tc.get("function", {}).get("name", "?") for tc in message["tool_calls"]
            )
            return "msg-tools", "tools", f"Requested {names}"
        return "msg-assistant", "agent", content
    if role == "tool":
        return "msg-tool", message.get("name", "tool"), content
    return "msg-system", role or "?", content


# --- SMALL MODALS -----------------------------------------------------------


class PromptScreen(ModalScreen[str | None]):
    """One-line answer. Escape returns None, enter returns the text."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, question: str, value: str = "", placeholder: str = "") -> None:
        super().__init__()
        self.question, self.value, self.placeholder = question, value, placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.question, classes="dialog-title")
            yield Input(value=self.value, placeholder=self.placeholder, id="answer")

    def on_mount(self) -> None:
        self.query_one("#answer", Input).focus()

    @on(Input.Submitted)
    def submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("escape,n", "dismiss(False)", "No"),
        Binding("y", "dismiss(True)", "Yes"),
    ]

    def __init__(self, question: str, confirm_label: str = "Delete") -> None:
        super().__init__()
        self.question, self.confirm_label = question, confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.question, classes="dialog-title")
            with Horizontal(classes="dialog-buttons"):
                yield Button(self.confirm_label, variant="error", id="yes")
                yield Button("Keep", id="no")

    @on(Button.Pressed, "#yes")
    def confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def cancel(self) -> None:
        self.dismiss(False)


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,f1,q", "dismiss(None)", "Close")]

    HELP = """\
## Keys

| Key | Action |
|---|---|
| `enter` | Send the message |
| `escape` | Stop the agent mid-loop |
| `f1` | This help |
| `f2` | Pick a model |
| `f3` | Manage conversations |
| `f4` / `f5` | Load a skill / a prompt |
| `f6` | Open a file |
| `f7` | Quick notes |
| `f8` | Switch workspace |
| `ctrl+b` | Sidebar |
| `ctrl+l` | Logs (raw request/response payloads) |
| `ctrl+t` | Tool calling on/off |
| `ctrl+g` | Auto-approve on/off |
| `ctrl+n` | New conversation |
| `ctrl+z` | Undo the last turn |
| `ctrl+q` | Quit |

## Typing

| Input | Effect |
|---|---|
| `!ls -la` | Run a command, add its output to the conversation |
| `!!git status` | Run a command, keep the output out of the conversation |
| `/skills`, `/skill <name> [task]` | List or load a skill |
| `/prompts`, `/prompt <name> [task]` | List or load a prompt |

Tool calls wait for approval. Approve them, deny them, or reject them with
written feedback the model has to answer for.
"""

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide"):
            yield Label("Agent TUI", classes="dialog-title")
            with VerticalScroll():
                yield Markdown(self.HELP)


# --- APPROVAL GATE ----------------------------------------------------------


class ToolApprovalScreen(ModalScreen[tuple[str, str]]):
    """The say-what-runs gate. Returns (approve|deny|feedback, feedback text)."""

    BINDINGS = [
        Binding("a", "approve", "Approve"),
        Binding("d,escape", "deny", "Deny"),
        Binding("f", "feedback", "Feedback"),
    ]

    def __init__(self, calls) -> None:
        super().__init__()
        self.calls = list(calls)

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide"):
            plural = "call" if len(self.calls) == 1 else "calls"
            yield Label(f"The agent wants to run {len(self.calls)} tool {plural}", classes="dialog-title")
            with VerticalScroll(id="calls"):
                for call in self.calls:
                    yield Static(escape(call.name), classes="tool-name")
                    for key, value in call.display_args.items():
                        yield Static(
                            f"[b]{escape(str(key))}[/b]\n{escape(str(value))}",
                            classes="tool-arg",
                        )
            with Horizontal(classes="dialog-buttons"):
                yield Button("Approve  a", variant="success", id="approve")
                yield Button("Deny  d", variant="error", id="deny")
                yield Button("Send feedback  f", id="feedback")
            yield Input(placeholder="What should it do instead?", id="feedback-input", classes="hidden")

    def action_approve(self) -> None:
        self.dismiss(("approve", ""))

    def action_deny(self) -> None:
        self.dismiss(("deny", ""))

    def action_feedback(self) -> None:
        field = self.query_one("#feedback-input", Input)
        field.remove_class("hidden")
        field.focus()

    @on(Button.Pressed, "#approve")
    def press_approve(self) -> None:
        self.action_approve()

    @on(Button.Pressed, "#deny")
    def press_deny(self) -> None:
        self.action_deny()

    @on(Button.Pressed, "#feedback")
    def press_feedback(self) -> None:
        self.action_feedback()

    @on(Input.Submitted, "#feedback-input")
    def send_feedback(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self.dismiss(("feedback", text) if text else ("deny", ""))


# --- MODEL PICKER -----------------------------------------------------------


class ModelScreen(ModalScreen[tuple[str, str] | None]):
    """Search the discovered catalog across providers."""

    BINDINGS = [
        Binding("escape", "dismiss(None)", "Cancel"),
        Binding("ctrl+r", "rediscover", "Re-discover"),
        Binding("down", "focus_list", "List", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide"):
            yield Label("Pick a model", classes="dialog-title")
            yield Static("", id="providers", classes="muted")
            yield Input(placeholder="Search provider or model…", id="query")
            yield OptionList(id="results")
            yield Static("ctrl+r re-discovers from every provider with a key set", classes="muted")

    def on_mount(self) -> None:
        self.query_one("#query", Input).focus()
        self.query_one("#results", OptionList).add_option(Option("Loading catalog…", id=None))
        self.load_catalog()

    def action_focus_list(self) -> None:
        self.query_one("#results", OptionList).focus()

    @work(thread=True, group="catalog")
    def load_catalog(self, refresh: bool = False) -> None:
        call = backend.refresh_catalog if refresh else backend.load_catalog
        _result, output = capture(call)
        pairs, _ = capture(lambda: backend.search_catalog(""))
        status, _ = capture(backend.provider_status)
        self.app.call_from_thread(self.app.log_output, output)
        self.app.call_from_thread(self.show_catalog, pairs, status)

    def show_catalog(self, pairs, status) -> None:
        self.pairs = pairs if isinstance(pairs, list) else []
        line = "   ".join(
            f"[{'green' if p['ready'] else 'red'}]●[/] {escape(p['provider'])} {p['count']}"
            for p in (status if isinstance(status, list) else [])
        )
        self.query_one("#providers", Static).update(line or "No providers configured.")
        self.render_results(self.query_one("#query", Input).value)

    def render_results(self, query: str) -> None:
        results = self.query_one("#results", OptionList)
        results.clear_options()
        pairs = getattr(self, "pairs", [])
        if query.strip():
            matches, _ = capture(lambda: backend.search_catalog(query))
            pairs = matches if isinstance(matches, list) else []
        if not pairs:
            results.add_option(Option("Nothing matched. Add a key to .env, then ctrl+r.", id=None))
            return
        for provider, model in pairs[:200]:
            mark = "" if backend.provider_ready(provider) else " [red](no key)[/red]"
            results.add_option(Option(f"[dim]{escape(provider)}[/dim]  {escape(model)}{mark}", id=f"{provider}\x00{model}"))

    @on(Input.Changed, "#query")
    def search(self, event: Input.Changed) -> None:
        self.render_results(event.value)

    @on(Input.Submitted, "#query")
    def jump_to_results(self) -> None:
        self.query_one("#results", OptionList).focus()

    @on(OptionList.OptionSelected, "#results")
    def choose(self, event: OptionList.OptionSelected) -> None:
        if not event.option.id:
            return
        provider, model = event.option.id.split("\x00", 1)
        self.dismiss((provider, model))

    def action_rediscover(self) -> None:
        self.query_one("#providers", Static).update("Re-discovering…")
        self.load_catalog(refresh=True)


# --- CONVERSATIONS ----------------------------------------------------------


class ThreadScreen(ModalScreen[bool]):
    """Switch, name, rename and delete the conversations in this workspace."""

    BINDINGS = [
        Binding("escape", "dismiss(False)", "Close"),
        Binding("ctrl+n", "new", "New"),
        Binding("ctrl+r", "rename", "Rename"),
        Binding("delete,ctrl+d", "delete", "Delete"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide"):
            yield Label("Conversations", classes="dialog-title")
            yield OptionList(id="threads")
            yield Static("enter opens · ctrl+n new · ctrl+r rename · delete removes", classes="muted")

    def on_mount(self) -> None:
        self.reload()

    def reload(self) -> None:
        session: ChatSession = self.app.session
        threads = self.query_one("#threads", OptionList)
        threads.clear_options()
        for thread_id in session.list_threads():
            summary = session.thread_summary(thread_id)
            here = " [reverse] open [/reverse]" if thread_id == session.thread_id else ""
            preview = shorten(summary["last_human"] or "empty", 52)
            threads.add_option(
                Option(
                    f"[b]{escape(thread_id)}[/b]{here}  [dim]{summary['count']} msgs[/dim]\n"
                    f"  [dim]{escape(preview)}[/dim]",
                    id=thread_id,
                )
            )
        threads.focus()

    def selected(self) -> str | None:
        threads = self.query_one("#threads", OptionList)
        index = threads.highlighted
        return threads.get_option_at_index(index).id if index is not None else None

    @on(OptionList.OptionSelected, "#threads")
    def open_thread(self, event: OptionList.OptionSelected) -> None:
        self.app.session.switch_thread(event.option.id)
        self.dismiss(True)

    def action_new(self) -> None:
        def create(name: str | None) -> None:
            if name is None:
                return
            error = self.app.session.new_thread(name or None)
            if error:
                self.notify(error, severity="error")
                return
            self.dismiss(True)

        self.app.push_screen(PromptScreen("Name the conversation (blank for a random id)"), create)

    def action_rename(self) -> None:
        old = self.selected()
        if not old:
            return

        def rename(new: str | None) -> None:
            if not new:
                return
            error = self.app.session.rename_thread(old, new)
            if error:
                self.notify(error, severity="error")
            else:
                self.reload()

        self.app.push_screen(PromptScreen(f"Rename '{old}' to", value=old), rename)

    def action_delete(self) -> None:
        target = self.selected()
        if not target:
            return

        def remove(confirmed: bool) -> None:
            if not confirmed:
                return
            self.app.session.delete_thread(target)
            self.app.refresh_transcript(full=True)
            self.reload()

        self.app.push_screen(ConfirmScreen(f"Delete conversation '{target}'?"), remove)


# --- SKILLS AND PROMPTS -----------------------------------------------------


class CatalogScreen(ModalScreen[str | None]):
    """Browse skills or prompts and load one with an optional task."""

    BINDINGS = [
        Binding("escape", "dismiss(None)", "Cancel"),
        Binding("ctrl+r", "reload_catalog", "Rescan"),
    ]

    def __init__(self, kind: str) -> None:
        super().__init__()
        self.kind = kind                       # "skill" or "prompt"

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide"):
            yield Label(f"{self.kind.capitalize()}s", classes="dialog-title")
            yield OptionList(id="entries")
            yield Input(placeholder="Task to hand it (optional)", id="task")
            yield Static("enter loads the highlighted entry · ctrl+r rescans the folder", classes="muted")

    def on_mount(self) -> None:
        self.reload()

    def reload(self) -> None:
        session: ChatSession = self.app.session
        entries = (session.skill_catalog if self.kind == "skill" else session.prompt_catalog)()
        options = self.query_one("#entries", OptionList)
        options.clear_options()
        if not entries:
            folder = f"{self.kind}s/<name>/{self.kind.upper()}.md"
            options.add_option(Option(f"Nothing here yet. Add one at {folder}", id=None))
        for entry in entries:
            description = entry.get("description") or "(no description)"
            options.add_option(
                Option(f"[b]{escape(entry['name'])}[/b]\n  [dim]{escape(shorten(description, 64))}[/dim]",
                       id=entry["name"])
            )
        options.focus()

    def action_reload_catalog(self) -> None:
        session: ChatSession = self.app.session
        (session.reload_skills if self.kind == "skill" else session.reload_prompts)()
        self.reload()

    def load(self, name: str) -> None:
        task = self.query_one("#task", Input).value.strip()
        self.dismiss(f"/{self.kind} {name} {task}".strip())

    @on(OptionList.OptionSelected, "#entries")
    def choose(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            self.load(event.option.id)

    @on(Input.Submitted, "#task")
    def submit_task(self) -> None:
        options = self.query_one("#entries", OptionList)
        index = options.highlighted
        if index is None:
            return
        option_id = options.get_option_at_index(index).id
        if option_id:
            self.load(option_id)


# --- WORKSPACE --------------------------------------------------------------


class WorkspaceScreen(ModalScreen[str | None]):
    """Open another project. Conversations follow the project, not the app."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide"):
            yield Label("Open a project", classes="dialog-title")
            yield Static(escape(workspace.current()), classes="muted")
            yield Input(placeholder="Path to a directory…", id="path")
            yield OptionList(id="recents")

    def on_mount(self) -> None:
        recents = self.query_one("#recents", OptionList)
        entries = workspace.load_recent_projects()
        if not entries:
            recents.add_option(Option("No recent projects yet.", id=None))
        for path in entries:
            recents.add_option(
                Option(f"[b]{escape(os.path.basename(path) or path)}[/b]\n  [dim]{escape(path)}[/dim]", id=path)
            )
        self.query_one("#path", Input).focus()

    @on(Input.Submitted, "#path")
    def open_typed(self, event: Input.Submitted) -> None:
        if event.value.strip():
            self.dismiss(event.value.strip())

    @on(OptionList.OptionSelected, "#recents")
    def open_recent(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            self.dismiss(event.option.id)


# --- NOTES AND FILES --------------------------------------------------------


class NotesScreen(ModalScreen[None]):
    """The scratchpad stored in this project's .chatbot/notes.md."""

    BINDINGS = [
        Binding("escape", "dismiss(None)", "Close"),
        Binding("ctrl+s", "save", "Save"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide tall"):
            yield Label("Quick notes", classes="dialog-title")
            yield TextArea(workspace.read_notes(), id="notes", language="markdown")
            yield Static("ctrl+s saves · escape closes", classes="muted")

    def on_mount(self) -> None:
        self.query_one("#notes", TextArea).focus()

    def action_save(self) -> None:
        message = workspace.write_notes(self.query_one("#notes", TextArea).text)
        self.notify(message, severity="information" if message.startswith("Notes") else "error")


class FileScreen(ModalScreen[None]):
    """Read and edit files inside the active workspace."""

    BINDINGS = [
        Binding("escape", "dismiss(None)", "Close"),
        Binding("ctrl+s", "save", "Save"),
        Binding("ctrl+f", "focus_filter", "Filter"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.open_path: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide tall"):
            yield Label("Files", id="file-title", classes="dialog-title")
            with Horizontal(id="file-body"):
                with Vertical(id="file-side"):
                    yield Input(placeholder="Filter…", id="filter")
                    yield OptionList(id="files")
                yield TextArea("", id="editor", read_only=True)
            yield Static("enter opens · ctrl+s saves · ctrl+f filters", classes="muted")

    def on_mount(self) -> None:
        self.all_files = workspace.list_project_files()
        self.render_files("")
        self.query_one("#filter", Input).focus()

    def render_files(self, needle: str) -> None:
        files = self.query_one("#files", OptionList)
        files.clear_options()
        needle = needle.lower().strip()
        matches = [f for f in self.all_files if needle in f.lower()] if needle else self.all_files
        if not matches:
            files.add_option(Option("No matching files.", id=None))
        for path in matches[:400]:
            files.add_option(Option(escape(path), id=path))

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    @on(Input.Changed, "#filter")
    def filter_files(self, event: Input.Changed) -> None:
        self.render_files(event.value)

    @on(Input.Submitted, "#filter")
    def jump_to_files(self) -> None:
        self.query_one("#files", OptionList).focus()

    @on(OptionList.OptionSelected, "#files")
    def open_file(self, event: OptionList.OptionSelected) -> None:
        path = event.option.id
        if not path:
            return
        editor = self.query_one("#editor", TextArea)
        editor.read_only = False
        editor.text = workspace.read_file(path)
        with contextlib.suppress(Exception):
            editor.language = workspace.language_for(path)
        self.open_path = path
        self.query_one("#file-title", Label).update(f"Files — {path}")
        editor.focus()

    def action_save(self) -> None:
        if not self.open_path:
            self.notify("Open a file first.", severity="warning")
            return
        message = workspace.write_file(self.open_path, self.query_one("#editor", TextArea).text)
        self.notify(message, severity="error" if message.startswith("Error") else "information")


# --- SIDEBAR ----------------------------------------------------------------


class Sidebar(Vertical):
    """Where you are, what you're talking to, and what it's allowed to do."""

    def compose(self) -> ComposeResult:
        yield Static("", id="workspace-line")
        yield Static("", id="model-line")
        yield Static("", id="thread-line")
        yield Rule()
        with Horizontal(classes="toggle"):
            yield Switch(value=True, id="tools-switch")
            yield Label("Tool calling")
        with Horizontal(classes="toggle"):
            yield Switch(value=False, id="approve-switch")
            yield Label("Auto-approve")
        yield Rule()
        yield Static("Conversations", classes="section")
        yield OptionList(id="thread-list")

    def refresh_info(self) -> None:
        session: ChatSession = self.app.session
        root = os.path.basename(session.root) or session.root
        if not session.model:
            model = "none yet — press f2"
        elif session.is_ready():
            model = f"{session.model}\n[dim]{escape(session.provider)}[/dim]"
        else:
            model = f"{session.model}\n[red]no key for {escape(session.provider)}[/red]"

        self.query_one("#workspace-line", Static).update(
            f"[b]{escape(root)}[/b]\n[dim]{escape(shorten(session.root, 32))}[/dim]"
        )
        self.query_one("#model-line", Static).update(f"[dim]model[/dim]\n{model}")
        self.query_one("#thread-line", Static).update(
            f"[dim]thread[/dim]\n{escape(session.thread_id)}\n"
            f"[dim]{len(session.messages)} msgs · ~{session.token_count} tok[/dim]"
        )

        threads = self.query_one("#thread-list", OptionList)
        threads.clear_options()
        for thread_id in session.list_threads()[:12]:
            marker = "[reverse] [/reverse] " if thread_id == session.thread_id else "  "
            threads.add_option(Option(f"{marker}{escape(thread_id)}", id=thread_id))


# --- APP --------------------------------------------------------------------


class AgentTUI(App):
    """Terminal frontend for the multi-provider agent."""

    TITLE = "Agent"
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen { layout: horizontal; }

    #main { width: 1fr; }

    Sidebar {
        width: 34;
        padding: 1 2;
        background: $panel;
        border-right: vkey $accent 40%;
    }
    Sidebar.collapsed { display: none; }
    Sidebar .section { color: $text-muted; text-style: bold; }
    Sidebar .toggle { height: 3; align-vertical: middle; }
    Sidebar .toggle Label { padding: 1 0 0 1; }
    Sidebar #thread-list { height: 1fr; border: none; background: transparent; }
    Sidebar Rule { margin: 0; }

    #transcript { height: 1fr; padding: 0 2; }
    #transcript > .message { padding: 0 0 0 1; margin: 1 0 0 0; }

    .gutter { color: $text-muted; text-style: bold; }
    .msg-user .gutter, .msg-user Markdown { border-left: outer $primary; padding-left: 1; }
    .msg-assistant .gutter, .msg-assistant Markdown { border-left: outer $success; padding-left: 1; }
    .msg-tools .gutter, .msg-tools Static { border-left: outer $warning; padding-left: 1; }
    .msg-shell .gutter, .msg-shell Static { border-left: outer $secondary; padding-left: 1; }
    .msg-system .gutter, .msg-system Static { border-left: outer $accent; padding-left: 1; }
    .msg-tool .gutter, .msg-tool Collapsible { border-left: outer $warning 50%; padding-left: 1; }

    #transcript Markdown { margin: 0; background: transparent; }
    #transcript MarkdownH1, #transcript MarkdownH2, #transcript MarkdownH3 {
        margin: 0; padding: 0; text-align: left; content-align: left middle;
        background: transparent;
        color: $text; text-style: bold; border: none;
    }
    #transcript MarkdownParagraph, #transcript MarkdownBlockQuote { margin: 0; }
    #transcript MarkdownFence { margin: 0 0 0 1; max-height: 24; }
    #transcript Collapsible { border: none; background: transparent; margin: 0; }
    #transcript .tool-body { color: $text-muted; }
    #transcript .empty { color: $text-muted; padding: 2 0; }

    #logs { height: 12; border-top: vkey $accent 40%; display: none; }
    #logs.visible { display: block; }

    #status { height: 1; padding: 0 2; color: $text-muted; background: $panel; }
    #status.thinking { color: $warning; }
    #status.failed { color: $error; }
    #status.settled { color: $text-muted; }

    #composer { height: 3; padding: 0 1; }

    ModalScreen { align: center middle; background: $background 55%; }
    #dialog {
        width: 64; height: auto; max-height: 80%;
        padding: 1 2; background: $surface;
        border: round $accent;
    }
    #dialog.wide { width: 96; }
    #dialog.tall { height: 90%; }
    .dialog-title { text-style: bold; padding-bottom: 1; }
    .dialog-buttons { width: 100%; height: auto; padding-top: 1; align-horizontal: right; }
    .dialog-buttons Button { margin-left: 2; min-width: 12; }
    .muted { color: $text-muted; padding-top: 1; }
    .hidden { display: none; }

    #calls { height: auto; max-height: 20; }
    .tool-name { text-style: bold; color: $warning; padding-top: 1; }
    .tool-arg { padding: 0 0 0 2; color: $text-muted; }

    #file-body { height: 1fr; }
    #file-side { width: 40; }
    #file-side OptionList { height: 1fr; }
    #editor { width: 1fr; }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("f1", "help", "Help"),
        Binding("f2", "models", "Model"),
        Binding("f3", "threads", "Chats"),
        Binding("f4", "skills", "Skills"),
        Binding("f5", "prompts", "Prompts"),
        Binding("f6", "files", "Files"),
        Binding("f7", "notes", "Notes"),
        Binding("f8", "open_workspace", "Project"),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar"),
        Binding("ctrl+l", "toggle_logs", "Logs"),
        Binding("ctrl+n", "new_thread", "New chat", show=False),
        Binding("ctrl+t", "toggle_tools", "Tools", show=False),
        Binding("ctrl+g", "toggle_approve", "Auto-approve", show=False),
        Binding("ctrl+z", "undo", "Undo turn", show=False),
        Binding("escape", "stop", "Stop", show=False),
    ]

    def __init__(self, root: str | None = None) -> None:
        super().__init__()
        if root:
            error = workspace.switch_to(root)
            if error:
                sys.exit(error)
        workspace.save_recent_project(workspace.current())
        self.session = ChatSession()
        self.auto_approve = False
        self._epoch = 0
        self._rendered = 0
        self._placeholder = False

    # --- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Sidebar()
        with Vertical(id="main"):
            yield Header(show_clock=True)
            yield VerticalScroll(id="transcript")
            yield RichLog(id="logs", wrap=False, markup=False, max_lines=2000)
            yield Static("", id="status")
            with Horizontal(id="composer"):
                yield Input(placeholder="Message the agent…  /skill  /prompt  !shell", id="composer-input")
            yield Footer()

    def on_mount(self) -> None:
        self.query_one("#tools-switch", Switch).value = self.session.tools_enabled
        self.refresh_transcript(full=True)
        self.query_one("#composer-input", Input).focus()
        if not self.session.is_ready():
            self.set_status("Pick a model with f2 to start.", "failed")
        if self.session.busy or self.session.pending:
            # The thread was saved mid-call. Pick up where it left off.
            self.run_agent()

    # --- transcript ---------------------------------------------------------

    def refresh_transcript(self, full: bool = False) -> None:
        """Mount whatever is new. `full` rebuilds after a thread or project change."""
        transcript = self.query_one("#transcript", VerticalScroll)
        if full:
            transcript.remove_children()
            self._rendered = 0
            self._placeholder = False

        messages = self.session.messages
        if self._rendered > len(messages):        # history was edited underneath us
            transcript.remove_children()
            self._rendered = 0

        if not messages:
            if not self._placeholder:
                transcript.remove_children()
                transcript.mount(
                    Static("Nothing here yet. Say something, or press f1 for the keys.", classes="empty")
                )
                self._placeholder = True
        else:
            if self._placeholder:
                transcript.remove_children()
                self._placeholder = False
                self._rendered = 0
            for message in messages[self._rendered :]:
                transcript.mount(self.build_message(message))
        self._rendered = len(messages)

        self.sidebar.refresh_info()
        transcript.scroll_end(animate=False)

    def build_message(self, message: dict) -> Vertical:
        """One stored message becomes a labelled block in the transcript."""
        style, label, body = describe_message(message)

        if message.get("role") == "tool":
            shown = body if len(body) <= TOOL_OUTPUT_LIMIT else (
                body[:TOOL_OUTPUT_LIMIT] + f"\n… {len(body) - TOOL_OUTPUT_LIMIT} more characters"
            )
            content = Collapsible(
                Static(escape(shown), classes="tool-body"),
                title=f"{label} · {shorten(body, 56)}",
                collapsed=True,
            )
            label = "output"
        elif style in ("msg-user", "msg-assistant"):
            content = Markdown(body or "*(empty reply)*")
        else:
            content = Static(escape(body))

        block = Vertical(Static(label, classes="gutter"), content, classes=f"message {style}")
        block.styles.height = "auto"
        return block

    # --- shortcuts ----------------------------------------------------------

    @property
    def sidebar(self) -> Sidebar:
        return self.query_one(Sidebar)

    def set_status(self, text: str, style: str = "") -> None:
        status = self.query_one("#status", Static)
        status.remove_class("thinking", "failed", "settled")
        if style:
            status.add_class(style)
        status.update(text)

    def log_output(self, text: str) -> None:
        if text and text.strip():
            self.query_one("#logs", RichLog).write(text.rstrip())

    def note(self, text: str) -> None:
        """Show a local answer (catalog listing, quiet shell output) without storing it."""
        self.log_output(text)
        self.query_one("#transcript", VerticalScroll).mount(
            Vertical(Static("local", classes="gutter"), Static(escape(text)), classes="message msg-system")
        )
        self.query_one("#transcript", VerticalScroll).scroll_end(animate=False)

    # --- input --------------------------------------------------------------

    @on(Input.Submitted, "#composer-input")
    def send(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        if self.session.busy or self.session.pending:
            self.notify("The agent is still working. Press escape to stop it.", severity="warning")
            return
        if not text.startswith(("!", "/")) and not self.session.is_ready():
            self.notify("No model selected. Press f2.", severity="error")
            return
        event.input.value = ""
        self.set_status("Working…", "thinking")
        self.submit_text(text)

    @work(thread=True, group="submit")
    def submit_text(self, text: str) -> None:
        """`submit()` can run a shell command, so it does not belong on the event loop."""
        note, output = capture(lambda: self.session.submit(text))
        self.call_from_thread(self.after_submit, note, output)

    def after_submit(self, note, output: str) -> None:
        self.log_output(output)
        if isinstance(note, BaseException):
            self.set_status(f"That failed: {note}", "failed")
            return
        self.refresh_transcript()
        if note:
            self.note(note)
        if self.session.busy:
            self.run_agent()
        else:
            self.set_status("Ready.")

    # --- agent loop ---------------------------------------------------------

    def invalidate(self) -> None:
        """Anything in flight belongs to a conversation the user has left."""
        self._epoch += 1

    def guarded(self, epoch: int, call):
        """Run a blocking core call, unwinding it if the user stopped us meanwhile.

        `step()` and `approve_tools()` mutate the session and save to disk when
        they finish. Cancelling the worker does not reach into the thread they
        run on, so without this an abandoned reply turns up in the conversation
        seconds after 'Stopped.' and is silently persisted.
        """
        session = self.session
        thread_id = session.thread_id
        before = list(session.messages)

        result = call()

        if epoch == self._epoch:
            return result
        if session is self.session and session.thread_id == thread_id:
            session.messages = before
            session.pending = []
            session.busy = False
            session._save()
        return None

    @work(exclusive=True, group="agent")
    async def run_agent(self) -> None:
        """Step the model until it settles, stopping at the approval gate."""
        epoch = self._epoch
        try:
            while self.session.busy:
                self.set_status(f"{self.session.model} is thinking…", "thinking")
                result, output = await asyncio.to_thread(
                    capture, lambda: self.guarded(epoch, self.session.step)
                )
                self.log_output(output)

                if isinstance(result, BaseException):
                    self.session.busy = False
                    self.set_status(f"That call failed: {result}", "failed")
                    return
                if result is None:
                    break

                self.refresh_transcript()

                if result["type"] == "error":
                    self.set_status(shorten(result["error"], 120), "failed")
                    self.log_output(result["error"])
                    return

                if not self.session.pending:
                    break

                if self.auto_approve:
                    decision = ("approve", "")
                else:
                    self.set_status("Waiting on you to approve the tool calls.", "thinking")
                    decision = await self.push_screen_wait(ToolApprovalScreen(self.session.pending))

                action, feedback = decision
                if action == "approve":
                    self.set_status("Running tools…", "thinking")
                    _results, output = await asyncio.to_thread(
                        capture, lambda: self.guarded(epoch, self.session.approve_tools)
                    )
                    self.log_output(output)
                elif action == "feedback":
                    self.session.send_tool_feedback(feedback)
                else:
                    self.session.deny_tools()
                self.refresh_transcript()
        except asyncio.CancelledError:
            self.session.busy = False
            self.session._save()
            self.set_status("Stopped.", "settled")
            raise
        finally:
            self.refresh_transcript()
            # Whatever ended the loop has already explained itself. Only a clean
            # finish gets to say "Ready.", or a 401 would flash past unread.
            if not self.session.busy and not any(
                self.query_one("#status", Static).has_class(state) for state in ("failed", "settled")
            ):
                self.set_status("Ready.")

    def action_stop(self) -> None:
        """Abandon the loop. The call in flight finishes, its answer is dropped."""
        if not self.session.busy and not self.session.pending:
            return
        self.invalidate()
        self.workers.cancel_group(self, "agent")
        self.session.busy = False
        self.session._save()          # so a reload does not think a call is still owed
        self.set_status("Stopped.", "settled")

    # --- actions ------------------------------------------------------------

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_toggle_sidebar(self) -> None:
        self.sidebar.toggle_class("collapsed")

    def action_toggle_logs(self) -> None:
        self.query_one("#logs", RichLog).toggle_class("visible")

    def action_toggle_tools(self) -> None:
        self.query_one("#tools-switch", Switch).toggle()

    def action_toggle_approve(self) -> None:
        self.query_one("#approve-switch", Switch).toggle()

    @on(Switch.Changed, "#tools-switch")
    def set_tools(self, event: Switch.Changed) -> None:
        self.session.tools_enabled = event.value
        self.set_status(f"Tool calling {'on' if event.value else 'off'}.")

    @on(Switch.Changed, "#approve-switch")
    def set_auto_approve(self, event: Switch.Changed) -> None:
        self.auto_approve = event.value
        self.set_status(
            "Auto-approve on — tools run without asking." if event.value
            else "Auto-approve off — every tool call waits for you."
        )

    def action_models(self) -> None:
        def apply(choice: tuple[str, str] | None) -> None:
            if not choice:
                return
            self.session.set_model(*choice)
            self.sidebar.refresh_info()
            self.set_status(f"Talking to {choice[1]} on {choice[0]}.")

        self.push_screen(ModelScreen(), apply)

    def action_threads(self) -> None:
        def reopen(changed: bool | None) -> None:
            if changed:
                self.invalidate()
                self.refresh_transcript(full=True)

        self.push_screen(ThreadScreen(), reopen)

    def action_new_thread(self) -> None:
        self.invalidate()
        error = self.session.new_thread()
        if error:
            self.notify(error, severity="error")
            return
        self.refresh_transcript(full=True)
        self.set_status(f"New conversation: {self.session.thread_id}")

    def action_skills(self) -> None:
        self.push_screen(CatalogScreen("skill"), self.run_command)

    def action_prompts(self) -> None:
        self.push_screen(CatalogScreen("prompt"), self.run_command)

    def run_command(self, command: str | None) -> None:
        if command:
            self.set_status("Working…", "thinking")
            self.submit_text(command)

    def action_files(self) -> None:
        self.push_screen(FileScreen())

    def action_notes(self) -> None:
        self.push_screen(NotesScreen())

    def action_open_workspace(self) -> None:
        def open_project(path: str | None) -> None:
            if not path:
                return
            error = workspace.switch_to(path)
            if error:
                self.notify(error, severity="error")
                return
            self.invalidate()
            self.session = ChatSession()
            self.refresh_transcript(full=True)
            self.set_status(f"Opened {workspace.current()}")

        self.push_screen(WorkspaceScreen(), open_project)

    def action_undo(self) -> None:
        self.invalidate()
        if self.session.undo_last_turn():
            self.refresh_transcript(full=True)
            self.set_status("Dropped the last turn.")
        else:
            self.set_status("Nothing to undo.")

    @on(OptionList.OptionSelected, "#thread-list")
    def switch_from_sidebar(self, event: OptionList.OptionSelected) -> None:
        if event.option.id and event.option.id != self.session.thread_id:
            self.invalidate()
            self.session.switch_thread(event.option.id)
            self.refresh_transcript(full=True)


def main() -> None:
    root = sys.argv[1] if len(sys.argv) > 1 else None
    AgentTUI(root).run()


if __name__ == "__main__":
    main()