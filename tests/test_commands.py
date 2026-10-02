import uuid

from muxherd import tmux
from muxherd.config import Agent
from muxherd.tui import matches, unique_name

CLAUDE = Agent("claude --session-id {id}", "claude --resume {id}")


def test_start_command_fills_a_fresh_uuid():
    command, agent_id = tmux.start_command(CLAUDE)
    assert command == f"claude --session-id {agent_id}"
    uuid.UUID(agent_id)
    assert tmux.start_command(Agent("grok")) == ("grok", "")


def test_resume_uses_stored_id():
    assert tmux.resume_command(CLAUDE, "abc") == ("claude --resume abc", "abc")


def test_resume_without_id_or_resume_command_starts_fresh():
    command, agent_id = tmux.resume_command(CLAUDE, "")
    assert command.startswith("claude --session-id ") and agent_id
    assert tmux.resume_command(Agent("grok"), "abc") == ("grok", "")
    assert tmux.resume_command(Agent("codex", "codex resume --last"), "") == ("codex resume --last", "")


def test_resumable_check_falls_back_to_start_with_same_id(local_host, isolated):
    marker = isolated / "conversations"
    marker.mkdir()
    agent = Agent("claude --session-id {id}", "claude --resume {id}", f"test -e {marker}/{{id}}")

    assert tmux.resume_command(agent, "abc", local_host) == ("claude --session-id abc", "abc")
    (marker / "abc").touch()
    assert tmux.resume_command(agent, "abc", local_host) == ("claude --resume abc", "abc")


def test_names():
    assert tmux.sanitize("my.project: v2") == "my-project-v2"
    assert unique_name("claude-api", {"claude-api", "claude-api-2"}) == "claude-api-3"
    name = tmux.funny_name("claude-api", set())
    assert name.startswith("claude-api-") and name.count("-") == 3


def test_short_path_and_ago():
    assert tmux.short_path("/home/alice/src/x") == "~/src/x"
    assert tmux.short_path("/Users/bob") == "~"
    assert tmux.short_path("/srv/x") == "/srv/x"
    assert tmux.ago(1000, now=1000 + 90) == "1m"
    assert tmux.ago(0) == "-"


def test_filter_matches_all_terms(local_host):
    s = tmux.Session(local_host, "claude-api", agent="claude", path="/src/api", directory="/src/api")
    assert matches(s, "claude api")
    assert matches(s, "live")
    assert not matches(s, "codex")
