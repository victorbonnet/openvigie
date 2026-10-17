# Fixture: codex (synthetic, 2026-10-14)

**Hand-written** from the Codex CLI rollout format (`codex-rs` rollout recorder), not recorded
from a real install. Replace it with a recording (`scripts/record_fixture.py codex ~/.codex …`)
when one is available, and keep the expected values below in sync.

## Files

- `config.toml`: model, provider, approval and sandbox mode, one MCP server.
- `sessions/2026/10/14/rollout-…-0001.jsonl`: Codex 0.46.0, `gpt-5-codex`, one tool call,
  a `token_count` with `info: null`, a duplicated `token_count`, and a final cumulative total.
- `sessions/2026/10/13/rollout-…-0002.jsonl`: Codex 0.45.0, `gpt-5`, one turn.
- `sessions/2026/10/12/rollout-…-0003.jsonl`: `gpt-6.1-sol`, two turns with cache writes
  (`cache_write_input_tokens`), written from `TokenUsage` in `codex-rs/protocol` at openai/codex
  commit 440312f (2026-10-16). Its CLI version, `0.0.0-placeholder`, is made up.

## Facts

- Agent version: 0.46.0 (newest rollout `session_meta.payload.cli_version`)
- Process name: `codex` (native binary; the npm package launches it from a node shim)
- Listening ports: none
- Usage: `event_msg` → `payload.type == "token_count"` → `info.total_token_usage` (cumulative)
  and `info.last_token_usage`. `cached_input_tokens` and `cache_write_input_tokens` are subsets
  of `input_tokens` (Codex fills them from the Responses API's `input_tokens_details`, see
  `codex-rs/codex-api/src/sse/responses.rs`); `reasoning_output_tokens` is a subset of
  `output_tokens`. Older rollouts have no `cache_write_input_tokens`.
- Tool calls: `response_item` → `function_call` (`name`, `call_id`, `arguments`) then
  `function_call_output` (`call_id`, `output`). Shell outputs are a JSON string with
  `metadata.exit_code`; some versions write plain text starting `Exit code: N`. Both are parsed
  for the span status; anything else leaves the status unset.

## Expected totals

| Session | Model | input | cached input | cache write | output | reasoning |
| --- | --- | --- | --- | --- | --- | --- |
| …-0001 | gpt-5-codex | 12000 | 4800 | — | 650 | 128 |
| …-0002 | gpt-5 | 3000 | 0 | — | 120 | 0 |
| …-0003 | gpt-6.1-sol | 22000 | 9000 | 8000 | 500 | 64 |
