# Contributing an adapter

An adapter teaches openvigie about one agent type. It answers three questions: is the agent
here, what is it doing, and (from P1) what can it access. The full interface guide lands with
the adapter plugin interface (#27); this page covers the part every adapter needs first:
**test fixtures recorded from a real install**.

## Recording fixtures

Adapters are tested only against recorded samples, never against real agents. Record them
with the scrubbing recorder:

```sh
uv run scripts/record_fixture.py <agent-type> <agent-dir> tests/adapters/<agent-type>/fixtures/<version>
# e.g.
uv run scripts/record_fixture.py claude-code ~/.claude tests/adapters/claude_code/fixtures/2.1
```

Useful options: `--max-files N`, `--max-lines N`, and `--include GLOB` (repeatable) to add
files outside the default allow-list for that agent.

What the recorder does:

- Copies only allow-listed files and **never** copies credential files: `.env*`, `auth.json`,
  `*.pem`, `*.key`, and names containing `credential`, `secret`, `token` or `cookie`.
- Redacts secrets, replaces your home path, user name, host name, e-mails and IPs, and
  replaces free text (prompts, outputs, tool I/O, long strings) with placeholders.
- Keeps structure, ids, timestamps, model names and usage numbers, which are what the tests
  need.
- Checks the result a second time and writes nothing if any secret pattern still matches.

## Before you commit a fixture

1. Read every file in the fixture. The recorder is a safety net, not a guarantee.
2. Fill in the "To fill in by hand" section of `MANIFEST.md`: agent version, how it runs,
   process signature, ports, where usage is recorded, and expected totals per session.
3. Commit the fixture in its own PR, or with the adapter that uses it.
