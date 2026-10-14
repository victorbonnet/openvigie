import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

from openvigie import cli
from openvigie.service import render

EXE = "/usr/bin/env"  # any existing binary outside /home: systemd-analyze checks ExecStart


def test_system_collector_sees_only_agent_folders() -> None:
    unit = render("collector", "system", [Path("/home/victor"), Path("/home/ci")], exe=EXE)
    assert "User=openvigie\n" in unit and "ProtectHome=tmpfs" in unit
    assert "BindReadOnlyPaths=-/home/victor/.claude -/home/victor/.codex" in unit
    assert "-/home/ci/.codex" in unit and "/home/victor/.ssh" not in unit
    assert "CapabilityBoundingSet=CAP_DAC_READ_SEARCH" in unit and "MemoryMax=150M" in unit
    with pytest.raises(ValueError, match="--home"):
        render("collector", "system", [], exe=EXE)


def test_system_units_refuse_a_binary_in_a_home() -> None:
    with pytest.raises(ValueError, match="system-wide"):
        render("hub", "system", [], exe="/home/v/.local/bin/openvigie")
    assert render("hub", "user", [], exe="/home/v/.local/bin/openvigie")


def test_hub_has_no_capabilities_and_no_homes() -> None:
    unit = render("hub", "system", [], exe=EXE)
    assert "User=openvigie-hub" in unit and "CapabilityBoundingSet=\n" in unit
    assert "ProtectHome=yes" in unit and f"ExecStart={EXE} hub serve" in unit


def test_user_unit_and_launchd() -> None:
    assert f"ExecStart={EXE} collect" in render("collector", "user", [], exe=EXE)
    plist = plistlib.loads(render("hub", "launchd", [], exe="/opt/a&b/openvigie").encode())
    assert plist["Label"] == "dev.openvigie.hub"
    assert plist["ProgramArguments"] == ["/opt/a&b/openvigie", "hub", "serve"]


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="needs systemd-analyze")
def test_units_pass_systemd_analyze_verify(tmp_path: Path) -> None:
    for name, unit in {
        "openvigie-collector.service": render("collector", "system", [Path("/home/v")], exe=EXE),
        "openvigie-hub.service": render("hub", "system", [], exe=EXE),
        "openvigie-user.service": render("collector", "user", [], exe=EXE),
    }.items():
        (tmp_path / name).write_text(unit)
        result = subprocess.run(  # noqa: S603 - fixed argv
            ["systemd-analyze", "verify", str(tmp_path / name)],  # noqa: S607
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        assert result.returncode == 0, result.stderr


def test_cli_prints_and_installs_nothing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("openvigie.service.executable", lambda: EXE)
    assert cli.main(["service", "print", "collector", "--home", "/home/v"]) == 0
    assert "[Service]" in capsys.readouterr().out
    assert cli.main(["service", "print", "collector"]) == 2
    assert cli.main(["service", "print", "hub", "--launchd"]) == 0
    assert "<plist" in capsys.readouterr().out
