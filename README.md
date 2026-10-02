# muxherd

Herd your AI coding agents — Claude Code, Codex, Grok Build — running in named tmux
sessions on one or more machines, and jump between them from anywhere on your tailnet.

```
 muxherd  2 live · 1 closed   excelsior ✓
 type to filter sessions…
    host       session          agent   idle  dir
 ●  excelsior  claude-infra     claude  4s    ~/develop/infra
 ○  excelsior  codex-api        codex   12m   ~/develop/api
 ✕  excelsior  grok-site        grok    1h    ~/develop/site
──────────────────────────────────────────────────────────────
 (live preview of the selected session's pane)
 ^e editor  ^o shell  ^r rename  ⏎ attach  ^x kill/forget  esc clear/quit  ^n new  ^t closed  ^p preview
```

- **One picker for every host.** muxherd lists tmux sessions on this machine and on any
  host you can reach over ssh, polling every 2 seconds.
- **Agents start in a login shell.** The agent command is typed into a normal shell, so
  it gets your full environment, and quitting the agent leaves you at a prompt instead
  of losing the session.
- **mosh for remote attach.** A laptop on flaky Wi-Fi stays connected, and the tmux
  session survives either way.
- **Closed sessions are remembered.** Every session is recorded in a small SQLite
  registry on the host that runs it. When a session ends, it stays in the list marked ✕,
  and Enter reopens it with the same name and directory. For Claude Code, that resumes
  the same conversation.
- **Nothing runs in the background.** muxherd talks to `tmux` directly, or over `ssh`
  for remote hosts, reusing one ssh connection per host via ControlMaster.

## Install

```sh
uv tool install git+ssh://git@github.com/kgx/muxherd
# from a clone (standalone copy, not linked to the repo):
make install      # make update = git pull + reinstall; plain `make` lists targets
# dev checkout (edits take effect immediately):
uv tool install -e ~/develop/muxherd
```

This installs `muxherd` and the short alias `mh`.

## Setup

**Host machine** (where the agents run, e.g. `excelsior`):

```sh
sudo apt install tmux mosh      # macOS: brew install tmux mosh
mh init                         # config with this machine as "local"
```

**Client laptops:**

```sh
brew install mosh               # or apt install mosh
mh init --no-local -r excelsior # only show the remote host's sessions
# or keep local sessions too:   mh init -r excelsior
mh hosts                        # check connectivity
```

`-r NAME` uses `NAME` as the ssh target (with MagicDNS that's the tailnet hostname).
Use `-r NAME=user@target` or an `~/.ssh/config` alias when they differ.

Remote hosts need **non-interactive ssh** (key auth, or Tailscale SSH) because muxherd
polls them with `ssh -o BatchMode=yes`. Make sure `ssh excelsior true` works without
a prompt.

### Keeping it on the tailnet

muxherd only ever talks to the targets in your config, so the tailnet boundary comes
from the host's firewall. On the host:

```sh
sudo ufw default deny incoming
sudo ufw allow in on tailscale0                       # ssh + mosh only via tailnet
sudo ufw enable
```

mosh starts with an ssh login and then uses UDP 60000–61000, which the `tailscale0` rule
covers. To keep sshd off other interfaces entirely, set
`ListenAddress <tailscale-ip>` in `sshd_config`, or turn on Tailscale SSH
(`tailscale up --ssh`) and close port 22.

## Usage

```sh
mh                       # picker
mh a infra               # attach (or reopen if closed): name, host:name, or unique substring

mh code infra            # open the session's directory in VS Code (no name: picker)
mh sh infra              # throwaway shell (own tmux session) in the session's project dir
mh new -a codex          # new codex session in cwd, named codex-<dir>, then attach
mh new api -a claude -H excelsior -d ~/develop/api -D   # create detached on a host
mh ls                    # list everything, closed sessions marked ✕ (--live to hide them)
mh kill excelsior:api    # kill (asks first; -y to skip); it stays listed as closed
mh forget api            # remove a closed session from the registry
mh rename api api-v2     # rename (live or closed; host:name works too)
mh hosts                 # reachability check
```

### Picker keys

| key            | action                                                     |
| -------------- | ---------------------------------------------------------- |
| type           | filter (space-separated terms match host, name, agent, dir) |
| ↑ ↓ PgUp PgDn  | move                                                       |
| ⏎              | attach, or reopen a closed session                         |
| ctrl+n         | new session (agent, host, directory, name); see below      |
| ctrl+r         | rename the selected session (live or closed)               |
| ctrl+x         | kill a live session / forget a closed one (with confirm)   |
| ctrl+t         | show/hide closed sessions                                  |
| ctrl+e         | open the session's directory in your editor                |
| ctrl+o         | throwaway shell (own tmux session) in the project directory |
| ctrl+p         | toggle preview pane                                        |
| F5             | refresh now (it also refreshes every 2s)                   |
| esc            | clear filter, then quit                                    |

In the new-session dialog, the directory field browses the chosen host, over ssh for
remote hosts. It starts out listing directories you've recently used there. As you type,
it lists matching subdirectories. **↑↓** pick one, **Tab** (or **→** at the end of the
line) steps into it, **Enter** on a picked entry takes it, and Enter again creates the
session. Hidden folders appear once you type a leading `.`.

How attach works:
- **Local session, run outside tmux:** `tmux attach`.
- **Local session, run inside tmux:** `tmux switch-client`, so tmux never nests.
- **Remote session:** `mosh <host> -- tmux attach`, falling back to `ssh -t` when mosh
  isn't installed or the config sets `attach = "ssh"`.

## Throwaway shells

`ctrl+o` in the picker, or `mh sh [name]`, opens a shell in the session's project
directory (where it was started) as **its own tmux session** on the same host. It gets a
generated name next to its parent, e.g. `claude-infra-brave-otter`, and you're attached
to it right away.

- If you get disconnected, it's still there: reattach from the picker like any session.
- `exit` ends it, and the registry forgets it instead of listing it as closed, so shells
  don't pile up.
- The agent's session is untouched (a window in the agent's own session would switch
  every attached client to it).

## Editor

`ctrl+e` in the picker, or `mh code [name]`, opens the session's current directory in
an editor **on the machine you're using**. For sessions on another host, it uses VS Code's
[Remote - SSH](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-ssh)
over the same tailnet ssh connection:

```sh
code -n --remote ssh-remote+excelsior /home/kgx/develop/infra
```

Each laptop needs the Remote - SSH extension and the `code` command on PATH (macOS: VS Code
command palette → "Shell Command: Install 'code' command in PATH"). The first time, VS Code
installs its server on the host automatically. To use another editor, change `[editor]`
in the config, e.g. Zed: `remote = "zed ssh://{host}{path}"`.

## Versioning

Versions follow semver and come from git tags (`vX.Y.Z`) via `hatch-vcs`, with no version
string in the source. A build of a tagged commit is that version. Later commits build as
dev versions like `0.2.1.dev3+g1a2b3c4` (3 commits past `v0.2.0`), so `mh --version`
shows exactly what a machine is running.

```sh
make version              # what this checkout builds as
make release VERSION=0.2.0  # tag, push and create a GitHub release (clean tree required)
```

## Config

`~/.config/muxherd/config.toml` (override with `$MUXHERD_CONFIG`):

```toml
attach = "mosh"            # or "ssh"

[hosts]
excelsior = "local"        # this machine
# venture = "kgx@venture"  # any ssh target

[editor]                   # {path} = session dir, {host} = host's ssh target
local = "code -n {path}"
remote = "code -n --remote ssh-remote+{host} {path}"

[agents.claude]
start = "claude --session-id {id}"   # typed into the new session's shell
resume = "claude --resume {id}"      # typed when reopening a closed session
resumable = "ls ~/.claude/projects/*/{id}.jsonl >/dev/null 2>&1"  # check run before resuming

[agents.codex]
start = "codex"
resume = "codex resume --last"

[agents.grok]
start = "grok"                       # no resume: reopening starts it fresh

[agents.shell]
start = ""
```

`{id}` becomes a fresh UUID when a session starts. muxherd stores it, and `resume` uses
it to pick up the same conversation. If an agent has no `resume`, or the session has no
stored ID, reopening runs `start` instead. `resumable` is an optional shell check that
runs on the session's host first. If it fails, reopening runs `start` with the same ID.
Claude Code only saves a conversation once you send a message, so this covers a session
where you never typed anything. A plain string (`yolo = "claude --dangerously-skip-permissions"`)
is shorthand for `start` only.

## Session registry

Each host keeps a registry at `~/.local/state/muxherd/registry.db` (SQLite; override
with `$MUXHERD_REGISTRY`). It's updated whenever muxherd lists that host's sessions:
live sessions are recorded, and sessions that disappeared are marked closed.
Sessions started outside muxherd (plain `tmux new -s`) are recorded too.

Clients read a remote host's registry with `ssh <host> muxherd _host sync`, so
**install muxherd on every host that runs sessions**. Without it, muxherd falls back
to plain tmux on that host and only shows live sessions.

A session counts as closed from the last time muxherd saw it alive. If no picker was
open when it ended, the close time is approximate.

The agent column shows the agent muxherd launched the session with, which it stores in
the `@muxherd_agent` tmux option. For sessions muxherd didn't create, it shows the pane's
current command instead.
