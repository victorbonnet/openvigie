# Sandboxing agents

openvigie shows, for every agent, whether it runs in a sandbox and what it can reach: files,
network, shell, MCP servers, secrets. It flags the risky combinations (FR-8, FR-9). Managing
[NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) sandboxes from openvigie (inventory,
starter policies, applying a policy with diff and rollback) comes next. It targets the latest
OpenShell release; the minimum supported version is OpenShell v0.1.4.

## Linux

OpenShell isolates an agent with Landlock, seccomp and a network proxy. Network rules are
named blocks of allowed endpoints and the binaries allowed to use them. Network rules can be
changed on a running sandbox; filesystem and process rules only take effect in a new one.

## macOS

OpenShell relies on Linux kernel features, so it can't sandbox an agent that runs directly on
macOS (PRD decision D11). On a Mac:

1. Run the agent inside a Linux VM or container, for example with
   [Lima](https://lima-vm.io), [OrbStack](https://orbstack.dev) or a devcontainer.
2. Install OpenShell and an openvigie collector **inside** that VM, and enrol it with your hub
   like any Linux host (`openvigie enrol https://<hub>.<tailnet>.ts.net <code>`).
3. The VM then shows up as its own host, and its agents can be sandboxed and managed like on
   Linux.

Agents left running directly on macOS are still detected, metered and risk-flagged; their
page says they can't be sandboxed there.
