"""Config loading: ~/.config/muxherd/config.toml"""

from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path(
    os.environ.get("MUXHERD_CONFIG")
    or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "muxherd" / "config.toml"
)

DEFAULT_AGENTS = {
    "claude": "claude",
    "codex": "codex",
    "grok": "grok",
    "shell": "",
}


def local_hostname() -> str:
    return socket.gethostname().split(".")[0]


@dataclass
class Config:
    # host name -> ssh target, or "local" for this machine
    hosts: dict[str, str] = field(default_factory=lambda: {local_hostname(): "local"})
    # agent name -> command typed into the new session's shell ("" = plain shell)
    agents: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_AGENTS))
    # how to attach to remote sessions: "mosh" or "ssh"
    attach: str = "mosh"


def load() -> Config:
    if not CONFIG_PATH.exists():
        return Config()
    data = tomllib.loads(CONFIG_PATH.read_text())
    cfg = Config()
    if hosts := data.get("hosts"):
        cfg.hosts = {str(k): str(v) for k, v in hosts.items()}
    if agents := data.get("agents"):
        cfg.agents = {str(k): str(v) for k, v in agents.items()}
    cfg.attach = data.get("attach", cfg.attach)
    return cfg


def render(hosts: dict[str, str], attach: str = "mosh") -> str:
    lines = [
        "# muxherd config",
        "",
        '# How to attach to sessions on remote hosts: "mosh" or "ssh"',
        f'attach = "{attach}"',
        "",
        "# Hosts whose tmux sessions muxherd shows.",
        '# name = "local" for this machine, otherwise an ssh target (tailnet name, user@host, ssh alias)',
        "[hosts]",
        *(f'{name} = "{target}"' for name, target in hosts.items()),
        "",
        "# Agents offered when creating a session: name = command run in the session's shell",
        "[agents]",
        *(f'{name} = "{cmd}"' for name, cmd in DEFAULT_AGENTS.items()),
        "",
    ]
    return "\n".join(lines)
