"""Cache load/save + NEW-diff for eligible PIM group assignments.

JSON schema matches Activate-PimGroup.ps1 so an existing PS cache seeds the diff.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from dateutil import parser as dateparser

CACHE_MAX_HOURS = 24


def cache_dir() -> Path:
    if os.environ.get("LOCALAPPDATA"):
        d = Path(os.environ["LOCALAPPDATA"]) / "pim_activate"
    else:
        d = Path.home() / ".pim_activate"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_file() -> Path:
    return cache_dir() / "eligible_cache.json"


def load() -> dict | None:
    f = cache_file()
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def is_fresh(cache: dict, principal_id: str) -> bool:
    if not cache or cache.get("principalId") != principal_id:
        return False
    fetched = cache.get("fetchedAt")
    if not fetched:
        return False
    try:
        age = datetime.now(UTC) - dateparser.isoparse(fetched)
    except Exception:
        return False
    return age.total_seconds() < CACHE_MAX_HOURS * 3600


def save(principal_id: str, eligible: list[dict]) -> None:
    payload = {
        "fetchedAt": datetime.now(UTC).isoformat(),
        "principalId": principal_id,
        "eligible": eligible,
    }
    cache_file().write_text(json.dumps(payload, indent=2), encoding="utf-8")


def mark_new(current: list[dict], previous: list[dict] | None) -> list[dict]:
    prev_ids = {e["groupId"] for e in (previous or [])}
    for e in current:
        e["isNew"] = bool(prev_ids) and e["groupId"] not in prev_ids
    return current
