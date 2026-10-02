"""Textual TUI: pick, create and kill tmux sessions across hosts."""

from __future__ import annotations

import os
import time

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Label, OptionList, Select, Static
from textual.worker import get_current_worker

from . import editor, tmux
from .config import Agent, Config
from .tmux import Host, HostError, MissingDirectory, Session

REFRESH_SECONDS = 2.0


def matches(session: Session, query: str) -> bool:
    state = "live" if session.live else "closed"
    hay = f"{session.key} {session.agent} {session.path} {session.directory} {state}".lower()
    return all(tok in hay for tok in query.lower().split())


def unique_name(base: str, taken: set[str]) -> str:
    base = tmux.sanitize(base) or "session"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name


class FilterInput(Input):
    """Filter box that also drives the session list with the arrow keys."""

    BINDINGS = [
        Binding("up", "app.cursor(-1)", show=False),
        Binding("down", "app.cursor(1)", show=False),
        Binding("pageup", "app.cursor(-10)", show=False),
        Binding("pagedown", "app.cursor(10)", show=False),
        Binding("enter", "app.attach", "attach"),
        Binding("ctrl+x", "app.kill", "kill/forget"),
        Binding("ctrl+e", "app.edit", "editor"),
        Binding("ctrl+o", "app.shell", "shell"),
        Binding("ctrl+r", "app.rename", "rename"),
        Binding("escape", "app.escape", "clear/quit"),
    ]


class EditorFilterInput(FilterInput):
    """Filter box for `mh code`: enter opens the editor instead of attaching."""

    BINDINGS = [Binding("enter", "app.attach", "open in editor")]


class ShellFilterInput(FilterInput):
    """Filter box for `mh sh`: enter opens a shell in the project directory."""

    BINDINGS = [Binding("enter", "app.attach", "open shell")]


FILTER_INPUTS = {"attach": FilterInput, "editor": EditorFilterInput, "shell": ShellFilterInput}


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "answer(True)", "yes"),
        Binding("n,escape", "answer(False)", "no"),
    ]

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.message)
            yield Label("[b]y[/b] confirm   [b]n[/b] cancel", classes="hint")

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)


class DirInput(Input):
    """Directory field: arrows move through the suggestions, tab steps into one."""

    BINDINGS = [
        Binding("down", "screen.suggest_move(1)", show=False),
        Binding("up", "screen.suggest_move(-1)", show=False),
        Binding("tab", "screen.suggest_accept", show=False),
        Binding("right", "screen.suggest_accept_at_end", show=False),
    ]


class NewSessionScreen(ModalScreen[dict | None]):
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(
        self,
        config: Config,
        hosts: list[Host],
        default_host: str,
        spec: dict | None = None,
        recent_dirs: dict[str, list[str]] | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.hosts = hosts
        self.default_host = spec["host"] if spec else default_host
        self.current_host = self.default_host
        self.spec = spec  # previous values when reopened to fix a typo
        self.recent_dirs = recent_dirs or {}
        self.suggestions: list[str] = []
        self.picked = False  # user moved into the suggestion list with the arrows
        self.set_value: str | None = None  # dir value we set ourselves (Changed for it isn't typing)

    def compose(self) -> ComposeResult:
        agents = list(self.config.agents)
        with Vertical(classes="dialog"):
            yield Label("[b]New session[/b]", classes="title")
            yield Label("agent")
            agent = self.spec["agent"] if self.spec else agents[0]
            yield Select([(a, a) for a in agents], value=agent, allow_blank=False, compact=True, id="agent")
            yield Label("host")
            yield Select([(h.name, h.name) for h in self.hosts], value=self.default_host, allow_blank=False, compact=True, id="host")
            yield Label("directory", id="dir-label")
            self.set_value = self.spec["dir"] if self.spec else self._default_dir(self.default_host)
            yield DirInput(self.set_value, compact=True, id="dir")
            suggest = OptionList(id="suggest", compact=True)
            suggest.can_focus = False
            yield suggest
            yield Label("name")
            yield Input(self.spec["name"] if self.spec else "", placeholder="auto: <agent>-<dir>", compact=True, id="name")
            yield Label(
                "[b]enter[/b] create & attach   [b]↑↓[/b] pick dir   [b]tab[/b] open dir   [b]esc[/b] cancel",
                classes="hint",
            )

    def on_mount(self) -> None:
        self.query_one("#dir", Input).focus()
        self._show_recent()

    @property
    def host(self) -> Host:
        name = str(self.query_one("#host", Select).value)
        return next(h for h in self.hosts if h.name == name)

    def _default_dir(self, host_name: str) -> str:
        host = next(h for h in self.hosts if h.name == host_name)
        return tmux.short_path(os.getcwd()) if host.is_local else "~"

    @on(Select.Changed, "#host")
    def host_changed(self, event: Select.Changed) -> None:
        # Select can report its initial value late (after the user started typing);
        # only an actual host switch should reset the directory.
        if str(event.value) == self.current_host:
            return
        self.current_host = str(event.value)
        self.set_value = self._default_dir(str(event.value))
        self.query_one("#dir", Input).value = self.set_value
        self._show_recent()

    # ----- directory suggestions -----

    def _show_recent(self) -> None:
        recent = self.recent_dirs.get(self.host.name, [])
        self._set_suggestions(recent, "recent directories on " + self.host.name if recent else "")
        if not recent:
            self._complete(self.query_one("#dir", Input).value)

    def _set_suggestions(self, paths: list[str], label: str = "") -> None:
        self.suggestions = paths
        self.picked = False
        ol = self.query_one("#suggest", OptionList)
        ol.clear_options()
        ol.add_options(paths)
        ol.display = bool(paths)
        ol.highlighted = None
        self.query_one("#dir-label", Label).update(f"directory  [dim]{label}[/dim]" if label else "directory")

    @on(Input.Changed, "#dir")
    def dir_changed(self, event: Input.Changed) -> None:
        if event.value == self.set_value:
            return  # our own default, keep showing recent dirs
        self.set_value = None
        self._complete(event.value)

    @work(thread=True, exclusive=True, group="dirs")
    def _complete(self, value: str) -> None:
        # "~/develop/he" -> list "~/develop", keep entries starting with "he"
        parent, _, prefix = value.rpartition("/")
        if not value.startswith(("/", "~")):
            parent, prefix = "~", value
        elif not parent and value.startswith("/"):
            parent = "/"
        try:
            names = tmux.list_dirs(self.host, parent or "/")
        except HostError:
            names = []
        show_hidden = prefix.startswith(".")
        base = parent.rstrip("/") if parent != "/" else ""
        matches = [
            f"{base}/{n}" for n in names
            if n.lower().startswith(prefix.lower()) and (show_hidden or not n.startswith("."))
        ]
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(self._set_suggestions, matches[:50])

    def action_suggest_move(self, delta: int) -> None:
        ol = self.query_one("#suggest", OptionList)
        if not self.suggestions:
            return
        if ol.highlighted is None:
            ol.highlighted = 0 if delta > 0 else len(self.suggestions) - 1
        else:
            ol.highlighted = max(0, min(len(self.suggestions) - 1, ol.highlighted + delta))
        ol.scroll_to_highlight()
        self.picked = True

    def _accept(self) -> bool:
        """Put the highlighted (or only) suggestion into the field and list its children."""
        ol = self.query_one("#suggest", OptionList)
        idx = ol.highlighted
        if idx is None and len(self.suggestions) == 1:
            idx = 0
        if idx is None:
            return False
        dir_input = self.query_one("#dir", Input)
        dir_input.value = self.suggestions[idx].rstrip("/") + "/"
        dir_input.cursor_position = len(dir_input.value)
        return True

    def action_suggest_accept(self) -> None:
        if not self._accept():
            self.focus_next()

    def action_suggest_accept_at_end(self) -> None:
        dir_input = self.query_one("#dir", Input)
        if dir_input.cursor_position < len(dir_input.value) or not self._accept():
            dir_input.action_cursor_right()

    @on(OptionList.OptionSelected, "#suggest")
    def suggestion_clicked(self, event: OptionList.OptionSelected) -> None:
        self.query_one("#suggest", OptionList).highlighted = event.option_index
        self._accept()
        self.query_one("#dir", Input).focus()

    # ----- submit -----

    @on(Input.Submitted)
    def submit(self, event: Input.Submitted) -> None:
        if event.input.id == "dir" and self.picked and self._accept():
            return  # enter on a picked suggestion takes it; enter again creates
        self.dismiss(
            {
                "agent": str(self.query_one("#agent", Select).value),
                "host": str(self.query_one("#host", Select).value),
                "dir": self.query_one("#dir", Input).value.strip().rstrip("/") or "~",
                "name": self.query_one("#name", Input).value.strip(),
            }
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class RenameScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"[b]Rename[/b] {self.session.key}", classes="title")
            yield Label("new name")
            yield Input(self.session.name, compact=True, id="name")
            yield Label("[b]enter[/b] rename   [b]esc[/b] cancel", classes="hint")

    @on(Input.Submitted)
    def submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class MuxherdApp(App[Session | None]):
    TITLE = "muxherd"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { layout: vertical; }
    #status { height: 1; padding: 0 1; background: $panel; }
    #filter { border: none; height: 1; padding: 0 1; margin: 0; }
    #table { height: 1fr; min-height: 4; }
    #preview { height: 45%; border-top: solid $panel-lighten-2; padding: 0 1; overflow: hidden; }
    .dialog {
        width: 64; height: auto; padding: 1 2; border: round $accent; background: $surface;
    }
    .dialog Label { margin-top: 1; color: $text-muted; }
    .dialog .title { margin-top: 0; color: $text; }
    .dialog .hint { color: $text-muted; }
    ConfirmScreen, NewSessionScreen, RenameScreen { align: center middle; }
    #suggest { height: auto; max-height: 8; margin-top: 0; background: $boost; }
    """
    BINDINGS = [
        Binding("ctrl+n", "new", "new"),
        Binding("ctrl+t", "toggle_closed", "closed"),
        Binding("f5", "refresh", "refresh", show=False),
        Binding("ctrl+p", "toggle_preview", "preview"),
        Binding("escape", "escape", "quit", show=False),
        Binding("ctrl+c", "quit", show=False, priority=True),
    ]

    def __init__(self, config: Config, initial_filter: str = "", mode: str = "attach") -> None:
        super().__init__()
        # What enter does: "attach", "editor" (`mh code`) or "shell" (`mh sh`).
        self.mode = mode
        # How the app ended, read by the CLI: "attach" or "shell" (with the session as return value).
        self.exit_action = "attach"
        self.config = config
        self.hosts = [Host(name, target) for name, target in config.hosts.items()]
        self.sessions: list[Session] = []
        self.shown: list[Session] = []
        self.errors: dict[str, str] = {}
        self.loaded = False
        self.show_closed = True
        self.focus_key: str | None = None  # select this row on the next render (e.g. after rename)
        self.initial_filter = initial_filter

    def compose(self) -> ComposeResult:
        yield Static("loading…", id="status")
        yield FILTER_INPUTS[self.mode](self.initial_filter, placeholder="type to filter sessions…", id="filter")
        table = DataTable(id="table", cursor_type="row", zebra_stripes=False)
        table.can_focus = False
        yield table
        yield Static("", id="preview")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("", "host", "session", "agent", "idle", "dir")
        self.query_one(FilterInput).focus()
        self.action_refresh()
        self.set_interval(REFRESH_SECONDS, self.action_refresh)

    # ----- data -----

    @work(thread=True, exclusive=True, group="refresh")
    def action_refresh(self) -> None:
        sessions, errors = tmux.list_all(self.hosts)
        if not get_current_worker().is_cancelled:
            self.call_from_thread(self._apply, sessions, errors)

    def _apply(self, sessions: list[Session], errors: dict[str, str]) -> None:
        self.sessions, self.errors, self.loaded = sessions, errors, True
        self._render_table()
        self._render_status()
        self._load_preview()

    @property
    def selected(self) -> Session | None:
        table = self.query_one(DataTable)
        if not self.shown or table.cursor_row < 0:
            return None
        return self.shown[min(table.cursor_row, len(self.shown) - 1)]

    def _render_table(self) -> None:
        table = self.query_one(DataTable)
        current = self.focus_key or (self.selected.key if self.selected else None)
        query = self.query_one(FilterInput).value
        self.shown = [s for s in self.sessions if (s.live or self.show_closed) and matches(s, query)]
        now = time.time()
        table.clear()
        for s in self.shown:
            if s.live:
                row = (
                    Text("●", style="green") if s.attached else Text("○", style="dim"),
                    Text(s.host.name, style="cyan"),
                    Text(s.name, style="bold"),
                    s.agent,
                    tmux.ago(s.activity, now),
                    Text(tmux.short_path(s.path), style="dim"),
                )
            else:
                row = (
                    Text("✕", style="red dim"),
                    Text(s.host.name, style="dim"),
                    Text(s.name, style="dim"),
                    Text(s.agent, style="dim"),
                    Text(tmux.ago(s.closed_at, now), style="dim"),
                    Text(tmux.short_path(s.directory), style="dim"),
                )
            table.add_row(*row, key=s.key)
        keys = [s.key for s in self.shown]
        if current in keys:
            self.focus_key = None
            table.move_cursor(row=keys.index(current), animate=False)
        elif self.shown:
            table.move_cursor(row=0, animate=False)

    def _render_status(self) -> None:
        live = sum(s.live for s in self.sessions)
        closed = len(self.sessions) - live
        counts = f"  {live} live · {closed} closed{'' if self.show_closed else ' (hidden)'}"
        if self.query_one(FilterInput).value:
            counts += f" · {len(self.shown)} shown"
        parts = [Text("muxherd", style="bold"), Text(counts + "  ")]
        for h in self.hosts:
            ok = h.name not in self.errors
            parts.append(Text(f" {h.name} {'✓' if ok else '✗'}", style="green" if ok else "red"))
        if self.errors:
            parts.append(Text("   " + "; ".join(self.errors.values()), style="red dim"))
        self.query_one("#status", Static).update(Text.assemble(*parts))

    def _load_preview(self) -> None:
        preview = self.query_one("#preview", Static)
        if not preview.display:
            return
        session = self.selected
        if session is None:
            preview.update(Text("no sessions — ctrl+n to start one" if self.loaded else "", style="dim"))
            return
        self._fetch_preview(session, max(preview.size.height, 5))

    @work(thread=True, exclusive=True, group="preview")
    def _fetch_preview(self, session: Session, height: int) -> None:
        if not session.live:
            text = self._closed_info(session)
            if not get_current_worker().is_cancelled:
                self.call_from_thread(self._show_preview, session.key, text)
            return
        try:
            out = tmux.capture(session)
            # Agent TUIs leave lots of trailing blank lines; show the last screenful of content.
            lines = out.rstrip().splitlines()[-height:]
            text = Text.from_ansi("\n".join(lines))
        except HostError as e:
            text = Text(str(e), style="red")
        if not get_current_worker().is_cancelled:
            self.call_from_thread(self._show_preview, session.key, text)

    def _closed_info(self, session: Session) -> Text:
        agent = self.config.agents.get(session.agent, Agent())
        try:
            # Runs the agent's `resumable` check on the host, so this shows what enter will do.
            command = tmux.resume_command(agent, session.agent_id, session.host)[0] or "(shell)"
        except HostError as e:
            command = f"? ({e})"
        return Text.assemble(
            (f"closed {tmux.ago(session.closed_at)} ago", "bold"),
            " — enter reopens it, ctrl+x forgets it\n\n",
            ("  dir    ", "dim"), tmux.short_path(session.directory), "\n",
            ("  agent  ", "dim"), session.agent or "shell", "\n",
            ("  runs   ", "dim"), command,
        )

    def _show_preview(self, key: str, text: Text) -> None:
        if self.selected and self.selected.key == key:
            self.query_one("#preview", Static).update(text)

    # ----- events & actions -----

    @on(Input.Changed, "#filter")
    def filter_changed(self) -> None:
        self._render_table()
        self._render_status()
        self._load_preview()

    @on(DataTable.RowHighlighted)
    def row_highlighted(self) -> None:
        self._load_preview()

    def action_cursor(self, delta: int) -> None:
        table = self.query_one(DataTable)
        if self.shown:
            row = max(0, min(len(self.shown) - 1, table.cursor_row + delta))
            table.move_cursor(row=row, animate=False)

    def action_attach(self) -> None:
        session = self.selected
        if session is None:
            return
        if self.mode == "editor":
            if self._open_editor(session):
                self.exit(None)
            return
        if self.mode == "shell":
            self.action_shell()
            return
        if session.live:
            self.exit(session)
        else:
            self._reopen(session)

    @work(thread=True)
    def _reopen(self, session: Session, create_dir: bool = False) -> None:
        try:
            reopened = tmux.reopen(session, self.config.agents, create_dir=create_dir)
        except MissingDirectory as e:
            def done(ok: bool | None) -> None:
                if ok:
                    self._reopen(session, create_dir=True)

            self.call_from_thread(
                self.push_screen, ConfirmScreen(f"[b]{e.path}[/b] no longer exists on [b]{e.host}[/b].\nCreate it?"), done
            )
            return
        except HostError as e:
            self.call_from_thread(self.notify, str(e), severity="error", timeout=8)
            return
        self.call_from_thread(self.exit, reopened)

    def action_shell(self) -> None:
        if self.selected:
            self.exit_action = "shell"
            self.exit(self.selected)

    def action_edit(self) -> None:
        if self.selected:
            self._open_editor(self.selected)

    def _open_editor(self, session: Session) -> bool:
        try:
            argv = editor.open_editor(session, self.config.editor)
        except HostError as e:
            self.notify(str(e), severity="error", timeout=8)
            return False
        self.notify(f"opening {tmux.short_path(argv[-1])} on {session.host.name}")
        return True

    def action_toggle_closed(self) -> None:
        self.show_closed = not self.show_closed
        self._render_table()
        self._render_status()
        self._load_preview()

    def action_escape(self) -> None:
        f = self.query_one(FilterInput)
        if f.value:
            f.value = ""
        else:
            self.exit(None)

    def action_toggle_preview(self) -> None:
        preview = self.query_one("#preview", Static)
        preview.display = not preview.display
        self._load_preview()

    def action_kill(self) -> None:
        session = self.selected
        if session is None:
            return

        def done(ok: bool | None) -> None:
            if ok:
                self._kill(session)

        if session.live:
            message = f"Kill session [b]{session.key}[/b]?\nIt stays in the list as closed, so you can reopen it."
        else:
            message = f"Forget closed session [b]{session.key}[/b]?"
        self.push_screen(ConfirmScreen(message), done)

    @work(thread=True)
    def _kill(self, session: Session) -> None:
        try:
            if session.live:
                tmux.kill_session(session)
            else:
                tmux.forget(session)
        except HostError as e:
            self.call_from_thread(self.notify, str(e), severity="error")
        self.call_from_thread(self.action_refresh)

    def action_new(self, spec: dict | None = None) -> None:
        default_host = self.selected.host.name if self.selected else self.hosts[0].name
        self.push_screen(NewSessionScreen(self.config, self.hosts, default_host, spec, self._recent_dirs()), self._create)

    def _recent_dirs(self, limit: int = 8) -> dict[str, list[str]]:
        """Most recently used session directories per host, for the new-session dialog."""
        by_host: dict[str, list[str]] = {}
        ordered = sorted(self.sessions, key=lambda s: s.activity if s.live else s.closed_at, reverse=True)
        for s in ordered:
            d = tmux.short_path(s.directory or s.path)
            dirs = by_host.setdefault(s.host.name, [])
            if d and d not in dirs and len(dirs) < limit:
                dirs.append(d)
        return by_host

    def action_rename(self) -> None:
        session = self.selected
        if session is None:
            return

        def done(new: str | None) -> None:
            if new and new != session.name:
                self._rename(session, new)

        self.push_screen(RenameScreen(session), done)

    @work(thread=True)
    def _rename(self, session: Session, new: str) -> None:
        try:
            new = tmux.rename_session(session, new)
        except HostError as e:
            self.call_from_thread(self.notify, str(e), severity="error", timeout=8)
            return
        self.focus_key = f"{session.host.name}:{new}"
        self.call_from_thread(self.action_refresh)

    def _create(self, spec: dict | None) -> None:
        if spec:
            self._create_worker(spec)

    @work(thread=True)
    def _create_worker(self, spec: dict) -> None:
        host = next(h for h in self.hosts if h.name == spec["host"])
        taken = {s.name for s in self.sessions if s.host.name == host.name}
        base = spec["name"] or f"{spec['agent']}-{os.path.basename(spec['dir'].rstrip('/')) or 'home'}"
        name = unique_name(base, taken) if not spec["name"] else tmux.sanitize(base)
        try:
            session = tmux.create(
                host, name, spec["agent"], self.config.agents, spec["dir"], create_dir=spec.get("create_dir", False)
            )
        except MissingDirectory as e:
            self.call_from_thread(self._confirm_mkdir, spec, e)
            return
        except HostError as e:
            self.call_from_thread(self.notify, str(e), severity="error", timeout=8)
            return
        self.call_from_thread(self.exit, session)

    def _confirm_mkdir(self, spec: dict, e: MissingDirectory) -> None:
        def done(ok: bool | None) -> None:
            if ok:
                self._create_worker({**spec, "create_dir": True})
            else:
                self.action_new(spec)  # back to the form to fix the path

        self.push_screen(
            ConfirmScreen(f"[b]{e.path}[/b] doesn't exist on [b]{e.host}[/b].\nCreate it?"), done
        )
