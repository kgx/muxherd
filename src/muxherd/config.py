"""Config loading: ~/.config/muxherd/config.toml"""

from __future__ import annotations

import json
import os
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path(
    os.environ.get("MUXHERD_CONFIG")
    or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "muxherd" / "config.toml"
)


@dataclass
class Agent:
    # Command typed into the new session's shell ("" = plain shell). "{id}" is replaced
    # with a fresh UUID that muxherd stores, so the conversation can be resumed later.
    start: str = ""
    # Command used to reopen a closed session. "{id}" is the stored UUID; if the session
    # has none (e.g. not started by muxherd), `start` is used instead.
    resume: str = ""
    # Optional shell check run on the session's host before resuming; if it fails (e.g.
    # the conversation was never used, so there's nothing to resume) `start` is used
    # with the same id.
    resumable: str = ""


DEFAULT_AGENTS = {
    # Claude Code only writes a conversation file once you send a message.
    "claude": Agent(
        "claude --session-id {id}",
        "claude --resume {id}",
        "ls ~/.claude/projects/*/{id}.jsonl >/dev/null 2>&1",
    ),
    "codex": Agent("codex", "codex resume --last"),
    "grok": Agent("grok"),
    "shell": Agent(),
}


@dataclass
class Editor:
    # Run on the machine where you use muxherd. {path} is the session's directory,
    # {host} the session host's ssh target, {uri} a VS Code Remote-SSH folder URI.
    local: str = "code -n {path}"
    # --folder-uri, not `--remote <host> <path>`: with the latter VS Code can't tell a
    # remote folder from a file and opens the parent directory.
    remote: str = "code -n --folder-uri {uri}"


SORT_MODES = {
    "name": "alphabetical by session name",
    "recent": "most recently used first",
}


@dataclass
class UI:
    # Live sessions always come first, closed ones below; `sort` orders within each group.
    sort: str = "name"
    show_closed: bool = True  # list closed sessions (ctrl+t toggles for the current run)
    preview: bool = True  # show the preview pane (ctrl+p toggles for the current run)
    # After attaching from the picker, come back to it when you detach or the session ends.
    return_to_picker: bool = True


# Earlier default for `remote`; configs written with it are upgraded on load.
LEGACY_REMOTE_EDITOR = "code -n --remote ssh-remote+{host} {path}"


def local_hostname() -> str:
    return socket.gethostname().split(".")[0]


@dataclass
class Config:
    # host name -> ssh target, or "local" for this machine
    hosts: dict[str, str] = field(default_factory=lambda: {local_hostname(): "local"})
    agents: dict[str, Agent] = field(default_factory=lambda: dict(DEFAULT_AGENTS))
    # how to attach to remote sessions: "mosh" or "ssh"
    attach: str = "mosh"
    editor: Editor = field(default_factory=Editor)
    ui: UI = field(default_factory=UI)


def _agent(value: str | dict) -> Agent:
    if isinstance(value, str):
        return Agent(start=value)
    return Agent(
        start=str(value.get("start", "")),
        resume=str(value.get("resume", "")),
        resumable=str(value.get("resumable", "")),
    )


def load() -> Config:
    if not CONFIG_PATH.exists():
        return Config()
    data = tomllib.loads(CONFIG_PATH.read_text())
    cfg = Config()
    if hosts := data.get("hosts"):
        cfg.hosts = {str(k): str(v) for k, v in hosts.items()}
    if agents := data.get("agents"):
        cfg.agents = {str(k): _agent(v) for k, v in agents.items()}
    cfg.attach = data.get("attach", cfg.attach)
    if editor := data.get("editor"):
        remote = str(editor.get("remote", cfg.editor.remote))
        cfg.editor = Editor(
            local=str(editor.get("local", cfg.editor.local)),
            remote=Editor.remote if remote == LEGACY_REMOTE_EDITOR else remote,
        )
    if ui := data.get("ui"):
        sort = str(ui.get("sort", cfg.ui.sort))
        cfg.ui = UI(
            sort=sort if sort in SORT_MODES else UI.sort,
            show_closed=bool(ui.get("show_closed", cfg.ui.show_closed)),
            preview=bool(ui.get("preview", cfg.ui.preview)),
            return_to_picker=bool(ui.get("return_to_picker", cfg.ui.return_to_picker)),
        )
    return cfg


def save_ui(ui: UI) -> None:
    """Write the [ui] table into the config file, leaving everything else (comments
    included) untouched. Creates the table, or the file, if needed."""
    values = {
        "sort": json.dumps(ui.sort),
        "show_closed": _toml_bool(ui.show_closed),
        "preview": _toml_bool(ui.preview),
        "return_to_picker": _toml_bool(ui.return_to_picker),
    }
    text = CONFIG_PATH.read_text() if CONFIG_PATH.exists() else ""
    lines = text.splitlines()
    header = next((i for i, line in enumerate(lines) if line.strip() == "[ui]"), None)
    if header is None:
        block = ["", "[ui]", *(f"{k} = {v}" for k, v in values.items())]
        lines = [*lines, *block] if lines else block[1:]
    else:
        end = next((i for i in range(header + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        pending = dict(values)
        for i in range(header + 1, end):
            key = lines[i].split("=", 1)[0].strip()
            if "=" in lines[i] and not lines[i].lstrip().startswith("#") and key in pending:
                lines[i] = f"{key} = {pending.pop(key)}"
        insert_at = end
        while insert_at > header + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1  # keep new keys above the blank line before the next table
        lines[insert_at:insert_at] = [f"{k} = {v}" for k, v in pending.items()]
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text("\n".join(lines) + "\n")


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


def render(hosts: dict[str, str], attach: str = "mosh") -> str:
    q = json.dumps  # TOML basic strings use the same escapes as JSON
    lines = [
        "# muxherd config",
        "",
        '# How to attach to sessions on remote hosts: "mosh" or "ssh"',
        f"attach = {q(attach)}",
        "",
        "# Hosts whose tmux sessions muxherd shows.",
        '# name = "local" for this machine, otherwise an ssh target (tailnet name, user@host, ssh alias)',
        "[hosts]",
        *(f"{name} = {q(target)}" for name, target in hosts.items()),
        "",
        "# Editor opened with ctrl+e in the picker or `mh code`. Runs on this machine.",
        "#   {path} = session directory, {host} = the session host's ssh target,",
        "#   {uri} = VS Code Remote-SSH folder URI for {host} + {path}",
        "[editor]",
        f"local = {q(Editor.local)}",
        f"remote = {q(Editor.remote)}",
        "",
        "# Picker settings (also editable in the picker's settings screen, ctrl+s)",
        '#   sort = "name" (alphabetical) or "recent"; live sessions are always listed first',
        "[ui]",
        f"sort = {q(UI.sort)}",
        "show_closed = true",
        "preview = true",
        "return_to_picker = true  # come back to the picker after detaching",
        "",
        "# Agents offered when creating a session.",
        "#   start  = command typed into the session's shell; {id} becomes a fresh UUID",
        "#   resume = command used to reopen a closed session; {id} is that same UUID",
        "#   resumable = optional shell check on the host; if it fails, reopen runs start instead",
    ]
    for name, agent in DEFAULT_AGENTS.items():
        lines += ["", f"[agents.{name}]", f"start = {q(agent.start)}"]
        if agent.resume:
            lines.append(f"resume = {q(agent.resume)}")
        if agent.resumable:
            lines.append(f"resumable = {q(agent.resumable)}")
    return "\n".join(lines) + "\n"
