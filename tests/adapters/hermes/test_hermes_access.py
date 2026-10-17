import json
import os
import shutil
from pathlib import Path

import pytest

from openvigie.adapters.hermes import HermesAdapter
from openvigie.adapters.hermes.access import read_access
from openvigie.adapters.testing import fake_context
from openvigie.model import Channel
from openvigie.risk import reachable_by_others, reached_by

FIXTURE = Path(__file__).parent / "fixtures" / "0.21.7" / "home" / ".hermes"
SECRETS = "TELEGRAM_BOT_TOKEN=123456:never-read\nOPENAI_API_KEY=sk-never-read\n"


def telegram(tmp_path: Path, env: str, approved: list[str] | None = None) -> Channel:
    (tmp_path / ".env").write_text(SECRETS + env)
    if approved is not None:
        (tmp_path / "platforms" / "pairing").mkdir(parents=True)
        approvals = {user: {"approved_at": 1} for user in approved}
        (tmp_path / "platforms" / "pairing" / "telegram-approved.json").write_text(
            json.dumps(approvals)
        )
    return read_access(tmp_path).channel("telegram")


@pytest.mark.parametrize(
    ("env", "approved", "expected", "who"),
    [
        ("TELEGRAM_ALLOWED_USERS=111\n", None, Channel("telegram", "allowlist", False, 1, False),
         "1 allowlisted person"),
        ('export TELEGRAM_ALLOWED_USERS="111, 222"\nGATEWAY_ALLOWED_USERS=222,333\n', None,
         Channel("telegram", "allowlist", False, 3, False), "3 allowlisted people"),
        ("TELEGRAM_ALLOWED_USERS=[\"111\", \"222\"]\n", ["333", "111"],
         Channel("telegram", "allowlist", False, 3, False), "3 allowlisted people"),
        ("", ["111"], Channel("telegram", "pairing", False, 1, False), "1 allowlisted person"),
        ("TELEGRAM_ALLOW_ALL_USERS=true\n", None, Channel("telegram", "open", True, None, True),
         "anyone (direct messages are open)"),
        ("TELEGRAM_ALLOWED_USERS=*\n", None, Channel("telegram", "open", True, None, True),
         "anyone (direct messages are open)"),
        ("GATEWAY_ALLOW_ALL_USERS=yes\n", None, Channel("telegram", "open", True, None, True),
         "anyone (direct messages are open)"),
        # an allowlist wins over the gateway-wide opt-in
        ("GATEWAY_ALLOW_ALL_USERS=1\nTELEGRAM_ALLOWED_USERS=111\n", None,
         Channel("telegram", "allowlist", False, 1, False), "1 allowlisted person"),
        ("TELEGRAM_ALLOWED_USERS=111\nTELEGRAM_GROUP_ALLOWED_CHATS=-100200\n", None,
         Channel("telegram", "allowlist", False, 1, True),
         "members of its group chats and 1 allowlisted person"),
        # nothing says who may write: the setting may live outside .env
        ("TELEGRAM_ALLOW_ALL_USERS=false\n# TELEGRAM_ALLOWED_USERS=999\n", None,
         Channel("telegram"), "unknown"),
        ("TELEGRAM_ALLOW_ALL_USERS=true  # open for now\n", None,
         Channel("telegram", "open", True, None, True), "anyone (direct messages are open)"),
        ("TELEGRAM_ALLOWED_USERS=*  # anyone\n", None,
         Channel("telegram", "open", True, None, True), "anyone (direct messages are open)"),
        ('TELEGRAM_ALLOW_ALL_USERS="true" # x\n', None,
         Channel("telegram", "open", True, None, True), "anyone (direct messages are open)"),
        ("TELEGRAM_ALLOWED_USERS='111 # not a comment',222  # two\n", None,
         Channel("telegram", "allowlist", False, 1, False), "1 allowlisted person"),
    ],
)  # fmt: skip
def test_who_may_message_a_profile(
    tmp_path: Path, env: str, approved: list[str] | None, expected: Channel, who: str
) -> None:
    channel = telegram(tmp_path, env, approved)
    assert channel == expected
    assert reached_by((channel,)) == who


def test_discord_roles_make_the_count_unknown(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("DISCORD_ALLOWED_USERS=1\nDISCORD_ALLOWED_ROLES=42\n")
    channel = read_access(tmp_path).channel("discord")
    assert channel.allowed_users is None
    assert reachable_by_others((channel,)) is None


def test_no_env_file_is_unknown(tmp_path: Path) -> None:
    assert read_access(tmp_path).channel("telegram") == Channel("telegram")


def test_unreadable_env_is_unknown(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TELEGRAM_ALLOWED_USERS=111\n" + "X=y\n" * 70_000)  # > 256 KiB
    assert read_access(tmp_path).channel("telegram") == Channel("telegram")
    real = tmp_path / "elsewhere.env"
    real.write_text("TELEGRAM_ALLOWED_USERS=111\n")
    (tmp_path / ".env").unlink()
    (tmp_path / ".env").symlink_to(real)
    assert read_access(tmp_path).channel("telegram") == Channel("telegram")


def test_a_platform_env_says_nothing_about_is_unknown(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(SECRETS + "TELEGRAM_ALLOWED_USERS=111\n")
    access = read_access(tmp_path)
    assert access.channel("api_server") == Channel("api_server")
    assert access.channel("telegram").allowed_users == 1


@pytest.mark.parametrize("folder", ["platforms/pairing", "pairing"])
def test_pairing_approvals_in_either_folder(tmp_path: Path, folder: str) -> None:
    (tmp_path / ".env").write_text(SECRETS)
    (tmp_path / folder).mkdir(parents=True)
    (tmp_path / folder / "telegram-approved.json").write_text('{"111": {}, "222": {}}')
    assert read_access(tmp_path).channel("telegram") == Channel(
        "telegram", "pairing", False, 2, False)  # fmt: skip


def test_roles(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("DISCORD_ALLOWED_ROLES=42\nGATEWAY_ALLOW_ALL_USERS=true\n")
    channel = read_access(tmp_path).channel("discord")
    assert (channel.dm_policy, channel.open_dms, channel.allowed_users) == (
        "allowlist",
        False,
        None,
    )
    (tmp_path / ".env").write_text("DISCORD_ALLOWED_ROLES=\nDISCORD_ALLOWED_USERS=1,2\n")
    assert read_access(tmp_path).channel("discord").allowed_users == 2


def test_only_access_values_are_looked_at(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(SECRETS + "TELEGRAM_ALLOWED_USERS=111,222\n")
    access = read_access(tmp_path)
    assert set(access.lists) == {"TELEGRAM_ALLOWED_USERS"}
    text = repr(access) + repr(access.channel("telegram"))
    assert "never-read" not in text and "111" not in text and "222" not in text


def test_profile_capabilities_count_people(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "home" / ".hermes"
    shutil.copytree(FIXTURE, root)
    (root / ".env").write_text(SECRETS + "TELEGRAM_ALLOWED_USERS=111\n")
    (root / "auth.json").write_text('{"token": "never-read"}')
    opened: list[str] = []
    real_open = os.open

    def spy(path: object, *args: object, **kwargs: object) -> int:
        opened.append(str(path))
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", spy)
    ctx = fake_context(homes=[tmp_path / "home"])
    adapter = HermesAdapter()
    (agent,) = adapter.detect(ctx)
    caps = adapter.capabilities(ctx, agent)
    assert caps is not None
    assert caps.channels == (Channel("telegram", "allowlist", False, 1, False),)
    assert "never-read" not in repr(caps)
    assert not any(p.endswith("auth.json") for p in opened)
