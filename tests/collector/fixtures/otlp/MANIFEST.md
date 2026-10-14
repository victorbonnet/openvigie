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
