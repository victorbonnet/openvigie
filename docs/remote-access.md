# Remote access over Tailscale

The hub listens on `127.0.0.1` only. To reach it from other machines on your tailnet (the
browser on your laptop, collectors on other hosts), put **Tailscale Serve** in front of it.
Tailscale terminates HTTPS with a real certificate for the node's MagicDNS name and forwards
to the hub on loopback, so the hub never listens on a network interface itself.

## Setup (on the hub machine)

1. Allow the MagicDNS name in `hub.toml` (`openvigie hub serve` prints the exact line):

   ```toml
   allowed_hosts = ["127.0.0.1", "localhost", "hub.tail1234.ts.net"]
   ```

2. Start the hub and Tailscale Serve:

   ```sh
   openvigie hub serve
   tailscale serve --bg --https=443 http://127.0.0.1:7878
   ```

3. Open `https://hub.tail1234.ts.net` from any device on your tailnet.

> **Use `tailscale serve`, never `tailscale funnel`.** Funnel publishes the hub on the public
> internet: anyone could read your agents, usage and audit log, and post to the hub. Serve
> keeps it inside your tailnet.

## Enrolling a remote collector

On the hub: `openvigie hub enrol-code`. On the other machine:

```sh
openvigie enrol https://hub.tail1234.ts.net <code>
openvigie collect
```

The collector refuses plain `http://` to anything but a hub on the same machine, so tokens
never travel unencrypted.

## Restrict who can reach the hub

By default every device on your tailnet can open the hub. Narrow it with a Tailscale ACL, for
example only your user and the tagged collector hosts:

```jsonc
{
  "tagOwners": { "tag:openvigie": ["autogroup:admin"] },
  "acls": [
    { "action": "accept", "src": ["you@example.com", "tag:openvigie"], "dst": ["tag:openvigie-hub:443"] }
  ]
}
```

## Why not `bind = "0.0.0.0"`?

A non-loopback bind exposes the hub to your whole LAN (or worse) with no TLS and no identity.
The hub refuses it unless you set `allow_non_loopback = true`, and warns loudly when you do.
Tailscale Serve gives you encryption, a certificate and an access policy for free.

## Identity headers

Tailscale Serve adds `Tailscale-User-Login` and similar headers. By default the hub ignores
them, and everyone is a **viewer**. To let some people make changes (P1 write actions), list
their Tailscale logins and turn trust on:

```toml
admins = ["you@example.com"]
trust_tailscale_identity = true
```

Only turn this on if **nobody else has a shell on the hub machine**. Tailscale Serve forwards
from `127.0.0.1`, so the hub can't tell its requests from a local user's `curl` with the same
header. The hub narrows it as far as it can: identity counts only on requests from loopback
addressed to the MagicDNS name, never on `http://127.0.0.1:7878`, and every `Tailscale-*` header
is removed before any page or API code runs. The header of every page shows who you are and
your role. Refused admin actions are recorded in the audit log (`/audit`).
