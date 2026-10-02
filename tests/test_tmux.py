"""Integration tests against a private tmux server (see conftest.isolated)."""

import pytest
from conftest import by_name, requires_tmux

from muxherd import config, tmux

pytestmark = requires_tmux
AGENTS = config.DEFAULT_AGENTS


def test_create_list_kill_and_reopen(local_host, projects):
    s = tmux.create(local_host, "work", "shell", AGENTS, str(projects / "alpha"))
    listed = by_name(tmux.list_sessions(local_host), "work")
    assert listed.live and listed.agent == "shell" and listed.directory == str(projects / "alpha")

    tmux.kill_session(s)
    closed = by_name(tmux.list_sessions(local_host), "work")
    assert not closed.live and closed.directory == str(projects / "alpha")

    tmux.reopen(closed, AGENTS)
    assert by_name(tmux.list_sessions(local_host), "work").live


def test_missing_directory_is_reported_then_created(local_host, projects):
    missing = projects / "new" / "dir"
    with pytest.raises(tmux.MissingDirectory):
        tmux.create(local_host, "w", "shell", AGENTS, str(missing))
    tmux.create(local_host, "w", "shell", AGENTS, str(missing), create_dir=True)
    assert missing.is_dir()


def test_rename_live_and_closed_without_leftovers(local_host, projects):
    tmux.create(local_host, "a", "shell", AGENTS, str(projects))
    tmux.create(local_host, "b", "shell", AGENTS, str(projects))
    sessions = tmux.list_sessions(local_host)

    with pytest.raises(tmux.HostError):
        tmux.rename_session(by_name(sessions, "a"), "b")  # name taken
    tmux.rename_session(by_name(sessions, "a"), "a2")

    tmux.kill_session(by_name(sessions, "b"))
    tmux.rename_session(by_name(tmux.list_sessions(local_host), "b"), "b2")

    state = {s.name: s.live for s in tmux.list_sessions(local_host)}
    assert state == {"a2": True, "b2": False}


def test_chdir_changes_project_directory_not_the_running_pane(local_host, projects):
    s = tmux.create(local_host, "w", "shell", AGENTS, str(projects / "alpha"))
    tmux.chdir_session(s, str(projects / "beta"))
    after = by_name(tmux.list_sessions(local_host), "w")
    assert after.directory == str(projects / "beta")

    with pytest.raises(tmux.MissingDirectory):
        tmux.chdir_session(after, str(projects / "gamma"))
    tmux.chdir_session(after, str(projects / "gamma"), create_dir=True)
    assert by_name(tmux.list_sessions(local_host), "w").directory == str(projects / "gamma")


def test_throwaway_shell_is_its_own_session_and_vanishes_when_closed(local_host, projects):
    parent = tmux.create(local_host, "claude-api", "shell", AGENTS, str(projects / "alpha"))
    shell = tmux.open_shell_session(parent)
    assert shell.name.startswith("claude-api-") and shell.ephemeral

    listed = by_name(tmux.list_sessions(local_host), shell.name)
    assert listed.live and listed.ephemeral and listed.directory == str(projects / "alpha")

    tmux.kill_session(listed)
    assert by_name(tmux.list_sessions(local_host), shell.name) is None  # forgotten, not closed
