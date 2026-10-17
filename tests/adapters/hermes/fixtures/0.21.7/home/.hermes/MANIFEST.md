# Fixture: hermes (2026-10-17)

Recorded with `scripts/record_fixture.py` from a real install, then scrubbed.

## Files

- `config.yaml`: yaml (comments dropped)
- `gateway_state.json`: json
- `gateway.pid`: 1 lines, only pattern-redacted: **check it by hand**
- `.env`, `profiles/<profile>/.env` (added 2026-10-17, #219): access settings only, every
  other line dropped unread; chat and user ids replaced by placeholders. A profile folder here
  holds only its `.env`, so detection doesn't count it as a profile. The `pairing/` and
  `platforms/pairing/` folders were empty on the recorded install: no approvals sample
- `state.db`: sqlite, 15 tables; dropped full-text index messages_fts, messages_fts_trigram; dropped 11 triggers; newest 200 of 24374 rows in messages
- `sessions/20260509_215746_c9848bfd.jsonl`: 30 lines
- `sessions/20260510_082739_9be81c1f.jsonl`: 82 lines
- `sessions/20260511_094828_ce264862.jsonl`: 62 lines
- `sessions/20260512_084952_f187ca93.jsonl`: 47 lines
- `sessions/20260513_081342_b09c6ad1.jsonl`: 25 lines
- `sessions/20260516_074613_37d88d9c.jsonl`: 119 lines
- `sessions/20260516_100307_67521b.jsonl`: 170 lines
- `sessions/20260517_084420_2a2da80e.jsonl`: 56 lines
- `sessions/20260517_102748_8ea53433.jsonl`: 20 lines
- `sessions/20260521_200801_7bd747a7.jsonl`: 3 lines
- `sessions/20260522_081327_0a20dcd3.jsonl`: 7 lines
- `sessions/request_dump_20260510_082739_9be81c1f_20260510_084127_911278.json`: json
- `sessions/request_dump_20260510_093945_4267f7_20260510_094014_451190.json`: json
- `sessions/request_dump_20260511_094828_ce264862_20260512_024528_462467.json`: json
- `sessions/request_dump_20260517_084420_2a2da80e_20260517_101013_870848.json`: json
- `sessions/request_dump_20260517_084420_2a2da80e_20260517_101830_079057.json`: json

## Dropped (never copied)

- none

## Long numbers left in the output (check each one by hand)

Numbers of 9 to 15 digits that aren't a placeholder or a timestamp: hashes and tool ids are
expected; a chat, group, bot or phone number is not.

- `sessions/request_dump_20260510_082739_9be81c1f_20260510_084127_911278.json`: `20539468215`
- `sessions/request_dump_20260510_093945_4267f7_20260510_094014_451190.json`: `710086862337548`
- `state.db` async_delegations.owner_started_at: `225909777`, `253260789`, `275717560`
- `state.db` messages.display_metadata: `222905398`
- `state.db` messages.tool_call_id: `8297353029`
- `state.db` messages.tool_call_uid: `153819706996`, `834284897`, `90154974057`, `935694081`
- `state.db` messages.tool_call_uids: `153819706996`, `90154974057`, `935694081`
- `state.db` session_turn_leases.holder: `4026531836`
- `state.db` system_prompts.hash: `00272294295`, `0533740792`, `057126580798`, `065885766`, `080336526`, `08756968809`, `11222341541`, `133064796`, `15978285523723`, `160189535`, `16039618342`, `201575123` (+21 more)

## To fill in by hand (SURVEY.md has the files, processes and ports seen)

- Agent version (`<agent> --version`): Hermes Agent v0.21.7+756.ge06df9c (2026.9.24), git install
- How it runs (CLI, gateway, systemd unit, Docker): one gateway, systemd user unit
  `hermes-gateway.service` (linger on), serving every profile of the home; Raspberry Pi 5,
  Debian 13 aarch64
- Process name / command-line signature: `hermes`, the install's own Python
  (`~/.hermes/tools/python-*/bin/python3`) running `hermes_cli/main.py gateway run`, plus a
  `tools/mcp_death_supervisor.py --parent-pgid <gateway pid>` helper; `gateway.pid` has the argv
- Listening ports: none (messaging platforms are polled)
- Config and state paths, and the environment variables that move them: `HERMES_HOME`
  (default `~/.hermes`); `config.yaml`, `gateway_state.json`, `gateway.pid`, `state.db`,
  `sessions/`; other profiles under `profiles/<name>/` with their own `config.yaml` and `state.db`
- Where token usage is recorded (file, field names): `state.db`, table `session_model_usage`
  (running totals per session, model, billing route and task): `input_tokens`, `output_tokens`,
  `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, `first_seen`, `last_seen`;
  `input_tokens` leaves cache reads out
- Expected totals per session (for contract tests): 196 rows over 164 sessions. For example
  `20260912_101456_b71ea675`: `deepseek/deepseek-v4.1-flash` (nous) input 633074, output 30313,
  cache read 7662464, reasoning 21054; `gpt-5.6-sol` (openai-codex) input 832546, output 31835,
  cache read 9158016, reasoning 13176. `20261002_192652_c39156`: `gpt-6.1-sol` input 10381,
  output 5
- Recorded while: gateway running, no conversation in progress

## Review checklist

- [x] No API keys, tokens, passwords or cookies (the recorder checked secret patterns)
- [x] No personal content: prompts, outputs, names, e-mails, chat contacts
- [x] Home paths, user and host names replaced
- [x] Usage numbers and ids kept intact
