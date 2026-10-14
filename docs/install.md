# Install guide

openvigie has two parts:

- the **collector**, one per machine that runs agents: it finds them and reads their activity;
- the **hub**, one in total: it stores the data and serves the web UI on `127.0.0.1:7878`.

Both come from the same package. Python 3.12 or newer is required.

```sh
pipx install openvigie
# before the first release on PyPI:
pipx install git+https://github.com/victorbonnet/openvigie
```

## One machine

```sh
openvigie scan                  # check what it finds; nothing is sent anywhere
openvigie hub serve             # keep running (or install it as a service, below)
```

In another terminal:

```sh
openvigie hub enrol-code        # one-time code, valid 15 minutes
openvigie enrol http://127.0.0.1:7878 <code>
openvigie collect               # keep running
```

Open <http://127.0.0.1:7878>. Agents appear within seconds. Usage from the last 30 days is
read on the first run, so the usage pages fill in within a minute.

## Several machines over Tailscale

Example: the hub on a Raspberry Pi, collectors on the Pi and on a Mac. Install
[Tailscale](https://tailscale.com/download) on both and sign them in to the same tailnet.

On the Pi (hub):

```sh
openvigie hub serve             # prints the exact `tailscale serve` command to run
tailscale serve --bg --https=443 http://127.0.0.1:7878
```

Add the Pi's tailnet name to `~/.config/openvigie/hub.toml` and restart the hub:

```toml
allowed_hosts = ["127.0.0.1", "localhost", "pi.your-tailnet.ts.net"]
```

Then enrol each collector with the HTTPS URL:

```sh
# on the Pi
openvigie hub enrol-code
# on the Mac
openvigie enrol https://pi.your-tailnet.ts.net <code>
openvigie collect
```

The hub never listens on the network itself. Tailscale carries the traffic, encrypted and
limited to your tailnet. See [remote-access.md](remote-access.md) for ACLs and details.

## Running as services

To start the collector and the hub at boot and restart them after a crash, see
[services.md](services.md). On Linux the collector runs as a dedicated user that your agents
can't impersonate. On macOS it runs as a LaunchAgent.

## Configuration files

| File | Default location | What |
| --- | --- | --- |
| `collector.toml` | `~/.config/openvigie/` | hub URL, homes to scan, adapters, OTLP receiver, allowed actions |
| `hub.toml` | `~/.config/openvigie/` | bind, port, allowed hosts, retention, Langfuse, admins |
| `prices.toml` | `~/.config/openvigie/` | optional price overrides |
| `budgets.toml` | `~/.config/openvigie/` | optional cost and token budgets ([alerts](alerts.md)) |
| `risk.toml` | `~/.config/openvigie/` | optional risk rule overrides |
| `token` | `~/.local/state/openvigie/` | the collector's hub token (`0600`) |
| `collector.db` | `~/.local/state/openvigie/` | read positions and the offline spool |
| `hub.db` | `~/.local/share/openvigie/` | the hub's SQLite store |

`$XDG_CONFIG_HOME`, `$XDG_STATE_HOME` and `$XDG_DATA_HOME` are honoured, and so are
`OPENVIGIE_CONFIG_DIR`, `OPENVIGIE_STATE_DIR` and `OPENVIGIE_DATA_DIR`.

## Upgrading

```sh
pipx upgrade openvigie
```

Then restart the hub and the collectors. The hub migrates its database on start. Collectors
and the hub can be upgraded in any order; a hub ignores data sections it doesn't know yet,
and says so.

## Uninstalling

```sh
pipx uninstall openvigie
rm -r ~/.config/openvigie ~/.local/state/openvigie ~/.local/share/openvigie
```

Remove the service files too if you installed them ([services.md](services.md)). On the hub,
`openvigie hub hosts revoke <host>` cuts off a collector you no longer run.
