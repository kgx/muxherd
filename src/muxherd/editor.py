"""Open a session's directory in an editor on this machine (VS Code Remote-SSH for remote hosts)."""

from __future__ import annotations

import shlex
import shutil
import subprocess

from .config import Editor
from .tmux import HostError, Session


def editor_argv(session: Session, editor: Editor) -> list[str]:
    path = (session.path if session.live else "") or session.directory or "~"
    template = editor.local if session.host.is_local else editor.remote
    # Substitute per argument so paths with spaces stay one argument.
    return [
        arg.replace("{path}", path).replace("{host}", session.host.target)
        for arg in shlex.split(template)
    ]


def open_editor(session: Session, editor: Editor) -> list[str]:
    argv = editor_argv(session, editor)
    if not shutil.which(argv[0]):
        raise HostError(f"{argv[0]!r} not found on this machine (set [editor] in the muxherd config)")
    # Detach so the editor outlives muxherd and its output doesn't scribble on the TUI.
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return argv
