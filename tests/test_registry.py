import sqlite3

from muxherd import registry


def live(name, created=100, **kw):
    s = {
        "name": name,
        "created": created,
        "path": "/p",
        "agent": "",
        "command": "bash",
        "agent_id": "",
        "directory": "",
    }
    s.update(kw)
    return s


AGENTS = {"claude", "codex", "shell"}


def rows():
    db = registry.connect()
    try:
        return {r["name"]: dict(r) for r in db.execute("select * from sessions")}
    finally:
        db.close()


def test_live_session_is_recorded_then_closed_when_it_vanishes():
    assert registry.sync([live("a", agent="claude", directory="/proj", agent_id="id1")], AGENTS) == []
    assert rows()["a"]["closed_at"] is None

    closed = registry.sync([], AGENTS)
    assert [c["name"] for c in closed] == ["a"]
    row = rows()["a"]
    assert row["closed_at"] == row["last_seen"]
    assert (row["agent"], row["directory"], row["agent_id"]) == ("claude", "/proj", "id1")


def test_session_coming_back_is_live_again():
    registry.sync([live("a")], AGENTS)
    registry.sync([], AGENTS)
    registry.sync([live("a", created=200)], AGENTS)
    assert rows()["a"]["closed_at"] is None


def test_ephemeral_session_is_forgotten_not_closed():
    registry.sync([live("sh", ephemeral=True)], AGENTS)
    assert registry.sync([], AGENTS) == []
    assert "sh" not in rows()


def test_unmanaged_session_remembers_agent_and_first_directory():
    registry.sync([live("u", command="claude", path="/first")], AGENTS)
    # Agent exits back to the shell and the shell cd's elsewhere: keep what we learned.
    registry.sync([live("u", command="bash", path="/elsewhere")], AGENTS)
    row = rows()["u"]
    assert (row["agent"], row["directory"]) == ("claude", "/first")


def test_new_incarnation_with_same_name_resets_learned_fields():
    registry.sync([live("u", command="claude", path="/first")], AGENTS)
    registry.sync([live("u", created=999, command="bash", path="/second")], AGENTS)
    row = rows()["u"]
    assert (row["agent"], row["directory"]) == ("", "/second")


def test_rename_forget_and_set_directory():
    registry.sync([live("a")], AGENTS)
    registry.rename("a", "b")
    registry.set_directory("b", "/new")
    assert rows()["b"]["directory"] == "/new"
    assert registry.names() == {"b"}

    assert not registry.forget("b")  # live sessions can't be forgotten
    registry.sync([], AGENTS)
    assert registry.forget("b")
    assert rows() == {}


def test_old_registry_without_ephemeral_column_is_migrated():
    registry.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(registry.DB_PATH)
    db.execute(
        "create table sessions (name text primary key, agent text not null default '', directory text not null"
        " default '', agent_id text not null default '', created integer not null default 0, last_seen integer"
        " not null default 0, closed_at integer)"
    )
    db.execute("insert into sessions values ('old', 'claude', '/x', 'id', 1, 2, 2)")
    db.commit()
    db.close()

    closed = registry.sync([], AGENTS)
    assert [c["name"] for c in closed] == ["old"]
    assert rows()["old"]["ephemeral"] == 0
