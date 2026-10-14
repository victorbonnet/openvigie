# Security policy

openvigie watches, and later controls, AI agents on your machines. A flaw in it can expose
every agent it governs, so we treat security reports as the highest priority.

## Supported versions

openvigie is pre-1.0. Only the latest release on PyPI and the `main` branch get security fixes.

## Reporting a vulnerability

**Please do not open a public issue.** Report privately through GitHub:
**Security → Report a vulnerability** on this repository (GitHub private vulnerability reporting).

Include what you can: affected version or commit, the component (collector, hub, an adapter),
steps to reproduce, and the impact you expect.

What to expect:

- Acknowledgement within 3 working days.
- A first assessment within 10 working days.
- A fix or mitigation plan agreed with you before any public disclosure, and credit in the
  release notes if you want it.

## Scope

In scope: anything in this repository, including how it reads agent files, stores data,
authenticates collectors, serves the web UI and exports traces.

Out of scope: vulnerabilities in the agents themselves (Claude Code, Codex, Hermes, OpenClaw),
in Tailscale, Langfuse or OpenShell. Report those to their maintainers.

## Design rules

The trust boundaries and the rules every change must follow are in
[`docs/threat-model.md`](docs/threat-model.md).
