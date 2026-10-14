# Troubleshooting

## An agent is not detected

1. Run `openvigie scan -v` on that machine. It lists each adapter's health and, with `-v`,
   what it looked at.
2. openvigie looks for config folders (`~/.claude`, `~/.codex`, or `$CLAUDE_CONFIG_DIR` and
   `$CODEX_HOME`) in the **homes** listed in `collector.toml`, which defaults to the home of
   the user running the collector. If the collector runs as a service user, list the homes
   to watch:

   ```toml
   homes = ["/home/alice"]
   ```

3. The folder must contain the agent's usual files: `projects/` for Claude Code, `sessions/`
   for Codex. An agent that was installed but never used has nothing to show yet.
4. On the agent's page, the **Adapter** line shows `degraded` or `error` with a reason when
   files couldn't be read or parsed.

## Usage shows "unknown" or "partly unknown"

openvigie never guesses a zero. "Unknown" means the agent's logs didn't include token counts
for those calls: an older agent version, or an interrupted session. "Estimated" means the
counts were derived rather than read. Costs that include unknown usage are shown as a lower
bound (`≥ $…`).

A model without a price shows its tokens but no cost. Add it to `prices.toml` (see the
**Prices** page for the format and the table in use).

## The status is wrong or late

Status (active, idle, stopped) comes from running processes and recent file activity, and
reaches the UI within about 10 seconds. If an agent shows **stopped** while running:

- **macOS:** the collector can't see other users' processes or listening ports without extra
  rights. Run the collector as the same user as the agents (the LaunchAgent does that).
- **Linux:** check that the process runs as a user whose home is in `homes`.

## A host shows "stale"

The hub hasn't heard from that collector for a while. On that machine:

- Is `openvigie collect` (or its service) running? `systemctl status openvigie-collector`,
  or `launchctl print gui/$(id -u)/dev.openvigie.collector` on macOS.
- `openvigie collect --once -v` pushes once and prints errors. **401** means the host's token
  was revoked or rotated: get a new code with `openvigie hub hosts rotate <host>` on the hub,
  then `openvigie enrol` again. A collector that can't reach the hub keeps its data in a local
  spool and sends it when the hub is back; nothing is lost up to `spool_limit_mb`.

## "Invalid host header" or HTTP 400 in the browser

The hub only answers to names in `allowed_hosts`. Add the name you use (for example the
Tailscale name) to `hub.toml` and restart the hub.

## Permission errors on files

openvigie refuses to read its own secret files (the collector token, the Langfuse key) if
other users can read them. Fix them with `chmod 600 <file>`, as the error message says.
