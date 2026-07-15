from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest


@pytest.fixture()
def fake_localappdata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return tmp_path


@pytest.fixture()
def frozen_now(monkeypatch: pytest.MonkeyPatch):
    """Return a fixed UTC datetime and patch datetime.now inside cache module."""
    fixed = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)

    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.replace(tzinfo=tz) if tz else fixed

    import azure_pim_cli.cache as cache_mod

    monkeypatch.setattr(cache_mod, "datetime", _FakeDatetime)
    return fixed


@pytest.fixture()
def fresh_cache_payload() -> dict:
    return {
        "fetchedAt": datetime.now(UTC).isoformat(),
        "principalId": "user-123",
        "eligible": [
            {"groupId": "grp-1", "displayName": "Group One", "accessId": "member"},
        ],
    }


def write_cache(cache_dir: Path, payload: dict) -> Path:
    d = cache_dir / "pim_activate"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "eligible_cache.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    return f
