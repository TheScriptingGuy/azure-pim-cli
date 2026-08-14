from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError

import pytest

import azure_pim_cli


class TestResolveVersion:
    def test_prefers_installed_metadata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(azure_pim_cli, "_metadata_version", lambda name: "9.9.9")
        assert azure_pim_cli._resolve_version() == "9.9.9"

    def test_falls_back_to_build_time_version_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _missing(name: str) -> str:
            raise PackageNotFoundError(name)

        monkeypatch.setattr(azure_pim_cli, "_metadata_version", _missing)
        # _version.py is generated at build time; import it to learn what to expect.
        from azure_pim_cli._version import version

        assert azure_pim_cli._resolve_version() == version

    def test_sentinel_when_neither_source_is_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _missing(name: str) -> str:
            raise PackageNotFoundError(name)

        monkeypatch.setattr(azure_pim_cli, "_metadata_version", _missing)
        # Simulate a source tree that was never built: block the _version import.
        monkeypatch.setitem(sys.modules, "azure_pim_cli._version", None)
        assert azure_pim_cli._resolve_version() == "0.0.0+unknown"


class TestPublicSurface:
    def test_exports_version(self) -> None:
        assert azure_pim_cli.__all__ == ["__version__"]
        assert isinstance(azure_pim_cli.__version__, str)
        assert azure_pim_cli.__version__
