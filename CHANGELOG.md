# Changelog

All notable changes to openvigie. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-14

First release: the read-only MVP (P0).

### Added

- Collector daemon per host with adapters for **Claude Code** and **Codex**: detection,
  live status (active / idle / stopped), version, sandbox state (FR-1, FR-2, FR-7).
- Token usage from session logs, with exact / estimated / unknown quality, and an optional
  local OTLP/HTTP JSON receiver for agents' own telemetry (FR-3).
- Hub on `127.0.0.1` with a web UI: agents, hosts, usage and cost dashboards, prices, settings
  (FR-4, FR-5). Remote access through `tailscale serve`.
- Enrolment with one-time codes, per-host tokens, revoke and rotate.
- Optional trace export to Langfuse over OTLP, metadata only by default, with trace links
  from the UI (FR-6).
- Configurable retention (data is kept until you opt in), redaction of secrets everywhere,
  footprint benchmark in CI.
- Service definitions for systemd (dedicated user) and launchd: `openvigie service print`.

### Known limitations

- Hermes and OpenClaw adapters are not in this release.
- OpenAI (Codex) prices are bundled but not yet verified against OpenAI's price list; the UI
  says so wherever they are used. Override them in `prices.toml` if needed.
