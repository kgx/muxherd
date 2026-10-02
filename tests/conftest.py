"""Test isolation: every test gets its own tmux server, registry and config.

Never let tests touch the real tmux server: people run live agents in it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from muxherd import config, registry, tmux


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    # Short path under /tmp: tmux socket paths must stay under ~104 bytes (macOS).
    root = Path(tempfile.mkdtemp(prefix="mh-", dir="/tmp"))
    monkeypatch.setenv("TMUX_TMPDIR", str(root))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(registry, "DB_PATH", root / "registry.db")
    monkeypatch.setattr(config, "CONFIG_PATH", root / "config.toml")  # absent: defaults
    yield root
    socket = root / f"tmux-{os.getuid()}" / "default"
    if socket.exists():
        subprocess.run(["tmux", "-S", str(socket), "kill-server"], capture_output=True)
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def local_host() -> tmux.Host:
    return tmux.Host(config.local_hostname(), "local")


@pytest.fixture
def projects(isolated) -> Path:
    path = isolated / "projects"
    (path / "alpha").mkdir(parents=True)
    (path / "beta").mkdir()
    return path


requires_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


def by_name(sessions, name):
    return next((s for s in sessions if s.name == name), None)
