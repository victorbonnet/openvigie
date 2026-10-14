# openvigie — Product Requirements Document

Oct 14, 2026 · @Victor · Status: Draft · Working name, may change

> Mirror of the PRD kept in the claude.ai project. When a decision changes, update both.

## Overview

openvigie is an open-source control tower for AI agents running on your own machines. It finds every agent on a host, shows what it is doing and what it costs, and lets you put it in an OpenShell sandbox with a policy you control.

**Problem.** People now run several agents side by side: Hermes, OpenClaw, Claude Code, Codex and more. Each has its own config, logs and token meter. Nobody has one view of which agents exist, which are active, what they can touch, and how much they spend. Most run unsandboxed with broad filesystem, network and secret access.

**Vision.** One install per host and one web UI give you an inventory of agents, live activity and cost, risk flags, and the controls to sandbox, configure and stop them. It is built for homelabs first: a Raspberry Pi and a Mac, reachable over Tailscale.

## Goals, non-goals and users

**Goals**

1. See every agent on your hosts in one place, with its status and capabilities.
2. Track activity and token usage per agent, converted to cost.
3. Debug agent runs through traces, with Langfuse as an optional backend.
4. Flag risky agents and help sandbox them with OpenShell.
5. Administer agents and sandbox policies from the web UI.

**Non-goals (v1)**

- Building an agent framework or orchestrator. openvigie observes and governs existing agents.
- A hosted SaaS or multi-tenant cloud service.
- Windows support.
- Evaluating answer quality. Langfuse and similar tools already do that.

**Target users**

| User | Situation | What they need first |
| --- | --- | --- |
| Homelab builder | Runs Hermes or OpenClaw on a Pi, Claude Code on a laptop | One dashboard, cost, a kill switch |
| Solo developer | Several coding agents on one Mac | Token spend and what each agent can access |
| Small team lead | Shared box with agents for a few people | Inventory, audit log, safe defaults |

## Scope

The MVP is a read-only monitoring dashboard. Write actions such as policies, start/stop and secrets come after it, once the inventory and telemetry are trustworthy.

**In the MVP**

- A collector daemon per host (Raspberry Pi on Linux ARM64, macOS on Apple Silicon).
- A hub with a built-in store and the web UI, reachable locally or over Tailscale.
- Adapters for Hermes, OpenClaw, Claude Code and Codex.
- An agent inventory: name, type, host, version, status (active, idle, stopped), sandboxed or not.
- Token usage and cost per agent, per model and per day.
- Optional export of traces to Langfuse, with links from each agent to its traces.

**After the MVP**

- Capability detection and risk scoring.
- OpenShell sandbox inventory and policy editor, with dry-run and rollback.
- Admin actions: start, stop and kill agents; edit agent config and skills; rotate secrets.
- Alerts (Telegram, email, webhook) on budgets, errors and risky changes.
- More adapters through a plugin interface.

## Functional requirements

Requirements are numbered so issues and PRs can reference them. P0 is the MVP, P1 is the next release, P2 is later.

| ID | Requirement | Priority |
| --- | --- | --- |
| FR-1 | Detect agents on a host from running processes, known config folders (`~/.claude`, `~/.codex`, `~/.hermes`, OpenClaw config) and listening ports | P0 |
| FR-2 | Show each agent's status: active (activity in the last 5 minutes, configurable), idle, stopped | P0 |
| FR-3 | Collect token usage per agent, model and session, from OpenTelemetry where the agent emits it and from logs or APIs otherwise. MVP reads local session logs first, OTLP later (D3) | P0 |
| FR-4 | Convert tokens to cost with an editable price table per model. MVP edits it through an override file (D4) | P0 |
| FR-5 | Web UI: agent list, agent detail page, usage and cost charts, host view | P0 |
| FR-6 | Send traces to Langfuse when configured, and link each agent and session to its traces. The hub exports; metadata only by default (D5) | P0 |
| FR-7 | Adapter plugin interface in Python so contributors can add new agent types | P0 |
| FR-8 | Inventory each agent's capabilities: tools, MCP servers, skills, filesystem scope, network egress, environment secrets | P1 |
| FR-9 | Flag each agent's risks from its capabilities and sandbox state, with a severity and the reason for each flag (D10) | P1 |
| FR-10 | List OpenShell sandboxes, which agent runs in each, and the active policy | P1 |
| FR-11 | Generate a starter OpenShell policy from an agent's observed capabilities | P1 |
| FR-12 | Edit and apply sandbox policies, with diff, dry-run and one-click rollback | P1 |
| FR-13 | Start, stop and kill agents from the UI | P1 |
| FR-14 | Budgets and alerts per agent: tokens, cost, error rate | P1 |
| FR-15 | Edit agent config and skills from the UI, with versioning | P2 |
| FR-16 | Rotate an agent's API keys and secrets | P2 |
| FR-17 | Audit log of every admin action: who, what, when, before and after | P1, required before any write action ships |

## Architecture

openvigie has two parts: a collector daemon on every host and one hub that stores data and serves the UI. Collectors push to the hub over Tailscale; the hub pushes policies and admin actions back.

Each collector loads one Python adapter per agent type. An adapter answers three questions: is this agent here, what is it doing, and what can it access. Agents that already emit OpenTelemetry (Claude Code, Codex) are read directly; others are read from logs, configs or their own APIs.

## Security, privacy and threat model

openvigie can stop agents, change their sandboxes and rotate their secrets, so the hub is the most valuable target on the network. It must be harder to compromise than the agents it governs.

**Access**

- The hub listens on localhost only by default. Remote access goes through Tailscale, and Tailscale identity decides who is an admin. In practice, `tailscale serve` proxies the tailnet to the hub on `127.0.0.1` (D6).
- Collectors authenticate to the hub with a per-host token created at enrolment.
- Read-only and admin roles. Write actions need the admin role.

**Threats to design against**

- An agent on the same host reads the collector's token or calls the hub to loosen its own sandbox.
- A prompt-injected agent edits its own config or skills to gain access.
- Secrets and personal data leak through traces sent to Langfuse.
- A bad policy locks an agent out, or a loose one silently opens it up.

**Mitigations**

- The collector runs as its own system user, outside every agent sandbox, with its token readable only by that user (Linux; on macOS it runs as the agents' user, see D8).
- Every write action is logged in the audit log (FR-17), shows a diff and supports rollback.
- Prompts and outputs are redacted before export by default: API keys, tokens and common secret patterns.
- Configurable retention for usage data and traces.
- Secret rotation ships last (P2) and only after an external security review.

## Non-functional requirements

| Area | Requirement |
| --- | --- |
| Platforms | Linux ARM64 (Raspberry Pi 4 and 5) and macOS on Apple Silicon for the MVP; Linux x86_64 next |
| Footprint | Collector under 100 MB RAM and under 2% CPU on a Pi 5 at idle; hub runs on the same Pi |
| Install | One command per host, for example `pipx install openvigie`, then `openvigie enrol` |
| Language | Python 3.12+ for collector, hub and adapters |
| Storage | Built-in SQLite store by default; Langfuse optional; no Postgres or ClickHouse needed for the MVP |
| Freshness | Agent status in the UI within 10 seconds of a change |
| Extensibility | New agent types added as Python plugins without changing the core |
| Telemetry standard | OpenTelemetry GenAI semantic conventions as the internal format |
| License | MIT |
| Privacy | No phone-home and no usage telemetry from openvigie itself |

## Success metrics and milestones

The MVP succeeds when a new user sees their agents and their spend within minutes of installing.

- Time from install to first detected agent: under 5 minutes on a Pi or a Mac.
- Detection coverage: 100% of running Hermes, OpenClaw, Claude Code and Codex instances found on test hosts.
- Cost accuracy: within 5% of the provider's billing for the same period.
- Adoption: 3 adapters contributed by people outside the core team in the first 6 months.
- Safety: zero write actions without an audit log entry.

Each phase ships on its own. Write actions start in phase 2 only once the audit log and roles are live, and secret rotation waits for an external review.

## Decision log

Decisions taken after this PRD was drafted. Each one is tracked in the roadmap ([#4](https://github.com/victorbonnet/openvigie/issues/4)).

| ID | Date | Decision | Affects |
| --- | --- | --- | --- |
| D1 | 2026-10-14 | Web UI is server-rendered with FastAPI, Jinja2 (autoescape on) and HTMX. All assets are vendored; no CDN. | Open question *Web UI stack*, FR-5 |
| D2 | 2026-10-14 | MVP detection uses running processes, known config folders and listening ports only. Hooks injected into agents are deferred until after the MVP. | Open question *Detection depth*, FR-1 |
| D3 | 2026-10-14 | Claude Code and Codex usage is read from local session logs first (`~/.claude/projects`, `~/.codex/sessions`), so it works with no agent setup. An OTLP/HTTP receiver follows for users who enable agent telemetry. | FR-3 |
| D4 | 2026-10-14 | The price table ships with the package and is edited through an override file. The UI shows it read-only; editing it from the web is a later write action. | FR-4 |
| D5 | 2026-10-14 | The hub, not the collectors, exports traces to Langfuse, so the Langfuse key lives on one machine. Export is metadata only by default; prompt and output content is opt-in and redacted. | FR-6, Security |
| D6 | 2026-10-14 | Remote access uses `tailscale serve` in front of a hub bound to `127.0.0.1`. The hub does not bind a tailnet or public interface unless the operator explicitly opts in. | Security → Access |
| D7 | 2026-10-14 | Collectors push to the hub over HTTP JSON with a per-host bearer token from enrolment, even when hub and collector share a host. | Architecture, Security → Access |
| D8 | 2026-10-14 | Collector user model. Linux: a dedicated `openvigie` system user with `CAP_DAC_READ_SEARCH`, confined by systemd to read-only bind mounts of the agent folders (homes otherwise hidden). Read ACLs were rejected: agents create `0600` files, which get an empty ACL mask. macOS (and simple Linux setups): a per-user service, documented as weaker isolation. | Open question *Collector user model*, Security → Mitigations |
| D9 | 2026-10-14 | Usage data and status history are kept forever by default; retention is opt-in per data type in `hub.toml` (`[retention]`). Spans are never kept: unexported ones leave the export queue after `span_queue_hours` (24 by default). Bundled prices not yet checked against the provider are shipped flagged `unverified`, and the UI says so wherever they are used. | Open question *Retention defaults*, FR-4, Security → Mitigations |
| D10 | 2026-10-14 | Risk is reported as rule-based flags, not a numeric score. Each flag has a severity (low, medium, high) and a human-readable reason, and an agent's risk level is its highest flag. Rules are declarative data shipped with the package and overridable, like the price table. | Open question *Risk scoring rules*, FR-9 |
| D11 | 2026-10-14 | OpenShell needs Linux, so agents that should be sandboxed on a Mac run inside a Linux VM or container (for example Lima, OrbStack or a devcontainer), with a collector inside it that reports like any Linux host. Agents running directly on macOS are monitored and risk-flagged, and the UI says they can't be sandboxed there. | Open question *macOS sandboxing*, FR-10 |

## Open questions and risks

**Open questions**

- [x] macOS sandboxing: OpenShell relies on Linux features (Landlock, seccomp, network namespaces). Do Mac agents run in a Linux VM or container, or stay monitor-only? *Resolved by D11.*
- [x] Detection depth: process and config scanning only, or also hooks injected into each agent (for example Claude Code hooks)? *Resolved for the MVP by D2; revisit after.*
- [x] Risk scoring rules: which capabilities count as high risk, and how are scores weighted? *Resolved by D10: flags with severities, no weights.*
- [x] Retention defaults for usage data and traces. *Resolved by D9: kept forever by default, configurable.*
- [x] Web UI stack: server-rendered (FastAPI + HTMX) or a separate front end? *Resolved by D1.*
- [x] Collector user model: a dedicated system user (as the mitigations above require) cannot read agent folders in other users' homes by default. Grant read-only access with ACLs, or run the collector as each agent's user with weaker isolation (especially on macOS)? *Resolved by D8.*

**Risks**

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Agent CLIs change their config or log formats often | Adapters break silently | Adapter contract tests and a health status per adapter in the UI |
| Not every agent emits token usage | Incomplete cost | Mark usage as estimated or unknown rather than zero |
| OpenShell's API or policy format changes | Policy editor breaks | Isolate OpenShell behind one module; pin supported versions |
| Hub compromise gives control of every agent | High | Localhost and Tailscale only, roles, audit log, write actions shipped late |
| Scope creep into a full agent platform | MVP slips | Hold the non-goals; monitoring ships first |
