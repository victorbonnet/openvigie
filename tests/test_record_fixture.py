import importlib.util
import json
import re
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "record_fixture.py"
    spec = importlib.util.spec_from_file_location("record_fixture", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["record_fixture"] = module
    spec.loader.exec_module(module)
    return module


rf = _load()

KEY = "sk-ant-api03-" + "Z" * 40
HOME = "/home/alice"


def make_claude_home(root: Path) -> Path:
    claude = root / ".claude"
    project = claude / "projects" / "-home-alice-src"
    project.mkdir(parents=True)
    (claude / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_API_KEY": KEY}, "model": "claude-sonnet-4-5"})
    )
    (claude / ".credentials.json").write_text(json.dumps({"token": KEY}))
    (claude / ".env").write_text(f"ANTHROPIC_API_KEY={KEY}\n")
    lines = [
        {
            "type": "user",
            "sessionId": "s1",
            "cwd": f"{HOME}/src",
            "message": {"role": "user", "content": f"my key is {KEY}, mail alice@corp.com"},
        },
        {
            "type": "assistant",
            "sessionId": "s1",
            "timestamp": "2026-10-14T10:00:00Z",
            "message": {
                "id": "msg_1",
                "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": "Sure, here is a long answer " * 20}],
                "usage": {"input_tokens": 12, "output_tokens": 345, "cache_read_input_tokens": 7},
            },
        },
    ]
    (project / "s1.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    return claude


def scrubber() -> object:
    return rf.Scrubber(HOME, "alice", "alice-mbp")


def all_text(root: Path) -> str:
    return "\n".join(
        p.read_bytes().decode("utf-8", "replace") for p in sorted(root.rglob("*")) if p.is_file()
    )


def test_records_scrubbed_fixture(tmp_path: Path) -> None:
    source = make_claude_home(tmp_path / "home")
    dest = tmp_path / "fixture"
    # try to pull in dotfiles too: the deny list must still win
    rf.record("claude-code", source, dest, extra_include=[".*"], scrubber=scrubber())

    text = all_text(dest)
    assert KEY not in text
    assert "alice" not in text
    assert "corp.com" not in text
    assert not (dest / ".credentials.json").exists()
    assert not (dest / ".env").exists()

    session = [json.loads(x) for x in (dest / "projects/-home-user-src/s1.jsonl").open()]
    assert session[1]["message"]["usage"] == {
        "input_tokens": 12,
        "output_tokens": 345,
        "cache_read_input_tokens": 7,
    }
    assert session[1]["message"]["id"] == "msg_1"
    assert session[1]["timestamp"] == "2026-10-14T10:00:00Z"
    assert session[1]["message"]["model"] == "claude-sonnet-4-5"
    assert session[1]["message"]["content"][0]["text"].startswith("[scrubbed text")
    assert session[0]["cwd"].startswith("[scrubbed text")

    settings = json.loads((dest / "settings.json").read_text())
    assert settings["env"]["ANTHROPIC_API_KEY"] == "[REDACTED:key]"
    manifest = (dest / "MANIFEST.md").read_text()
    assert ".credentials.json" in manifest and "`.env`" in manifest


def test_refuses_when_secrets_survive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = make_claude_home(tmp_path / "home")
    s = scrubber()
    monkeypatch.setattr(s, "value", lambda value, key=None: value)  # broken scrubber
    with pytest.raises(SystemExit, match="still match"):
        rf.record("claude-code", source, tmp_path / "fixture", scrubber=s)
    assert not (tmp_path / "fixture").exists()


def test_sqlite_text_columns_scrubbed(tmp_path: Path) -> None:
    source = tmp_path / ".hermes"
    source.mkdir()
    db = source / "state.db"
    with sqlite3.connect(db) as conn:
        conn.execute("create table messages (id text, content text, input_tokens integer)")
        conn.execute("insert into messages values ('m1', ?, 42)", (f"secret {KEY}",))
    dest = tmp_path / "fixture"
    rf.record("hermes", source, dest, scrubber=scrubber())
    with sqlite3.connect(dest / "state.db") as conn:
        row = conn.execute("select id, content, input_tokens from messages").fetchone()
    assert row[0] == "m1"
    assert row[1].startswith("[scrubbed text")
    assert row[2] == 42


def test_sqlite_unique_text_column_stays_unique(tmp_path: Path) -> None:
    # Hermes' state.db has a unique session title; equal placeholders would violate it
    source = tmp_path / ".hermes"
    source.mkdir()
    with sqlite3.connect(source / "state.db") as conn:
        conn.execute("create table sessions (id text primary key, title text unique)")
        conn.executemany("insert into sessions values (?, ?)", [("s1", "Fix bug"), ("s2", "Plan")])
    dest = tmp_path / "fixture"
    rf.record("hermes", source, dest, scrubber=scrubber())
    with sqlite3.connect(dest / "state.db") as conn:
        rows = conn.execute("select id, title from sessions order by id").fetchall()
    assert [r[0] for r in rows] == ["s1", "s2"]
    assert all(r[1].startswith("[scrubbed text") for r in rows)
    assert len({r[1] for r in rows}) == 2


@pytest.mark.parametrize("conflict", ["rollback", "ignore", "replace", "abort", "fail"])
def test_sqlite_unique_conflict_clause_never_leaks(tmp_path: Path, conflict: str) -> None:
    # the table's ON CONFLICT clause must not undo earlier scrubs, keep raw text or drop rows
    source = tmp_path / ".hermes"
    source.mkdir()
    with sqlite3.connect(source / "state.db") as conn:
        conn.execute("create table messages (id text, content text)")
        conn.execute("insert into messages values ('m1', 'my private prompt')")
        conn.execute(f"create table sessions (id text, title text unique on conflict {conflict})")
        conn.executemany("insert into sessions values (?, ?)", [("s1", "Fix bug"), ("s2", "Plan")])
    dest = tmp_path / "fixture"
    rf.record("hermes", source, dest, scrubber=scrubber())
    with sqlite3.connect(dest / "state.db") as conn:
        content = conn.execute("select content from messages").fetchone()[0]
        titles = [r[0] for r in conn.execute("select title from sessions order by id")]
    assert content.startswith("[scrubbed text")
    assert len(titles) == 2
    assert all(t.startswith("[scrubbed text") for t in titles)
    assert len(set(titles)) == 2


def test_sqlite_unique_column_without_rowid_fails_clearly(tmp_path: Path) -> None:
    source = tmp_path / ".hermes"
    source.mkdir()
    with sqlite3.connect(source / "state.db") as conn:
        conn.execute("create table sessions (id text primary key, title text unique) without rowid")
        conn.executemany("insert into sessions values (?, ?)", [("s1", "Fix bug"), ("s2", "Plan")])
    with pytest.raises(SystemExit, match="sessions"):
        rf.record("hermes", source, tmp_path / "fixture", scrubber=scrubber())
    assert not (tmp_path / "fixture").exists()


def test_unknown_agent_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        rf.record("nope", tmp_path, tmp_path / "out")


TELEGRAM = "1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawxx"
OPENCLAW_JSON5 = f"""{{
  // OpenClaw config: JSON5, so comments and unquoted keys
  agents: {{ defaults: {{ model: "anthropic/claude-opus-5-5" }} }},
  channels: {{ telegram: {{ botToken: "{TELEGRAM}", allowFrom: ["alice@corp.com"] }} }},
  gateway: {{ port: 18789, auth: {{ token: 'gw-0123456789abcdef' }} }},
  apiKey: "{KEY}",
  docs: "https://docs.example.org/a//b", /* a block
  comment */ trailing: [1, 2,],
}}
"""


def make_openclaw(root: Path) -> Path:
    state = root / ".openclaw"
    sessions = state / "agents" / "main" / "sessions"
    sessions.mkdir(parents=True)
    (state / "openclaw.json").write_text(OPENCLAW_JSON5)
    (state / "agents" / "main" / "agent").mkdir()
    (state / "agents" / "main" / "agent" / "auth-profiles.json").write_text(
        json.dumps({"anthropic": {"apiKey": KEY}})
    )
    line = {"type": "message", "message": {"role": "assistant", "content": "hi alice",
            "usage": {"input": 10, "output": 20, "cacheRead": 5}}}  # fmt: skip
    (sessions / "s1.jsonl").write_text(json.dumps(line) + "\n")
    return state


class FakePs:
    """psutil, as far as the survey uses it: two matching processes and the recorder itself."""

    CONN_LISTEN = "LISTEN"
    AccessDenied = PermissionError
    NoSuchProcess = ProcessLookupError

    class Proc:
        def __init__(self, pid: int, name: str, cmdline: list[str], ports: list[int]) -> None:
            self.info = {"pid": pid, "name": name, "cmdline": cmdline}
            self.ports = ports

        def net_connections(self, kind: str) -> list[object]:
            from types import SimpleNamespace

            return [SimpleNamespace(status="LISTEN", laddr=SimpleNamespace(port=p))
                    for p in self.ports]  # fmt: skip

    def process_iter(self, attrs: list[str]) -> list[object]:
        return [
            self.Proc(101, "node", ["node", "/usr/lib/node_modules/openclaw/dist/index.js",
                                     "gateway", "--token", "gw-supersecret-value",
                                     f"--config={HOME}/.openclaw/openclaw.json"], [18789]),
            self.Proc(102, "openclaw", ["openclaw", "tui", "OPENAI_API_KEY=whatever"], []),
            self.Proc(103, "python", ["python", "scripts/record_fixture.py", "openclaw"], []),
            self.Proc(104, "bash", ["bash"], []),
        ]  # fmt: skip


def test_openclaw_json5_and_survey(tmp_path: Path) -> None:
    source = make_openclaw(tmp_path / "home")
    dest = tmp_path / "fixture"
    rf.record("openclaw", source, dest, scrubber=scrubber(), ps=FakePs())
    text = all_text(dest)
    for secret in (KEY, TELEGRAM, "gw-0123456789abcdef", "gw-supersecret-value", "whatever",
                   "alice", "corp.com"):  # fmt: skip
        assert secret not in text, secret
    assert not (dest / "agents/main/agent/auth-profiles.json").exists()
    config = json.loads((dest / "openclaw.json").read_text())
    assert config["gateway"]["port"] == 18789
    assert config["channels"]["telegram"]["botToken"] == "[REDACTED:key]"
    assert config["agents"]["defaults"]["model"] == "anthropic/claude-opus-5-5"
    assert config["docs"] == "https://docs.example.org/a//b"  # "//" inside a string is kept
    assert "JSON5, written as JSON" in (dest / "MANIFEST.md").read_text()
    session = json.loads((dest / "agents/main/sessions/s1.jsonl").read_text())
    assert session["message"]["usage"] == {"input": 10, "output": 20, "cacheRead": 5}

    survey = (dest / "SURVEY.md").read_text()
    assert "`agents/main/sessions/s1.jsonl`" in survey and "(copied)" in survey
    assert "`agents/main/agent/auth-profiles.json`" in survey and "(never copied)" in survey
    assert "gateway --token [REDACTED] --config=/home/user/.openclaw/openclaw.json" in survey
    assert "listening on: 18789" in survey and "OPENAI_API_KEY=[REDACTED]" in survey
    assert "`python`" not in survey and "`bash`" not in survey  # the recorder, unrelated


def test_hermes_yaml_is_scrubbed_structurally(tmp_path: Path) -> None:
    source = tmp_path / ".hermes"
    source.mkdir()
    (source / "config.yaml").write_text(
        "model:\n  default: anthropic/claude-opus-5-5\n  api_key: plainsecretvalue\n"
        "agent:\n  system_prompt: You are Alice's private assistant for corp.com.\n"
        "terminal:\n  backend: local\n  timeout: 180\n"
    )
    dest = tmp_path / "fixture"
    rf.record("hermes", source, dest, scrubber=scrubber(), ps=FakePs())
    import yaml

    config = yaml.safe_load((dest / "config.yaml").read_text())
    assert config["model"] == {"default": "anthropic/claude-opus-5-5",
                               "api_key": "[REDACTED:key]"}  # fmt: skip
    assert config["terminal"] == {"backend": "local", "timeout": 180}
    assert "Alice" not in (dest / "config.yaml").read_text()
    assert "none running" in (dest / "SURVEY.md").read_text()


# Shapes below come from recording real agent installs (#55).
@pytest.mark.parametrize(
    "text",
    [
        "redact_secrets: true",
        '"allowInsecureAuth": false',
        "secretsMasked: null",
        "access_token_env: VAULT_ACCESS_TOKEN",
        "record_key: ctrl+r",
        '"hotkey": "ctrl+shift+k"',
        '"sessionKey": "agent:main:telegram:direct:100000001"',
        "secrets:\n  vault:\n",  # a YAML section, not key: value
        '"sessionKey": "[scrubbed text <= 128 chars]"',
        '"authProfileId": "openai:user@example.com"',  # an e-mail we already replaced
    ],
)
def test_benign_secret_lookalikes_pass(text: str) -> None:
    assert not rf.has_secret(text)


@pytest.mark.parametrize(
    "text",
    [
        'client_auth: "abcd1234wxyz"',
        "access_token_env: abcd1234wxyz",  # not an env var name: a value
        "record_key: hunter2hunter2",
        '"authProfileId": "openai:sk-proj-abcdefgh12345678"',
        f"note {KEY}",
    ],
)
def test_real_secret_shapes_still_match(text: str) -> None:
    assert rf.has_secret(text)


def test_hermes_config_lookalikes_recorded(tmp_path: Path) -> None:
    source = tmp_path / ".hermes"
    source.mkdir()
    (source / "config.yaml").write_text(
        "display:\n  redact_secrets: true\n  show_token_analytics: false\n"
        "voice:\n  record_key: ctrl+r\n"
        "secrets:\n  vault:\n    access_token_env: VAULT_ACCESS_TOKEN\n"
    )
    dest = tmp_path / "fixture"
    rf.record("hermes", source, dest, scrubber=scrubber(), ps=FakePs())
    import yaml

    config = yaml.safe_load((dest / "config.yaml").read_text())
    assert config["voice"]["record_key"] == "ctrl+r"
    assert config["secrets"]["vault"]["access_token_env"] == "VAULT_ACCESS_TOKEN"  # noqa: S105


def test_session_key_peer_ids_replaced_stably(tmp_path: Path) -> None:
    source = tmp_path / ".openclaw"
    sessions = source / "agents" / "main" / "sessions"
    sessions.mkdir(parents=True)
    keys = ["agent:main:telegram:direct:123456789", "agent:main:telegram:direct:123456789",
            "agent:main:whatsapp:direct:+33612345678", "agent:main:main"]  # fmt: skip
    lines = [{"type": "turn", "sessionKey": k, "data": {"note": f"see {k}"}} for k in keys]
    (sessions / "s1.trajectory.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    dest = tmp_path / "fixture"
    rf.record("openclaw", source, dest, scrubber=scrubber(), ps=FakePs())
    text = all_text(dest)
    assert "123456789" not in text and "33612345678" not in text
    written = (dest / "agents/main/sessions/s1.trajectory.jsonl").read_text()
    out = [json.loads(x)["sessionKey"] for x in written.splitlines()]
    assert out[0] == out[1] != out[2]
    assert out[0].startswith("agent:main:telegram:direct:") and out[3] == "agent:main:main"


def test_sqlite_checked_per_value_not_raw_bytes(tmp_path: Path) -> None:
    # adjacent values ("…-authcheck" then "APP_SRC=…") and schema text are not secrets
    source = tmp_path / ".hermes"
    source.mkdir()
    with sqlite3.connect(source / "state.db") as conn:
        conn.execute("create table events (root text, command text)")
        conn.execute("insert into events values ('/tmp/build-authcheck', 'APP_SRC=/opt/h x')")
        conn.execute("create table m (tokenize text check (tokenize = 'trigram'))")
    dest = tmp_path / "fixture"
    rf.record("hermes", source, dest, scrubber=scrubber(), ps=FakePs())
    assert (dest / "state.db").exists()


def test_sqlite_secret_in_a_value_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / ".hermes"
    source.mkdir()
    with sqlite3.connect(source / "state.db") as conn:
        conn.execute("create table t (v blob)")
        conn.execute("insert into t values (?)", (f"x {KEY} y".encode(),))  # blobs aren't scrubbed
    with pytest.raises(SystemExit, match="still match"):
        rf.record("hermes", source, tmp_path / "fixture", scrubber=scrubber(), ps=FakePs())


def test_large_clean_file_passes_and_key_block_fails() -> None:
    clean = "\n".join(json.dumps({"n": i, "pad": "x" * 100}) for i in range(20_000))
    assert len(clean) > 1024 * 1024  # past redact()'s truncation
    assert not rf.text_has_secret(clean)
    assert rf.text_has_secret(clean + "\n-----BEGIN RSA PRIVATE KEY-----\nabc\n")
    assert rf.text_has_secret(clean + f"\nnote {KEY}\n")


PEER = "123456789"


def test_peer_ids_replaced_everywhere_openclaw(tmp_path: Path) -> None:
    source = tmp_path / ".openclaw"
    sessions = source / "agents" / "main" / "sessions"
    sessions.mkdir(parents=True)
    (source / "openclaw.json").write_text(
        json.dumps({"channels": {"telegram": {"allowFrom": [f"tg:{PEER}"]}}})
    )
    line = {"sessionKey": f"agent:main:telegram:direct:{PEER}", "chatId": int(PEER),
            "origin": {"from_id": PEER}}  # fmt: skip
    (sessions / "s1.jsonl").write_text(json.dumps(line) + "\n")
    dest = tmp_path / "fixture"
    rf.record("openclaw", source, dest, scrubber=scrubber(), ps=FakePs())
    assert PEER not in all_text(dest)
    config = json.loads((dest / "openclaw.json").read_text())
    out = json.loads((dest / "agents/main/sessions/s1.jsonl").read_text())
    placeholder = out["sessionKey"].rsplit(":", 1)[1]
    assert config["channels"]["telegram"]["allowFrom"] == [f"tg:{placeholder}"]
    assert out["chatId"] == int(placeholder)
    assert out["origin"]["from_id"] == placeholder


def test_peer_ids_replaced_in_sqlite(tmp_path: Path) -> None:
    source = tmp_path / ".hermes"
    source.mkdir()
    with sqlite3.connect(source / "state.db") as conn:
        conn.execute("create table sessions (id text, user_id text, chat_id integer)")
        conn.execute("insert into sessions values ('s1', ?, ?)", (PEER, int(PEER)))
    (source / "config.yaml").write_text(f"telegram:\n  allowed_users: [{PEER}]\n")
    dest = tmp_path / "fixture"
    rf.record("hermes", source, dest, scrubber=scrubber(), ps=FakePs())
    assert PEER not in all_text(dest)
    with sqlite3.connect(dest / "state.db") as conn:
        user_id, chat_id = conn.execute("select user_id, chat_id from sessions").fetchone()
    assert user_id == str(chat_id)


def test_refuses_when_a_peer_id_survives(tmp_path: Path) -> None:
    source = tmp_path / ".hermes"
    source.mkdir()
    with sqlite3.connect(source / "state.db") as conn:
        conn.execute("create table sessions (chat_id text, raw blob)")  # blobs aren't scrubbed
        conn.execute("insert into sessions values (?, ?)", (PEER, f"to {PEER}".encode()))
    with pytest.raises(SystemExit, match="chat or user ids"):
        rf.record("hermes", source, tmp_path / "fixture", scrubber=scrubber(), ps=FakePs())
    assert not (tmp_path / "fixture").exists()


def test_sqlite_fts_index_and_triggers_dropped(tmp_path: Path) -> None:
    # an FTS index keeps the original terms after its content is scrubbed (Hermes' state.db)
    source = tmp_path / ".hermes"
    source.mkdir()
    with sqlite3.connect(source / "state.db") as conn:
        conn.executescript("""
            create table messages (id integer primary key, content text, token_count integer);
            create virtual table messages_fts using fts5(content, content=messages,
                                                         content_rowid=id);
            create virtual table messages_tri using fts5(content, tokenize='trigram');
            create trigger messages_fts_insert after insert on messages begin
                insert into messages_fts(rowid, content) values (new.id, new.content);
                insert into messages_tri(rowid, content) values (new.id, new.content);
            end;
        """)
        conn.execute("insert into messages values (1, 'zanzibarprivate plans', 42)")
    dest = tmp_path / "fixture"
    rf.record("hermes", source, dest, scrubber=scrubber(), ps=FakePs())
    assert "zanzibar" not in all_text(dest)
    with sqlite3.connect(dest / "state.db") as conn:
        names = {r[0] for r in conn.execute("select name from sqlite_master")}
        row = conn.execute("select content, token_count from messages").fetchone()
    assert names == {"messages"}
    assert row == ("[scrubbed text <= 128 chars]", 42)
    manifest = (dest / "MANIFEST.md").read_text()
    assert "messages_fts" in manifest and "1 trigger" in manifest


def test_sqlite_keeps_newest_rows(tmp_path: Path) -> None:
    source = tmp_path / ".hermes"
    source.mkdir()
    with sqlite3.connect(source / "state.db") as conn:
        conn.execute("create table messages (id integer primary key, token_count integer)")
        conn.executemany("insert into messages values (?, ?)", [(i, i) for i in range(1, 301)])
    dest = tmp_path / "fixture"
    rf.record("hermes", source, dest, max_lines=50, scrubber=scrubber(), ps=FakePs())
    with sqlite3.connect(dest / "state.db") as conn:
        ids = [r[0] for r in conn.execute("select id from messages order by id")]
    assert ids == list(range(251, 301))
    assert "newest 50 of 300 rows in messages" in (dest / "MANIFEST.md").read_text()


def test_sqlite_plain_full_text_table_dropped(tmp_path: Path) -> None:
    source = tmp_path / ".hermes"
    source.mkdir()
    with sqlite3.connect(source / "state.db") as conn:
        conn.execute("create table sessions (id text, input_tokens integer)")
        conn.execute("insert into sessions values ('s1', 7)")
        conn.execute("create virtual table notes using fts5(body)")
        conn.execute("insert into notes values ('please summarise my lawyer letter')")
    dest = tmp_path / "fixture"
    rf.record("hermes", source, dest, scrubber=scrubber(), ps=FakePs())
    assert "lawyer" not in all_text(dest)
    with sqlite3.connect(dest / "state.db") as conn:
        assert conn.execute("select * from sessions").fetchall() == [("s1", 7)]


def test_sqlite_stat_tables_dropped(tmp_path: Path) -> None:
    # ANALYZE samples (sqlite_stat4) are blobs of indexed values, which aren't scrubbed
    source = tmp_path / ".hermes"
    source.mkdir()
    with sqlite3.connect(source / "state.db") as conn:
        conn.execute("create table sessions (title text)")
        conn.execute("create index sessions_title on sessions (title)")
        conn.execute("insert into sessions values ('Plan the trip')")
        conn.execute("analyze")
    dest = tmp_path / "fixture"
    rf.record("hermes", source, dest, scrubber=scrubber(), ps=FakePs())
    with sqlite3.connect(dest / "state.db") as conn:
        names = {r[0] for r in conn.execute("select name from sqlite_master")}
    assert not any(n.startswith("sqlite_stat") for n in names)


def test_allow_list_ids_scrubbed(tmp_path: Path) -> None:
    # ids that appear nowhere else (no session key, no chat_id field)
    hermes = tmp_path / ".hermes"
    hermes.mkdir()
    (hermes / "config.yaml").write_text(
        "telegram:\n  allowed_users: [987654321]\n  allowed_chats: ['-1009876543210']\n"
    )
    rf.record("hermes", hermes, tmp_path / "h", scrubber=scrubber(), ps=FakePs())
    text = all_text(tmp_path / "h")
    assert "987654321" not in text and "1009876543210" not in text

    openclaw = tmp_path / ".openclaw"
    openclaw.mkdir()
    config = {"gateway": {"port": 18789},
              "channels": {"telegram": {"allowFrom": ["tg:555666777"]},
                           "whatsapp": {"groupAllowFrom": ["+33612345678"]}}}  # fmt: skip
    (openclaw / "openclaw.json").write_text(json.dumps(config))
    rf.record("openclaw", openclaw, tmp_path / "o", scrubber=scrubber(), ps=FakePs())
    text = all_text(tmp_path / "o")
    assert "555666777" not in text and "33612345678" not in text
    out = json.loads((tmp_path / "o" / "openclaw.json").read_text())
    assert out["gateway"]["port"] == 18789
    assert out["channels"]["telegram"]["allowFrom"][0].startswith("tg:1000000")
    assert out["channels"]["whatsapp"]["groupAllowFrom"][0].startswith("+1000000")


def test_peer_ids_replaced_as_whole_numbers_only(tmp_path: Path) -> None:
    source = tmp_path / ".hermes"
    source.mkdir()
    with sqlite3.connect(source / "state.db") as conn:
        conn.execute("create table sessions (user_id text, started_at text, input_tokens integer)")
        conn.execute("insert into sessions values ('10001', '2026-10-14T10:00:01.100014Z', 100014)")
    dest = tmp_path / "fixture"
    rf.record("hermes", source, dest, scrubber=scrubber(), ps=FakePs())
    with sqlite3.connect(dest / "state.db") as conn:
        row = conn.execute("select user_id, started_at, input_tokens from sessions").fetchone()
    assert row == ("100000001", "2026-10-14T10:00:01.100014Z", 100014)


# Leaks and gaps found recording real agent installs (#55)
GROUP, LEGACY_GROUP, BOT = "-1001234567890", "-4012345678", "7012345678"


def all_names(root: Path) -> str:
    return "\n".join(str(p.relative_to(root)) for p in root.rglob("*"))


def test_keys_scrubbed_like_values(tmp_path: Path) -> None:
    source = tmp_path / ".openclaw"
    source.mkdir()
    config = {
        "auth": {"profiles": {"openai:alice@corp.com": {"mode": "oauth"},
                              "anthropic:bob@corp.com": {"mode": "token"}}},
        "channels": {"telegram": {"groups": {GROUP: {"requireMention": True},
                                             LEGACY_GROUP: {}}}},
        "bindings": [{"agentId": "bot-a", "match": {"peer": {"kind": "group", "id": GROUP}}}],
    }  # fmt: skip
    (source / "openclaw.json").write_text(json.dumps(config))
    dest = tmp_path / "fixture"
    rf.record("openclaw", source, dest, scrubber=scrubber(), ps=FakePs())
    text = all_text(dest)
    for leak in ("corp.com", "alice", GROUP.lstrip("-"), LEGACY_GROUP.lstrip("-")):
        assert leak not in text, leak
    out = json.loads((dest / "openclaw.json").read_text())
    assert len(out["auth"]["profiles"]) == 2  # scrubbed keys that collide are kept apart
    groups = out["channels"]["telegram"]["groups"]
    assert out["bindings"][0]["match"]["peer"]["id"] in groups  # same placeholder everywhere


def test_telegram_group_id_in_a_plain_value_scrubbed(tmp_path: Path) -> None:
    source = tmp_path / ".hermes"
    source.mkdir()
    (source / "config.yaml").write_text(f"telegram:\n  home_channel: '{GROUP}'\n")
    rf.record("hermes", source, tmp_path / "fixture", scrubber=scrubber(), ps=FakePs())
    assert GROUP.lstrip("-") not in all_text(tmp_path / "fixture")


def test_ids_in_file_names_scrubbed(tmp_path: Path) -> None:
    source = tmp_path / ".openclaw"
    sessions = source / "agents" / "bot-a" / "sessions"
    sessions.mkdir(parents=True)
    (source / "openclaw.json").write_text("{}")
    (source / f"telegram_update_receipts_{BOT}.json").write_text("{}")  # not copied, surveyed
    (sessions / f"agent_bot-a_telegram_group_{GROUP}.jsonl").write_text('{"type": "turn"}\n')
    (sessions / "20260101_120000_abcdef12.jsonl").write_text('{"type": "turn"}\n')
    dest = tmp_path / "fixture"
    rf.record("openclaw", source, dest, scrubber=scrubber(), ps=FakePs())
    names = all_names(dest) + all_text(dest)
    assert BOT not in names and GROUP.lstrip("-") not in names
    assert "20260101_120000_abcdef12" in names  # dates and short ids in names are kept


def test_recorded_database_not_left_in_wal_mode(tmp_path: Path) -> None:
    source = tmp_path / ".hermes"
    source.mkdir()
    with sqlite3.connect(source / "state.db") as conn:
        conn.execute("pragma journal_mode=wal")
        conn.execute("create table sessions (id text)")
    dest = tmp_path / "fixture"
    rf.record("hermes", source, dest, scrubber=scrubber(), ps=FakePs())
    with sqlite3.connect(dest / "state.db") as conn:
        assert conn.execute("pragma journal_mode").fetchone()[0] == "delete"
    assert not list(dest.glob("*-wal")) and not list(dest.glob("*-shm"))


def test_sqlite_suffix_recorded_as_a_database(tmp_path: Path) -> None:
    store = tmp_path / ".openclaw" / "agents" / "main" / "agent"
    store.mkdir(parents=True)
    with sqlite3.connect(store / "openclaw-agent.sqlite") as conn:
        conn.execute("create table messages (content text, input_tokens integer)")
        conn.execute("insert into messages values ('my private prompt', 12)")
    dest = tmp_path / "fixture"
    rf.record("openclaw", tmp_path / ".openclaw", dest, scrubber=scrubber(), ps=FakePs())
    with sqlite3.connect(dest / "agents/main/agent/openclaw-agent.sqlite") as conn:
        row = conn.execute("select content, input_tokens from messages").fetchone()
    assert row == ("[scrubbed text <= 128 chars]", 12)


def test_selection_not_starved_by_a_big_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # hermes-agent/ (a git checkout) sorts before sessions/ and used up the walk's budget
    real = rf.walk_bounded
    monkeypatch.setattr(rf, "walk_bounded", lambda *a, **k: real(*a, **{**k, "max_entries": 50}))
    source = tmp_path / ".hermes"
    (source / "hermes-agent").mkdir(parents=True)
    for i in range(100):
        (source / "hermes-agent" / f"f{i}.py").write_text("")
    (source / "sessions").mkdir()
    (source / "sessions" / "20260101_120000_abcdef12.jsonl").write_text('{"type": "turn"}\n')
    (source / "cron").mkdir()
    (source / "cron" / "executions.db").write_bytes(b"")
    chosen, _ = rf.select_files(source, rf.INCLUDE["hermes"], 20)
    assert Path("sessions/20260101_120000_abcdef12.jsonl") in chosen
    assert Path("cron/executions.db") not in chosen


def test_survey_summarises_big_folders_and_reaches_sessions(tmp_path: Path) -> None:
    source = tmp_path / ".hermes"
    (source / "cache").mkdir(parents=True)
    for i in range(500):
        (source / "cache" / f"c{i}.json").write_text("{}")
    (source / "sessions").mkdir()
    (source / "sessions" / "s1.jsonl").write_text("{}\n")
    (source / "logs").mkdir()
    (source / "logs" / "agent.log").write_text("x\n")
    (source / "config.yaml").write_text("a: 1\n")
    dest = tmp_path / "fixture"
    rf.record("hermes", source, dest, scrubber=scrubber(), ps=FakePs())
    survey = (dest / "SURVEY.md").read_text()
    assert "`sessions/s1.jsonl`" in survey and "`logs/agent.log`" in survey
    assert "`cache/`: 500 files" in survey
    assert "c499.json" not in survey


def test_id_in_a_copied_name_scrubbed_when_the_walk_stops_early(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = rf.walk_breadth_first
    monkeypatch.setattr(
        rf, "walk_breadth_first", lambda *a, **k: real(*a, **{**k, "max_entries": 30})
    )
    source = tmp_path / ".openclaw"
    (source / "aaa").mkdir(parents=True)  # sorts first and uses up the walk
    for i in range(100):
        (source / "aaa" / f"f{i}").write_text("")
    sessions = source / "agents" / "main" / "sessions"
    sessions.mkdir(parents=True)
    (source / "openclaw.json").write_text("{}")
    (sessions / f"agent_main_telegram_group_{GROUP}.jsonl").write_text('{"type": "turn"}\n')
    dest = tmp_path / "fixture"
    rf.record("openclaw", source, dest, scrubber=scrubber(), ps=FakePs())
    assert GROUP.lstrip("-") not in all_names(dest) + all_text(dest)


def test_legacy_group_and_bot_ids_in_plain_values_scrubbed(tmp_path: Path) -> None:
    source = tmp_path / ".hermes"
    source.mkdir()
    (source / "config.yaml").write_text(
        f"telegram:\n  home_channel: '{LEGACY_GROUP}'\n  bot_id: {BOT}\n"
        "  group_timeout_ms: 30000\n  thread_updated_at: 1760000000\n"
    )
    dest = tmp_path / "fixture"
    rf.record("hermes", source, dest, scrubber=scrubber(), ps=FakePs())
    text = all_text(dest)
    assert LEGACY_GROUP.lstrip("-") not in text and BOT not in text
    import yaml

    config = yaml.safe_load((dest / "config.yaml").read_text())["telegram"]
    assert config["group_timeout_ms"] == 30000  # short numbers under these names are kept
    assert config["thread_updated_at"] == 1760000000  # so are timestamps


def test_manifest_lists_long_numbers_left_for_review(tmp_path: Path) -> None:
    source = tmp_path / ".hermes"
    source.mkdir()
    (source / "config.yaml").write_text(
        "build: 123456789012\nsent_at_ms: 1760000000123\nhash: 12345678901234567890\n"
        f"telegram:\n  chat_id: {PEER}\n"
    )
    with sqlite3.connect(source / "state.db") as conn:
        conn.execute("create table events (ref text, n integer)")
        conn.execute("insert into events values ('ticket 98765432101', 99887766554)")
    dest = tmp_path / "fixture"
    rf.record("hermes", source, dest, scrubber=scrubber(), ps=FakePs())
    manifest = (dest / "MANIFEST.md").read_text()
    section = manifest.split("## Long numbers left in the output", 1)[1].split("\n## ", 1)[0]
    assert "- `config.yaml`: `123456789012`" in section
    assert "- `state.db` events.ref: `98765432101`" in section
    assert "- `state.db` events.n: `99887766554`" in section
    assert "100000001" not in section  # placeholders aren't listed
    assert "1760000000123" not in section  # nor timestamps
    assert "12345678901234567890" not in section  # nor numbers too long to be an id


def test_selection_never_walks_through_a_symlinked_folder(tmp_path: Path) -> None:
    outside = tmp_path / "outside" / "b"
    outside.mkdir(parents=True)
    (outside / "secret.txt").write_text("x")
    source = tmp_path / "src"
    source.mkdir()
    (source / "a").symlink_to(tmp_path / "outside")
    chosen, _ = rf.select_files(source, ["a/b/*"], 20)
    assert chosen == []


def test_hermes_store_taken_before_session_logs(tmp_path: Path) -> None:
    source = tmp_path / ".hermes"
    (source / "sessions").mkdir(parents=True)
    for i in range(30):
        (source / "sessions" / f"20260101_1200{i:02d}_abcdef12.jsonl").write_text("{}\n")
    with sqlite3.connect(source / "state.db") as conn:
        conn.execute("create table messages (id integer primary key, timestamp real)")
    chosen, _ = rf.select_files(source, rf.INCLUDE["hermes"], 20)
    assert Path("state.db") in chosen


def test_hermes_reasoning_names_and_other_stores_not_kept(tmp_path: Path) -> None:
    source = tmp_path / ".hermes"
    (source / "sessions").mkdir(parents=True)
    turn = {"role": "assistant", "reasoning": "Alice is saying hello.",
            "reasoning_content": "Alice is saying hello."}  # fmt: skip
    (source / "sessions" / "20260101_120000_abcdef12.jsonl").write_text(json.dumps(turn) + "\n")
    with sqlite3.connect(source / "state.db") as conn:
        conn.execute("create table sessions (id text, display_name text, api_content text)")
        conn.execute("insert into sessions values ('s1', 'Alice', 'Hi Alice')")
    with sqlite3.connect(source / "verification_evidence.db") as conn:
        conn.execute("create table runs (root text)")
    dest = tmp_path / "fixture"
    rf.record("hermes", source, dest, scrubber=scrubber(), ps=FakePs())
    session = json.loads((dest / "sessions" / "20260101_120000_abcdef12.jsonl").read_text())
    assert session["reasoning"].startswith("[scrubbed text")
    assert session["reasoning_content"].startswith("[scrubbed text")
    with sqlite3.connect(dest / "state.db") as conn:
        row = conn.execute("select id, display_name, api_content from sessions").fetchone()
    assert row[0] == "s1"
    assert row[1].startswith("[scrubbed text") and row[2].startswith("[scrubbed text")
    assert not (dest / "verification_evidence.db").exists()


def test_openclaw_assistant_texts_tool_metas_and_labels_scrubbed(tmp_path: Path) -> None:
    sessions = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
    sessions.mkdir(parents=True)
    event = {"data": {"assistantTexts": ["Hi Alice!"],
                      "toolMetas": [{"toolName": "exec", "meta": "cat ~/alice-notes"}]},
             "label": "Alice", "displayName": "Alice", "ownerDisplay": "Alice",
             "meta": {"lastTouchedVersion": "2026.9.4"}}  # fmt: skip
    (sessions / "s1.trajectory.jsonl").write_text(json.dumps(event) + "\n")
    dest = tmp_path / "fixture"
    rf.record("openclaw", tmp_path / ".openclaw", dest, scrubber=scrubber(), ps=FakePs())
    text = (dest / "agents" / "main" / "sessions" / "s1.trajectory.jsonl").read_text()
    assert "Alice" not in text and "alice" not in text
    assert json.loads(text)["meta"] == {"lastTouchedVersion": "2026.9.4"}


def test_tailnet_name_scrubbed() -> None:
    origins = '["https://pi.tail0f00ba.ts.net", "http://alice-mbp.tail0f00ba.ts.net:18789"]'
    out = rf.Scrubber(HOME, "alice", "alice-mbp").text(origins)
    assert "tail0f00ba" not in out and "alice" not in out
    assert out.count("host.example.ts.net") == 2


def test_hermes_profile_names_scrubbed(tmp_path: Path) -> None:
    source = tmp_path / ".hermes"
    (source / "profiles" / "bob").mkdir(parents=True)
    (source / "gateway_state.json").write_text(
        json.dumps({"served_profiles": ["default", "bob", "acme-dev", "cafe"]})
    )
    with sqlite3.connect(source / "state.db") as conn:
        conn.execute("create table sessions (session_key text, adapter_profile text, hash text)")
        conn.execute(
            "insert into sessions values ('agent:bob:telegram:dm:7', 'acme-dev', 'a3cafe4f')"
        )
    dest = tmp_path / "fixture"
    rf.record("hermes", source, dest, scrubber=scrubber(), ps=FakePs())
    served = json.loads((dest / "gateway_state.json").read_text())["served_profiles"]
    assert served == ["default", "profile-3", "profile-1", "profile-2"]
    with sqlite3.connect(dest / "state.db") as conn:
        row = conn.execute("select * from sessions").fetchone()
    assert row == ("agent:profile-3:telegram:dm:7", "profile-1", "a3cafe4f")
    text = all_text(dest)
    assert "bob" not in text and "acme" not in text


def test_memory_note_names_scrubbed_dated_notes_kept() -> None:
    s = rf.Scrubber(HOME, "alice", "alice-mbp")
    assert s.text("memory/trip-to-lyon.md") == "memory/note-1.md"
    assert s.text("see memory/dreaming/light/trip-to-lyon.md") == (
        "see memory/dreaming/light/note-1.md"
    )
    assert s.text("memory/shopping.md") == "memory/note-2.md"
    assert s.text("memory/2026-05-14.md") == "memory/2026-05-14.md"
    assert s.text("memory/2026-05-08-0420.md") == "memory/2026-05-08-0420.md"
    assert s.text("memory/2026-04-19-skill-safety.md") == "memory/note-3.md"
    assert s.text("dist/memory/index.js") == "dist/memory/index.js"
    assert s.text('"memory/my trip.md"') == '"memory/note-4.md"'
    assert s.text("memory/a.md and memory/b c.md") == "memory/note-5.md and memory/note-6.md"


def test_memory_note_names_scrubbed_in_paths() -> None:
    s = rf.Scrubber(HOME, "alice", "alice-mbp")
    assert s.path(Path("workspace/memory/trip-to-lyon.md")) == Path("workspace/memory/note-1.md")
    assert s.path(Path("workspace/memory/2026-05-14.md")) == Path("workspace/memory/2026-05-14.md")


def test_names_after_a_dash_or_underscore_scrubbed_uuids_kept() -> None:
    s = rf.Scrubber(HOME, "alice", "alice-mbp")
    s.add_names(["bob", "cafe"], "profile")
    assert s.text("workspace-bob profile_bob Bob's agent:bob: bob-dev") == (
        "workspace-profile-2 profile_profile-2 profile-2's agent:profile-2: profile-2-dev"
    )
    assert s.text("1234-cafe-5678 a3cafe4f cafe_notes") == "1234-cafe-5678 a3cafe4f profile-1_notes"


def test_last_check_catches_names_and_notes_the_scrub_missed() -> None:
    s = rf.Scrubber(HOME, "alice", "alice-mbp")
    s.add_names(["bob"], "profile")
    assert s.names_left("workspace-bob")
    assert s.names_left("memory/my trip.md")
    assert not s.names_left("memory/note-3.md memory/2026-05-14.md 12-cafe-34 bobcat")


def test_private_words_scrubbed(tmp_path: Path) -> None:
    source = tmp_path / ".hermes"
    source.mkdir()
    (source / "config.yaml").write_text(
        "skills:\n  enabled: [tax-return-mcp, web]\n  label_hint: Tax Return MCP\n"
    )
    dest = tmp_path / "fixture"
    rf.record("hermes", source, dest, words=["tax-return"], scrubber=scrubber(), ps=FakePs())
    text = (dest / "config.yaml").read_text()
    assert "tax" not in text.lower()
    assert "word-1-mcp" in text and "web" in text


def test_refuses_when_a_profile_name_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / ".hermes"
    source.mkdir()
    (source / "gateway_state.json").write_text(json.dumps({"served_profiles": ["bob"]}))
    s = rf.Scrubber(HOME, "alice", "alice-mbp")
    monkeypatch.setattr(s, "swap_names", lambda text: text)
    with pytest.raises(SystemExit, match="still present"):
        rf.record("hermes", source, tmp_path / "fixture", scrubber=s, ps=FakePs())
    assert not (tmp_path / "fixture").exists()


def test_hermes_env_keeps_only_access_settings(tmp_path: Path) -> None:
    source = tmp_path / ".hermes"
    (source / "profiles" / "bob").mkdir(parents=True)
    (source / "config.yaml").write_text("terminal:\n  backend: local\n")
    (source / ".env").write_text(
        f"OPENAI_API_KEY={KEY}\nTELEGRAM_BOT_TOKEN=123456:ABCdefGHIjkl\n"
        f"TELEGRAM_ALLOWED_USERS={PEER},@alice_handle  # the owner\n"
        "GATEWAY_ALLOW_ALL_USERS=false\nexport DISCORD_ALLOWED_USERS=*\n"
    )
    (source / "profiles" / "bob" / ".env").write_text(
        f'TELEGRAM_ALLOWED_USERS="{PEER},987654321"\nTELEGRAM_GROUP_ALLOWED_CHATS=-1001234567890\n'
    )
    dest = tmp_path / "fixture"
    rf.record("hermes", source, dest, scrubber=scrubber(), ps=FakePs(), max_files=50)
    default = (dest / ".env").read_text().splitlines()
    owner = default[0].split("=")[1].split(",")[0]
    assert re.fullmatch(r"1000000\d\d", owner)
    assert default == [f"TELEGRAM_ALLOWED_USERS={owner},user-1", "GATEWAY_ALLOW_ALL_USERS=false",
                       "DISCORD_ALLOWED_USERS=*"]  # fmt: skip
    (profile,) = (dest / "profiles").iterdir()
    assert profile.name != "bob"
    lines = (profile / ".env").read_text().splitlines()
    assert lines[0].startswith(f"TELEGRAM_ALLOWED_USERS={owner},")  # same person, same id
    assert re.fullmatch(r"TELEGRAM_GROUP_ALLOWED_CHATS=-1000000\d\d", lines[1])
    text = all_text(dest)
    for secret in (KEY, "ABCdefGHIjkl", PEER, "987654321", "1234567890", "alice_handle"):
        assert secret not in text


def test_claude_code_env_is_still_never_copied(tmp_path: Path) -> None:
    source = make_claude_home(tmp_path / "home")
    (source / ".env").write_text(f"TELEGRAM_ALLOWED_USERS={PEER}\n")
    dest = tmp_path / "fixture"
    rf.record("claude-code", source, dest, extra_include=[".env"], scrubber=scrubber())
    assert not (dest / ".env").exists()


def test_openclaw_transcript_events_keep_only_usage_fields(tmp_path: Path) -> None:
    source = tmp_path / ".openclaw"
    (source / "agents" / "main" / "agent").mkdir(parents=True)
    (source / "openclaw.json").write_text("{}")
    event = {"type": "message", "id": "e1a2", "parentId": "p0",
             "timestamp": "2026-10-14T10:00:00.000Z",
             "message": {"role": "assistant", "model": "gpt-5.5", "provider": "openai",
                         "timestamp": 1792237771053, "stopReason": "stop", "api": "responses",
                         "content": [{"type": "text", "text": "Alice's tax return is done"}],
                         "usage": {"input": 10, "output": 5, "cacheRead": 7, "cacheWrite": 0,
                                   "cost": {"total": 0.01}, "note": "x"}}}  # fmt: skip
    odd = {"type": "message", "id": "id with spaces", "message": {"role": "Alice Smith"}}
    with sqlite3.connect(source / "agents" / "main" / "agent" / "openclaw-agent.sqlite") as db:
        db.execute("create table transcript_events (session_id text, seq integer, event_json text)")
        db.executemany("insert into transcript_events values ('s1', ?, ?)",
                       [(0, json.dumps(event)), (1, json.dumps(odd)),
                        (2, "{not json")])  # fmt: skip
    dest = tmp_path / "fixture"
    rf.record("openclaw", source, dest, scrubber=scrubber(), ps=FakePs())
    with sqlite3.connect(dest / "agents" / "main" / "agent" / "openclaw-agent.sqlite") as db:
        rows = [r[0] for r in db.execute("select event_json from transcript_events order by seq")]
    assert json.loads(rows[0]) == {
        "type": "message", "id": "e1a2", "timestamp": "2026-10-14T10:00:00.000Z",
        "message": {"role": "assistant", "stopReason": "stop", "model": "gpt-5.5",
                    "provider": "[scrubbed text <= 16 chars]", "timestamp": 1792237771053,
                    "usage": {"input": 10, "output": 5, "cacheRead": 7, "cacheWrite": 0,
                              "cost": {"total": 0.01}}},
    }  # fmt: skip
    odd_kept = json.loads(rows[1])
    assert odd_kept["id"].startswith("[scrubbed")
    assert odd_kept["message"]["role"].startswith("[scrubbed")
    assert rows[2].startswith("[scrubbed text")
    assert "Alice" not in all_text(dest)
