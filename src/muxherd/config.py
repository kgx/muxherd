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


def local_hostname() -> str:
    return socket.gethostname().split(".")[0]


@dataclass
class Config:
    # host name -> ssh target, or "local" for this machine
    hosts: dict[str, str] = field(default_factory=lambda: {local_hostname(): "local"})
    agents: dict[str, Agent] = field(default_factory=lambda: dict(DEFAULT_AGENTS))
    # how to attach to remote sessions: "mosh" or "ssh"
    attach: str = "mosh"


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
    return cfg


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
