from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import azure_pim_cli.cache as cache


class TestCacheDir:
    def test_uses_localappdata(self, fake_localappdata: Path) -> None:
        d = cache.cache_dir()
        assert d == fake_localappdata / "pim_activate"
        assert d.exists()

    def test_fallback_to_home_when_no_localappdata(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        d = cache.cache_dir()
        assert d == tmp_path / ".pim_activate"


class TestLoad:
    def test_returns_none_when_file_missing(self, fake_localappdata: Path) -> None:
        assert cache.load() is None

    def test_returns_none_on_corrupt_json(self, fake_localappdata: Path) -> None:
        f = fake_localappdata / "pim_activate"
        f.mkdir(parents=True, exist_ok=True)
        (f / "eligible_cache.json").write_text("{not json}", encoding="utf-8")
        assert cache.load() is None

    def test_returns_dict_on_valid_json(self, fake_localappdata: Path) -> None:
        payload = {"fetchedAt": "2025-06-01T12:00:00+00:00", "principalId": "u1", "eligible": []}
        d = fake_localappdata / "pim_activate"
        d.mkdir(parents=True, exist_ok=True)
        (d / "eligible_cache.json").write_text(json.dumps(payload), encoding="utf-8")
        result = cache.load()
        assert result is not None
        assert result["principalId"] == "u1"


class TestIsFresh:
    def _make_cache(self, hours_ago: float, principal_id: str = "u1") -> dict:
        fetched = datetime.now(UTC) - timedelta(hours=hours_ago)
        return {"fetchedAt": fetched.isoformat(), "principalId": principal_id, "eligible": []}

    def test_fresh_within_24h(self) -> None:
        c = self._make_cache(hours_ago=1)
        assert cache.is_fresh(c, "u1") is True

    def test_stale_beyond_24h(self) -> None:
        c = self._make_cache(hours_ago=25)
        assert cache.is_fresh(c, "u1") is False

    def test_wrong_principal_id(self) -> None:
        c = self._make_cache(hours_ago=1)
        assert cache.is_fresh(c, "other-user") is False

    def test_missing_fetchedAt(self) -> None:
        assert cache.is_fresh({"principalId": "u1"}, "u1") is False

    def test_malformed_fetchedAt(self) -> None:
        assert cache.is_fresh({"fetchedAt": "not-a-date", "principalId": "u1"}, "u1") is False


class TestSave:
    def test_creates_file_with_correct_keys(self, fake_localappdata: Path) -> None:
        eligible = [{"groupId": "g1", "displayName": "G1"}]
        cache.save("u1", eligible)
        f = fake_localappdata / "pim_activate" / "eligible_cache.json"
        assert f.exists()
        data = json.loads(f.read_text())
        assert data["principalId"] == "u1"
        assert data["eligible"] == eligible
        assert "fetchedAt" in data


class TestMarkNew:
    def test_marks_absent_groups_as_new(self) -> None:
        prev = [{"groupId": "old"}]
        curr = [{"groupId": "old"}, {"groupId": "new"}]
        result = cache.mark_new(curr, prev)
        assert result[0]["isNew"] is False
        assert result[1]["isNew"] is True

    def test_no_previous_means_nothing_is_new(self) -> None:
        curr = [{"groupId": "grp"}]
        result = cache.mark_new(curr, None)
        assert result[0]["isNew"] is False

    def test_empty_previous_means_nothing_is_new(self) -> None:
        curr = [{"groupId": "grp"}]
        result = cache.mark_new(curr, [])
        assert result[0]["isNew"] is False
