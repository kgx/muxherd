"""Talking to tmux on local and remote (ssh) hosts."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, fields
from pathlib import Path

from . import config, registry
from .config import Agent

SEP = "\t"
FIELDS = [
    "session_name",
    "session_attached",
    "session_activity",
    "session_created",
    "session_windows",
    "@muxherd_agent",
    "@muxherd_id",
    "@muxherd_dir",
    "pane_current_command",
    "pane_current_path",
]
FORMAT = SEP.join(f"#{{{f}}}" for f in FIELDS)

CONTROL_DIR = Path.home() / ".cache" / "muxherd"
# Reuse one ssh connection per host so polling and previews stay cheap.
SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=4",
    "-o", "ControlMaster=auto",
    "-o", f"ControlPath={CONTROL_DIR}/ssh-%C",
    "-o", "ControlPersist=10m",
]
# Non-interactive ssh gets a minimal PATH; make sure common tmux locations are on it.
REMOTE_PATH = 'PATH="$PATH:/usr/local/bin:/opt/homebrew/bin:$HOME/.local/bin"; '
# tmux refuses to attach if $TERM has no terminfo entry on that machine (e.g. xterm-kitty
# on a stock server), so fall back to a universally available one.
FALLBACK_TERM = "xterm-256color"
REMOTE_TERM_FIX = f'infocmp "$TERM" >/dev/null 2>&1 || export TERM={FALLBACK_TERM}; '


class HostError(Exception):
    pass


class MissingDirectory(HostError):
    def __init__(self, host: str, path: str) -> None:
        super().__init__(f"{host}: no such directory {path}")
        self.host, self.path = host, path


def sanitize(name: str) -> str:
    """tmux session names can't contain '.' or ':'; keep them shell-friendly too."""
    return re.sub(r"[^\w-]+", "-", name).strip("-")


def exact(name: str) -> str:
    """Target that matches the session name exactly (no prefix/fnmatch matching)."""
    return f"={name}:"


@dataclass
class Host:
    name: str
    target: str  # "local" or an ssh target

    @property
    def is_local(self) -> bool:
        return self.target == "local"

    def run(self, args: list[str], timeout: float = 8) -> subprocess.CompletedProcess[str]:
        if self.is_local:
            cmd = args
        else:
            CONTROL_DIR.mkdir(parents=True, exist_ok=True)
            cmd = ["ssh", *SSH_OPTS, self.target, REMOTE_PATH + shlex.join(args)]
        try:
            # No tty on stdin: tmux skips its terminal checks (e.g. unknown $TERM) for these queries.
            return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise HostError(f"{self.name}: timed out") from e
        except FileNotFoundError as e:
            raise HostError(f"{self.name}: {e.filename} not found") from e

    def tmux(self, *args: str, timeout: float = 8) -> str:
        p = self.run(["tmux", *args], timeout=timeout)
        if p.returncode != 0:
            raise HostError(f"{self.name}: {(p.stderr or p.stdout).strip()}")
        return p.stdout

    def expand_dir(self, path: str) -> str:
        """tmux -c does not expand '~', so resolve it against the host's $HOME."""
        if not path:
            path = "~"
        if not path.startswith("~"):
            return path
        if self.is_local:
            return os.path.expanduser(path)
        p = self.run(["sh", "-c", 'printf %s "$HOME"'])
        home = p.stdout.strip()
        if p.returncode != 0 or not home:
            raise HostError(f"{self.name}: could not resolve $HOME ({p.stderr.strip() or 'ssh failed'})")
        return home + path[1:]


@dataclass
class Session:
    host: Host
    name: str
    attached: int = 0
    activity: int = 0
    created: int = 0
    windows: int = 1
    agent: str = ""
    path: str = ""  # current directory of the active pane
    directory: str = ""  # directory the session was started in (where it reopens)
    agent_id: str = ""  # agent conversation id used to resume (e.g. claude --session-id)
    live: bool = True  # False: closed, known only from the registry
    closed_at: int = 0

    @property
    def key(self) -> str:
        return f"{self.host.name}:{self.name}"

    @classmethod
    def from_dict(cls, host: Host, d: dict) -> Session:
        known = {f.name for f in fields(cls)} - {"host"}
        return cls(host=host, **{k: v for k, v in d.items() if k in known and v is not None})


def tmux_live(host: Host) -> list[dict]:
    """Raw list of live tmux sessions on a host."""
    p = host.run(["tmux", "list-sessions", "-F", FORMAT])
    if p.returncode != 0:
        err = (p.stderr or "").strip()
        if "no server running" in err or "error connecting to" in err or "No such file" in err:
            return []  # tmux installed but no server: no sessions
        if not host.is_local and p.returncode == 255:
            raise HostError(f"{host.name}: unreachable ({err.splitlines()[-1] if err else 'ssh failed'})")
        raise HostError(f"{host.name}: {err or 'tmux failed'}")
    sessions = []
    for line in p.stdout.splitlines():
        parts = line.split(SEP)
        if len(parts) != len(FIELDS):
            continue
        name, attached, activity, created, windows, agent, agent_id, directory, command, path = parts
        sessions.append(
            {
                "name": name,
                "attached": int(attached or 0),
                "activity": int(activity or 0),
                "created": int(created or 0),
                "windows": int(windows or 0),
                "agent": agent,
                "agent_id": agent_id,
                "directory": directory,
                "command": command,
                "path": path,
            }
        )
    return sessions


def host_sync() -> list[dict]:
    """Runs on the host that owns the sessions: update its registry, return live + closed."""
    live = tmux_live(Host(config.local_hostname(), "local"))
    closed = registry.sync(live, set(config.load().agents))
    out = [{**s, "agent": s["agent"] or s["command"], "live": True} for s in live]
    out += [{**c, "path": c["directory"], "live": False} for c in closed]
    return out


def list_sessions(host: Host) -> list[Session]:
    if host.is_local:
        rows = host_sync()
    else:
        p = host.run(["muxherd", "_host", "sync"])
        if p.returncode == 0:
            rows = json.loads(p.stdout)
        elif p.returncode == 127:
            # muxherd isn't installed on that host: live sessions only, no registry.
            rows = [{**s, "agent": s["agent"] or s["command"]} for s in tmux_live(host)]
        elif p.returncode == 255:
            err = p.stderr.strip()
            raise HostError(f"{host.name}: unreachable ({err.splitlines()[-1] if err else 'ssh failed'})")
        else:
            raise HostError(f"{host.name}: {p.stderr.strip() or 'muxherd _host sync failed'}")
    return [Session.from_dict(host, r) for r in rows]


def list_all(hosts: list[Host]) -> tuple[list[Session], dict[str, str]]:
    """Query every host in parallel. Returns (sessions, {host name: error})."""
    sessions: list[Session] = []
    errors: dict[str, str] = {}
    if not hosts:
        return sessions, errors
    with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        futures = {h.name: pool.submit(list_sessions, h) for h in hosts}
        for name, fut in futures.items():
            try:
                sessions.extend(fut.result())
            except HostError as e:
                errors[name] = str(e)
    # Live sessions first, then closed ones, most recently closed first.
    sessions.sort(key=lambda s: (0, s.host.name, s.name, 0) if s.live else (1, "", "", -s.closed_at))
    return sessions, errors


def capture(session: Session, lines: int = 200) -> str:
    out = session.host.tmux("capture-pane", "-p", "-J", "-t", exact(session.name), "-S", f"-{lines}", timeout=5)
    return out.rstrip("\n")


def new_session(
    host: Host, name: str, agent: str, command: str, directory: str, *, agent_id: str = "", create_dir: bool = False
) -> Session:
    name = sanitize(name)
    if not name:
        raise HostError("session name is empty")
    directory = host.expand_dir(directory)
    # tmux silently falls back to $HOME for a missing -c directory, so check first and
    # let the caller confirm creating it (a missing dir is often a typo).
    if host.run(["test", "-d", directory]).returncode != 0:
        if not create_dir:
            raise MissingDirectory(host.name, directory)
        p = host.run(["mkdir", "-p", directory])
        if p.returncode != 0:
            raise HostError(f"{host.name}: could not create {directory}: {p.stderr.strip()}")
    args = [
        "new-session", "-d", "-s", name, "-c", directory,
        ";", "set-option", "-t", exact(name), "@muxherd_agent", agent,
        ";", "set-option", "-t", exact(name), "@muxherd_id", agent_id,
        ";", "set-option", "-t", exact(name), "@muxherd_dir", directory,
    ]
    if command:
        # Type the command into the login shell so the agent gets the user's full
        # environment, and quitting the agent drops back to a shell instead of
        # killing the session.
        args += [";", "send-keys", "-t", exact(name), command, "Enter"]
    host.tmux(*args)
    try:
        list_sessions(host)  # record it in the registry right away
    except HostError:
        pass
    return Session(host, name, agent=agent, path=directory, directory=directory, agent_id=agent_id)


def start_command(agent: Agent) -> tuple[str, str]:
    """(command, agent_id) for a fresh session."""
    agent_id = str(uuid.uuid4()) if "{id}" in agent.start else ""
    return agent.start.replace("{id}", agent_id), agent_id


def resume_command(agent: Agent, agent_id: str) -> tuple[str, str]:
    """(command, agent_id) for reopening a closed session."""
    if agent.resume and ("{id}" not in agent.resume or agent_id):
        return agent.resume.replace("{id}", agent_id), agent_id
    return start_command(agent)


def create(host: Host, name: str, agent_name: str, agents: dict[str, Agent], directory: str, create_dir: bool = False) -> Session:
    command, agent_id = start_command(agents.get(agent_name, Agent()))
    return new_session(host, name, agent_name, command, directory, agent_id=agent_id, create_dir=create_dir)


def reopen(session: Session, agents: dict[str, Agent], create_dir: bool = False) -> Session:
    """Start a closed session again under the same name, resuming the agent if possible."""
    command, agent_id = resume_command(agents.get(session.agent, Agent()), session.agent_id)
    return new_session(
        session.host, session.name, session.agent, command, session.directory or "~",
        agent_id=agent_id, create_dir=create_dir,
    )


def forget(session: Session) -> None:
    """Drop a closed session from its host's registry."""
    if session.host.is_local:
        registry.forget(session.name)
        return
    p = session.host.run(["muxherd", "_host", "forget", session.name])
    if p.returncode != 0:
        raise HostError(f"{session.host.name}: {p.stderr.strip() or 'forget failed'}")


def kill_session(session: Session) -> None:
    session.host.tmux("kill-session", "-t", exact(session.name))


def attach_argv(session: Session, mode: str = "mosh") -> list[str]:
    target = exact(session.name)
    host = session.host
    if host.is_local:
        if os.environ.get("TMUX"):
            return ["tmux", "switch-client", "-t", target]
        return ["tmux", "attach-session", "-t", target]
    if mode == "mosh" and shutil.which("mosh"):
        return ["mosh", host.target, "--", "tmux", "attach-session", "-t", target]
    return ["ssh", "-t", host.target, REMOTE_PATH + REMOTE_TERM_FIX + shlex.join(["tmux", "attach-session", "-t", target])]


def has_terminfo(term: str) -> bool:
    if not shutil.which("infocmp"):
        return True  # can't tell; leave $TERM alone
    return subprocess.run(["infocmp", term], capture_output=True).returncode == 0


def attach(session: Session, mode: str = "mosh") -> None:
    """Replace this process with the attach command."""
    argv = attach_argv(session, mode)
    term = os.environ.get("TERM", "")
    if session.host.is_local and term and not has_terminfo(term):
        os.environ["TERM"] = FALLBACK_TERM
    os.execvp(argv[0], argv)


def ago(ts: int, now: float | None = None) -> str:
    if not ts:
        return "-"
    secs = max(0, int((now or time.time()) - ts))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit}"
    return f"{secs}s"


def short_path(path: str) -> str:
    # Paths may come from another machine, so don't rely on the local $HOME.
    return re.sub(r"^/(?:home|Users)/[^/]+", "~", path)
