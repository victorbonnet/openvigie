# Fixture: codex (synthetic, 2026-10-14)

**Hand-written** from the Codex CLI rollout format (`codex-rs` rollout recorder), not recorded
from a real install. Replace it with a recording (`scripts/record_fixture.py codex ~/.codex …`)
when one is available, and keep the expected values below in sync.

## Files

- `config.toml`: model, provider, approval and sandbox mode, one MCP server.
- `sessions/2026/10/14/rollout-…-0001.jsonl`: Codex 0.46.0, `gpt-5-codex`, one tool call,
  a `token_count` with `info: null`, a duplicated `token_count`, and a final cumulative total.
- `sessions/2026/10/13/rollout-…-0002.jsonl`: Codex 0.45.0, `gpt-5`, one turn.

## Facts

- Agent version: 0.46.0 (newest rollout `session_meta.payload.cli_version`)
- Process name: `codex` (native binary; the npm package launches it from a node shim)
- Listening ports: none
- Usage: `event_msg` → `payload.type == "token_count"` → `info.total_token_usage` (cumulative)
  and `info.last_token_usage`. `cached_input_tokens` is a subset of `input_tokens`;
  `reasoning_output_tokens` is a subset of `output_tokens`.

## Expected totals

| Session | Model | input | cached input | output | reasoning |
| --- | --- | --- | --- | --- | --- |
| …-0001 | gpt-5-codex | 12000 | 4800 | 650 | 128 |
| …-0002 | gpt-5 | 3000 | 0 | 120 | 0 |
