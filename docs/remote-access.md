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

Tailscale Serve adds `Tailscale-User-Login` and similar headers. The read-only MVP ignores and
strips them; the admin roles planned for write actions (P1) will use them, trusting them only
on requests that come from the local Tailscale proxy.
