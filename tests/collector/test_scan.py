import json
import os
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openvigie import cli
from openvigie.adapters.claude_code import ClaudeCodeAdapter
from openvigie.adapters.codex import CodexAdapter
from openvigie.adapters.registry import Discovery
from openvigie.adapters.testing import FIXED_NOW, fake_context, fake_process
from openvigie.collector.scan import ago, run_scan, to_json, to_text
from openvigie.model import AdapterHealth, HealthState

FIXTURES = Path(__file__).parents[1] / "adapters"
WINDOW = timedelta(minutes=5)


def make_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    shutil.copytree(FIXTURES / "claude_code/fixtures/home/.claude", home / ".claude")
    shutil.copytree(FIXTURES / "codex/fixtures/home/.codex", home / ".codex")
    recent = (FIXED_NOW - timedelta(minutes=2)).timestamp()
    old = (FIXED_NOW - timedelta(hours=3)).timestamp()
    for path in (home / ".claude").rglob("*.jsonl"):
        os.utime(path, (recent, recent))
    for path in (home / ".codex").rglob("*.jsonl"):
        when = old if "2026-10-14" in path.name else old - 86400
        os.utime(path, (when, when))
    return home


def discovery() -> Discovery:
    broken = AdapterHealth("hermes", HealthState.ERROR, "ImportError: nope")
    return Discovery(adapters=[ClaudeCodeAdapter(), CodexAdapter()], failures=[broken])


def test_scan_text_lists_agents_and_adapter_health(tmp_path: Path) -> None:
    ctx = fake_context(
        homes=[make_home(tmp_path)],
        processes=[fake_process(300, "codex", exe="/usr/bin/codex")],
    )
    text = to_text(run_scan(ctx, discovery(), WINDOW))
    lines = text.splitlines()
    assert lines[0] == "openvigie scan: testhost (linux/aarch64)"
    header = lines[2].split()
    assert header[:4] == ["TYPE", "NAME", "VERSION", "STATUS"]
    claude = next(line for line in lines if line.startswith("claude-code"))
    codex = next(line for line in lines if line.startswith("codex"))
    assert "2.1.299" in claude and "active" in claude and "2m ago" in claude
    assert "0.46.0" in codex and "idle" in codex and "3h ago" in codex
    assert "~/.codex" in codex
    assert "hermes         error: ImportError: nope" in text


def test_scan_json_is_redacted_and_parseable(tmp_path: Path) -> None:
    ctx = fake_context(homes=[make_home(tmp_path)])
    data = json.loads(to_json(run_scan(ctx, discovery(), WINDOW)))
    assert data["schema_version"] == 1
    assert data["host"]["hostname"] == "testhost"
    statuses = {a["agent_type"]: a["status"] for a in data["agents"]}
    assert statuses == {"claude-code": "active", "codex": "stopped"}
    assert {h["adapter"] for h in data["adapters"]} == {"claude-code", "codex", "hermes"}


def test_scan_only_one_adapter(tmp_path: Path) -> None:
    ctx = fake_context(homes=[make_home(tmp_path)])
    result = run_scan(ctx, discovery(), WINDOW, only="codex")
    assert [a.agent_type for a in result.agents] == ["codex"]
    assert [h.adapter for h in result.health] == ["codex"]


def test_scan_empty_host_prints_hint(tmp_path: Path) -> None:
    ctx = fake_context(homes=[tmp_path / "nobody"])
    start = time.perf_counter()
    text = to_text(run_scan(ctx, Discovery(adapters=[ClaudeCodeAdapter(), CodexAdapter()]), WINDOW))
    assert time.perf_counter() - start < 2
    assert "No agents found" in text


def test_ago() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert ago(None, now) == "unknown"
    assert ago(now - timedelta(seconds=5), now) == "5s ago"
    assert ago(now - timedelta(days=2), now) == "2d ago"
    assert ago(now + timedelta(seconds=5), now) == "in the future"


def test_cli_scan_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = make_home(tmp_path)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "collector.toml").write_text(f'homes = ["{home}"]\n')
    monkeypatch.setenv("OPENVIGIE_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("OPENVIGIE_STATE_DIR", str(tmp_path / "state"))
    assert cli.main(["scan", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {a["agent_type"] for a in data["agents"]} == {"claude-code", "codex"}
    assert (tmp_path / "state" / "host_id").exists()


def test_cli_scan_bad_config_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "collector.toml").write_text("nope = 1\n")
    monkeypatch.setenv("OPENVIGIE_CONFIG_DIR", str(tmp_path))
    assert cli.main(["scan"]) == 2
    assert "unknown key" in capsys.readouterr().err
