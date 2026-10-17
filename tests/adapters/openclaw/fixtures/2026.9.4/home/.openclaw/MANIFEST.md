# Fixture: openclaw (2026-10-17)

Recorded with `scripts/record_fixture.py` from a real install, then scrubbed.

## Files

- `openclaw.json`: json
- `agents/vulpes/agent/openclaw-agent.sqlite`: sqlite, 40 tables; dropped full-text index standing_intents_fts, session_transcript_fts, memory_index_chunks_fts, memory_index_paths_fts; dropped 16 triggers
- `agents/lutra/agent/openclaw-agent.sqlite`: sqlite, 40 tables; dropped full-text index standing_intents_fts, session_transcript_fts; dropped 13 triggers
- `agents/main/agent/openclaw-agent.sqlite`: sqlite, 40 tables; dropped full-text index memory_index_chunks_fts, standing_intents_fts, session_transcript_fts, memory_index_paths_fts; dropped 16 triggers
- `agents/main/agent/openclaw-agent.sqlite.reindex-lock.sqlite`: sqlite, 0 tables
- `agents/main/sessions/0522830f-08a9-4d33-917f-04ac9c44cb58.trajectory.jsonl`: 102 lines
- `agents/main/sessions/0be2a4d7-9d3d-45d9-940a-852ba454885f.trajectory.jsonl`: 22 lines
- `agents/main/sessions/13b2fe0a-f73d-4b1b-a30e-7a57cc7ef591.trajectory.jsonl`: 7 lines
- `agents/main/sessions/16ee7327-d91e-4c39-82c3-5cd05941dba4.jsonl`: 1 lines
- `agents/main/sessions/1aa4b39c-6bf8-46ef-baae-b6d7ac1c593b.trajectory.jsonl`: 7 lines
- `agents/main/sessions/1c1ccc7d-27f5-4c88-9b5c-4095e6f80706.jsonl`: 13 lines
- `agents/main/sessions/1c1ccc7d-27f5-4c88-9b5c-4095e6f80706.trajectory.jsonl`: 7 lines
- `agents/main/sessions/1d8d8d48-f4aa-4820-98c5-479080b582a9.trajectory.jsonl`: 54 lines
- `agents/main/sessions/1fb6feed-4f35-497d-93a4-ebd0ecf569fe.trajectory.jsonl`: 7 lines
- `agents/main/sessions/2760a019-bf22-4560-bcde-dec9b3e5c544.trajectory.jsonl`: 5 lines
- `agents/main/sessions/2ec7b8e0-9644-48bc-ace9-c47ae9fbe8ef.trajectory.jsonl`: 7 lines
- `agents/main/sessions/337ea29d-24bd-441c-925b-751ea047138d.trajectory.jsonl`: 7 lines
- `agents/main/sessions/352a19c5-6a6e-40aa-b8aa-f08214936b91.jsonl`: 13 lines
- `agents/main/sessions/352a19c5-6a6e-40aa-b8aa-f08214936b91.trajectory.jsonl`: 7 lines
- `agents/main/sessions/3a090eb7-a98f-40c7-815d-76d1dd88601a.trajectory.jsonl`: 7 lines

## Dropped (never copied)

- none

## Long numbers left in the output (check each one by hand)

Numbers of 9 to 15 digits that aren't a placeholder or a timestamp: hashes and tool ids are
expected; a chat, group, bot or phone number is not.

- `agents/vulpes/agent/openclaw-agent.sqlite` conversations.conversation_id: `4201149747659`
- `agents/vulpes/agent/openclaw-agent.sqlite` memory_index_chunk_provenance.chunk_id: `019298846`, `05019271539`, `1149704390956`, `120595140`, `166540820`, `166739031796`, `19090683796`, `197761592`, `223597700`, `224359522`, `239221567`, `243889580` (+23 more)
- `agents/vulpes/agent/openclaw-agent.sqlite` memory_index_chunks.hash: `080335683`, `096365477`, `150306055350`, `186974577`, `241986226`, `267833153`, `27595326561`, `2968620767425`, `48386812276`, `5373502039428`, `5914892160`, `7018008740` (+5 more)
- `agents/vulpes/agent/openclaw-agent.sqlite` memory_index_chunks.id: `019298846`, `05019271539`, `1149704390956`, `120595140`, `166540820`, `166739031796`, `19090683796`, `197761592`, `223597700`, `224359522`, `239221567`, `243889580` (+23 more)
- `agents/vulpes/agent/openclaw-agent.sqlite` session_conversations.conversation_id: `4201149747659`
- `agents/vulpes/agent/openclaw-agent.sqlite` session_windows.primary_conversation_id: `4201149747659`
- `agents/vulpes/agent/openclaw-agent.sqlite` transcript_rewrite_watermarks.generation: `2586848108312`, `6997166402`, `877104866`
- `agents/lutra/agent/openclaw-agent.sqlite` session_transcript_index_state.leaf_event_id: `57782532165`, `7673502669`
- `agents/lutra/agent/openclaw-agent.sqlite` transcript_event_identities.event_id: `57782532165`, `7673502669`
- `agents/main/agent/openclaw-agent.sqlite` memory_index_chunk_provenance.chunk_id: `010642552`, `063072699`, `071257467`, `078285826`, `0796949251`, `091989680`, `123650093`, `1258544523`, `1301823593`, `140796063642370`, `14853372064761`, `189530342` (+32 more)
- `agents/main/agent/openclaw-agent.sqlite` memory_index_chunks.hash: `000097252079386`, `008695706`, `0298392076241`, `0436437504`, `0557527474704`, `0861189483`, `094348307`, `150351692`, `172859141`, `198279962`, `207304590`, `209091367` (+32 more)
- `agents/main/agent/openclaw-agent.sqlite` memory_index_chunks.id: `010642552`, `063072699`, `071257467`, `078285826`, `0796949251`, `091989680`, `123650093`, `1258544523`, `1301823593`, `140796063642370`, `14853372064761`, `189530342` (+32 more)
- `agents/main/agent/openclaw-agent.sqlite` transcript_event_identities.event_id: `4995225897`
- `agents/main/agent/openclaw-agent.sqlite` transcript_event_identities.parent_id: `4995225897`
- `agents/main/agent/openclaw-agent.sqlite` transcript_rewrite_watermarks.generation: `3123136807`, `868590150`
- `agents/main/sessions/0522830f-08a9-4d33-917f-04ac9c44cb58.trajectory.jsonl`: `172800000`, `699375046`
- `agents/main/sessions/0be2a4d7-9d3d-45d9-940a-852ba454885f.trajectory.jsonl`: `172800000`, `84319954151`, `88424246019`, `990817107`
- `agents/main/sessions/13b2fe0a-f73d-4b1b-a30e-7a57cc7ef591.trajectory.jsonl`: `172800000`
- `agents/main/sessions/1aa4b39c-6bf8-46ef-baae-b6d7ac1c593b.trajectory.jsonl`: `172800000`
- `agents/main/sessions/1c1ccc7d-27f5-4c88-9b5c-4095e6f80706.trajectory.jsonl`: `172800000`
- `agents/main/sessions/1d8d8d48-f4aa-4820-98c5-479080b582a9.trajectory.jsonl`: `172800000`, `8142468654`
- `agents/main/sessions/1fb6feed-4f35-497d-93a4-ebd0ecf569fe.trajectory.jsonl`: `172800000`
- `agents/main/sessions/2ec7b8e0-9644-48bc-ace9-c47ae9fbe8ef.trajectory.jsonl`: `172800000`
- `agents/main/sessions/337ea29d-24bd-441c-925b-751ea047138d.trajectory.jsonl`: `172800000`
- `agents/main/sessions/352a19c5-6a6e-40aa-b8aa-f08214936b91.trajectory.jsonl`: `172800000`
- `agents/main/sessions/3a090eb7-a98f-40c7-815d-76d1dd88601a.trajectory.jsonl`: `172800000`

## To fill in by hand (SURVEY.md has the files, processes and ports seen)

- Agent version (`<agent> --version`): OpenClaw 2026.9.4 (3a9d69d), npm global install;
  `meta.lastTouchedVersion` in `openclaw.json` says the same
- How it runs (CLI, gateway, systemd unit, Docker): one gateway for all agents (`main`, `lutra`,
  `vulpes`), systemd user unit `openclaw-gateway.service` (linger on); Raspberry Pi 5,
  Debian 13 aarch64
- Process name / command-line signature: `node …/node_modules/openclaw/dist/index.js gateway
  --port 18789` (the process names itself `MainThread`)
- Listening ports: 18789 (gateway and control UI)
- Config and state paths, and the environment variables that move them: `OPENCLAW_STATE_DIR`
  (default `~/.openclaw`); `openclaw.json` (JSON5), `agents/<id>/agent/openclaw-agent.sqlite`,
  `agents/<id>/sessions/`, `workspace-<id>/`
- Where token usage is recorded (file, field names): assistant `message` events with `usage`
  (`input`, `output`, `cacheRead`, `cacheWrite`) in `transcript_events` of
  `agent/openclaw-agent.sqlite` (2026.9) and in older `sessions/<id>.jsonl`; `input` leaves
  cache reads out. The recorder scrubs `transcript_events.event_json` whole, so tests build
  those rows from the `.jsonl` files. `*.trajectory.jsonl` holds run traces, not read for usage
- Expected totals per session (for contract tests): `1c1ccc7d-…`: 3 assistant messages,
  `moonshotai/Kimi-K2.6:deepinfra`, input 26560, output 1435, cache read 38400;
  `352a19c5-…`: 3 messages, same model, input 18635, output 929, cache read 28928;
  `16ee7327-…`: 1 message, `claude-sonnet-4-6`, all zero
- Recorded while: gateway running, no conversation in progress

## Review checklist

- [x] No API keys, tokens, passwords or cookies (the recorder checked secret patterns)
- [x] No personal content: prompts, outputs, names, e-mails, chat contacts
- [x] Home paths, user and host names replaced
- [x] Usage numbers and ids kept intact
