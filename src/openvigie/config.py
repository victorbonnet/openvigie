"""Settings for the collector and the hub, loaded from TOML, with safe defaults.

Files live in ``$OPENVIGIE_CONFIG_DIR`` (default ``$XDG_CONFIG_HOME/openvigie``, else
``~/.config/openvigie``) on Linux and macOS alike. Unknown keys are errors, so typos fail loudly.
The hub binds loopback unless ``allow_non_loopback = true`` is set explicitly.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import logging
import os
import re
import stat
import tomllib
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

MAX_CONFIG_BYTES = 1024 * 1024


class ConfigError(ValueError):
    pass


def _xdg(env: str, var: str, fallback: str) -> Path:
    if override := os.environ.get(env):
        return Path(override).expanduser()
    base = os.environ.get(var)
    return (Path(base) if base else Path.home() / fallback) / "openvigie"


def config_dir() -> Path:
    return _xdg("OPENVIGIE_CONFIG_DIR", "XDG_CONFIG_HOME", ".config")


def state_dir() -> Path:
    return _xdg("OPENVIGIE_STATE_DIR", "XDG_STATE_HOME", ".local/state")


def data_dir() -> Path:
    return _xdg("OPENVIGIE_DATA_DIR", "XDG_DATA_HOME", ".local/share")


@dataclass(frozen=True)
class Retention:
    """Days to keep data; 0 keeps it forever (the default until a retention policy is chosen)."""

    usage_days: int = 0
    status_events_days: int = 0
    # spans are only queued for export, never kept: unexported ones older than this are dropped
    span_queue_hours: int = 24


@dataclass(frozen=True)
class LangfuseSettings:
    """``[langfuse]`` in hub.toml. Absent: no trace export and no outbound connection at all."""

    host: str = ""  # e.g. "https://cloud.langfuse.com" or a self-hosted URL
    public_key: str = ""
    # 0600 file holding the secret key; LANGFUSE_SECRET_KEY in the hub's environment also works
    secret_key_file: Path | None = None
    export_content: bool = False  # prompts/outputs/tool I/O; collectors must opt in too
    project_id: str | None = None  # for trace links (#63)

    def secret_key(self) -> str:
        """The secret key; never log or display it."""
        if secret := os.environ.get("LANGFUSE_SECRET_KEY", "").strip():
            return secret
        if self.secret_key_file is None:
            raise ConfigError("langfuse: set secret_key_file or LANGFUSE_SECRET_KEY")
        return read_private_file(self.secret_key_file)


@dataclass(frozen=True)
class HubSettings:
    bind: str = "127.0.0.1"
    port: int = 7878
    allow_non_loopback: bool = False
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")
    db_path: Path | None = None  # default: data_dir()/hub.db
    display_timezone: str | None = None  # default: hub local time
    retention: Retention = field(default_factory=Retention)
    langfuse: LangfuseSettings | None = None

    def resolved_db_path(self) -> Path:
        return self.db_path or data_dir() / "hub.db"


@dataclass(frozen=True)
class OtlpSettings:
    """``[otlp]`` in collector.toml: a local OTLP/HTTP JSON receiver (#64). Off by default."""

    enabled: bool = False
    port: int = 4318  # always on 127.0.0.1
    token_file: Path | None = None  # if set, agents must send "Authorization: Bearer <token>"
    usage_source: str = "auto"  # "auto" | "logs" | "otlp"


@dataclass(frozen=True)
class CollectorSettings:
    hub_url: str | None = None
    token_file: Path | None = None  # default: state_dir()/token
    homes: tuple[Path, ...] = field(default_factory=lambda: (Path.home(),))
    disable_adapters: tuple[str, ...] = ()  # built-in adapters to turn off
    enable_plugins: tuple[str, ...] = ()  # third-party adapters to load (off by default)
    active_window_s: int = 300
    fast_tick_s: float = 5.0
    slow_tick_s: float = 30.0
    backfill_days: int = 30  # history read on first run; older usage is skipped
    spool_limit_mb: int = 50
    # allow prompts/outputs/tool I/O in spans when the hub also asks for them (FR-6, #62)
    export_content: bool = False
    otlp: OtlpSettings = field(default_factory=OtlpSettings)

    def resolved_token_file(self) -> Path:
        return self.token_file or state_dir() / "token"


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_hub(settings: HubSettings) -> HubSettings:
    if not 0 < settings.port < 65536:
        raise ConfigError("hub.port must be between 1 and 65535")
    if settings.retention.usage_days < 0 or settings.retention.status_events_days < 0:
        raise ConfigError("retention days must be 0 (keep forever) or positive")
    if settings.retention.span_queue_hours < 1:
        raise ConfigError("retention.span_queue_hours must be at least 1")
    if settings.langfuse is not None:
        validate_langfuse(settings.langfuse)
    if not is_loopback(settings.bind):
        if not settings.allow_non_loopback:
            raise ConfigError(
                f"hub.bind = {settings.bind!r} is not a loopback address. Use `tailscale serve` "
                "for remote access, or set allow_non_loopback = true if you really mean it."
            )
        log.warning(
            "hub is binding %s: it is reachable from the network, not just this host",
            settings.bind,
        )
    return settings


def validate_langfuse(langfuse: LangfuseSettings) -> None:
    parts = urlsplit(langfuse.host)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username:
        raise ConfigError("langfuse.host must be an http(s) URL without credentials")
    if parts.scheme == "http" and not is_loopback(parts.hostname):
        raise ConfigError("langfuse.host must use https unless Langfuse runs on this machine")
    if not langfuse.public_key.startswith("pk-") or len(langfuse.public_key) > 256:
        raise ConfigError("langfuse.public_key must be a Langfuse public key (pk-…)")
    if langfuse.project_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}",
                                                            langfuse.project_id):  # fmt: skip
        raise ConfigError("langfuse.project_id: letters, digits, '-' and '_' only")


def validate_collector(settings: CollectorSettings) -> CollectorSettings:
    if settings.active_window_s <= 0:
        raise ConfigError("collector.active_window_s must be positive")
    if settings.fast_tick_s <= 0 or settings.slow_tick_s <= 0:
        raise ConfigError("collector tick intervals must be positive")
    if settings.backfill_days < 0 or settings.spool_limit_mb <= 0:
        raise ConfigError("collector.backfill_days must be >= 0 and spool_limit_mb > 0")
    if settings.otlp.usage_source not in ("auto", "logs", "otlp"):
        raise ConfigError('otlp.usage_source must be "auto", "logs" or "otlp"')
    if not 0 < settings.otlp.port < 65536:
        raise ConfigError("otlp.port must be between 1 and 65535")
    return settings


def load_hub_settings(path: Path | None = None) -> HubSettings:
    data = _read_toml(path or config_dir() / "hub.toml")
    return validate_hub(_build(HubSettings, data, "hub"))


def load_collector_settings(path: Path | None = None) -> CollectorSettings:
    data = _read_toml(path or config_dir() / "collector.toml")
    return validate_collector(_build(CollectorSettings, data, "collector"))


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        st = path.stat()
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_CONFIG_BYTES:
        raise ConfigError(f"{path}: not a regular file or larger than {MAX_CONFIG_BYTES} bytes")
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: invalid TOML ({exc})") from None


def _build[T](cls: type[T], data: dict[str, Any], where: str) -> T:
    hints = get_type_hints(cls)
    names = {f.name for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
    unknown = sorted(set(data) - names)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)}")
    return cls(**{k: _coerce(hints[k], v, f"{where}.{k}") for k, v in data.items()})


def _coerce(tp: Any, value: Any, where: str) -> Any:
    origin = get_origin(tp)
    if origin in (Union, types.UnionType):
        non_none = [t for t in get_args(tp) if t is not type(None)]
        return _coerce(non_none[0], value, where)
    if origin is tuple:
        if not isinstance(value, list):
            raise ConfigError(f"{where}: expected a list")
        item_tp = get_args(tp)[0]
        return tuple(_coerce(item_tp, v, where) for v in value)
    if dataclasses.is_dataclass(tp) and isinstance(tp, type):
        if not isinstance(value, dict):
            raise ConfigError(f"{where}: expected a table")
        return _build(tp, value, where)
    if tp is Path:
        if not isinstance(value, str):
            raise ConfigError(f"{where}: expected a path string")
        return Path(value).expanduser()
    if tp is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if tp is int and isinstance(value, bool):
        raise ConfigError(f"{where}: expected int")
    if not isinstance(value, tp):
        raise ConfigError(f"{where}: expected {tp.__name__}")
    return value


# --- private files -----------------------------------------------------------------------------


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (and parents) and make it ``0700``."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def write_private_file(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` with mode ``0600``, creating the parent dir ``0700``."""
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    path.chmod(0o600)


def read_private_file(path: Path, max_bytes: int = 64 * 1024) -> str:
    """Read a secret file, refusing it unless it is ours and not group/world accessible."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise ConfigError(f"{path}: not a regular file")
        if st.st_uid != os.geteuid():
            raise ConfigError(f"{path}: not owned by the current user")
        if st.st_mode & 0o077:
            raise ConfigError(f"{path}: permissions too open, run `chmod 600 {path}`")
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ConfigError(f"{path}: larger than {max_bytes} bytes")
    return data.decode("utf-8").strip()
