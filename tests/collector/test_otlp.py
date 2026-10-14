import json
import shutil
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from openvigie.adapters.claude_code import ClaudeCodeAdapter
from openvigie.adapters.registry import Discovery
from openvigie.adapters.testing import fake_context
from openvigie.collector.client import HubClient
from openvigie.collector.daemon import Collector
from openvigie.collector.otlp import MAX_BODY, OtlpReceiver, parse_logs
from openvigie.config import CollectorSettings, ConfigError, OtlpSettings, load_collector_settings

FIXTURES = Path(__file__).parent / "fixtures" / "otlp"
CLAUDE = Path(__file__).parents[1] / "adapters" / "claude_code" / "fixtures" / "home" / ".claude"
JSON = {"Content-Type": "application/json"}


def claude_logs() -> bytes:
    return (FIXTURES / "claude_code_logs.json").read_bytes()


def test_claude_code_api_request_events() -> None:
    usage = parse_logs(json.loads(claude_logs()))
    assert len(usage) == 9 and {u.agent_type for u in usage} == {"claude-code"}
    assert sum(u.output_tokens or 0 for u in usage) == 24009
    assert sum(u.cache_read_input_tokens or 0 for u in usage) == 529331
    assert all(u.request_id and u.session_id and u.model == "claude-opus-5-5" for u in usage)


def test_other_events_and_services_are_ignored() -> None:
    payload = json.loads(claude_logs())
    record = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    record["attributes"][0]["value"]["stringValue"] = "user_prompt"
    record["body"]["stringValue"] = "claude_code.user_prompt"
    assert len(parse_logs(payload)) == 8
    payload["resourceLogs"][0]["resource"]["attributes"][0]["value"]["stringValue"] = "other"
    assert parse_logs(payload) == []
    assert parse_logs({"resourceLogs": "nope"}) == [] and parse_logs([]) == []


def test_request_handling() -> None:
    receiver = OtlpReceiver(token="t0ken")  # noqa: S106 - test value
    auth = {**JSON, "Authorization": "Bearer t0ken"}
    secret_looking = b'{"resourceLogs": "sk-ant-api03-xxxx"'
    assert receiver.handle("/v1/logs", auth, claude_logs()) == (200, b"{}")
    assert len(receiver.drain()) == 9 and receiver.drain() == []
    assert receiver.handle("/v1/metrics", auth, b"{}")[0] == 200  # accepted, ignored
    assert receiver.handle("/v1/traces", auth, b"{}")[0] == 200
    assert receiver.handle("/v1/logs", JSON, claude_logs())[0] == 401
    assert receiver.handle("/v1/logs", {**JSON, "Authorization": "Bearer nope"}, b"{}")[0] == 401
    protobuf = {**auth, "Content-Type": "application/x-protobuf"}
    assert receiver.handle("/v1/logs", protobuf, b"\x0a")[0] == 415
    assert receiver.handle("/v2/logs", auth, b"{}")[0] == 404
    assert receiver.handle("/v1/logs", auth, b" " * (MAX_BODY + 1))[0] == 413
    status, body = receiver.handle("/v1/logs", auth, secret_looking)
    assert status == 400 and b"sk-ant" not in body  # never echoes the input


def test_disabled_by_default_and_binds_loopback(tmp_path: Path) -> None:
    assert CollectorSettings().otlp == OtlpSettings(enabled=False)
    receiver = OtlpReceiver(port=0)
    receiver.start()
    try:
        assert receiver.server is not None
        assert receiver.server.server_address[0] == "127.0.0.1"
        url = f"http://127.0.0.1:{receiver.port}/v1/logs"
        request = urllib.request.Request(url, data=claude_logs(), headers=JSON, method="POST")
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - local
            assert response.status == 200
        assert len(receiver.drain()) == 9
    finally:
        receiver.stop()
    config = tmp_path / "collector.toml"
    config.write_text('[otlp]\nenabled = true\nusage_source = "both"\n')
    with pytest.raises(ConfigError, match="usage_source"):
        load_collector_settings(config)


class Hub:
    """Stub hub: remembers every usage record pushed."""

    def __init__(self) -> None:
        self.usage: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, body: bytes | None, headers: Any,
                 timeout: float) -> tuple[int, bytes]:  # fmt: skip
        self.usage += json.loads(body or b"{}").get("usage", [])
        return 200, b'{"ignored": []}'

    def output_tokens(self) -> int:
        return sum(r["output_tokens"] or 0 for r in self.usage)


def collector(tmp_path: Path, source: str = "auto") -> tuple[Collector, OtlpReceiver, Hub]:
    shutil.copytree(CLAUDE, tmp_path / "home" / ".claude")
    settings = CollectorSettings(homes=(tmp_path / "home",), slow_tick_s=0.0, backfill_days=36500,
                                 otlp=OtlpSettings(usage_source=source))  # fmt: skip
    hub, receiver = Hub(), OtlpReceiver()
    client = HubClient("http://127.0.0.1:7878", token="t", transport=hub)  # noqa: S106
    c = Collector(settings, fake_context(homes=[tmp_path / "home"]),
                  Discovery(adapters=[ClaudeCodeAdapter()]), client, otlp=receiver)  # fmt: skip
    return c, receiver, hub


def test_logs_then_otlp_counted_once(tmp_path: Path) -> None:
    c, receiver, hub = collector(tmp_path)
    assert c.tick()
    assert hub.output_tokens() == 24009  # from the transcripts
    receiver.handle("/v1/logs", JSON, claude_logs())  # the same 9 calls, reported by OTLP
    c.tick()
    assert hub.output_tokens() == 24009 and len(hub.usage) == 9


def test_otlp_first_then_logs_counted_once(tmp_path: Path) -> None:
    c, receiver, hub = collector(tmp_path)
    receiver.handle("/v1/logs", JSON, claude_logs())
    c.tick()
    c.tick()
    assert hub.output_tokens() == 24009 and len(hub.usage) == 9


@pytest.mark.parametrize("source", ["logs", "otlp"])
def test_forced_source(tmp_path: Path, source: str) -> None:
    c, receiver, hub = collector(tmp_path, source)
    receiver.handle("/v1/logs", JSON, claude_logs())
    c.tick()
    c.tick()
    assert hub.output_tokens() == 24009 and len(hub.usage) == 9
