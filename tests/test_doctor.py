import pytest
from conftest import requires_tmux

from muxherd import __version__, config, doctor, tmux

GOOD_MS = doctor.TMUX_LINES["Ms"]
# The override from before the fix: skips %p1, which newer ncurses rejects, so tmux sends nothing.
BROKEN_MS = "set -as terminal-overrides ',xterm*:Ms=\\E]52;c;%p2%s\\7'"


@pytest.fixture
def tmux_conf(isolated, monkeypatch):
    """Point tmux's default config lookup at a temp home and return a writer for it."""
    home = isolated / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))

    def write(*lines: str) -> None:
        (home / ".tmux.conf").write_text("\n".join(lines) + "\n")

    return write


def test_parse_version():
    assert doctor.parse_version("tmux 3.6") == (3, 6)
    assert doctor.parse_version("mosh-server (mosh 1.4.0) [build mosh 1.4.0]") == (1, 4, 0)
    assert doctor.parse_version("tmux next-3.7") == (3, 7)
    assert doctor.parse_version(None) is None


@requires_tmux
def test_probe_with_recommended_override_sends_clipboard_selection(tmux_conf):
    tmux_conf("set -g set-clipboard on", GOOD_MS, "set -g mouse on")
    facts = doctor.probe_tmux()
    assert facts["clipboard"] == {"sent": True, "selection": "c"}
    assert facts["tmux_options"]["mouse"] == "on"


@requires_tmux
def test_probe_detects_clipboard_turned_off(tmux_conf):
    tmux_conf("set -g set-clipboard off")
    facts = doctor.probe_tmux()
    assert facts["clipboard"]["sent"] is False
    assert facts["tmux_options"]["set-clipboard"] == "off"


@requires_tmux
def test_probe_without_config_reports_what_tmux_sends(tmux_conf):
    facts = doctor.probe_tmux()
    assert facts["clipboard"]["sent"] is True
    assert facts["clipboard"]["selection"] in ("", "c")


@requires_tmux
def test_probe_reports_config_errors(tmux_conf):
    tmux_conf("set -g no-such-option on")
    assert "tmux_error" in doctor.probe_tmux()


def _facts(**over):
    facts = {
        "version": __version__,
        "platform": "Linux x86_64",
        "tmux": "tmux 3.6",
        "mosh_server": "mosh-server (mosh 1.4.0)",
        "tmux_options": {"mouse": "on", "focus-events": "on", "history-limit": "50000", "set-clipboard": "on"},
        "clipboard": {"sent": True, "selection": "c"},
    }
    facts.update(over)
    return facts


def _judge(monkeypatch, facts):
    monkeypatch.setattr(doctor, "host_facts", lambda: facts)
    checks, missing = doctor.host_checks(tmux.Host("devbox", "local"), config.Config())
    return {c.name: c.status for c in checks}, missing


def test_healthy_host_needs_nothing(monkeypatch):
    statuses, missing = _judge(monkeypatch, _facts())
    assert set(statuses.values()) == {"ok"}
    assert missing == []


@pytest.mark.parametrize(
    "clipboard, opts, expected",
    [
        ({"sent": True, "selection": ""}, {}, "warn"),  # blank selection: mosh drops it
        ({"sent": False, "selection": None}, {}, "fail"),  # e.g. a broken Ms override
        ({"sent": False, "selection": None}, {"set-clipboard": "off"}, "fail"),
    ],
)
def test_clipboard_problems_suggest_the_tested_override(monkeypatch, clipboard, opts, expected):
    facts = _facts(clipboard=clipboard)
    facts["tmux_options"].update(opts)
    statuses, missing = _judge(monkeypatch, facts)
    assert statuses["clipboard"] == expected
    assert GOOD_MS in missing


def test_missing_settings_and_version_mismatch(monkeypatch):
    facts = _facts(version="0.0.1")
    facts["tmux_options"].update({"mouse": "off", "history-limit": "2000"})
    statuses, missing = _judge(monkeypatch, facts)
    assert statuses["muxherd"] == statuses["mouse"] == statuses["history-limit"] == "warn"
    assert doctor.TMUX_LINES["mouse"] in missing and doctor.TMUX_LINES["history-limit"] in missing


def test_missing_tmux_fails(monkeypatch):
    statuses, _ = _judge(monkeypatch, _facts(tmux=None))
    assert statuses["tmux"] == "fail"
