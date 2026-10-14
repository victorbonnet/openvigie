# OTLP fixtures (synthetic)

**Not recorded from a live agent.** No agent can run in the CI sandbox. Payloads are built from
the documented event shapes; replace them with real captures when available, e.g. with
`nc -l 4318` or a debug OTLP collector.

- `claude_code_logs.json`: OTLP/HTTP JSON `ExportLogsServiceRequest` with one
  `claude_code.api_request` event per API call of the recorded Claude Code transcript fixture
  (`tests/adapters/claude_code/fixtures`): same request ids, timestamps and token counts.
  Shape from https://code.claude.com/docs/en/monitoring-usage: resource `service.name =
  claude-code`, attributes `event.name = api_request`, `event.timestamp`, `session.id`,
  `model`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`,
  `request_id`, sent as strings.
- Totals: 9 calls, 24009 output tokens (same as the transcript fixture).
- `codex_logs.json`: `codex.sse_event` log events with `event.kind = response.completed`, one per
  model call of the Codex rollout fixture (`tests/adapters/codex/fixtures`, itself synthetic),
  plus events that must be ignored: another `event.kind`, a failed completion
  (`error.message`) and `codex.api_request`. Attribute keys follow `codex-rs/otel/src/events`
  (`input_token_count`, `output_token_count`, `cached_token_count`, `reasoning_token_count`,
  and the common `event.timestamp`, `conversation.id`, `model`). Totals: 3 calls; input 15000,
  cached 4800, output 770, reasoning 128 (the two sessions of the rollout fixture).
