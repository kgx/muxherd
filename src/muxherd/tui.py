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
from textual.widgets import DataTable, Footer, Input, Label, Select, Static
from textual.worker import get_current_worker

from . import tmux
from .config import Config
from .tmux import Host, HostError, Session

REFRESH_SECONDS = 2.0


def matches(session: Session, query: str) -> bool:
    hay = f"{session.key} {session.agent} {session.path}".lower()
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
        Binding("ctrl+x", "app.kill", "kill"),
        Binding("escape", "app.escape", "clear/quit"),
    ]


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


class NewSessionScreen(ModalScreen[dict | None]):
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, config: Config, hosts: list[Host], default_host: str) -> None:
        super().__init__()
        self.config = config
        self.hosts = hosts
        self.default_host = default_host

    def compose(self) -> ComposeResult:
        agents = list(self.config.agents)
        with Vertical(classes="dialog"):
            yield Label("[b]New session[/b]", classes="title")
            yield Label("agent")
            yield Select([(a, a) for a in agents], value=agents[0], allow_blank=False, compact=True, id="agent")
            yield Label("host")
            yield Select([(h.name, h.name) for h in self.hosts], value=self.default_host, allow_blank=False, compact=True, id="host")
            yield Label("directory")
            yield Input(self._default_dir(self.default_host), compact=True, id="dir")
            yield Label("name")
            yield Input(placeholder="auto: <agent>-<dir>", compact=True, id="name")
            yield Label("[b]enter[/b] create & attach   [b]tab[/b] next field   [b]esc[/b] cancel", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#dir", Input).focus()

    def _default_dir(self, host_name: str) -> str:
        host = next(h for h in self.hosts if h.name == host_name)
        return tmux.short_path(os.getcwd()) if host.is_local else "~"

    @on(Select.Changed, "#host")
    def host_changed(self, event: Select.Changed) -> None:
        self.query_one("#dir", Input).value = self._default_dir(str(event.value))

    @on(Input.Submitted)
    def submit(self) -> None:
        self.dismiss(
            {
                "agent": str(self.query_one("#agent", Select).value),
                "host": str(self.query_one("#host", Select).value),
                "dir": self.query_one("#dir", Input).value.strip() or "~",
                "name": self.query_one("#name", Input).value.strip(),
            }
        )

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
    ConfirmScreen, NewSessionScreen { align: center middle; }
    """
    BINDINGS = [
        Binding("ctrl+n", "new", "new"),
        Binding("ctrl+r", "refresh", "refresh"),
        Binding("ctrl+p", "toggle_preview", "preview"),
        Binding("escape", "escape", "quit", show=False),
        Binding("ctrl+c", "quit", show=False, priority=True),
    ]

    def __init__(self, config: Config, initial_filter: str = "") -> None:
        super().__init__()
        self.config = config
        self.hosts = [Host(name, target) for name, target in config.hosts.items()]
        self.sessions: list[Session] = []
        self.shown: list[Session] = []
        self.errors: dict[str, str] = {}
        self.loaded = False
        self.initial_filter = initial_filter

    def compose(self) -> ComposeResult:
        yield Static("loading…", id="status")
        yield FilterInput(self.initial_filter, placeholder="type to filter sessions…", id="filter")
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
        current = self.selected.key if self.selected else None
        query = self.query_one(FilterInput).value
        self.shown = [s for s in self.sessions if matches(s, query)]
        now = time.time()
        table.clear()
        for s in self.shown:
            dot = Text("●", style="green") if s.attached else Text("○", style="dim")
            table.add_row(
                dot,
                Text(s.host.name, style="cyan"),
                Text(s.name, style="bold"),
                s.agent,
                tmux.ago(s.activity, now),
                Text(tmux.short_path(s.path), style="dim"),
                key=s.key,
            )
        keys = [s.key for s in self.shown]
        if current in keys:
            table.move_cursor(row=keys.index(current), animate=False)
        elif self.shown:
            table.move_cursor(row=0, animate=False)

    def _render_status(self) -> None:
        parts = [Text("muxherd", style="bold"), Text(f"  {len(self.shown)}/{len(self.sessions)} sessions  ")]
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
        try:
            out = tmux.capture(session)
            # Agent TUIs leave lots of trailing blank lines; show the last screenful of content.
            lines = out.rstrip().splitlines()[-height:]
            text = Text.from_ansi("\n".join(lines))
        except HostError as e:
            text = Text(str(e), style="red")
        if not get_current_worker().is_cancelled:
            self.call_from_thread(self._show_preview, session.key, text)

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
        if self.selected:
            self.exit(self.selected)

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

        self.push_screen(ConfirmScreen(f"Kill session [b]{session.key}[/b]?"), done)

    @work(thread=True)
    def _kill(self, session: Session) -> None:
        try:
            tmux.kill_session(session)
        except HostError as e:
            self.call_from_thread(self.notify, str(e), severity="error")
        self.call_from_thread(self.action_refresh)

    def action_new(self) -> None:
        default_host = self.selected.host.name if self.selected else self.hosts[0].name
        self.push_screen(NewSessionScreen(self.config, self.hosts, default_host), self._create)

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
            name = tmux.new_session(host, name, spec["agent"], self.config.agents.get(spec["agent"], ""), spec["dir"])
        except HostError as e:
            self.call_from_thread(self.notify, str(e), severity="error", timeout=8)
            return
        session = Session(host, name, 0, 0, 0, 1, spec["agent"], spec["dir"])
        self.call_from_thread(self.exit, session)
