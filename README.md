# muxherd

Herd your AI coding agents — Claude Code, Codex, Grok Build — running in named tmux
sessions on one or more machines, and jump between them from anywhere on your tailnet.

```
 muxherd  3/3 sessions   excelsior ✓
 type to filter sessions…
    host       session          agent   idle  dir
 ●  excelsior  claude-infra     claude  4s    ~/develop/infra
 ○  excelsior  codex-api        codex   12m   ~/develop/api
 ○  excelsior  grok-site        grok    1h    ~/develop/site
──────────────────────────────────────────────────────────────
 (live preview of the selected session's pane)
 ⏎ attach  ^x kill  esc clear/quit  ^n new  ^r refresh  ^p preview
```

- **One picker for every host.** muxherd lists tmux sessions on this machine and on any
  host you can reach over ssh, polling every 2 seconds.
- **Agents start in a login shell.** The agent command is typed into a normal shell, so
  it gets your full environment, and quitting the agent leaves you at a prompt instead
  of losing the session.
- **mosh for remote attach.** A laptop on flaky Wi-Fi stays connected, and the tmux
  session survives either way.
- **Nothing runs in the background.** muxherd talks to `tmux` directly, or over `ssh`
  for remote hosts, reusing one ssh connection per host via ControlMaster.

## Install

```sh
uv tool install git+ssh://git@github.com/kgx/muxherd
# dev checkout:
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
mh a infra               # attach: exact name, host:name, or unique substring (else picker)
mh new -a codex          # new codex session in cwd, named codex-<dir>, then attach
mh new api -a claude -H excelsior -d ~/develop/api -D   # create detached on a host
mh ls                    # list everything
mh kill excelsior:api    # kill (asks first; -y to skip)
mh hosts                 # reachability check
```

### Picker keys

| key            | action                                                     |
| -------------- | ---------------------------------------------------------- |
| type           | filter (space-separated terms match host, name, agent, dir) |
| ↑ ↓ PgUp PgDn  | move                                                       |
| ⏎              | attach                                                     |
| ctrl+n         | new session (agent, host, directory, name)                 |
| ctrl+x         | kill selected (with confirm)                               |
| ctrl+p         | toggle preview pane                                        |
| ctrl+r         | refresh now                                                |
| esc            | clear filter, then quit                                    |

How attach works:
- **Local session, run outside tmux:** `tmux attach`.
- **Local session, run inside tmux:** `tmux switch-client`, so tmux never nests.
- **Remote session:** `mosh <host> -- tmux attach`, falling back to `ssh -t` when mosh
  isn't installed or the config sets `attach = "ssh"`.

## Config

`~/.config/muxherd/config.toml` (override with `$MUXHERD_CONFIG`):

```toml
attach = "mosh"            # or "ssh"

[hosts]
excelsior = "local"        # this machine
# venture = "kgx@venture"  # any ssh target

[agents]                   # name = command typed into the new session's shell
claude = "claude"
codex = "codex"
grok = "grok"
shell = ""
```

Agent commands can include flags, e.g. `claude-yolo = "claude --dangerously-skip-permissions"`.

The agent column shows the agent muxherd launched the session with, which it stores in
the `@muxherd_agent` tmux option. For sessions muxherd didn't create, it shows the pane's
current command instead.
