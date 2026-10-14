# Write actions

openvigie is read-only unless you set it up otherwise. Write actions (P1) let an admin change
things on a host, such as an OpenShell sandbox's network policy (#17). Each change goes through
the same steps, and each step is recorded in the audit log (the *Audit* page, and `openvigie hub audit verify`):

1. **Propose.** An admin starts an action from the UI. The hub works out the change and stores
   a proposal, valid for 15 minutes.
2. **Review and confirm.** The proposal page shows the diff between now and after. To confirm,
   the admin types the target's name (a host or sandbox name). Anyone can read the page; only
   an admin can confirm or cancel.
3. **Command.** Confirming queues a command for the target host. It rides in the reply to that
   host's next push (within seconds), delivered at most once, and expires after 10 minutes.
4. **Result.** The collector runs the command only if its own `allow_actions` lists the action,
   then reports the result with its next push. The proposal page shows it.
5. **Rollback.** A confirmed change that can be undone offers *Propose a rollback*, which
   proposes the inverse change, to review and confirm like any other.

![A proposal waiting for confirmation](img/action-confirm.png)

## Setting it up

On the hub, name the admins and trust Tailscale identity ([remote access](remote-access.md)
explains the single-user hub host this requires):

```toml
# hub.toml
admins = ["you@example.com"]
trust_tailscale_identity = true
```

On each host that should accept an action, allow it by name:

```toml
# collector.toml
allow_actions = ["noop"]
```

`noop` does nothing; it checks the whole path. On a host's page, an admin sees *Send a test
command…*. When the result reads *succeeded*, the channel works.

## Safety

- **Two keys.** The hub decides what to ask, and each host decides what it accepts. A
  compromised hub can't make a collector run anything outside its allowlist, which is empty by
  default.
- **No replays.** Commands expire. A collector refuses an id it has seen before, even after a
  restart.
- **No cross-site requests.** Every form carries a token bound to the admin and the form. The
  hub refuses posts that a browser marks as coming from another site. Without this, a web page
  an admin visits could post to the hub through their browser, and Tailscale Serve would add
  the admin's identity.
- **Refusals are audited.** Non-admins, bad tokens and mistyped names are recorded too, at most
  30 refusals a minute.
