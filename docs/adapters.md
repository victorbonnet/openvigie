# Writing an adapter

An adapter is a Python class that teaches openvigie about one agent type. The collector loads
it through the `openvigie.adapters` entry-point group and calls it on every tick.

## The interface

```python
from openvigie.adapters.base import ActivityBatch, Adapter, AdapterState, HostContext
from openvigie.model import AgentInstance, stable_id


class MyAgentAdapter(Adapter):
    agent_type = "my-agent"  # slug, must equal the entry-point name
    display_name = "My Agent"

    def detect(self, ctx: HostContext) -> list[AgentInstance]:
        """Is the agent here? One instance per install (e.g. per config folder)."""
        found = []
        for home in ctx.homes:
            root = home / ".my-agent"
            if root.is_dir():
                found.append(
                    AgentInstance(
                        agent_id=stable_id(ctx.host.host_id, self.agent_type, str(root)),
                        host_id=ctx.host.host_id,
                        agent_type=self.agent_type,
                        name=self.display_name,
                        config_root=str(root),
                        process_running=any(p.name == "my-agent" for p in ctx.processes()),
                    )
                )
        return found

    def collect(self, ctx: HostContext, agent: AgentInstance, state: AdapterState) -> ActivityBatch:
        """What is it doing? Activity, sessions and usage since the cursors in `state`."""
        return ActivityBatch()
```

`capabilities()` ("what can it access?") is reserved for P1 (FR-8).

## Rules

- **Touch the host only through `HostContext`**: `ctx.homes`, `ctx.processes()`, `ctx.ports()`,
  `ctx.env(...)`, `ctx.sandbox_of(pid)`, `ctx.now()`. That keeps adapters testable offline.
- **Read agent files only with `openvigie.safeio`**: bounded reads, no symlinks, malformed lines
  skipped and counted in `ActivityBatch.stats`, which feeds the adapter's health in the UI.
- Never execute the agent's binary, never read credential files, and never keep prompt or output
  text in usage records.
- Agent ids must be stable: derive them with `stable_id(host_id, agent_type, <install key>)`.
- Unknown is not zero: leave token counts as `None` when you can't read them.
- Store read positions with `state.stage(key, cursor.to_dict())`. The collector commits them
  only after the hub has stored the data.
- Raising is safe but costly: the collector catches it and marks the adapter `error`.

## Registering

Built-in adapters live in `src/openvigie/adapters/<name>/` and are declared in `pyproject.toml`:

```toml
[project.entry-points."openvigie.adapters"]
my-agent = "openvigie.adapters.my_agent:MyAgentAdapter"
```

Third-party packages declare the same entry point. They run inside the collector with its
privileges, so users must enable them by name in `collector.toml`:

```toml
enable_plugins = ["my-agent"]
```

## Testing

Subclass the contract suite from `openvigie.adapters.testing` and describe fake hosts built from
recorded fixtures (see [contributing-adapters.md](contributing-adapters.md)):

```python
from openvigie.adapters.testing import AdapterContract, fake_context, fake_process


class TestMyAgent(AdapterContract):
    adapter_cls = MyAgentAdapter

    def scenarios(self, tmp_path):
        home = copy_fixture(tmp_path)  # your helper
        return {
            "installed": fake_context(homes=[home]),
            "running": fake_context(homes=[home], processes=[fake_process(42, "my-agent")]),
        }
```

The suite checks that an empty host detects nothing, that ids are stable and unique, and that
every instance and usage record belongs to the right host and agent and survives the wire codec.
