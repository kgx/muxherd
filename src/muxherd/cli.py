"""muxherd command line."""

from __future__ import annotations

import json
import os
import shlex
import time
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__, config, editor, registry, tmux
from .tmux import Host, HostError, Session

app = typer.Typer(
    help="Herd AI coding agents running in tmux across your tailnet. Run with no command for the picker.",
    no_args_is_help=False,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
console = Console()
err = Console(stderr=True)


def _hosts(cfg: config.Config, only: str | None = None) -> list[Host]:
    hosts = [Host(n, t) for n, t in cfg.hosts.items()]
    if only:
        hosts = [h for h in hosts if h.name == only]
        if not hosts:
            err.print(f"[red]unknown host {only!r}[/red] (configured: {', '.join(cfg.hosts)})")
            raise typer.Exit(2)
    return hosts


def _report_errors(errors: dict[str, str]) -> None:
    for msg in errors.values():
        err.print(f"[yellow]warning:[/yellow] {msg}")


def _pick(initial_filter: str = "", mode: str = "attach") -> None:
    from .tui import MuxherdApp

    cfg = config.load()
    session = MuxherdApp(cfg, initial_filter, mode=mode).run()
    if session is not None:
        # The picker already reopened closed sessions / created shells.
        tmux.attach(session, cfg.attach)


def _find(query: str, cfg: config.Config) -> tuple[list[Session], dict[str, str]]:
    """Resolve 'name' or 'host:name' — exact matches first, then substring."""
    host_name, _, name = query.rpartition(":")
    sessions, errors = tmux.list_all(_hosts(cfg, host_name or None))
    for pool in ([s for s in sessions if s.live], sessions):  # prefer live over closed
        exact = [s for s in pool if s.name == name]
        if exact:
            return exact, errors
        partial = [s for s in pool if name.lower() in s.name.lower()]
        if partial:
            return partial, errors
    return [], errors


def _open(session: Session, cfg: config.Config) -> None:
    """Attach to a live session, or reopen a closed one and attach."""
    if not session.live:
        try:
            try:
                session = tmux.reopen(session, cfg.agents)
            except tmux.MissingDirectory as e:
                if not typer.confirm(f"{e.path} no longer exists on {e.host}. Create it?"):
                    raise typer.Exit(1)
                session = tmux.reopen(session, cfg.agents, create_dir=True)
        except HostError as e:
            err.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
        console.print(f"reopened [cyan]{session.host.name}[/cyan]:[bold]{session.name}[/bold]")
    tmux.attach(session, cfg.attach)


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", "-V", help="Show version.")] = False,
) -> None:
    if version:
        console.print(f"muxherd {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        _pick()


@app.command("ls")
def ls(
    host: Annotated[str | None, typer.Option("--host", "-H", help="Only this host.")] = None,
    live: Annotated[bool, typer.Option("--live", "-l", help="Hide closed sessions.")] = False,
) -> None:
    """List sessions on all configured hosts (closed ones marked ✕)."""
    cfg = config.load()
    sessions, errors = tmux.list_all(_hosts(cfg, host))
    if live:
        sessions = [s for s in sessions if s.live]
    _report_errors(errors)
    if not sessions:
        console.print("[dim]no sessions[/dim]")
        return
    now = time.time()
    table = Table(box=None, pad_edge=False, header_style="dim")
    for col in ("", "host", "session", "agent", "idle", "dir"):
        table.add_column(col)
    for s in sessions:
        if s.live:
            table.add_row(
                "[green]●[/green]" if s.attached else "[dim]○[/dim]",
                f"[cyan]{s.host.name}[/cyan]",
                f"[bold]{s.name}[/bold]",
                s.agent,
                tmux.ago(s.activity, now),
                f"[dim]{tmux.short_path(tmux.project_dir(s))}[/dim]",
            )
        else:
            table.add_row(
                "[red dim]✕[/red dim]",
                *(
                    f"[dim]{v}[/dim]"
                    for v in (s.host.name, s.name, s.agent, tmux.ago(s.closed_at, now), tmux.short_path(s.directory))
                ),
            )
    console.print(table)


@app.command("a", hidden=True)
@app.command("attach")
def attach(query: Annotated[str, typer.Argument(help="Session name, 'host:name', or part of a name.")]) -> None:
    """Attach to a session, reopening it if closed (opens the picker if ambiguous)."""
    cfg = config.load()
    found, errors = _find(query, cfg)
    _report_errors(errors)
    if len(found) == 1:
        _open(found[0], cfg)
    _pick(query.rpartition(":")[2])


@app.command("code")
def code(
    query: Annotated[str | None, typer.Argument(help="Session name, 'host:name', or part of a name.")] = None,
) -> None:
    """Open a session's directory in your editor (VS Code Remote-SSH for remote hosts)."""
    cfg = config.load()
    if query:
        found, errors = _find(query, cfg)
        _report_errors(errors)
        if len(found) == 1:
            try:
                argv = editor.open_editor(found[0], cfg.editor)
            except HostError as e:
                err.print(f"[red]{e}[/red]")
                raise typer.Exit(1)
            console.print(f"[dim]{shlex.join(argv)}[/dim]")
            return
    _pick(query.rpartition(":")[2] if query else "", mode="editor")


@app.command("sh")
def sh(
    query: Annotated[str | None, typer.Argument(help="Session name, 'host:name', or part of a name.")] = None,
) -> None:
    """Open a throwaway shell (its own tmux session) in a session's project directory."""
    cfg = config.load()
    if query:
        found, errors = _find(query, cfg)
        _report_errors(errors)
        if len(found) == 1:
            try:
                shell = tmux.open_shell_session(found[0])
            except HostError as e:
                err.print(f"[red]{e}[/red]")
                raise typer.Exit(1)
            console.print(
                f"opened [cyan]{shell.host.name}[/cyan]:[bold]{shell.name}[/bold] in {tmux.short_path(shell.directory)}"
            )
            tmux.attach(shell, cfg.attach)
    _pick(query.rpartition(":")[2] if query else "", mode="shell")


@app.command("new")
def new(
    name: Annotated[str | None, typer.Argument(help="Session name (default: <agent>-<dir>).")] = None,
    agent: Annotated[str, typer.Option("--agent", "-a", help="Agent to launch (see config [agents]).")] = "claude",
    host: Annotated[
        str | None, typer.Option("--host", "-H", help="Host to run on (default: first configured).")
    ] = None,
    directory: Annotated[
        str | None, typer.Option("--dir", "-d", help="Working directory (default: cwd locally, ~ remotely).")
    ] = None,
    detach: Annotated[bool, typer.Option("--detach", "-D", help="Create without attaching.")] = False,
) -> None:
    """Start an agent in a new tmux session."""
    cfg = config.load()
    if agent not in cfg.agents:
        err.print(f"[red]unknown agent {agent!r}[/red] (configured: {', '.join(cfg.agents)})")
        raise typer.Exit(2)
    h = _hosts(cfg, host)[0]
    directory = directory or (os.getcwd() if h.is_local else "~")
    if name is None:
        from .tui import unique_name

        taken = {s.name for s in tmux.list_sessions(h)}
        name = unique_name(f"{agent}-{os.path.basename(directory.rstrip('/'))}", taken)
    try:
        try:
            session = tmux.create(h, name, agent, cfg.agents, directory)
        except tmux.MissingDirectory as e:
            if not typer.confirm(f"{e.path} doesn't exist on {e.host}. Create it?"):
                raise typer.Exit(1)
            session = tmux.create(h, name, agent, cfg.agents, directory, create_dir=True)
    except HostError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(f"started [cyan]{h.name}[/cyan]:[bold]{session.name}[/bold] ({agent})")
    if not detach:
        tmux.attach(session, cfg.attach)


@app.command("kill")
def kill(
    query: Annotated[str, typer.Argument(help="Session name or 'host:name'.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
) -> None:
    """Kill a session."""
    cfg = config.load()
    host_name, _, name = query.rpartition(":")
    sessions, errors = tmux.list_all(_hosts(cfg, host_name or None))
    _report_errors(errors)
    found = [s for s in sessions if s.name == name and s.live]
    if not found:
        closed = any(s.name == name for s in sessions)
        err.print(f"[red]no live session named {query!r}[/red]" + (" (it's closed; use `mh forget`)" if closed else ""))
        raise typer.Exit(1)
    if len(found) > 1:
        err.print(f"[red]ambiguous:[/red] {', '.join(s.key for s in found)} — use host:name")
        raise typer.Exit(1)
    s = found[0]
    if not yes and not typer.confirm(f"kill {s.key}?"):
        raise typer.Exit(1)
    tmux.kill_session(s)
    console.print(f"killed {s.key}")


@app.command("rename")
def rename(
    query: Annotated[str, typer.Argument(help="Session name or 'host:name' (live or closed).")],
    new_name: Annotated[str, typer.Argument(help="New name.")],
) -> None:
    """Rename a session."""
    cfg = config.load()
    host_name, _, name = query.rpartition(":")
    sessions, errors = tmux.list_all(_hosts(cfg, host_name or None))
    _report_errors(errors)
    found = [s for s in sessions if s.name == name]
    if len(found) != 1:
        err.print(f"[red]{'ambiguous (use host:name)' if found else 'no session named'} {query!r}[/red]")
        raise typer.Exit(1)
    try:
        new = tmux.rename_session(found[0], new_name)
    except HostError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(f"renamed {found[0].key} → {found[0].host.name}:{new}")


@app.command("chdir")
def chdir(
    query: Annotated[str, typer.Argument(help="Session name or 'host:name' (live or closed).")],
    directory: Annotated[str, typer.Argument(help="New project directory on the session's host.")],
) -> None:
    """Change a session's project directory (where it reopens; editor and shells open there)."""
    cfg = config.load()
    host_name, _, name = query.rpartition(":")
    sessions, errors = tmux.list_all(_hosts(cfg, host_name or None))
    _report_errors(errors)
    found = [s for s in sessions if s.name == name]
    if len(found) != 1:
        err.print(f"[red]{'ambiguous (use host:name)' if found else 'no session named'} {query!r}[/red]")
        raise typer.Exit(1)
    try:
        try:
            path = tmux.chdir_session(found[0], directory)
        except tmux.MissingDirectory as e:
            if not typer.confirm(f"{e.path} doesn't exist on {e.host}. Create it?"):
                raise typer.Exit(1)
            path = tmux.chdir_session(found[0], directory, create_dir=True)
    except HostError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(f"{found[0].key} → {tmux.short_path(path)}")
    if found[0].live:
        console.print("[dim]the running agent stays where it is; this applies on reopen, editor and shells[/dim]")


@app.command("forget")
def forget(query: Annotated[str, typer.Argument(help="Closed session name or 'host:name'.")]) -> None:
    """Remove a closed session from the registry."""
    cfg = config.load()
    host_name, _, name = query.rpartition(":")
    sessions, errors = tmux.list_all(_hosts(cfg, host_name or None))
    _report_errors(errors)
    found = [s for s in sessions if s.name == name and not s.live]
    if len(found) != 1:
        err.print(f"[red]{'ambiguous' if found else 'no closed session named'} {query!r}[/red]")
        raise typer.Exit(1)
    tmux.forget(found[0])
    console.print(f"forgot {found[0].key}")


# Commands run on a session host (locally or by clients over ssh); not for humans.
host_app = typer.Typer(hidden=True, add_completion=False)
app.add_typer(host_app, name="_host", hidden=True)


@host_app.command("sync")
def host_sync() -> None:
    """Record live sessions in this host's registry; print live + closed as JSON."""
    try:
        print(json.dumps(tmux.host_sync()))
    except HostError as e:
        err.print(str(e))
        raise typer.Exit(1)


@host_app.command("rename")
def host_rename(old: str, new: str) -> None:
    try:
        tmux.host_rename(old, new)
    except HostError as e:
        err.print(str(e))
        raise typer.Exit(1)


@host_app.command("doctor")
def host_doctor() -> None:
    """Facts about this host for `mh doctor` on a client, as JSON."""
    from . import doctor

    print(json.dumps(doctor.host_facts()))


@host_app.command("chdir")
def host_chdir(name: str, directory: str, create: bool = False) -> None:
    try:
        print(tmux.host_chdir(name, directory, create_dir=create))
    except tmux.MissingDirectory as e:
        print(e.path)
        raise typer.Exit(tmux.EXIT_MISSING_DIR)
    except HostError as e:
        err.print(str(e))
        raise typer.Exit(1)


@host_app.command("forget")
def host_forget(name: str) -> None:
    if not registry.forget(name):
        err.print(f"no closed session named {name!r}")
        raise typer.Exit(1)


@app.command("doctor")
def doctor_cmd(
    host: Annotated[str | None, typer.Option("--host", "-H", help="Only check this host.")] = None,
    clipboard: Annotated[
        bool, typer.Option("--clipboard", "-c", help="Also test copy → your clipboard live, through the real attach.")
    ] = False,
) -> None:
    """Check this machine and each host (versions, tmux settings, clipboard) and suggest fixes."""
    from . import doctor

    cfg = config.load()
    icons = {"ok": "[green]✓[/green]", "warn": "[yellow]⚠[/yellow]", "fail": "[red]✗[/red]", "skip": "[dim]–[/dim]"}
    failed = False

    def show(title: str, checks: list) -> None:
        nonlocal failed
        console.print(f"\n[bold]{title}[/bold]")
        for c in checks:
            failed |= c.status == "fail"
            console.print(f"  {icons[c.status]} {c.name:<15} {escape(c.detail)}")
            if c.fix and c.status in ("warn", "fail"):
                console.print(f"    [dim]→ {escape(c.fix)}[/dim]")

    show(f"this machine ({config.local_hostname()}, muxherd {__version__})", doctor.client_checks(cfg))
    for h in _hosts(cfg, host):
        with console.status(f"checking {h.name}…"):
            checks, missing = doctor.host_checks(h, cfg)
        show(f"{h.name} ({'this machine' if h.is_local else h.target})", checks)
        if missing:
            console.print(
                f"    [dim]→ add to ~/.tmux.conf on {h.name}, then `tmux source-file ~/.tmux.conf` and reattach:[/dim]"
            )
            for line in missing:
                console.print(f"        {line}", markup=False, highlight=False)
        if clipboard:
            show(f"{h.name}: live clipboard test", [doctor.clipboard_roundtrip(h, cfg)])
    if not clipboard:
        console.print("\n[dim]To test copying all the way to this machine's clipboard: mh doctor --clipboard[/dim]")
    if failed:
        raise typer.Exit(1)


@app.command("init")
def init(
    remote: Annotated[
        list[str] | None,
        typer.Option("--remote", "-r", help="Remote host as NAME or NAME=SSH_TARGET. Repeatable."),
    ] = None,
    no_local: Annotated[bool, typer.Option("--no-local", help="Don't include this machine.")] = False,
    force: Annotated[bool, typer.Option("--force", "-f", help="Overwrite an existing config.")] = False,
) -> None:
    """Write a starter config to ~/.config/muxherd/config.toml."""
    path = config.CONFIG_PATH
    if path.exists() and not force:
        err.print(f"[yellow]{path} already exists[/yellow] (use --force to overwrite)")
        raise typer.Exit(1)
    hosts: dict[str, str] = {}
    if not no_local:
        hosts[config.local_hostname()] = "local"
    for r in remote or []:
        name, _, target = r.partition("=")
        hosts[name] = target or name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config.render(hosts))
    console.print(f"wrote {path}")
    console.print("[dim]next: `mh doctor` checks this machine and each host, and suggests fixes[/dim]")


@app.command("hosts")
def hosts() -> None:
    """Check that each configured host is reachable."""
    cfg = config.load()
    console.print(
        f"[dim]config: {config.CONFIG_PATH}{'' if config.CONFIG_PATH.exists() else ' (missing, using defaults)'}[/dim]"
    )
    for h in _hosts(cfg):
        try:
            sessions = tmux.list_sessions(h)
            live = sum(s.live for s in sessions)
            console.print(
                f"[green]✓[/green] [cyan]{h.name}[/cyan] ({h.target}) — {live} live, {len(sessions) - live} closed"
            )
        except HostError as e:
            console.print(f"[red]✗[/red] [cyan]{h.name}[/cyan] ({h.target}) — {e}")


def main() -> None:
    app()
