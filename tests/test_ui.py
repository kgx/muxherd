import asyncio

from conftest import requires_tmux
from textual.widgets import DataTable, Select

from muxherd import config, tmux
from muxherd.tui import MuxherdApp, SettingsScreen


def session(name, live=True, activity=0, closed_at=0):
    return tmux.Session(tmux.Host("devbox", "local"), name, live=live, activity=activity, closed_at=closed_at)


def names(sessions):
    return [s.name for s in sessions]


def test_sort_by_name_puts_live_first_then_closed():
    sessions = [
        session("zeta"),
        session("Alpha", live=False, closed_at=50),
        session("beta"),
        session("gamma", live=False, closed_at=90),
    ]
    assert names(tmux.sort_sessions(sessions, "name")) == ["beta", "zeta", "Alpha", "gamma"]


def test_sort_by_recent_uses_activity_then_close_time():
    sessions = [
        session("old", activity=10),
        session("new", activity=99),
        session("closed-early", live=False, closed_at=5),
        session("closed-late", live=False, closed_at=50),
    ]
    assert names(tmux.sort_sessions(sessions, "recent")) == ["new", "old", "closed-late", "closed-early"]


def test_save_ui_creates_then_updates_only_the_ui_table():
    config.save_ui(config.UI(sort="recent"))
    assert config.load().ui == config.UI(sort="recent")

    original = (
        '# my notes\n[hosts]\ndevbox = "local"  # keep me\n\n[ui]\n# why\nsort = "recent"\n\n[agents.x]\nstart = "x"\n'
    )
    config.CONFIG_PATH.write_text(original)
    config.save_ui(config.UI(sort="name", show_closed=False, preview=True))
    text = config.CONFIG_PATH.read_text()
    assert "# my notes" in text and "# keep me" in text and "# why" in text
    assert 'sort = "name"' in text and "show_closed = false" in text
    assert text.index("show_closed") < text.index("[agents.x]")  # stays inside [ui]
    cfg = config.load()
    assert cfg.ui == config.UI(sort="name", show_closed=False, preview=True)
    assert cfg.agents == {"x": config.Agent("x")}


def test_unknown_sort_falls_back_to_default():
    config.CONFIG_PATH.write_text('[ui]\nsort = "chaos"\n')
    assert config.load().ui.sort == "name"


@requires_tmux
def test_refresh_updates_cells_in_place_and_settings_resort(local_host, projects, monkeypatch):
    for i in range(40):
        tmux.create(local_host, f"proj-{i:02d}", "shell", config.DEFAULT_AGENTS, str(projects))

    clears = []
    real_clear = DataTable.clear
    monkeypatch.setattr(DataTable, "clear", lambda self, *a, **k: (clears.append(1), real_clear(self, *a, **k))[1])

    async def run():
        app = MuxherdApp(config.load())
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.pause(1.5)
            table = app.query_one(DataTable)
            assert table.row_count == 40
            first = len(clears)
            for _ in range(3):  # same sessions: refreshes must not rebuild the table
                app.action_refresh()
                await pilot.pause(0.6)
            assert len(clears) == first

            await pilot.press("ctrl+s")
            await pilot.pause(0.3)
            assert isinstance(app.screen, SettingsScreen)
            app.screen.query_one("#sort", Select).value = "recent"
            await pilot.press("ctrl+s")
            await pilot.pause(0.3)
            return app.config.ui.sort

    assert asyncio.run(run()) == "recent"
    assert config.load().ui.sort == "recent"
