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


def test_unknown_agent_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        rf.record("nope", tmp_path, tmp_path / "out")
