# Threat model

openvigie's hub will be able to stop agents, change their sandboxes and rotate their secrets
(P1/P2). It must be harder to compromise than the agents it governs. This document lists what
we protect, where the trust boundaries are, which attackers we design against, and which issue
implements each mitigation. Source: PRD → *Security, privacy and threat model*.

## Assets

| Asset | Where | Why it matters |
| --- | --- | --- |
| Hub database | Hub host, `0600` SQLite file | Inventory, usage history, host token hashes |
| Per-host collector token | Collector state dir, `0600` | Lets its holder write data as that host |
| Langfuse secret key | Hub config, `0600` file or env var | Write access to the trace backend |
| Agent configs, logs and session transcripts | Agent users' homes | Contain prompts, personal data and sometimes secrets |
| The web UI session | Operator's browser | Read access to everything above |

## Trust boundaries

```
agent files / processes ──(untrusted input)──▶ collector ──(bearer token, HTTP JSON)──▶ hub
                                                                                      │
                     operator browser ◀──(Host-checked, CSP)── hub UI on 127.0.0.1 ◀──┤ tailscale serve
                                                                                      │
                                                         Langfuse ◀──(OTLP, redacted)──┘ (optional)
```

1. **Agent → collector.** Everything read from agent configs, logs and process tables is
   untrusted: it may be malformed, huge or hostile.
2. **Collector → hub.** Collectors are authenticated per host, but their payloads are still
   validated as untrusted input.
3. **Browser → hub.** The hub listens on loopback. Any web page the operator visits can make
   requests to `127.0.0.1`, so the hub checks the `Host` header and sends a strict CSP.
   Everyone is a viewer; admins (write actions, P1) are recognised from Tailscale Serve's
   identity header only when the operator opts in, which requires a single-user hub host (T13).
4. **Hub → Langfuse.** Only explicitly configured, and only redacted metadata by default.

## Threats and mitigations

| # | Threat | Mitigation | Issue |
| --- | --- | --- | --- |
| T1 | A hostile or corrupted agent file crashes or exhausts the collector (huge lines, symlinks to `/etc`, FIFOs) | Bounded readers, no symlink following outside the agent root, malformed lines skipped and counted | #25 |
| T2 | Agent data executes as code | No `eval`, no `shell=True`, `yaml.safe_load` only, agent binaries are never executed | #25, #30, #31 |
| T3 | Secrets leak through logs, the hub or `scan --json` | One redaction module applied to every outbound payload and to the root logger; process command lines are never serialized | #24, #28 |
| T4 | An agent on the same host reads the collector token and writes fake data or, later, loosens its own sandbox | Token file `0600` owned by the collector user; per-host tokens, revocable; collector runs as its own user; HTTP clients never follow redirects, so the token only goes to the configured hub | #23, #36, #54, #65 |
| T5 | A web page performs DNS rebinding against the hub on `127.0.0.1` | `TrustedHostMiddleware` with an allow-list, strict CSP, no CORS | #35 |
| T6 | Stored XSS through agent names, versions or error messages | Jinja2 autoescape, no inline scripts, values escaped in every template | #35, #39 |
| T7 | The hub is reached from the LAN or the internet | Loopback bind by default; a non-loopback bind needs an explicit opt-in; remote access only via `tailscale serve` (the docs warn never to use `tailscale funnel`) | #23, #53 |
| T8 | Secrets and personal data leak through traces sent to Langfuse | Export is off unless configured, metadata only by default, content opt-in and redacted again on the hub | #60, #61, #62 |
| T9 | Usage data accumulates forever | Configurable retention | #51 |
| T10 | A prompt-injected agent edits its own config or skills to gain access | MVP is read-only; write actions wait for the audit log and roles | #12 |
| T11 | A bad sandbox policy locks an agent out, or a loose one silently opens it up | Diff, dry-run and rollback; every change audited | #17 |
| T12 | A third-party adapter plugin runs malicious code in the collector | Only built-in adapters load by default; third-party plugins must be enabled by name | #27 |
| T13 | Someone forges `Tailscale-User-Login` to become admin | Identity is ignored unless `trust_tailscale_identity` is on; then only on loopback requests addressed to the MagicDNS name; `Tailscale-*` headers removed before handlers. Residual risk: any local user on the hub host can forge it, so trust requires a single-user hub host | #162 |
| T14 | Admin actions are denied or hidden after the fact | Hash-chained audit log, kept forever, redacted, verified by `openvigie hub audit verify`; keep the printed head hash elsewhere to detect truncation | #161 |
| T15 | A compromised hub, or a forged reply, makes collectors run actions | Each collector runs only actions on its own `allow_actions` (empty by default); commands expire, ids are never accepted twice (kept across restarts), the hub delivers each at most once; replies come only over the authenticated push, and collectors never follow redirects | #163 |
| T16 | A compromised hub stops or kills processes on a host | Each host allows `agent.stop` / `agent.kill` separately (off by default); the hub names an agent, never a PID; the collector signals only processes it attributed to that agent and their children, re-checked by start time, never PID 1 or itself, and without privilege escalation | #174 |
| T17 | Alerts leak data, or the webhook leaks its secret | Off unless configured; https only (http to loopback); no redirects; the URL and token are never logged or shown; the payload holds budget ids, scopes and amounts, no prompts or secrets | #179 |
| T18 | Anyone who can message an agent through a shared channel (group chat, open bot) instructs it with the agent's access | Capabilities list each agent's channels and who can reach it; a risk flag, high when combined with broad access outside a sandbox (FR-8, FR-9) | #183 |
| T19 | Reading Hermes' `.env` to count who may message a profile leaks the API keys and bot tokens kept in the same file | Only lines named like an access setting (`*_ALLOWED_USERS`, `*_ALLOW_ALL_USERS`, `*_GROUP_ALLOWED_CHATS`, `*_ALLOWED_ROLES`) have their value looked at; entries are hashed at once to count people, flags reduced to true or false; nothing else is parsed, kept, logged or sent | #183 |

## Rules for every change

- Never bind to `0.0.0.0` or `::` by default.
- Never log, store or export raw secrets. Redact before anything leaves the process.
- Treat everything read from agents as untrusted: bounded reads, no `eval`, no `shell=True`,
  `yaml.safe_load` only.
- Never execute an agent's binary to learn something about it.
- No phone-home and no telemetry from openvigie itself. Outbound connections happen only to
  endpoints the operator configured (the hub, Langfuse).
- No write action on agents before the audit log (FR-17) exists.
