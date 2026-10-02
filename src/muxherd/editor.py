"""Open a session's directory in an editor on this machine (VS Code Remote-SSH for remote hosts)."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from urllib.parse import quote

from .config import Editor
from .tmux import HostError, Session


def vscode_remote_uri(target: str, path: str) -> str:
    """vscode-remote:// folder URI for Remote-SSH.

    The host goes in Remote-SSH's hex-encoded JSON form, which survives targets like
    user@host or host:port that would otherwise be misread as URI syntax.
    """
    authority = json.dumps({"hostName": target}, separators=(",", ":")).encode().hex()
    return f"vscode-remote://ssh-remote+{authority}{quote(path)}"


def editor_argv(session: Session, editor: Editor) -> list[str]:
    path = (session.path if session.live else "") or session.directory or "~"
    template = editor.local if session.host.is_local else editor.remote
    values = {
        "{path}": path,
        "{host}": session.host.target,
        "{uri}": vscode_remote_uri(session.host.target, path),
    }
    # Substitute per argument so paths with spaces stay one argument.
    argv = []
    for arg in shlex.split(template):
        for key, value in values.items():
            arg = arg.replace(key, value)
        argv.append(arg)
    return argv


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
