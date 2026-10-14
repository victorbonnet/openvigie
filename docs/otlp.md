# Usage from agents' own telemetry (OTLP)

By default openvigie reads token usage from each agent's session logs. Agents that emit
OpenTelemetry can also send it straight to the collector, which helps when the collector can't
read the logs, for example when the agent runs in a container or a sandbox.

## Turn the receiver on

In `collector.toml`:

```toml
[otlp]
enabled = true
port = 4318                 # always bound to 127.0.0.1
token_file = "~/.config/openvigie/otlp-token"   # optional, chmod 600; see below
usage_source = "auto"       # "auto" | "logs" | "otlp"
```

The receiver accepts **OTLP/HTTP with JSON encoding only** (`http/json`); protobuf requests
get HTTP 415. It listens on loopback only. With `token_file` set, agents must send
`Authorization: Bearer <token>`, so other local processes can't inject fake usage.

## Claude Code

Claude Code reports each API call as a `claude_code.api_request` event
([monitoring docs](https://code.claude.com/docs/en/monitoring-usage)). Events are log records,
so the logs exporter is the one that matters:

```sh
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_LOGS_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://127.0.0.1:4318/v1/logs
# only with token_file:
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer $(cat ~/.config/openvigie/otlp-token)"
```

Prompts are not included unless you set `OTEL_LOG_USER_PROMPTS=1`, and openvigie ignores
them anyway: only token counts, model, session and request ids are kept.

OTLP usage is credited to the Claude Code install the collector detects (the first one, if
there are several config folders).

## Which source wins

- `auto` (default): logs until OTLP usage first arrives for an agent, then OTLP. The switch
  happens right after the newest log record already sent, so the overlap is never counted twice.
  To go back to logs, set `usage_source = "logs"`.
- `logs`: OTLP usage is ignored.
- `otlp`: usage comes only from OTLP. Logs are still used to detect agents and their sessions.

Log transcripts are slightly richer. Claude Code transcripts say which cache writes used the
1-hour TTL, and OTLP events don't, so costs for cache writes are a little less precise with OTLP.
