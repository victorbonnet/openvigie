import importlib.util
import json
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
