"""Service definitions for the collector and the hub (#65): printed, never installed.

``openvigie service print`` fills in paths and prints a systemd unit or a launchd plist for the
operator to review and install. Nothing here writes outside stdout (MVP is read-only).

Two models (PRD decision log, collector user model):

- **system** (Linux): the collector runs as a dedicated ``openvigie`` user, so agents can't read
  its token. Agent folders are often ``0700``/``0600`` (Claude Code transcripts are), which ACLs
  can't open reliably: a file created ``0600`` gets an empty ACL mask. The unit instead grants
  ``CAP_DAC_READ_SEARCH`` (read-only, any file) **inside a mount namespace that hides every home
  folder except the agent folders**, bind-mounted read-only, with the usual systemd hardening.
- **user** (macOS LaunchAgent, ``systemd --user``): runs as the agents' own user. Simplest, and
  the only option on macOS, but an agent running as that user can read the collector token.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

AGENT_DIRS = (".claude", ".codex", ".hermes", ".openclaw")
DOCS = "https://github.com/victorbonnet/openvigie/blob/main/docs/install.md"

HARDENING = """\
NoNewPrivileges=yes
ProtectSystem=strict
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallFilter=~@privileged @resources
UMask=0077
RemoveIPC=yes"""

COLLECTOR_SYSTEM = """\
# openvigie collector, system service with a dedicated user ({docs})
# Install: useradd --system --home-dir /var/lib/openvigie --shell /usr/sbin/nologin openvigie
#          then copy to /etc/systemd/system/openvigie-collector.service
[Unit]
Description=openvigie collector (read-only agent monitor)
Documentation={docs}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=openvigie
Group=openvigie
ExecStart={exe} collect
Restart=on-failure
RestartSec=5
Environment=OPENVIGIE_CONFIG_DIR=/var/lib/openvigie OPENVIGIE_STATE_DIR=/var/lib/openvigie
StateDirectory=openvigie
StateDirectoryMode=0700
# Agent folders are owner-only (0700/0600). Read them without owning them, but see nothing
# else of any home: /home and /root are empty except for these read-only bind mounts.
AmbientCapabilities=CAP_DAC_READ_SEARCH
CapabilityBoundingSet=CAP_DAC_READ_SEARCH
ProtectHome=tmpfs
BindReadOnlyPaths={binds}
InaccessiblePaths=-/etc/shadow -/etc/gshadow -/etc/ssh
{hardening}
MemoryMax=150M

[Install]
WantedBy=multi-user.target
"""

HUB_SYSTEM = """\
# openvigie hub, system service with a dedicated user ({docs})
# Install: useradd --system --home-dir /var/lib/openvigie-hub --shell /usr/sbin/nologin \
#            openvigie-hub
#          then copy to /etc/systemd/system/openvigie-hub.service
[Unit]
Description=openvigie hub (store and web UI on 127.0.0.1)
Documentation={docs}
After=network-online.target

[Service]
Type=simple
User=openvigie-hub
Group=openvigie-hub
ExecStart={exe} hub serve
Restart=on-failure
RestartSec=5
Environment=OPENVIGIE_CONFIG_DIR=/var/lib/openvigie-hub OPENVIGIE_DATA_DIR=/var/lib/openvigie-hub
StateDirectory=openvigie-hub
StateDirectoryMode=0700
CapabilityBoundingSet=
ProtectHome=yes
{hardening}

[Install]
WantedBy=multi-user.target
"""

USER_UNIT = """\
# openvigie {role}, per-user service ({docs})
# Install: copy to ~/.config/systemd/user/openvigie-{role}.service
#          systemctl --user enable --now openvigie-{role}; loginctl enable-linger $USER
# Weaker isolation: it runs as you, so your agents can read its files.
[Unit]
Description=openvigie {role}
Documentation={docs}

[Service]
Type=simple
ExecStart={exe} {command}
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
UMask=0077

[Install]
WantedBy=default.target
"""

LAUNCHD = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- openvigie {role}, LaunchAgent ({docs})
     Install: save as ~/Library/LaunchAgents/dev.openvigie.{role}.plist, then
              launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.openvigie.{role}.plist
     Runs as you: your agents can read its files (weaker isolation than the Linux system unit). -->
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.openvigie.{role}</string>
  <key>ProgramArguments</key>
  <array>{args}</array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Background</string>
  <key>Umask</key><integer>63</integer>
  <key>StandardOutPath</key><string>{logs}/{role}.log</string>
  <key>StandardErrorPath</key><string>{logs}/{role}.log</string>
</dict>
</plist>
"""

COMMANDS = {"collector": "collect", "hub": "hub serve"}


def executable() -> str:
    found = shutil.which("openvigie")
    return str(Path(found).resolve()) if found else f"{sys.executable} -m openvigie"


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def bind_paths(homes: list[Path]) -> str:
    """Read-only bind mounts for the agent folders in these homes (missing ones are skipped)."""
    return " ".join(f"-{home / name}" for home in homes for name in AGENT_DIRS)


def render(role: str, kind: str, homes: list[Path], exe: str | None = None) -> str:
    """The service definition for ``role`` (collector|hub) as ``kind`` (system|user|launchd)."""
    exe = exe or executable()
    if kind == "launchd":
        args = "".join(f"<string>{_xml(a)}</string>" for a in [exe, *COMMANDS[role].split()])
        logs = _xml(str(Path.home() / "Library" / "Logs" / "openvigie"))
        return LAUNCHD.format(role=role, docs=DOCS, args=args, logs=logs)
    if kind == "user":
        return USER_UNIT.format(role=role, docs=DOCS, exe=exe, command=COMMANDS[role])
    if exe.startswith(("/home/", "/root/", "/Users/")):
        raise ValueError(
            f"{exe} is inside a home folder, which system services can't see. Install "
            "openvigie system-wide first (e.g. `sudo pipx install --global openvigie`), "
            "or use --systemd-user"
        )
    if role == "hub":
        return HUB_SYSTEM.format(docs=DOCS, exe=exe, hardening=HARDENING)
    if not homes:
        raise ValueError("the system collector needs --home for each user whose agents it reads")
    return COLLECTOR_SYSTEM.format(docs=DOCS, exe=exe, binds=bind_paths(homes),
                                   hardening=HARDENING)  # fmt: skip
