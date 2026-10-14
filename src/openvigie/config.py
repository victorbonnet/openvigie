"""Settings for the collector and the hub, loaded from TOML, with safe defaults.

Files live in ``$OPENVIGIE_CONFIG_DIR`` (default ``$XDG_CONFIG_HOME/openvigie``, else
``~/.config/openvigie``) on Linux and macOS alike. Each section is a Pydantic dataclass (PRD D12):
building one validates it, unknown keys are errors so typos fail loudly, and errors name the key
but never quote its value. The hub binds loopback unless ``allow_non_loopback = true`` is set.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import stat
import tomllib
from dataclasses import field
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast, dataclass_transform
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    ConfigDict,
    Field,
    Strict,
    StrictBool,
    StrictStr,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from pydantic.dataclasses import dataclass

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


Port = Annotated[int, Strict(), Field(ge=1, le=65535)]
Days = Annotated[int, Strict(), Field(ge=0)]  # 0 keeps data forever
Seconds = Annotated[float, Strict(), Field(gt=0)]
UserPath = Annotated[Path, AfterValidator(Path.expanduser)]
UsageSource = Literal["auto", "logs", "otlp"]
SETTINGS = ConfigDict(extra="forbid", hide_input_in_errors=True)


@dataclass_transform(frozen_default=True, field_specifiers=(field, Field))
def settings_type[C](cls: type[C]) -> type[C]:
    """Decorator for a settings section: a frozen Pydantic dataclass that rejects unknown keys."""
    return cast(type[C], dataclass(frozen=True, config=SETTINGS)(cls))


@settings_type
class Retention:
    """Days to keep data; 0 keeps it forever (the default until a retention policy is chosen)."""

    usage_days: Days = 0
    status_events_days: Days = 0
    # spans are only queued for export, never kept: unexported ones older than this are dropped
    span_queue_hours: Annotated[int, Strict(), Field(ge=1)] = 24


@settings_type
class LangfuseSettings:
    """``[langfuse]`` in hub.toml. Absent: no trace export and no outbound connection at all."""

    host: StrictStr = ""  # e.g. "https://cloud.langfuse.com" or a self-hosted URL
    public_key: StrictStr = ""
    # 0600 file holding the secret key; LANGFUSE_SECRET_KEY in the hub's environment also works
    secret_key_file: UserPath | None = None
    export_content: StrictBool = False  # prompts/outputs/tool I/O; collectors must opt in too
    project_id: StrictStr | None = None  # for trace links (#63)

    @model_validator(mode="after")
    def _check(self) -> Self:
        parts = urlsplit(self.host)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username:
            raise ValueError("langfuse.host must be an http(s) URL without credentials")
        if parts.scheme == "http" and not is_loopback(parts.hostname):
            raise ValueError("langfuse.host must use https unless Langfuse runs on this machine")
        if not self.public_key.startswith("pk-") or len(self.public_key) > 256:
            raise ValueError("langfuse.public_key must be a Langfuse public key (pk-…)")
        if self.project_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}",
                                                            self.project_id):  # fmt: skip
            raise ValueError("langfuse.project_id: letters, digits, '-' and '_' only")
        return self

    def secret_key(self) -> str:
        """The secret key; never log or display it."""
        if secret := os.environ.get("LANGFUSE_SECRET_KEY", "").strip():
            return secret
        if self.secret_key_file is None:
            raise ConfigError("langfuse: set secret_key_file or LANGFUSE_SECRET_KEY")
        return read_private_file(self.secret_key_file)


@settings_type
class HubSettings:
    bind: StrictStr = "127.0.0.1"
    port: Port = 7878
    allow_non_loopback: StrictBool = False
    allowed_hosts: tuple[StrictStr, ...] = ("127.0.0.1", "localhost")
    db_path: UserPath | None = None  # default: data_dir()/hub.db
    display_timezone: StrictStr | None = None  # default: hub local time
    retention: Retention = field(default_factory=Retention)
    langfuse: LangfuseSettings | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not is_loopback(self.bind):
            if not self.allow_non_loopback:
                raise ValueError(
                    f"hub.bind = {self.bind!r} is not a loopback address. Use `tailscale serve` "
                    "for remote access, or set allow_non_loopback = true if you really mean it."
                )
            log.warning(
                "hub is binding %s: it is reachable from the network, not just this host",
                self.bind,
            )
        return self

    def resolved_db_path(self) -> Path:
        return self.db_path or data_dir() / "hub.db"


@settings_type
class OtlpSettings:
    """``[otlp]`` in collector.toml: a local OTLP/HTTP JSON receiver (#64). Off by default."""

    enabled: StrictBool = False
    port: Port = 4318  # always on 127.0.0.1
    token_file: UserPath | None = None  # if set, agents must send "Authorization: Bearer <token>"
    usage_source: UsageSource = "auto"


@settings_type
class CollectorSettings:
    hub_url: StrictStr | None = None
    token_file: UserPath | None = None  # default: state_dir()/token
    homes: tuple[UserPath, ...] = field(default_factory=lambda: (Path.home(),))
    disable_adapters: tuple[StrictStr, ...] = ()  # built-in adapters to turn off
    enable_plugins: tuple[StrictStr, ...] = ()  # third-party adapters to load (off by default)
    active_window_s: Annotated[int, Strict(), Field(gt=0)] = 300
    fast_tick_s: Seconds = 5.0
    slow_tick_s: Annotated[float, Strict(), Field(ge=0)] = 30.0  # 0: on every tick
    backfill_days: Days = 30  # history read on first run; older usage is skipped
    spool_limit_mb: Annotated[int, Strict(), Field(gt=0)] = 50
    # allow prompts/outputs/tool I/O in spans when the hub also asks for them (FR-6, #62)
    export_content: StrictBool = False
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


def load_hub_settings(path: Path | None = None) -> HubSettings:
    return _load(HubSettings, path or config_dir() / "hub.toml", "hub")


def load_collector_settings(path: Path | None = None) -> CollectorSettings:
    return _load(CollectorSettings, path or config_dir() / "collector.toml", "collector")


def _load[T](cls: type[T], path: Path, where: str) -> T:
    try:
        settings: T = TypeAdapter(cls).validate_python(_read_toml(path))
    except ValidationError as exc:
        raise ConfigError(f"{path}: {describe(exc, where)}") from None
    return settings


def describe(exc: ValidationError, where: str) -> str:
    """The first problem, located as ``section.key``, without the offending value."""
    first = exc.errors(include_url=False, include_context=False, include_input=False)[0]
    key = ".".join([where, *(str(p) for p in first["loc"])])
    if first["type"] == "unexpected_keyword_argument":
        return f"{key}: unknown key"
    if first["type"] == "tuple_type":
        return f"{key}: expected a list"
    message = first["msg"].removeprefix("Value error, ")
    return message if first["type"] == "value_error" else f"{key}: {message}"


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
