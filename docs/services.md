# Running as a service

`openvigie service print` prints a service definition with your paths filled in. It installs
nothing: read it, then copy it into place yourself.

## Linux, system service (recommended)

The collector runs as a dedicated `openvigie` user, so your agents can't read its token. The
hub runs as `openvigie-hub`.

**Why a capability rather than ACLs:** agent folders are owner-only. Claude Code creates
`0700` folders and `0600` transcripts, and a file created `0600` gets an empty ACL mask, so a
read ACL for another user stops working on every new transcript. The collector unit instead
grants `CAP_DAC_READ_SEARCH` (read any file, never write), and systemd confines it:

- `/home` and `/root` are replaced by empty folders, except the agent folders (`~/.claude`,
  `~/.codex`, …) of the homes you name, bind-mounted **read-only**. The collector can't see
  `~/.ssh` or anything else in a home.
- `/etc/shadow`, `/etc/gshadow` and `/etc/ssh` are hidden; the rest of the system is
  read-only. Only `/var/lib/openvigie` is writable.
- No other capabilities, a system-call filter, no new privileges, and 150 MB of memory at most.

`systemd-analyze security` rates the collector unit at 1.7 and the hub unit at 1.4 ("OK").

```sh
# 1. Install system-wide: a pipx install in your home is invisible to system services
sudo pipx install --global openvigie

# 2. Users
sudo useradd --system --home-dir /var/lib/openvigie --shell /usr/sbin/nologin openvigie
sudo useradd --system --home-dir /var/lib/openvigie-hub --shell /usr/sbin/nologin openvigie-hub

# 3. Units (one --home per user whose agents should be watched)
openvigie service print collector --home "$HOME" | sudo tee /etc/systemd/system/openvigie-collector.service
openvigie service print hub | sudo tee /etc/systemd/system/openvigie-hub.service
sudo systemctl daemon-reload
sudo systemctl enable --now openvigie-hub

# 4. Enrol the collector as its own user, and tell it which homes to read
as_hub() { sudo -u openvigie-hub env OPENVIGIE_CONFIG_DIR=/var/lib/openvigie-hub OPENVIGIE_DATA_DIR=/var/lib/openvigie-hub openvigie "$@"; }
as_collector() { sudo -u openvigie env OPENVIGIE_CONFIG_DIR=/var/lib/openvigie OPENVIGIE_STATE_DIR=/var/lib/openvigie openvigie "$@"; }
as_hub hub enrol-code                                  # prints a one-time code
as_collector enrol http://127.0.0.1:7878 <code>
echo "homes = [\"$HOME\"]" | sudo -u openvigie tee -a /var/lib/openvigie/collector.toml
sudo systemctl enable --now openvigie-collector
```

## macOS, and simple Linux setups: per-user service

The collector runs as you, which is the only option on macOS. It is simpler, but the isolation
is weaker: an agent running as you can read the collector's token and send fake data to the
hub. In the read-only MVP, the worst it can do is report fake usage; revoke the host
(`openvigie hub hosts revoke`) if you suspect it.

```sh
# macOS
mkdir -p ~/Library/LaunchAgents ~/Library/Logs/openvigie
openvigie service print collector --launchd > ~/Library/LaunchAgents/dev.openvigie.collector.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.openvigie.collector.plist

# Linux, per user
openvigie service print collector --systemd-user > ~/.config/systemd/user/openvigie-collector.service
systemctl --user enable --now openvigie-collector
loginctl enable-linger "$USER"   # keep it running when you log out
```

Use `hub` instead of `collector` for the hub.
