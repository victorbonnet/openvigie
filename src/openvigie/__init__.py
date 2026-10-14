"""openvigie: control tower for AI agents on your own machines."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("openvigie")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0"

__all__ = ["__version__"]
