"""Tests for token_grabber's pure helpers and its Playwright-facing glue.

The browser objects are duck-typed fakes: the helpers only ever touch a handful
of attributes (frames, pages, url, evaluate, goto, reload), so a small stand-in
exercises the real control flow without launching a browser.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

import azure_pim_cli.token_grabber as tg


def jwt(payload: dict) -> str:
    """Build a token whose middle segment decodes to payload."""
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{raw}.signature"


class TestProfileDir:
    def test_prefers_localappdata(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        d = tg._profile_dir()
        assert d == tmp_path / "pim_activate" / "browser_profile"
        assert d.is_dir(), "the profile directory should be created"

    def test_falls_back_to_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        d = tg._profile_dir()
        assert d == tmp_path / ".pim_activate" / "browser_profile"
        assert d.is_dir()


class TestDecodePayload:
    def test_decodes_claims(self) -> None:
        assert tg._decode_payload(jwt({"scp": "x", "exp": 42})) == {"scp": "x", "exp": 42}

    def test_pads_unpadded_base64(self) -> None:
        # A single-key payload lands on a length needing padding restored.
        assert tg._decode_payload(jwt({"a": 1})) == {"a": 1}

    @pytest.mark.parametrize("bad", ["", "not-a-jwt", "a.!!!.c", "only-one-segment"])
    def test_malformed_token_returns_none(self, bad: str) -> None:
        assert tg._decode_payload(bad) is None


class TestDecodeExp:
    def test_returns_exp(self) -> None:
        assert tg._decode_exp(jwt({"exp": 1234})) == 1234

    def test_missing_exp_is_zero(self) -> None:
        assert tg._decode_exp(jwt({"scp": "x"})) == 0

    def test_undecodable_returns_none(self) -> None:
        assert tg._decode_exp("garbage") is None


class TestHasPimScope:
    def test_read_scope_accepted_by_default(self) -> None:
        token = jwt({"scp": "PrivilegedAccess.Read.AzureADGroup User.Read"})
        assert tg._has_pim_scope(token) is True

    def test_read_scope_rejected_when_readwrite_required(self) -> None:
        token = jwt({"scp": "PrivilegedAccess.Read.AzureADGroup"})
        assert tg._has_pim_scope(token, require_readwrite=True) is False

    def test_readwrite_scope_accepted(self) -> None:
        token = jwt({"scp": "PrivilegedAccess.ReadWrite.AzureADGroup"})
        assert tg._has_pim_scope(token, require_readwrite=True) is True

    def test_unrelated_scope_rejected(self) -> None:
        assert tg._has_pim_scope(jwt({"scp": "User.Read Mail.Send"})) is False

    def test_missing_scp_rejected(self) -> None:
        assert tg._has_pim_scope(jwt({"aud": "x"})) is False

    def test_undecodable_rejected(self) -> None:
        assert tg._has_pim_scope("garbage") is False

    def test_acrs_required_and_present_as_list(self) -> None:
        token = jwt({"scp": "PrivilegedAccess.ReadWrite.AzureADGroup", "acrs": ["c1"]})
        assert tg._has_pim_scope(token, require_readwrite=True, require_acrs=True) is True

    def test_acrs_required_and_present_as_string(self) -> None:
        token = jwt({"scp": "PrivilegedAccess.ReadWrite.AzureADGroup", "acrs": "C1"})
        assert tg._has_pim_scope(token, require_readwrite=True, require_acrs=True) is True

    def test_acrs_required_but_absent(self) -> None:
        token = jwt({"scp": "PrivilegedAccess.ReadWrite.AzureADGroup"})
        assert tg._has_pim_scope(token, require_readwrite=True, require_acrs=True) is False


class TestIsAzrbacToken:
    def test_detects_legacy_audience(self) -> None:
        assert tg._is_azrbac_token(jwt({"aud": tg.AZRBAC_AUD_MARKERS[0]})) is True

    def test_graph_audience_is_not_azrbac(self) -> None:
        assert tg._is_azrbac_token(jwt({"aud": "https://graph.microsoft.com"})) is False

    def test_missing_aud_is_false(self) -> None:
        assert tg._is_azrbac_token(jwt({"scp": "x"})) is False

    def test_undecodable_is_false(self) -> None:
        assert tg._is_azrbac_token("garbage") is False


class FakeFrame:
    def __init__(self, result: Any = None, raises: bool = False) -> None:
        self._result = result
        self._raises = raises

    def evaluate(self, _js: str) -> Any:
        if self._raises:
            raise RuntimeError("frame detached")
        return self._result


class FakePage:
    def __init__(self, url: str = "https://portal.azure.com/#home", frames: list[FakeFrame] | None = None) -> None:
        self.url = url
        self.frames = frames or []


class TestScrapeStorage:
    def test_returns_first_token_found(self) -> None:
        page = FakePage(frames=[FakeFrame(None), FakeFrame("tok-2"), FakeFrame("tok-3")])
        assert tg._scrape_storage(page) == "tok-2"

    def test_skips_frames_that_raise(self) -> None:
        page = FakePage(frames=[FakeFrame(raises=True), FakeFrame("tok")])
        assert tg._scrape_storage(page) == "tok"

    def test_none_when_no_frame_has_a_token(self) -> None:
        assert tg._scrape_storage(FakePage(frames=[FakeFrame(None)])) is None


class TestIsPortalTab:
    def test_true_for_portal_host(self) -> None:
        assert tg._is_portal_tab("https://portal.azure.com/#view/blade") is True

    def test_false_when_portal_only_appears_in_query(self) -> None:
        assert tg._is_portal_tab("https://login.microsoftonline.com/?next=portal.azure.com") is False

    def test_false_for_unparsable(self) -> None:
        assert tg._is_portal_tab("http://[") is False


class FakeContext:
    def __init__(self, pages: list[Any]) -> None:
        self.pages = pages


class ExplodingUrlPage:
    frames: list[FakeFrame] = []

    @property
    def url(self) -> str:
        raise RuntimeError("page closed")


class TestScrapeAllPages:
    def test_scans_only_portal_tabs(self) -> None:
        ctx = FakeContext(
            [
                FakePage(url="https://example.com", frames=[FakeFrame("wrong-tab")]),
                FakePage(url="https://portal.azure.com/#x", frames=[FakeFrame("right-tab")]),
            ]
        )
        assert tg._scrape_all_pages(ctx) == "right-tab"

    def test_skips_pages_whose_url_raises(self) -> None:
        ctx = FakeContext([ExplodingUrlPage(), FakePage(frames=[FakeFrame("tok")])])
        assert tg._scrape_all_pages(ctx) == "tok"

    def test_none_when_nothing_found(self) -> None:
        assert tg._scrape_all_pages(FakeContext([FakePage(frames=[FakeFrame(None)])])) is None


class EvalPage:
    """Page whose evaluate() returns queued body texts (or raises)."""

    def __init__(self, bodies: list[Any]) -> None:
        self._bodies = list(bodies)
        self.goto_calls: list[str] = []
        self.reload_calls = 0
        self.goto_errors: list[Any] = []

    def evaluate(self, _js: str) -> Any:
        if not self._bodies:
            return ""
        b = self._bodies.pop(0)
        if isinstance(b, Exception):
            raise b
        return b

    def goto(self, url: str, **_kw: Any) -> None:
        self.goto_calls.append(url)
        if self.goto_errors:
            err = self.goto_errors.pop(0)
            if err is not None:
                raise err

    def reload(self, **_kw: Any) -> None:
        self.reload_calls += 1

    def wait_for_timeout(self, _ms: int) -> None:
        return None


class TestPortalErrorVisible:
    def test_detects_error_text(self) -> None:
        assert tg._portal_error_visible(EvalPage(["Hmmm... looks like something went wrong"])) is True

    def test_detects_dutch_error_text(self) -> None:
        assert tg._portal_error_visible(EvalPage(["Er is iets misgegaan"])) is True

    def test_normal_page_is_not_an_error(self) -> None:
        assert tg._portal_error_visible(EvalPage(["Privileged Identity Management"])) is False

    def test_empty_body_is_not_an_error(self) -> None:
        assert tg._portal_error_visible(EvalPage([""])) is False

    def test_evaluate_failure_is_not_an_error(self) -> None:
        assert tg._portal_error_visible(EvalPage([RuntimeError("no body")])) is False


class TestNavWithRetry:
    def test_clean_navigation_does_not_retry(self) -> None:
        page = EvalPage(["all good"])
        tg._nav_with_retry(page, "https://portal.azure.com/x")
        assert len(page.goto_calls) == 1
        assert page.reload_calls == 0

    def test_retries_after_goto_timeout(self) -> None:
        page = EvalPage(["ok"])
        page.goto_errors = [TimeoutError("timeout"), None]
        tg._nav_with_retry(page, "https://portal.azure.com/x")
        assert len(page.goto_calls) == 2
        assert page.reload_calls == 1, "a failed goto triggers a reload before the next attempt"

    def test_reload_clears_portal_error_page(self) -> None:
        # First body shows the error, the post-reload body is clean.
        page = EvalPage(["Hmmm, try again", "recovered"])
        tg._nav_with_retry(page, "https://portal.azure.com/x")
        assert page.reload_calls == 1
        assert len(page.goto_calls) == 1

    def test_persistent_error_page_exhausts_attempts(self) -> None:
        page = EvalPage(["try again"] * 10)
        tg._nav_with_retry(page, "https://portal.azure.com/x", attempts=2)
        assert len(page.goto_calls) == 2

    def test_gives_up_after_all_attempts_fail(self, capsys: pytest.CaptureFixture[str]) -> None:
        page = EvalPage([])
        page.goto_errors = [TimeoutError("boom")] * 3
        tg._nav_with_retry(page, "https://portal.azure.com/x", attempts=3)
        assert len(page.goto_calls) == 3
        assert "gave up after 3 attempts" in capsys.readouterr().err

    def test_reload_failure_after_goto_timeout_is_survivable(self) -> None:
        class _BadReload(EvalPage):
            def reload(self, **_kw: Any) -> None:
                raise RuntimeError("reload failed")

        page = _BadReload(["ok"])
        page.goto_errors = [TimeoutError("boom"), None]
        tg._nav_with_retry(page, "https://portal.azure.com/x", attempts=2)
        assert len(page.goto_calls) == 2

    def test_settle_timeout_failure_is_survivable(self) -> None:
        class _BadWait(EvalPage):
            def wait_for_timeout(self, _ms: int) -> None:
                raise RuntimeError("navigation interrupted")

        page = _BadWait(["all good"])
        tg._nav_with_retry(page, "https://portal.azure.com/x")
        assert len(page.goto_calls) == 1

    def test_reload_failure_during_error_recovery_is_survivable(self) -> None:
        class _BadReload(EvalPage):
            def reload(self, **_kw: Any) -> None:
                raise RuntimeError("reload failed")

        page = _BadReload(["try again", "try again"])
        tg._nav_with_retry(page, "https://portal.azure.com/x", attempts=2)
        assert len(page.goto_calls) == 2
