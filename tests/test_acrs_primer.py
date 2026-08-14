from __future__ import annotations

import base64
import json

import pytest

from azure_pim_cli.acrs_primer import ACTIVATION_URL, _has_c1


def jwt(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{raw}.signature"


class TestHasC1:
    def test_true_when_acrs_list_contains_c1(self) -> None:
        assert _has_c1(jwt({"acrs": ["c1"]})) is True

    def test_true_when_acrs_is_a_bare_string(self) -> None:
        assert _has_c1(jwt({"acrs": "c1"})) is True

    def test_comparison_is_case_insensitive(self) -> None:
        assert _has_c1(jwt({"acrs": ["C1"]})) is True

    def test_false_for_other_claim_values(self) -> None:
        assert _has_c1(jwt({"acrs": ["c2", "c3"]})) is False

    def test_false_when_claim_absent(self) -> None:
        assert _has_c1(jwt({"scp": "User.Read"})) is False

    @pytest.mark.parametrize("bad", ["", "not-a-jwt", "a.!!!.c"])
    def test_false_for_undecodable_token(self, bad: str) -> None:
        assert _has_c1(bad) is False


class TestActivationUrl:
    def test_points_at_the_pim_group_activation_blade(self) -> None:
        assert ACTIVATION_URL.startswith("https://portal.azure.com/")
        assert "aadgroup" in ACTIVATION_URL
