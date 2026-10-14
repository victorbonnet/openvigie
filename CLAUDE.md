# openvigie — guide for Claude Code

Open-source control tower for AI agents on your own machines (Hermes, OpenClaw,
Claude Code, Codex): discover them, track activity and cost, flag risks, sandbox
them with NVIDIA OpenShell. Read `docs/PRD.md` before any design decision; cite requirement IDs (`FR-n`) in commits and PRs.

## Architecture (MVP)

- `src/openvigie/collector/` — one daemon per host. Loads adapters, pushes to the hub.
- `src/openvigie/hub/` — SQLite store + web UI (FastAPI). Listens on localhost only.
- `src/openvigie/adapters/` — one Python plugin per agent type. An adapter answers:
  is the agent here, what is it doing, what can it access.
- Internal telemetry format: OpenTelemetry GenAI semantic conventions. No custom formats
  where an open standard exists.

## Hard rules

- **MVP is read-only (P0).** Do not add code that starts/stops agents, edits configs,
  writes sandbox policies or touches secrets unless the task explicitly targets a
  P1/P2 requirement *and* the audit log (FR-17) exists.
- **Security first.** The collector and hub must be harder to compromise than the agents.
  - Never bind to `0.0.0.0` by default. Hub binds `127.0.0.1`; remote access is via Tailscale.
  - Never log, store or export raw secrets. Redact before anything leaves the process.
  - Treat everything read from agent configs, logs and processes as untrusted input:
    no `eval`, no `shell=True`, no YAML `load` (use `safe_load`), bounded file reads.
  - No phone-home, no telemetry from openvigie itself.
- **Pi-friendly.** Collector target: < 100 MB RAM, < 2% CPU idle on a Pi 5. Prefer stdlib;
  every new runtime dependency needs a one-line justification in the PR.
- **Unknown ≠ zero.** If token usage can't be read, mark it `estimated` or `unknown`.
- Platforms: Linux ARM64 + macOS arm64 first. Note: cloud dev VMs are x86_64 — don't
  rely on arch-specific wheels or binaries without a fallback.

## Workflow

- Python 3.12+, managed with `uv`. Setup: `uv sync`.
- Before every commit, all must pass:
  - `uv run ruff format --check . && uv run ruff check .`
  - `uv run mypy`
  - `uv run pytest`
- Small PRs, one concern each. Branch names: `feat/…`, `fix/…`, `chore/…`.
- Conventional commit subjects (`feat(adapters): detect Codex via ~/.codex (FR-1)`).
- New adapter ⇒ fixture-based contract tests under `tests/adapters/<name>/` using
  recorded config/log samples (scrubbed of secrets). Never hit real agents in tests.
- If a change contradicts the PRD, stop and say which PRD section needs updating.
