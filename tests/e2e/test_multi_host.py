"""Two collectors (a Pi and a Mac) against one real hub over HTTP; revoke and rotate."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_single_host import FakeMachine, Hub, statuses

from openvigie import cli
from openvigie.adapters.claude_code import ClaudeCodeAdapter
from openvigie.adapters.codex import CodexAdapter
from openvigie.adapters.registry import Discovery
from openvigie.adapters.testing import fake_process
from openvigie.collector.client import HubClient
from openvigie.collector.daemon import Collector
from openvigie.collector.enrol import enrol
from openvigie.config import CollectorSettings, read_private_file
from openvigie.hub.store.credentials import Credentials
from openvigie.hub.store.inventory import Inventory
from openvigie.model import Host

pytestmark = pytest.mark.e2e

PI = Host("host-pi-0000000000000000000000001", "pi5", "linux", "aarch64", "0.0.1")
MAC = Host("host-mac-000000000000000000000002", "mbp", "darwin", "arm64", "0.0.1")


@pytest.fixture
def hub(tmp_path: Path) -> Iterator[Hub]:
    with Hub(tmp_path) as running:
        yield running


MACHINES: dict[Path, FakeMachine] = {}


def collector_for(
    hub: Hub, host: Host, tmp_path: Path, code: str | None = None, state_dir: str = "state"
) -> Collector:
    """A collector on ``host``; the same fake machine (home, agents) is reused on re-enrolment."""
    root = tmp_path / host.hostname
    if root not in MACHINES:
        MACHINES[root] = FakeMachine(root)
        MACHINES[root].processes = [fake_process(97, "claude")]
    ctx = MACHINES[root].context()
    ctx.host = host
    state = tmp_path / host.hostname / state_dir
    settings = CollectorSettings(homes=(MACHINES[root].home,), token_file=state / "token")
    code = code or Credentials(hub.db).create_enrol_code(datetime.now(UTC), timedelta(minutes=5))
    token_file = enrol(hub.url, code, host, settings, config_path=state / "collector.toml")
    client = HubClient(hub.url, token=read_private_file(token_file))
    discovery = Discovery(adapters=[ClaudeCodeAdapter(), CodexAdapter()])
    return Collector(settings, ctx, discovery, client)


def test_two_hosts_revoke_and_rotate(hub: Hub, tmp_path: Path) -> None:
    pi = collector_for(hub, PI, tmp_path)
    mac = collector_for(hub, MAC, tmp_path)
    assert pi.tick() and mac.tick()

    inventory = Inventory(hub.db)
    by_host = {r.hostname: r.agent.agent_type for r in inventory.agents()}
    assert len(inventory.agents()) == 4  # Claude Code + Codex on each machine
    assert set(by_host) == {"pi5", "mbp"}
    assert set(statuses(hub.get("/")).values()) == {"idle", "stopped"}

    Credentials(hub.db).revoke_host(PI.host_id, datetime.now(UTC))
    pi.monotonic = lambda: 1e9  # skip the push backoff window
    assert pi.tick() is False and pi.state.unauthorized
    assert mac.tick()  # the other host is unaffected
    assert "revoked" in hub.get("/hosts")

    # an ordinary code can't bring a revoked host back; a rotate code can, keeping history
    with pytest.raises(Exception, match="409"):
        collector_for(hub, PI, tmp_path, state_dir="again")
    rotate = Credentials(hub.db).create_enrol_code(
        datetime.now(UTC), timedelta(minutes=5), bound_host_id=PI.host_id
    )
    pi = collector_for(hub, PI, tmp_path, code=rotate, state_dir="rotated")
    assert pi.tick()
    host = inventory.host(PI.host_id)
    assert host is not None and host.revoked_at is None
    assert len(inventory.agents(host_id=PI.host_id)) == 2


def test_hosts_cli(hub: Hub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                   capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    collector_for(hub, PI, tmp_path).tick()
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "hub.toml").write_text(f'db_path = "{hub.db.path}"\n')
    monkeypatch.setenv("OPENVIGIE_CONFIG_DIR", str(cfg))

    assert cli.main(["hub", "hosts", "list"]) == 0
    out = capsys.readouterr().out
    assert "pi5" in out and "active" in out

    assert cli.main(["hub", "hosts", "revoke", "nope"]) == 1
    assert cli.main(["hub", "hosts", "revoke", "pi5"]) == 0
    assert Credentials(hub.db).is_revoked(PI.host_id)
    assert cli.main(["hub", "hosts", "rotate", PI.host_id[:12]]) == 0
    assert "only for this host" in capsys.readouterr().out
