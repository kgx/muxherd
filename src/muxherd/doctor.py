"""`mh doctor`: check this machine and every host, and say how to fix what's off.

Host-side facts are gathered on the host itself (`muxherd _host doctor`, JSON) and judged
here on the client, so one client version decides what "good" means. Nothing is changed.
"""

from __future__ import annotations

import fcntl
import json
import os
import platform
import pty
import re
import secrets
import select
import shlex
import shutil
import struct
import subprocess
import tempfile
import termios
import time
from dataclasses import dataclass
from pathlib import Path

from . import __version__, config, registry, tmux
from .tmux import Host, HostError

MIN_TMUX = (3, 0)
MIN_MOSH = (1, 4)  # OSC 52 clipboard forwarding
MIN_HISTORY = 10000

# Tested settings, printed as fixes. The Ms override names the system clipboard ("c")
# because tmux leaves the selection blank, which doesn't reach the clipboard through
# mosh; %p1%.0s consumes tmux's first parameter, since ncurses rejects formats that skip
# it and tmux then silently sends nothing.
TMUX_LINES = {
    "mouse": "set -g mouse on",
    "focus-events": "set -g focus-events on",
    "history-limit": "set -g history-limit 50000",
    "set-clipboard": "set -g set-clipboard on",
    "Ms": "set -as terminal-overrides ',xterm*:Ms=\\E]52;c%p1%.0s;%p2%s\\7'",
}


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail | skip
    detail: str = ""
    fix: str = ""


def parse_version(text: str | None) -> tuple[int, ...] | None:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(g) for g in match.groups() if g is not None) if match else None


def _first_line(argv: list[str]) -> str | None:
    if not shutil.which(argv[0]):
        return None
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (p.stdout or p.stderr).strip().splitlines()
    return out[0] if out else ""


# ----- host side (runs on the machine that owns the sessions) -----


def host_facts() -> dict:
    facts: dict = {
        "version": __version__,
        "platform": f"{platform.system()} {platform.machine()}",
        "tmux": _first_line(["tmux", "-V"]),
        "mosh_server": _first_line(["mosh-server", "--version"]),
    }
    try:
        registry.connect().close()
        facts["registry"] = str(registry.DB_PATH)
    except Exception as e:  # noqa: BLE001 - reported, not raised
        facts["registry_error"] = str(e)
    if facts["tmux"]:
        facts.update(probe_tmux())
    return facts


def probe_tmux(timeout: float = 4.0) -> dict:
    """Start a private tmux server with the user's normal config and see what it does.

    Reports config errors, the effective options, and what tmux sends to a terminal when
    text is copied: a client is attached through a pseudo-terminal as TERM=xterm-256color
    (what mosh presents), text is copied in copy mode like a mouse drag does, and the
    client's output is searched for OSC 52.
    """
    root = Path(tempfile.mkdtemp(prefix="mh-doc-", dir="/tmp"))  # short: socket path limits
    sock = str(root / "s")
    tmux_cmd = ["tmux", "-S", sock]
    result: dict = {"tmux_options": {}, "clipboard": None}
    pid, fd = -1, -1
    try:
        p = subprocess.run(
            [*tmux_cmd, "new-session", "-d", "-s", "probe", "-x", "80", "-y", "24", "echo muxherd-doctor; sleep 60"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if p.returncode != 0:
            result["tmux_error"] = (p.stderr or p.stdout).strip()
            return result
        # tmux starts even with a broken config (errors only show inside the session), so
        # load it again explicitly to surface them.
        conf = _tmux_conf_path()
        if conf:
            p = subprocess.run([*tmux_cmd, "source-file", str(conf)], capture_output=True, text=True)
            if p.returncode != 0:
                result["tmux_error"] = f"{conf}: {(p.stderr or p.stdout).strip()}"
                return result
        for name in ("mouse", "focus-events", "history-limit", "set-clipboard"):
            value = ""
            for scope in ("-gqv", "-sqv"):
                value = subprocess.run([*tmux_cmd, "show", scope, name], capture_output=True, text=True).stdout.strip()
                if value:
                    break
            result["tmux_options"][name] = value

        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        env["TERM"] = "xterm-256color"
        pid, fd = pty.fork()
        if pid == 0:  # child: become the tmux client
            os.execvpe("tmux", [*tmux_cmd, "attach-session", "-t", "probe"], env)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))

        output = bytearray()
        deadline = time.monotonic() + timeout
        client = ""
        while time.monotonic() < deadline and not client:
            output += _drain(fd, 0.2)
            listed = subprocess.run([*tmux_cmd, "list-clients", "-F", "#{client_name}"], capture_output=True, text=True)
            client = listed.stdout.strip().splitlines()[0] if listed.stdout.strip() else ""
        if not client:
            result["clipboard"] = {"error": "could not attach a test client"}
            return result
        # Copy a line in copy mode, the way a mouse drag ends (this honours set-clipboard;
        # `set-buffer -w` would always send).
        for args in (["copy-mode"], ["send", "-X", "history-top"], ["send", "-X", "begin-selection"],
                     ["send", "-X", "end-of-line"], ["send", "-X", "copy-selection-no-clear"]):  # fmt: skip
            subprocess.run([*tmux_cmd, *args, "-t", "probe"] if args[0] == "copy-mode" else
                           [*tmux_cmd, args[0], "-t", "probe", *args[1:]], capture_output=True)  # fmt: skip
        output += _drain(fd, 0.8)

        match = re.search(rb"\x1b\]52;([^;]*);", bytes(output))
        result["clipboard"] = {"sent": match is not None, "selection": match.group(1).decode() if match else None}
        return result
    except (OSError, subprocess.TimeoutExpired) as e:
        result["tmux_error"] = str(e)
        return result
    finally:
        subprocess.run([*tmux_cmd, "kill-server"], capture_output=True)
        if pid > 0:
            try:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
            except OSError:
                pass
        if fd >= 0:
            os.close(fd)
        shutil.rmtree(root, ignore_errors=True)


def _tmux_conf_path() -> Path | None:
    """The config file tmux loads by default, if any."""
    home = Path(os.environ.get("HOME", Path.home()))
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    for path in (home / ".tmux.conf", xdg / "tmux" / "tmux.conf", home / ".config" / "tmux" / "tmux.conf"):
        if path.is_file():
            return path
    return None


def _drain(fd: int, seconds: float) -> bytes:
    out = bytearray()
    end = time.monotonic() + seconds
    while (left := end - time.monotonic()) > 0:
        ready, _, _ = select.select([fd], [], [], left)
        if not ready:
            break
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
    return bytes(out)


# ----- client side -----


def client_checks(cfg: config.Config) -> list[Check]:
    hosts = [Host(n, t) for n, t in cfg.hosts.items()]
    remote = any(not h.is_local for h in hosts)
    checks = []

    if remote:
        ssh = _first_line(["ssh", "-V"])
        checks.append(
            Check("ssh", "ok", ssh) if ssh is not None else Check("ssh", "fail", "not found", "install OpenSSH")
        )
        if cfg.attach == "mosh":
            checks.append(_mosh_check(_first_line(["mosh", "--version"]), "mosh", "on this machine"))
        else:
            checks.append(Check("mosh", "skip", 'attach = "ssh" in config'))

    template = cfg.editor.remote if remote else cfg.editor.local
    editor_cmd = shlex.split(template)[0] if template.strip() else ""
    if editor_cmd and shutil.which(editor_cmd):
        checks.append(Check("editor", "ok", editor_cmd))
    elif editor_cmd:
        hint = "VS Code: command palette → \"Shell Command: Install 'code' command in PATH\""
        fix = hint if editor_cmd == "code" else f"install {editor_cmd}"
        checks.append(
            Check("editor", "warn", f"{editor_cmd!r} not found (ctrl+e won't work)", fix + ", or change [editor]")
        )

    term = os.environ.get("TERM", "")
    if term and not tmux.has_terminfo(term):
        checks.append(
            Check("terminal", "warn", f"no terminfo for TERM={term} here", "muxherd falls back to xterm-256color")
        )
    elif term:
        checks.append(Check("terminal", "ok", term))
    return checks


def _mosh_check(line: str | None, name: str, where: str) -> Check:
    if line is None:
        return Check(name, "warn", f"not installed {where}", 'install mosh 1.4+ (or set attach = "ssh")')
    version = parse_version(line)
    if version and version < MIN_MOSH:
        return Check(name, "warn", f"{line}: clipboard needs 1.4+", "upgrade mosh to 1.4 or newer")
    return Check(name, "ok", ".".join(map(str, version)) if version else line)


def host_checks(host: Host, cfg: config.Config) -> tuple[list[Check], list[str]]:
    """Checks for one host, plus tmux.conf lines that would fix what's missing."""
    checks: list[Check] = []
    started = time.monotonic()
    if host.is_local:
        facts = host_facts()
    else:
        try:
            p = host.run(["muxherd", "_host", "doctor"], timeout=30)
        except HostError as e:
            return [Check("reachable", "fail", str(e))], []
        elapsed = time.monotonic() - started
        if p.returncode == 255:
            return [Check("reachable", "fail", p.stderr.strip().splitlines()[-1] if p.stderr.strip() else "ssh failed",
                          "check `ssh <host> true` works without a prompt")], []  # fmt: skip
        if p.returncode == 127:
            return [Check("reachable", "ok"), Check("muxherd", "fail", "not installed on host",
                          "install it there: uv tool install muxherd")], []  # fmt: skip
        if p.returncode != 0:
            other = host.run(["muxherd", "--version"]).stdout.strip() or "unknown version"
            return [Check("reachable", "ok"), Check("muxherd", "fail", f"{other} on host has no doctor",
                          f"upgrade muxherd on {host.name} to match {__version__}")], []  # fmt: skip
        facts = json.loads(p.stdout)
        status = "warn" if elapsed > 3 else "ok"
        fix = "slow ssh: check ssh config (e.g. Match exec probes) and the network" if status == "warn" else ""
        checks.append(Check("reachable", status, f"{elapsed:.1f}s", fix))

    if facts.get("version") == __version__:
        checks.append(Check("muxherd", "ok", f"{facts['version']} ({facts.get('platform', '')})"))
    else:
        checks.append(
            Check(
                "muxherd", "warn", f"{facts.get('version')} on host, {__version__} here", "use the same version on both"
            )
        )
    if facts.get("registry_error"):
        checks.append(Check("registry", "fail", facts["registry_error"]))

    tmux_version = parse_version(facts.get("tmux"))
    if not facts.get("tmux"):
        checks.append(Check("tmux", "fail", "not installed", "install tmux 3.x"))
        return checks, []
    if tmux_version and tmux_version < MIN_TMUX:
        checks.append(Check("tmux", "warn", f"{facts['tmux']}: 3.0+ recommended", "upgrade tmux"))
    else:
        checks.append(Check("tmux", "ok", facts["tmux"]))

    if not host.is_local:
        if cfg.attach == "mosh":
            checks.append(_mosh_check(facts.get("mosh_server"), "mosh-server", "on host"))
    if facts.get("tmux_error"):
        checks.append(Check("tmux.conf", "fail", facts["tmux_error"], "fix the error in the host's tmux config"))
        return checks, []

    missing: list[str] = []
    opts = facts.get("tmux_options", {})
    for name, label, why in (
        ("mouse", "mouse", "touchpad scrolling and drag-to-copy"),
        ("focus-events", "focus-events", "agents can tell when you switch to them"),
    ):
        if opts.get(name) == "on":
            checks.append(Check(label, "ok", "on"))
        else:
            checks.append(Check(label, "warn", f"{opts.get(name) or 'off'} ({why})"))
            missing.append(TMUX_LINES[name])
    history = int(opts.get("history-limit") or 0)
    if history >= MIN_HISTORY:
        checks.append(Check("history-limit", "ok", str(history)))
    else:
        checks.append(Check("history-limit", "warn", f"{history} lines (agents are chatty)"))
        missing.append(TMUX_LINES["history-limit"])

    clip = facts.get("clipboard") or {}
    if clip.get("error"):
        checks.append(Check("clipboard", "warn", f"couldn't test: {clip['error']}"))
    elif opts.get("set-clipboard") == "off":
        checks.append(Check("clipboard", "fail", "set-clipboard off: copies stay inside tmux"))
        missing += [TMUX_LINES["set-clipboard"], TMUX_LINES["Ms"]]
    elif not clip.get("sent"):
        checks.append(
            Check(
                "clipboard",
                "fail",
                "tmux sends nothing on copy (a broken terminal-overrides Ms entry?)",
                "replace any Ms override with the line below",
            )  # fmt: skip
        )
        missing.append(TMUX_LINES["Ms"])
    elif clip.get("selection") == "c":
        checks.append(Check("clipboard", "ok", "copies are sent to your terminal's clipboard (OSC 52)"))
    else:
        checks.append(Check("clipboard", "warn", "tmux sends copies with a blank selection; mosh drops these"))
        missing.append(TMUX_LINES["Ms"])
    return checks, missing


# ----- interactive clipboard test -----


def read_local_clipboard() -> str | None:
    for argv in (["pbpaste"], ["wl-paste", "-n"], ["xclip", "-o", "-selection", "clipboard"], ["xsel", "-b", "-o"]):
        if shutil.which(argv[0]):
            p = subprocess.run(argv, capture_output=True, text=True, timeout=5)
            if p.returncode == 0:
                return p.stdout
    return None


def clipboard_roundtrip(host: Host, cfg: config.Config) -> Check:
    """Attach to a throwaway session on `host` that copies a token, then check our clipboard."""
    if host.is_local and os.environ.get("TMUX"):
        return Check("clipboard (live)", "skip", "run `mh doctor --clipboard` outside tmux to test this machine")
    token = f"muxherd clipboard test {secrets.token_hex(3)}"
    script = (
        'until [ "$(tmux display -p "#{session_attached}")" != 0 ]; do sleep 0.2; done; sleep 0.5; '
        f"tmux set-buffer -w {shlex.quote(token)}; "
        'printf "\\n\\nmuxherd sent a test string to your clipboard. Press Enter to finish. "; read _; exit'
    )
    name = f"muxherd-clipboard-{secrets.token_hex(2)}"
    session = tmux.new_session(host, name, "shell", script, "~", ephemeral=True)
    try:
        argv = tmux.attach_argv(session, cfg.attach)
        subprocess.run(argv, env=tmux.attach_env(session))
    finally:
        try:
            tmux.kill_session(session)
        except HostError:
            pass  # already gone: the script exits on Enter
    pasted = read_local_clipboard()
    if pasted is None:
        answer = input("Paste here (Cmd-V / Ctrl-Shift-V) and press Enter, or just Enter if nothing came:\n> ")
        pasted = answer
    if token in (pasted or ""):
        return Check("clipboard (live)", "ok", "a copy in tmux on the host reached this machine's clipboard")
    return Check(
        "clipboard (live)",
        "fail",
        "the copy didn't reach this machine's clipboard",
        "if the tmux clipboard check is ok: check mosh ≥ 1.4 on both ends, and that your terminal allows OSC 52 "
        "writes (e.g. kitty: clipboard_control write-clipboard)",
    )
