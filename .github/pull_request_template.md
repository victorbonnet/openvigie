## What

<!-- One concern per PR. Reference requirements: FR-1, FR-3… -->

## Why / PRD

- Requirements: FR-
- PRD changes needed: none / section …

## Checklist

- [ ] `ruff format --check`, `ruff check`, `mypy`, `pytest` pass
- [ ] No new runtime dependency, or justified here: …
- [ ] Untrusted input (agent configs/logs/processes) handled safely; no secrets logged or exported
- [ ] Read-only (P0) — or the audit log (FR-17) covers this write action
