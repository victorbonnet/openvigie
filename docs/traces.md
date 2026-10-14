# Traces in Langfuse

openvigie can turn agent sessions into OpenTelemetry GenAI traces and send them to
[Langfuse](https://langfuse.com), cloud or self-hosted. It is **off by default**: without a
`[langfuse]` section the hub makes no outbound connection.

## What a trace looks like

One trace per agent session (Claude Code session, Codex rollout):

```
invoke_agent claude-code          session.id, provider
├─ chat claude-opus-5-5           gen_ai.usage.* tokens, models, duration
│  └─ execute_tool Bash           tool name, call id, OK / ERROR
└─ invoke_agent subagent
```

Trace and span ids are deterministic, so re-sent spans update the same trace instead of
duplicating it.

## Setup

1. In Langfuse, create a project and an API key pair.
2. Save the secret key in a file only you can read:

   ```sh
   install -m 600 /dev/null ~/.config/openvigie/langfuse-secret
   $EDITOR ~/.config/openvigie/langfuse-secret
   ```

   Or set `LANGFUSE_SECRET_KEY` in the hub's environment.
3. Add to `hub.toml` and restart the hub:

   ```toml
   [langfuse]
   host = "https://cloud.langfuse.com"     # or us.cloud.langfuse.com, or your own instance
   public_key = "pk-lf-…"
   secret_key_file = "~/.config/openvigie/langfuse-secret"
   export_content = false

   [retention]
   span_queue_hours = 24    # unexported spans older than this are dropped
   ```

The **Settings** page shows the queue (waiting, exported, dropped) and the last error.

## Privacy

- **No content by default.** Prompts, model outputs, tool arguments and tool results are
  left out. Shell commands can contain secrets, so they are not exported either.
- Content is exported only when **both** the hub (`export_content = true` here) and the
  collector on that machine allow it.
- Every attribute passes the redactor twice: on the collector, and again on the hub before
  it leaves your network.
- Spans are not stored by the hub: they sit in a short export queue and are then deleted.

## Delivery

The hub sends batches over OTLP/HTTP with JSON encoding to
`<host>/api/public/otel/v1/traces`.

| Langfuse response | What the hub does |
| --- | --- |
| 5xx, 429 or network error | Retries with backoff (10 s up to 10 min); drops a span after 8 attempts |
| 401 or 403 | Pauses export for 10 minutes; nothing is dropped. Check the keys. |
| Any other 4xx | Drops that batch right away, since resending it would fail the same way |

Redirects are never followed, so your keys only go to the configured host.
