"""Tests for the orchestration layer in cli.py.

Graph is stubbed with FakeGC rather than respx here: these tests are about the
CLI's decision-making (caching, filtering, retry, exit codes), and a stub keeps
each test's setup to the handful of responses that actually drive the branch.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

import pytest

import azure_pim_cli.cli as cli
from azure_pim_cli.graph_client import GraphError, PermissionDenied, TokenExpired


class FakeGC:
    """Stand-in for GraphClient recording calls and replaying canned responses."""

    def __init__(
        self,
        *,
        me: dict | None = None,
        paged: Any = None,
        batches: list[dict] | None = None,
        post_result: Any = None,
        get_result: Any = None,
        active: Any = None,
        inflight: Any = None,
        approvals: Any = None,
        approve_error: Exception | None = None,
    ) -> None:
        self._me = me or {"id": "me-1", "userPrincipalName": "me@example.com"}
        self._paged = paged if paged is not None else []
        self._batches = list(batches or [])
        self._post_result = post_result
        self._get_result = get_result
        self._active = active if active is not None else []
        self._inflight = inflight if inflight is not None else []
        self._approvals = approvals if approvals is not None else []
        self._approve_error = approve_error

        self.posts: list[tuple[str, dict]] = []
        self.batch_calls: list[list[dict]] = []
        self.approved: list[tuple[str, str]] = []
        self.token: str | None = None
        self.get_calls: list[str] = []

    async def get(self, path: str) -> dict:
        self.get_calls.append(path)
        if path.startswith("/me"):
            return self._me
        if isinstance(self._get_result, Exception):
            raise self._get_result
        if isinstance(self._get_result, list):
            return self._get_result.pop(0)
        return self._get_result or {}

    async def get_paged(self, path: str) -> list[dict]:
        if isinstance(self._paged, Exception):
            raise self._paged
        return self._paged

    async def batch(self, requests: list[dict]) -> dict:
        self.batch_calls.append(requests)
        return self._batches.pop(0) if self._batches else {}

    async def post(self, path: str, body: dict) -> dict:
        self.posts.append((path, body))
        if isinstance(self._post_result, Exception):
            raise self._post_result
        return self._post_result or {}

    async def list_pim_group_active_assignments(self) -> list[dict]:
        if isinstance(self._active, Exception):
            raise self._active
        return self._active

    async def list_pim_group_inflight_requests(self) -> list[dict]:
        if isinstance(self._inflight, Exception):
            raise self._inflight
        return self._inflight

    async def list_pim_group_pending_approvals(self) -> list[dict]:
        if isinstance(self._approvals, Exception):
            raise self._approvals
        return self._approvals

    async def approve_pim_group_request(self, approval_id: str, justification: str) -> None:
        if self._approve_error:
            raise self._approve_error
        self.approved.append((approval_id, justification))

    def set_token(self, token: str) -> None:
        self.token = token


def make_args(**overrides: Any) -> argparse.Namespace:
    """Real parser defaults, so tests track the actual CLI surface."""
    args = cli.build_parser().parse_args([])
    for k, v in overrides.items():
        setattr(args, k, v)
    return args


def elig(**overrides: Any) -> dict:
    base = {
        "groupId": "g1",
        "displayName": "Group One",
        "description": "",
        "accessId": "member",
        "endDateTime": "Permanent",
        "policyMaxDurationHours": 8,
        "requiresJustification": True,
        "requiresTicket": False,
        "requiresMfa": False,
    }
    base.update(overrides)
    return base


def pend(**overrides: Any) -> dict:
    base = {
        "requestId": "req-1",
        "approvalId": "ap-1",
        "groupId": "g9",
        "displayName": "Pending Group",
        "accessId": "member",
        "requester": "someone@example.com",
        "justification": "need it",
        "duration": "PT8H",
    }
    base.update(overrides)
    return base


@pytest.fixture()
def stub_cache(monkeypatch: pytest.MonkeyPatch):
    """Neutralize the on-disk cache; individual tests opt into behavior."""

    state: dict[str, Any] = {"loaded": None, "saved": [], "valid": False}

    monkeypatch.setattr(cli.cache_mod, "load", lambda: state["loaded"])
    monkeypatch.setattr(cli.cache_mod, "save", lambda pid, elig: state["saved"].append((pid, elig)))
    monkeypatch.setattr(cli.cache_mod, "mark_new", lambda new, prev: new)
    monkeypatch.setattr(cli.cache_mod, "is_valid_for_session", lambda *a, **k: state["valid"])
    monkeypatch.setattr(cli, "port_alive", lambda port: False)
    return state


class TestFetchMe:
    async def test_selects_identity_fields(self) -> None:
        gc = FakeGC(me={"id": "abc", "userPrincipalName": "u@example.com"})
        assert (await cli.fetch_me(gc))["id"] == "abc"  # type: ignore[arg-type]
        assert "$select=id,displayName,userPrincipalName" in gc.get_calls[0]


class TestFetchEligibilities:
    async def test_no_eligibilities_short_circuits(self) -> None:
        gc = FakeGC(paged=[])
        assert await cli.fetch_eligibilities(gc, "me-1", 4) == []  # type: ignore[arg-type]
        assert gc.batch_calls == []

    async def test_enriches_from_policy_rules(self) -> None:
        raw = [
            {
                "groupId": "g1",
                "accessId": "member",
                "group": {"displayName": "Group One", "description": "desc"},
                "scheduleInfo": {"expiration": {"endDateTime": "2026-01-01T00:00:00Z"}},
            }
        ]
        gc = FakeGC(
            paged=raw,
            batches=[
                {"0": {"value": [{"policyId": "p1"}]}},
                {
                    "p1": {
                        "value": [
                            {"id": "Expiration_EndUser_Assignment", "maximumDuration": "PT4H"},
                            {
                                "id": "Enablement_EndUser_Assignment",
                                "enabledRules": ["Justification", "MultiFactorAuthentication"],
                            },
                        ]
                    }
                },
            ],
        )
        out = await cli.fetch_eligibilities(gc, "me-1", 4)  # type: ignore[arg-type]
        assert len(out) == 1
        item = out[0]
        assert item["displayName"] == "Group One"
        assert item["description"] == "desc"
        assert item["policyMaxDurationHours"] == 4
        assert item["requiresJustification"] is True
        assert item["requiresMfa"] is True
        assert item["requiresTicket"] is False
        assert item["endDateTime"] == "2026-01-01T00:00:00Z"

    async def test_defaults_when_policy_lookup_yields_nothing(self) -> None:
        raw = [{"groupId": "g1", "accessId": "owner", "group": {}}]
        gc = FakeGC(paged=raw, batches=[{"0": {"value": []}}, {}])
        out = await cli.fetch_eligibilities(gc, "me-1", 4)  # type: ignore[arg-type]
        assert out[0]["policyMaxDurationHours"] == 8
        assert out[0]["displayName"] == "g1", "falls back to groupId when the group has no displayName"
        assert out[0]["endDateTime"] == "Permanent"

    async def test_unparsable_duration_keeps_default(self) -> None:
        raw = [{"groupId": "g1", "accessId": "member", "group": {}}]
        gc = FakeGC(
            paged=raw,
            batches=[
                {"0": {"value": [{"policyId": "p1"}]}},
                {"p1": {"value": [{"id": "Expiration_EndUser_Assignment", "maximumDuration": "P1D"}]}},
            ],
        )
        out = await cli.fetch_eligibilities(gc, "me-1", 4)  # type: ignore[arg-type]
        assert out[0]["policyMaxDurationHours"] == 8

    async def test_dedupes_policy_rule_fetches(self) -> None:
        raw = [
            {"groupId": "g1", "accessId": "member", "group": {}},
            {"groupId": "g2", "accessId": "member", "group": {}},
        ]
        gc = FakeGC(
            paged=raw,
            batches=[
                {"0": {"value": [{"policyId": "p1"}]}, "1": {"value": [{"policyId": "p1"}]}},
                {"p1": {"value": []}},
            ],
        )
        await cli.fetch_eligibilities(gc, "me-1", 4)  # type: ignore[arg-type]
        assert len(gc.batch_calls[1]) == 1, "the shared policy should be fetched once"


class TestFetchActiveGroupIds:
    async def test_unions_active_and_inflight(self) -> None:
        gc = FakeGC(
            active=[{"group": {"id": "g1"}}, {"groupId": "g2"}],
            inflight=[{"group": {"id": "g3"}}],
        )
        assert await cli.fetch_active_group_ids(gc) == {"g1", "g2", "g3"}  # type: ignore[arg-type]

    async def test_ignores_entries_without_group_id(self) -> None:
        gc = FakeGC(active=[{"group": {}}, {}], inflight=[])
        assert await cli.fetch_active_group_ids(gc) == set()  # type: ignore[arg-type]

    async def test_graph_error_on_active_is_reported_not_fatal(self) -> None:
        gc = FakeGC(active=GraphError(500, "Boom", "x"), inflight=[{"groupId": "g3"}])
        assert await cli.fetch_active_group_ids(gc) == {"g3"}  # type: ignore[arg-type]

    async def test_graph_error_on_inflight_is_reported_not_fatal(self) -> None:
        gc = FakeGC(active=[{"groupId": "g1"}], inflight=GraphError(500, "Boom", "x"))
        assert await cli.fetch_active_group_ids(gc) == {"g1"}  # type: ignore[arg-type]

    async def test_non_graph_error_propagates(self) -> None:
        gc = FakeGC(active=RuntimeError("network down"), inflight=[])
        with pytest.raises(RuntimeError, match="network down"):
            await cli.fetch_active_group_ids(gc)  # type: ignore[arg-type]

    async def test_non_graph_error_on_inflight_propagates(self) -> None:
        gc = FakeGC(active=[], inflight=RuntimeError("inflight down"))
        with pytest.raises(RuntimeError, match="inflight down"):
            await cli.fetch_active_group_ids(gc)  # type: ignore[arg-type]


class TestFetchPendingApprovals:
    async def test_flattens_expanded_objects(self) -> None:
        gc = FakeGC(
            approvals=[
                {
                    "id": "req-1",
                    "approvalId": "ap-1",
                    "group": {"id": "g1", "displayName": "Group One"},
                    "principal": {"userPrincipalName": "req@example.com"},
                    "accessId": "member",
                    "justification": "why",
                    "scheduleInfo": {"expiration": {"duration": "PT8H"}},
                }
            ]
        )
        out = await cli.fetch_pending_approvals(gc)  # type: ignore[arg-type]
        assert out == [
            {
                "requestId": "req-1",
                "approvalId": "ap-1",
                "groupId": "g1",
                "displayName": "Group One",
                "accessId": "member",
                "requester": "req@example.com",
                "justification": "why",
                "duration": "PT8H",
            }
        ]

    async def test_falls_back_when_fields_missing(self) -> None:
        gc = FakeGC(approvals=[{"id": "req-1", "principal": {"displayName": "Display Only"}}])
        out = await cli.fetch_pending_approvals(gc)  # type: ignore[arg-type]
        assert out[0]["approvalId"] == "req-1", "approvalId falls back to the request id"
        assert out[0]["displayName"] == "?"
        assert out[0]["requester"] == "Display Only"
        assert out[0]["accessId"] == "member"

    async def test_graph_error_returns_empty(self) -> None:
        gc = FakeGC(approvals=GraphError(403, "Forbidden", "nope"))
        assert await cli.fetch_pending_approvals(gc) == []  # type: ignore[arg-type]


class TestActivate:
    async def test_provisioned_immediately_reports_expiry(self) -> None:
        gc = FakeGC(post_result={"id": "r1", "status": "Provisioned"})
        status, detail = await cli.activate(gc, "me-1", elig(), "why", 8, None)  # type: ignore[arg-type]
        assert status == "Provisioned"
        assert detail, "expiry timestamp should be reported"

    async def test_clamps_hours_to_policy_max(self) -> None:
        gc = FakeGC(post_result={"id": "r1", "status": "Provisioned"})
        await cli.activate(gc, "me-1", elig(policyMaxDurationHours=2), "why", 8, None)  # type: ignore[arg-type]
        body = gc.posts[0][1]
        assert body["scheduleInfo"]["expiration"]["duration"] == "PT2H"

    async def test_sends_ticket_info_only_when_required(self) -> None:
        gc = FakeGC(post_result={"id": "r1", "status": "Provisioned"})
        await cli.activate(gc, "me-1", elig(requiresTicket=True), "why", 4, "TICKET-9")  # type: ignore[arg-type]
        assert gc.posts[0][1]["ticketInfo"] == {"ticketNumber": "TICKET-9", "ticketSystem": "Provided"}

    async def test_omits_ticket_info_when_not_required(self) -> None:
        gc = FakeGC(post_result={"id": "r1", "status": "Provisioned"})
        await cli.activate(gc, "me-1", elig(), "why", 4, "TICKET-9")  # type: ignore[arg-type]
        assert "ticketInfo" not in gc.posts[0][1]

    async def test_awaiting_approval_short_circuits_polling(self) -> None:
        gc = FakeGC(post_result={"id": "r1", "status": "PendingApproval"})
        status, detail = await cli.activate(gc, "me-1", elig(), "why", 8, None)  # type: ignore[arg-type]
        assert status == "AwaitingApproval"
        assert detail == "reqId=r1"

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("RoleAssignmentExists", "AlreadyActive"),
            ("MatchingRoleAssignmentExists", "AlreadyActive"),
            ("SubjectHasActiveAssignment", "AlreadyActive"),
            ("MfaRequired", "MfaRequired"),
            ("StrongAuthenticationRequired", "MfaRequired"),
            ("PendingApproval", "PendingApproval"),
            ("PendingRoleAssignmentRequest", "PendingRequest"),
        ],
    )
    async def test_maps_known_graph_errors(self, message: str, expected: str) -> None:
        gc = FakeGC(post_result=GraphError(400, message, message))
        status, _ = await cli.activate(gc, "me-1", elig(), "why", 8, None)  # type: ignore[arg-type]
        assert status == expected

    async def test_unknown_graph_error_is_failed_with_detail(self) -> None:
        gc = FakeGC(post_result=GraphError(500, "Weird", "something odd"))
        status, detail = await cli.activate(gc, "me-1", elig(), "why", 8, None)  # type: ignore[arg-type]
        assert status == "Failed"
        assert "something odd" in detail

    async def test_polls_until_terminal_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "POLL_INTERVAL", 0)
        gc = FakeGC(
            post_result={"id": "r1", "status": "Granted"},
            get_result=[{"status": "PendingProvisioning"}, {"status": "Provisioned"}],
        )
        status, _ = await cli.activate(gc, "me-1", elig(), "why", 8, None)  # type: ignore[arg-type]
        assert status == "Provisioned"
        assert len(gc.get_calls) == 2

    async def test_poll_error_stops_polling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "POLL_INTERVAL", 0)
        gc = FakeGC(post_result={"id": "r1", "status": "Granted"}, get_result=GraphError(500, "x", "y"))
        status, _ = await cli.activate(gc, "me-1", elig(), "why", 8, None)  # type: ignore[arg-type]
        assert status == "Granted", "status stays at the last known value when polling fails"

    async def test_deadline_exhaustion_reports_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "POLL_INTERVAL", 0)
        monkeypatch.setattr(cli, "POLL_TIMEOUT", 0)
        gc = FakeGC(post_result={"id": "r1", "status": "Granted"})
        status, detail = await cli.activate(gc, "me-1", elig(), "why", 8, None)  # type: ignore[arg-type]
        assert status == "Timeout"
        assert detail == "reqId=r1"

    async def test_missing_request_id_skips_polling(self) -> None:
        gc = FakeGC(post_result={"status": "Granted"})
        status, detail = await cli.activate(gc, "me-1", elig(), "why", 8, None)  # type: ignore[arg-type]
        assert (status, detail) == ("Granted", "")
        assert gc.get_calls == []


class TestApprove:
    async def test_uses_approval_id(self) -> None:
        gc = FakeGC()
        assert await cli.approve(gc, pend(), "ok") == ("Approved", "")  # type: ignore[arg-type]
        assert gc.approved == [("ap-1", "ok")]

    async def test_falls_back_to_request_id(self) -> None:
        gc = FakeGC()
        await cli.approve(gc, pend(approvalId=""), "ok")  # type: ignore[arg-type]
        assert gc.approved == [("req-1", "ok")]

    async def test_graph_error_returns_failed(self) -> None:
        gc = FakeGC(approve_error=GraphError(403, "Forbidden", "not an approver"))
        status, detail = await cli.approve(gc, pend(), "ok")  # type: ignore[arg-type]
        assert status == "Failed"
        assert "not an approver" in detail


class TestBuildChoices:
    def test_pending_sorted_first_then_eligible(self) -> None:
        choices = cli.build_choices(
            [elig(displayName="Zebra"), elig(displayName="Alpha")],
            [pend(displayName="Middle")],
        )
        kinds = [c.value[0] for c in choices]
        assert kinds == ["APPROVE", "ACTIVATE", "ACTIVATE"]
        assert [c.value[1]["displayName"] for c in choices] == ["Middle", "Alpha", "Zebra"]

    def test_new_eligibility_is_flagged(self) -> None:
        choices = cli.build_choices([elig(isNew=True)], [])
        assert "*NEW*" in choices[0].title

    def test_title_carries_role_and_policy_facts(self) -> None:
        title = cli._elig_title(elig(accessId="owner", policyMaxDurationHours=2, requiresMfa=True), mark_new=False)
        assert "owner" in title
        assert "max 2h" in title
        assert "MFA=True" in title


class TestPrinters:
    def test_print_list_renders_both_tables(self, capsys: pytest.CaptureFixture[str]) -> None:
        cli._print_list([elig()], [pend()])
        out = capsys.readouterr().out
        assert "Eligible" in out
        assert "Group One" in out
        assert "Pending approvals" in out

    def test_print_list_handles_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        cli._print_list([], [])
        assert "Nothing to show" in capsys.readouterr().out

    def test_print_summary_lists_rows(self, capsys: pytest.CaptureFixture[str]) -> None:
        cli._print_summary([("ACTIVATE", "Group One", "member", "Provisioned", "")])
        out = capsys.readouterr().out
        assert "Summary" in out
        assert "Provisioned" in out


class TestRunWithClient:
    async def test_list_only_prints_and_exits_zero(self, stub_cache, capsys: pytest.CaptureFixture[str]) -> None:
        gc = FakeGC(
            paged=[{"groupId": "g1", "accessId": "member", "group": {"displayName": "Group One"}}],
            batches=[{"0": {"value": []}}, {}],
        )
        rc = await cli._run_with_client(make_args(list_only=True), gc, None)  # type: ignore[arg-type]
        assert rc == 0
        assert "Group One" in capsys.readouterr().out

    async def test_nothing_to_do_returns_one(self, stub_cache) -> None:
        gc = FakeGC(paged=[])
        rc = await cli._run_with_client(make_args(), gc, None)  # type: ignore[arg-type]
        assert rc == 1

    async def test_uses_cache_when_valid(self, stub_cache, capsys: pytest.CaptureFixture[str]) -> None:
        stub_cache["loaded"] = {"eligible": [elig(displayName="Cached Group")]}
        stub_cache["valid"] = True
        gc = FakeGC(paged=RuntimeError("eligibility fetch should not run"))
        rc = await cli._run_with_client(make_args(list_only=True), gc, None)  # type: ignore[arg-type]
        assert rc == 0
        out = capsys.readouterr().out
        assert "Using cached eligible list" in out
        assert "Cached Group" in out

    async def test_refresh_bypasses_cache_and_saves(self, stub_cache) -> None:
        stub_cache["loaded"] = {"eligible": [elig(displayName="Cached Group")]}
        stub_cache["valid"] = True
        gc = FakeGC(
            paged=[{"groupId": "g1", "accessId": "member", "group": {"displayName": "Fresh Group"}}],
            batches=[{"0": {"value": []}}, {}],
        )
        rc = await cli._run_with_client(make_args(list_only=True, refresh=True), gc, None)  # type: ignore[arg-type]
        assert rc == 0
        assert stub_cache["saved"], "a fresh fetch must repopulate the cache"

    async def test_skips_already_active_groups(self, stub_cache, capsys: pytest.CaptureFixture[str]) -> None:
        gc = FakeGC(
            paged=[
                {"groupId": "g1", "accessId": "member", "group": {"displayName": "Active Group"}},
                {"groupId": "g2", "accessId": "member", "group": {"displayName": "Free Group"}},
            ],
            batches=[{"0": {"value": []}, "1": {"value": []}}, {}],
            active=[{"groupId": "g1"}],
        )
        rc = await cli._run_with_client(make_args(list_only=True), gc, None)  # type: ignore[arg-type]
        assert rc == 0
        out = capsys.readouterr().out
        assert "Skipping 1 already-active/in-flight group(s): Active Group" in out
        assert "Free Group" in out

    async def test_group_regex_filters_both_feeds(self, stub_cache, capsys: pytest.CaptureFixture[str]) -> None:
        gc = FakeGC(
            paged=[
                {"groupId": "g1", "accessId": "member", "group": {"displayName": "db-admins"}},
                {"groupId": "g2", "accessId": "member", "group": {"displayName": "web-admins"}},
            ],
            batches=[{"0": {"value": []}, "1": {"value": []}}, {}],
        )
        rc = await cli._run_with_client(make_args(list_only=True, group="^db-"), gc, None)  # type: ignore[arg-type]
        assert rc == 0
        out = capsys.readouterr().out
        assert "db-admins" in out
        assert "web-admins" not in out

    async def test_group_filter_activates_without_picker(self, stub_cache, capsys: pytest.CaptureFixture[str]) -> None:
        gc = FakeGC(
            paged=[{"groupId": "g1", "accessId": "member", "group": {"displayName": "db-admins"}}],
            batches=[{"0": {"value": []}}, {}],
            post_result={"id": "r1", "status": "Provisioned"},
        )
        args = make_args(group="db-", justification="because", parallel=1)
        rc = await cli._run_with_client(args, gc, None)  # type: ignore[arg-type]
        assert rc == 0
        assert gc.posts, "matched group should be activated directly"
        assert "Provisioned" in capsys.readouterr().out

    async def test_parallel_path_reports_each_result(self, stub_cache, capsys: pytest.CaptureFixture[str]) -> None:
        gc = FakeGC(
            paged=[
                {"groupId": "g1", "accessId": "member", "group": {"displayName": "one"}},
                {"groupId": "g2", "accessId": "member", "group": {"displayName": "two"}},
            ],
            batches=[{"0": {"value": []}, "1": {"value": []}}, {}],
            post_result={"id": "r1", "status": "Provisioned"},
        )
        args = make_args(group="o|t", justification="because", parallel=4)
        rc = await cli._run_with_client(args, gc, None)  # type: ignore[arg-type]
        assert rc == 0
        out = capsys.readouterr().out
        assert "workers=4" in out
        assert len(gc.posts) == 2

    async def test_approvals_only_approves_selection(self, stub_cache) -> None:
        gc = FakeGC(
            approvals=[
                {
                    "id": "req-1",
                    "approvalId": "ap-1",
                    "group": {"id": "g9", "displayName": "Pending Group"},
                    "principal": {"userPrincipalName": "req@example.com"},
                }
            ]
        )
        args = make_args(approvals_only=True, group="Pending", justification="ok", parallel=1)
        rc = await cli._run_with_client(args, gc, None)  # type: ignore[arg-type]
        assert rc == 0
        assert gc.approved == [("ap-1", "ok")]

    async def test_picker_selection_drives_actions(
        self, stub_cache, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        gc = FakeGC(
            paged=[{"groupId": "g1", "accessId": "member", "group": {"displayName": "Group One"}}],
            batches=[{"0": {"value": []}}, {}],
            post_result={"id": "r1", "status": "Provisioned"},
        )

        def fake_checkbox(*a: Any, **k: Any):
            class _Q:
                def ask(self_inner):
                    return [("ACTIVATE", elig())]

            return _Q()

        monkeypatch.setattr(cli.questionary, "checkbox", fake_checkbox)
        rc = await cli._run_with_client(make_args(justification="because", parallel=1), gc, None)  # type: ignore[arg-type]
        assert rc == 0
        assert gc.posts

    async def test_empty_picker_selection_exits_zero(self, stub_cache, monkeypatch: pytest.MonkeyPatch) -> None:
        gc = FakeGC(
            paged=[{"groupId": "g1", "accessId": "member", "group": {"displayName": "Group One"}}],
            batches=[{"0": {"value": []}}, {}],
        )

        monkeypatch.setattr(cli.questionary, "checkbox", lambda *a, **k: type("Q", (), {"ask": lambda s: []})())
        rc = await cli._run_with_client(make_args(justification="because"), gc, None)  # type: ignore[arg-type]
        assert rc == 0
        assert gc.posts == []

    async def test_prompts_for_justification_when_missing(self, stub_cache, monkeypatch: pytest.MonkeyPatch) -> None:
        gc = FakeGC(
            paged=[{"groupId": "g1", "accessId": "member", "group": {"displayName": "db-x"}}],
            batches=[{"0": {"value": []}}, {}],
            post_result={"id": "r1", "status": "Provisioned"},
        )
        monkeypatch.setattr(cli.questionary, "text", lambda *a, **k: type("Q", (), {"ask": lambda s: "typed reason"})())
        rc = await cli._run_with_client(make_args(group="db-", parallel=1), gc, None)  # type: ignore[arg-type]
        assert rc == 0
        assert gc.posts[0][1]["justification"] == "typed reason"

    async def test_blank_justification_aborts(self, stub_cache, monkeypatch: pytest.MonkeyPatch) -> None:
        gc = FakeGC(
            paged=[{"groupId": "g1", "accessId": "member", "group": {"displayName": "db-x"}}],
            batches=[{"0": {"value": []}}, {}],
        )
        monkeypatch.setattr(cli.questionary, "text", lambda *a, **k: type("Q", (), {"ask": lambda s: ""})())
        rc = await cli._run_with_client(make_args(group="db-"), gc, None)  # type: ignore[arg-type]
        assert rc == 1
        assert gc.posts == []

    async def test_acrs_failure_primes_and_retries(
        self, stub_cache, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        posts: list[Any] = [
            GraphError(400, "RoleAssignmentRequestAcrsValidationFailed", "AcrsValidationFailed"),
            {"id": "r1", "status": "Provisioned"},
        ]

        class _RetryGC(FakeGC):
            async def post(self, path: str, body: dict) -> dict:
                self.posts.append((path, body))
                nxt = posts.pop(0)
                if isinstance(nxt, Exception):
                    raise nxt
                return nxt

        gc = _RetryGC(
            paged=[{"groupId": "g1", "accessId": "member", "group": {"displayName": "db-x"}}],
            batches=[{"0": {"value": []}}, {}],
        )

        import azure_pim_cli.acrs_primer as primer

        monkeypatch.setattr(primer, "prime_acrs", lambda *a, **k: "fresh-token")
        args = make_args(group="db-", justification="because", parallel=1)
        rc = await cli._run_with_client(args, gc, "http://localhost:9222")  # type: ignore[arg-type]

        assert rc == 0
        assert gc.token == "fresh-token", "client should be re-tokenized before the retry"
        assert len(gc.posts) == 2
        assert "Provisioned" in capsys.readouterr().out

    async def test_acrs_prime_failure_falls_back_to_manual(
        self, stub_cache, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        gc = FakeGC(
            paged=[{"groupId": "g1", "accessId": "member", "group": {"displayName": "db-x"}}],
            batches=[{"0": {"value": []}}, {}],
            post_result=GraphError(400, "Acrs", "AcrsValidationFailed"),
        )

        import azure_pim_cli.acrs_primer as primer

        def _boom(*a: Any, **k: Any) -> str:
            raise RuntimeError("no portal")

        monkeypatch.setattr(primer, "prime_acrs", _boom)
        args = make_args(group="db-", justification="because", parallel=1)
        rc = await cli._run_with_client(args, gc, "http://localhost:9222")  # type: ignore[arg-type]

        assert rc == 0
        out = capsys.readouterr().out
        assert "Auto-prime failed" in out
        assert "Falling back to manual prime" in out

    async def test_acrs_without_cdp_endpoint_lazily_launches_chrome(
        self, stub_cache, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--token/--no-auto-cdp leaves cdp_endpoint None; the prime path launches Chrome itself."""
        posts: list[Any] = [
            GraphError(400, "RoleAssignmentRequestAcrsValidationFailed", "AcrsValidationFailed"),
            {"id": "r1", "status": "Provisioned"},
        ]

        class _RetryGC(FakeGC):
            async def post(self, path: str, body: dict) -> dict:
                self.posts.append((path, body))
                nxt = posts.pop(0)
                if isinstance(nxt, Exception):
                    raise nxt
                return nxt

        gc = _RetryGC(
            paged=[{"groupId": "g1", "accessId": "member", "group": {"displayName": "db-x"}}],
            batches=[{"0": {"value": []}}, {}],
        )

        launched: dict[str, Any] = {}

        def _launch(**kwargs: Any) -> str:
            launched.update(kwargs)
            return "http://localhost:9222"

        import azure_pim_cli.acrs_primer as primer

        monkeypatch.setattr(cli, "launch_debug_chrome", _launch)
        monkeypatch.setattr(primer, "prime_acrs", lambda *a, **k: "fresh-token")

        args = make_args(group="db-", justification="because", parallel=1)
        rc = await cli._run_with_client(args, gc, None)  # type: ignore[arg-type]

        assert rc == 0
        assert launched["port"] == args.auto_cdp_port
        assert gc.token == "fresh-token"
        assert len(gc.posts) == 2

    async def test_acrs_lazy_chrome_launch_failure_goes_manual(
        self, stub_cache, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        gc = FakeGC(
            paged=[{"groupId": "g1", "accessId": "member", "group": {"displayName": "db-x"}}],
            batches=[{"0": {"value": []}}, {}],
            post_result=GraphError(400, "Acrs", "AcrsValidationFailed"),
        )

        def _no_chrome(**kwargs: Any) -> str:
            raise RuntimeError("chrome.exe not found")

        monkeypatch.setattr(cli, "launch_debug_chrome", _no_chrome)

        args = make_args(group="db-", justification="because", parallel=1)
        rc = await cli._run_with_client(args, gc, None)  # type: ignore[arg-type]

        assert rc == 0
        out = capsys.readouterr().out
        assert "Chrome launch for acrs prime failed" in out
        assert "Falling back to manual prime" in out

    async def test_eligibilities_only_skips_approval_feed(self, stub_cache, capsys) -> None:
        gc = FakeGC(
            paged=[{"groupId": "g1", "accessId": "member", "group": {"displayName": "Group One"}}],
            batches=[{"0": {"value": []}}, {}],
            approvals=RuntimeError("approvals must not be fetched"),
        )
        rc = await cli._run_with_client(make_args(list_only=True, eligibilities_only=True), gc, None)  # type: ignore[arg-type]
        assert rc == 0

    async def test_reports_new_eligibilities(
        self, stub_cache, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(cli.cache_mod, "mark_new", lambda new, prev: [{**e, "isNew": True} for e in new])
        gc = FakeGC(
            paged=[{"groupId": "g1", "accessId": "member", "group": {"displayName": "Group One"}}],
            batches=[{"0": {"value": []}}, {}],
        )
        rc = await cli._run_with_client(make_args(list_only=True), gc, None)  # type: ignore[arg-type]
        assert rc == 0
        assert "1 NEW eligible group(s)" in capsys.readouterr().out

    async def test_pending_only_run_cancels_unused_active_task(self, stub_cache) -> None:
        """No eligibilities means the active-ids task has nothing to filter; it must be drained."""
        gc = FakeGC(paged=[], approvals=[{"id": "req-1", "group": {"id": "g9", "displayName": "Pending Group"}}])
        rc = await cli._run_with_client(make_args(list_only=True), gc, None)  # type: ignore[arg-type]
        assert rc == 0


class TestRun:
    async def test_uses_supplied_token_and_skips_browser(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        class _CM:
            def __init__(self, token: str) -> None:
                seen["token"] = token

            async def __aenter__(self):
                return "gc"

            async def __aexit__(self, *a: Any) -> None:
                return None

        async def _fake_run_with_client(args, gc, cdp):
            seen["cdp"] = cdp
            return 0

        def _no_browser(*a: Any, **k: Any) -> str:
            raise AssertionError("grab_token must not be called when --token is supplied")

        monkeypatch.setattr(cli, "GraphClient", _CM)
        monkeypatch.setattr(cli, "_run_with_client", _fake_run_with_client)
        monkeypatch.setattr(cli, "grab_token", _no_browser)

        rc = await cli.run(make_args(token="tok-1", auto_cdp=False))
        assert rc == 0
        assert seen["token"] == "tok-1"

    async def test_auto_cdp_launches_chrome_then_grabs_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        class _CM:
            def __init__(self, token: str) -> None:
                seen["token"] = token

            async def __aenter__(self):
                return "gc"

            async def __aexit__(self, *a: Any) -> None:
                return None

        def _launch(**kwargs: Any) -> str:
            seen["launch"] = kwargs
            return "http://localhost:9222"

        def _grab(**kwargs: Any) -> str:
            seen["grab"] = kwargs
            return "tok-from-browser"

        async def _fake_run_with_client(args, gc, cdp):
            seen["cdp"] = cdp
            return 7

        monkeypatch.setattr(cli, "GraphClient", _CM)
        monkeypatch.setattr(cli, "launch_debug_chrome", _launch)
        monkeypatch.setattr(cli, "grab_token", _grab)
        monkeypatch.setattr(cli, "_run_with_client", _fake_run_with_client)

        rc = await cli.run(make_args(auto_cdp=True, list_only=True))
        assert rc == 7
        assert seen["cdp"] == "http://localhost:9222"
        assert seen["token"] == "tok-from-browser"
        assert seen["grab"]["cdp_endpoint"] == "http://localhost:9222"
        assert seen["grab"]["require_readwrite"] is False, "--list-only only needs read scope"


class TestMain:
    def _patch_argv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["pim-activate", "--list-only"])

    def test_returns_run_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_argv(monkeypatch)

        async def _run(args):
            return 0

        monkeypatch.setattr(cli, "run", _run)
        assert cli.main() == 0

    def test_token_expired_returns_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_argv(monkeypatch)

        async def _run(args):
            raise TokenExpired(401, "TokenExpired", "expired")

        monkeypatch.setattr(cli, "run", _run)
        assert cli.main() == 2

    def test_permission_denied_returns_3(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_argv(monkeypatch)

        async def _run(args):
            raise PermissionDenied(403, "Forbidden", "nope")

        monkeypatch.setattr(cli, "run", _run)
        assert cli.main() == 3

    def test_keyboard_interrupt_returns_130(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_argv(monkeypatch)

        async def _run(args):
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "run", _run)
        assert cli.main() == 130


class TestModuleEntrypoint:
    def test_main_module_delegates_to_cli_main(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import runpy

        monkeypatch.setattr(cli, "main", lambda: 0)
        with pytest.raises(SystemExit) as exc:
            runpy.run_module("azure_pim_cli.__main__", run_name="__main__")
        assert exc.value.code == 0


class TestAsyncioSanity:
    async def test_event_loop_available(self) -> None:
        assert asyncio.get_running_loop() is not None
