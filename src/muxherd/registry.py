"""Per-host session registry (SQLite), so closed sessions can be listed and reopened.

The registry lives on the machine that runs the tmux sessions. Clients reach a remote
host's registry by running `muxherd _host ...` there over ssh.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(
    os.environ.get("MUXHERD_REGISTRY")
    or Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "muxherd" / "registry.db"
)

SCHEMA = """
create table if not exists sessions (
    name       text primary key,
    agent      text not null default '',
    directory  text not null default '',
    agent_id   text not null default '',  -- e.g. Claude Code --session-id, used to resume
    created    integer not null default 0,  -- tmux session_created of the latest incarnation
    last_seen  integer not null default 0,
    closed_at  integer,                   -- null while the tmux session is alive
    ephemeral  integer not null default 0  -- throwaway shell: deleted instead of closed
)
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=5, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=wal")
    db.execute(SCHEMA)
    columns = {r["name"] for r in db.execute("pragma table_info(sessions)")}
    if "ephemeral" not in columns:  # registries created before throwaway shells
        db.execute("alter table sessions add column ephemeral integer not null default 0")
    return db


def sync(live: list[dict], known_agents: set[str]) -> list[dict]:
    """Record the live tmux sessions and mark vanished ones closed.

    `live` holds dicts from tmux (name, created, path, agent, command, agent_id, directory).
    Returns the closed sessions as dicts.
    """
    now = int(time.time())
    db = connect()
    try:
        db.execute("begin immediate")
        existing = {r["name"]: r for r in db.execute("select * from sessions")}
        for s in live:
            old = existing.get(s["name"])
            same = old is not None and old["created"] == s["created"]
            # Sessions muxherd started carry their agent/dir/id as tmux options. For others,
            # remember the agent if one is running now, and keep the first directory seen.
            agent = s["agent"] or (s["command"] if s["command"] in known_agents else "")
            if not agent and same:
                agent = old["agent"]
            directory = s["directory"] or (old["directory"] if same and old["directory"] else s["path"])
            agent_id = s["agent_id"] or (old["agent_id"] if same else "")
            db.execute(
                """insert into sessions (name, agent, directory, agent_id, created, last_seen, closed_at, ephemeral)
                   values (?, ?, ?, ?, ?, ?, null, ?)
                   on conflict(name) do update set
                     agent = excluded.agent, directory = excluded.directory, agent_id = excluded.agent_id,
                     created = excluded.created, last_seen = excluded.last_seen, closed_at = null,
                     ephemeral = excluded.ephemeral""",
                (s["name"], agent, directory, agent_id, s["created"], now, int(s.get("ephemeral", False))),
            )
        live_names = {s["name"] for s in live}
        for name, row in existing.items():
            if name not in live_names and row["closed_at"] is None:
                if row["ephemeral"]:
                    db.execute("delete from sessions where name = ?", (name,))  # throwaway shell ended
                else:
                    # We only know it was alive when last seen; that's the best close time we have.
                    db.execute("update sessions set closed_at = last_seen where name = ?", (name,))
        db.execute("commit")
        return [dict(r) for r in db.execute("select * from sessions where closed_at is not null")]
    except BaseException:
        db.execute("rollback")
        raise
    finally:
        db.close()


def names() -> set[str]:
    db = connect()
    try:
        return {r["name"] for r in db.execute("select name from sessions")}
    finally:
        db.close()


def rename(old: str, new: str) -> None:
    db = connect()
    try:
        db.execute("update sessions set name = ? where name = ?", (new, old))
    finally:
        db.close()


def forget(name: str) -> bool:
    db = connect()
    try:
        return db.execute("delete from sessions where name = ? and closed_at is not null", (name,)).rowcount > 0
    finally:
        db.close()
