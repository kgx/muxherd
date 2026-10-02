import json

from muxherd import config, editor, tmux


def test_render_load_roundtrip():
    config.CONFIG_PATH.write_text(config.render({"devbox": "local", "laptop": "me@laptop"}, attach="ssh"))
    cfg = config.load()
    assert cfg.hosts == {"devbox": "local", "laptop": "me@laptop"}
    assert cfg.attach == "ssh"
    assert cfg.agents == config.DEFAULT_AGENTS
    assert cfg.editor == config.Editor()


def test_plain_string_agent_is_start_only():
    config.CONFIG_PATH.write_text('[agents]\nyolo = "claude --dangerously-skip-permissions"\n')
    assert config.load().agents == {"yolo": config.Agent("claude --dangerously-skip-permissions")}


def test_legacy_remote_editor_template_is_upgraded():
    config.CONFIG_PATH.write_text(f"[editor]\nremote = {json.dumps(config.LEGACY_REMOTE_EDITOR)}\n")
    assert config.load().editor.remote == config.Editor.remote


def test_vscode_remote_uri_encodes_host_and_path():
    uri = editor.vscode_remote_uri("me@devbox", "/home/me/my project")
    prefix = "vscode-remote://ssh-remote+"
    assert uri.startswith(prefix)
    authority, _, path = uri[len(prefix) :].partition("/")
    assert json.loads(bytes.fromhex(authority)) == {"hostName": "me@devbox"}
    assert path == "home/me/my%20project"


def test_editor_argv_uses_project_directory():
    remote = tmux.Host("devbox", "me@devbox")
    s = tmux.Session(remote, "x", path="/elsewhere", directory="/src/my app")
    argv = editor.editor_argv(s, config.Editor())
    assert argv[:3] == ["code", "-n", "--folder-uri"]
    assert argv[3].endswith("/src/my%20app")

    local = tmux.Session(tmux.Host("me", "local"), "x", directory="/src/my app")
    assert editor.editor_argv(local, config.Editor()) == ["code", "-n", "/src/my app"]
