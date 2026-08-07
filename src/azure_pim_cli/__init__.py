"""azure-pim-cli package metadata."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _metadata_version


def _resolve_version() -> str:
    """Installed metadata first; fall back to the build-time file, then a sentinel."""
    try:
        return _metadata_version("azure-pim-cli")
    except PackageNotFoundError:
        pass
    try:
        from ._version import version
    except ImportError:
        # Source tree that has never been built or installed.
        return "0.0.0+unknown"
    return version


__version__ = _resolve_version()

__all__ = ["__version__"]
