"""Render docs/picker.svg: the picker with made-up demo sessions.

Runs against a private tmux server, registry and config (never your real sessions):

    uv run python docs/screenshot.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="mh-demo-", dir="/tmp"))
os.environ["TMUX_TMPDIR"] = str(ROOT)
os.environ.pop("TMUX", None)

from muxherd import config, registry, tmux  # noqa: E402  (env must be set first)
from muxherd.tui import MuxherdApp  # noqa: E402

registry.DB_PATH = ROOT / "registry.db"
config.CONFIG_PATH = ROOT / "config.toml"
config.CONFIG_PATH.write_text('[hosts]\ndevbox = "local"\n')
HOME = ROOT / "home"

CLAUDE_OUTPUT = """\
> add rate limiting to the /search endpoint

● I'll add a token-bucket limiter as middleware so it applies per API key.

● Read(api/routes/search.py)
  ⎿  Read 118 lines

● Update(api/middleware.py)
  ⎿  Added 24 lines: RateLimiter(rate=30/min, burst=10), keyed on X-Api-Key

● Bash(uv run pytest tests/test_search.py -q)
  ⎿  14 passed in 0.52s

● Done. /search now returns 429 with a Retry-After header once a key
  exceeds 30 requests/minute. Want me to make the limit configurable?
"""

# name, agent, project dir, idle seconds (None = closed session)
DEMO = [
    ("claude-api", "claude", "src/api", 4, CLAUDE_OUTPUT),
    ("claude-api-brave-otter", "shell", "src/api", 95, "$ git status -sb\n## main...origin/main [ahead 2]\n"),
    ("codex-web", "codex", "src/web", 720, "codex> refactor the settings page into tabs\n"),
    ("grok-docs", "grok", "src/docs", 3 * 3600, "grok> draft the v2 migration guide\n"),
    ("claude-infra", "claude", "src/infra", None, ""),
]


def setup() -> None:
    host = tmux.Host("devbox", "local")
    for name, agent, rel, _idle, output in DEMO:
        directory = HOME / rel
        directory.mkdir(parents=True, exist_ok=True)
        text = ROOT / f"{name}.txt"
        text.write_text(output)
        subprocess.run(
            ["tmux", "new-session", "-d", "-x", "100", "-y", "30", "-s", name, "-c", str(directory),
             f"cat {text}; exec sleep 86400",
             ";", "set-option", "-t", f"={name}:", "@muxherd_agent", agent,
             ";", "set-option", "-t", f"={name}:", "@muxherd_dir", str(directory),
             ";", "set-option", "-t", f"={name}:", "@muxherd_ephemeral", "1" if name.count("-") == 3 else ""],
            check=True,
        )  # fmt: skip
    tmux.list_sessions(host)  # record them
    for name, _agent, _rel, idle, _output in DEMO:
        if idle is None:
            subprocess.run(["tmux", "kill-session", "-t", f"={name}:"], check=True)
    time.sleep(0.3)


def main() -> None:
    setup()
    idle = {name: secs for name, _a, _r, secs, _o in DEMO}
    real_list_all = tmux.list_all

    def list_all(hosts):  # make "idle" times look like a real afternoon
        sessions, errors = real_list_all(hosts)
        now = time.time()
        for s in sessions:
            if s.live:
                s.activity = int(now - idle[s.name])
            else:
                s.closed_at = int(now - 26 * 60)
        return sessions, errors

    tmux.list_all = list_all
    real_short_path = tmux.short_path
    tmux.short_path = lambda p: real_short_path(p).replace(str(HOME), "~")

    async def shoot() -> None:
        app = MuxherdApp(config.load())
        async with app.run_test(size=(104, 30)) as pilot:
            await pilot.pause(2.5)
            app.save_screenshot("picker.svg", str(Path(__file__).parent))

    asyncio.run(shoot())


if __name__ == "__main__":
    try:
        main()
        print("wrote docs/picker.svg")
    finally:
        sock = ROOT / f"tmux-{os.getuid()}" / "default"
        subprocess.run(["tmux", "-S", str(sock), "kill-server"], capture_output=True)
        shutil.rmtree(ROOT, ignore_errors=True)
