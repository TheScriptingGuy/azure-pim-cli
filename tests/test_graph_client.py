from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from urllib.parse import unquote

import httpx
import pytest
import respx

from azure_pim_cli.graph_client import (
    GRAPH_BASE,
    GraphClient,
    GraphError,
    PermissionDenied,
    TokenExpired,
)

BETA_APPROVALS = "https://graph.microsoft.com/beta/identityGovernance/privilegedAccess/group/assignmentApprovals"


@pytest.fixture()
async def mock_router() -> AsyncGenerator[respx.MockRouter, None]:
    async with respx.MockRouter(assert_all_mocked=True, assert_all_called=False) as router:
        yield router


@pytest.fixture()
async def client(mock_router: respx.MockRouter) -> AsyncGenerator[GraphClient, None]:
    c = GraphClient(token="test-token", _http2=False)
    try:
        yield c
    finally:
        await c.aclose()


class TestGraphError:
    def test_str_representation(self) -> None:
        e = GraphError(400, "BadRequest", "something went wrong")
        assert "[400]" in str(e)
        assert "BadRequest" in str(e)
        assert "something went wrong" in str(e)

    def test_attributes(self) -> None:
        e = GraphError(403, "Forbidden", "no access")
        assert e.status == 403
        assert e.code == "Forbidden"
        assert e.message == "no access"


class TestTokenExpired:
    def test_is_graph_error(self) -> None:
        e = TokenExpired(401, "TokenExpired", "rejected")
        assert isinstance(e, GraphError)
        assert e.status == 401


class TestPermissionDenied:
    def test_is_graph_error(self) -> None:
        e = PermissionDenied(403, "Forbidden", "denied")
        assert isinstance(e, GraphError)
        assert e.status == 403


class TestGraphClientGet:
    async def test_success_returns_json(self, client: GraphClient, mock_router: respx.MockRouter) -> None:
        mock_router.get(f"{GRAPH_BASE}/me").mock(return_value=httpx.Response(200, json={"id": "u1"}))
        result = await client.get("/me")
        assert result["id"] == "u1"

    async def test_401_raises_token_expired(self, client: GraphClient, mock_router: respx.MockRouter) -> None:
        mock_router.get(f"{GRAPH_BASE}/me").mock(return_value=httpx.Response(401))
        with pytest.raises(TokenExpired):
            await client.get("/me")

    async def test_403_raises_permission_denied(self, client: GraphClient, mock_router: respx.MockRouter) -> None:
        body = {"error": {"code": "Forbidden", "message": "Access denied"}}
        mock_router.get(f"{GRAPH_BASE}/me").mock(return_value=httpx.Response(403, json=body))
        with pytest.raises(PermissionDenied) as exc_info:
            await client.get("/me")
        assert exc_info.value.status == 403

    async def test_500_raises_graph_error(self, client: GraphClient, mock_router: respx.MockRouter) -> None:
        body = {"error": {"code": "InternalError", "message": "server problem"}}
        mock_router.get(f"{GRAPH_BASE}/me").mock(return_value=httpx.Response(500, json=body))
        with pytest.raises(GraphError) as exc_info:
            await client.get("/me")
        assert exc_info.value.status == 500
        assert exc_info.value.code == "InternalError"

    async def test_empty_200_returns_empty_dict(self, client: GraphClient, mock_router: respx.MockRouter) -> None:
        mock_router.get(f"{GRAPH_BASE}/me").mock(return_value=httpx.Response(200, content=b""))
        result = await client.get("/me")
        assert result == {}

    async def test_absolute_url_passthrough(self, client: GraphClient, mock_router: respx.MockRouter) -> None:
        url = "https://graph.microsoft.com/beta/something"
        mock_router.get(url).mock(return_value=httpx.Response(200, json={"ok": True}))
        result = await client.get(url)
        assert result["ok"] is True

    async def test_429_retries_once(self, client: GraphClient, mock_router: respx.MockRouter) -> None:
        mock_router.get(f"{GRAPH_BASE}/me").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(200, json={"id": "u1"}),
            ]
        )
        result = await client.get("/me")
        assert result["id"] == "u1"


class TestGraphClientGetPaged:
    async def test_single_page(self, client: GraphClient, mock_router: respx.MockRouter) -> None:
        mock_router.get(f"{GRAPH_BASE}/items").mock(
            return_value=httpx.Response(200, json={"value": [{"id": "1"}, {"id": "2"}]})
        )
        result = await client.get_paged("/items")
        assert len(result) == 2

    async def test_follows_next_link(self, client: GraphClient, mock_router: respx.MockRouter) -> None:
        page1_url = f"{GRAPH_BASE}/items"
        page2_url = f"{GRAPH_BASE}/items?skiptoken=abc"
        # url__eq pins the first route to the query-less URL. A plain get(page1_url)
        # leaves the query string unconstrained, so it also matches the page-2
        # request and replays page 1 — nextLink and all — into an endless loop.
        mock_router.get(url__eq=page1_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [{"id": "1"}],
                    "@odata.nextLink": page2_url,
                },
            )
        )
        mock_router.get(page2_url).mock(return_value=httpx.Response(200, json={"value": [{"id": "2"}]}))
        result = await client.get_paged("/items")
        assert [r["id"] for r in result] == ["1", "2"]


class TestSetToken:
    async def test_set_token_updates_header(self, client: GraphClient) -> None:
        client.set_token("new-token")
        assert client.client.headers["Authorization"] == "Bearer new-token"


class TestAsyncContextManager:
    async def test_closes_underlying_client_on_exit(self, mock_router: respx.MockRouter) -> None:
        gc = GraphClient(token="t", _http2=False)
        async with gc as entered:
            assert entered is gc
            assert gc.client.is_closed is False
        assert gc.client.is_closed is True


class TestGraphClientErrorBody:
    async def test_non_json_error_body_falls_back_to_text(
        self, client: GraphClient, mock_router: respx.MockRouter
    ) -> None:
        mock_router.get(f"{GRAPH_BASE}/me").mock(return_value=httpx.Response(500, text="<html>boom</html>"))
        with pytest.raises(GraphError) as exc:
            await client.get("/me")
        assert exc.value.code == "Unknown"
        assert "boom" in exc.value.message

    async def test_long_error_text_is_truncated(self, client: GraphClient, mock_router: respx.MockRouter) -> None:
        mock_router.get(f"{GRAPH_BASE}/me").mock(return_value=httpx.Response(500, text="x" * 900))
        with pytest.raises(GraphError) as exc:
            await client.get("/me")
        assert len(exc.value.message) == 500


class TestGraphClientPost:
    async def test_post_sends_body_and_returns_json(self, client: GraphClient, mock_router: respx.MockRouter) -> None:
        route = mock_router.post(f"{GRAPH_BASE}/things").mock(return_value=httpx.Response(201, json={"id": "new"}))
        result = await client.post("/things", {"name": "x"})
        assert result == {"id": "new"}
        assert json.loads(route.calls[0].request.content) == {"name": "x"}


def _echo_batch(request: httpx.Request) -> httpx.Response:
    """Reflect every sub-request back as a 200 whose body carries its id."""
    payload = json.loads(request.content)
    return httpx.Response(
        200,
        json={"responses": [{"id": r["id"], "status": 200, "body": {"v": r["id"]}} for r in payload["requests"]]},
    )


class TestGraphClientBatch:
    async def test_empty_requests_short_circuits(self, client: GraphClient, mock_router: respx.MockRouter) -> None:
        route = mock_router.post(f"{GRAPH_BASE}/$batch")
        assert await client.batch([]) == {}
        assert route.call_count == 0

    async def test_maps_ids_to_bodies(self, client: GraphClient, mock_router: respx.MockRouter) -> None:
        mock_router.post(f"{GRAPH_BASE}/$batch").mock(side_effect=_echo_batch)
        result = await client.batch(
            [
                {"id": "a", "method": "GET", "url": "/one"},
                {"id": "b", "method": "GET", "url": "/two"},
            ]
        )
        assert result == {"a": {"v": "a"}, "b": {"v": "b"}}

    async def test_drops_none_valued_keys_from_sub_requests(
        self, client: GraphClient, mock_router: respx.MockRouter
    ) -> None:
        route = mock_router.post(f"{GRAPH_BASE}/$batch").mock(side_effect=_echo_batch)
        await client.batch([{"id": "a", "method": "GET", "url": "/one", "body": None}])
        sent = json.loads(route.calls[0].request.content)["requests"][0]
        assert "body" not in sent

    async def test_non_2xx_sub_response_becomes_none(self, client: GraphClient, mock_router: respx.MockRouter) -> None:
        mock_router.post(f"{GRAPH_BASE}/$batch").mock(
            return_value=httpx.Response(
                200,
                json={
                    "responses": [
                        {"id": "ok", "status": 200, "body": {"v": 1}},
                        {"id": "bad", "status": 404, "body": {"error": {}}},
                    ]
                },
            )
        )
        result = await client.batch(
            [
                {"id": "ok", "method": "GET", "url": "/one"},
                {"id": "bad", "method": "GET", "url": "/two"},
            ]
        )
        assert result == {"ok": {"v": 1}, "bad": None}

    async def test_2xx_without_body_becomes_empty_dict(
        self, client: GraphClient, mock_router: respx.MockRouter
    ) -> None:
        mock_router.post(f"{GRAPH_BASE}/$batch").mock(
            return_value=httpx.Response(200, json={"responses": [{"id": "a", "status": 204}]})
        )
        assert await client.batch([{"id": "a", "method": "GET", "url": "/one"}]) == {"a": {}}

    async def test_splits_requests_into_chunks_of_20(self, client: GraphClient, mock_router: respx.MockRouter) -> None:
        route = mock_router.post(f"{GRAPH_BASE}/$batch").mock(side_effect=_echo_batch)
        reqs = [{"id": str(i), "method": "GET", "url": f"/item/{i}"} for i in range(25)]
        result = await client.batch(reqs)
        assert route.call_count == 2
        sizes = sorted(len(json.loads(c.request.content)["requests"]) for c in route.calls)
        assert sizes == [5, 20]
        assert len(result) == 25

    async def test_retries_429_sub_responses_once(self, client: GraphClient, mock_router: respx.MockRouter) -> None:
        sent_ids: list[list[str]] = []

        def _throttle_first(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            sent_ids.append([r["id"] for r in payload["requests"]])
            if len(sent_ids) == 1:
                return httpx.Response(
                    200,
                    json={
                        "responses": [
                            {"id": "0", "status": 429, "headers": {"Retry-After": "0"}},
                            {"id": "1", "status": 200, "body": {"v": 1}},
                        ]
                    },
                )
            return httpx.Response(200, json={"responses": [{"id": "0", "status": 200, "body": {"v": 0}}]})

        mock_router.post(f"{GRAPH_BASE}/$batch").mock(side_effect=_throttle_first)
        result = await client.batch(
            [
                {"id": "0", "method": "GET", "url": "/zero"},
                {"id": "1", "method": "GET", "url": "/one"},
            ]
        )
        assert result == {"0": {"v": 0}, "1": {"v": 1}}
        assert sent_ids[1] == ["0"], "only the throttled sub-request should be retried"

    async def test_failed_retry_becomes_none(self, client: GraphClient, mock_router: respx.MockRouter) -> None:
        responses = [
            httpx.Response(200, json={"responses": [{"id": "0", "status": 429, "headers": {"Retry-After": "0"}}]}),
            httpx.Response(200, json={"responses": [{"id": "0", "status": 503, "body": {}}]}),
        ]
        mock_router.post(f"{GRAPH_BASE}/$batch").mock(side_effect=responses)
        assert await client.batch([{"id": "0", "method": "GET", "url": "/zero"}]) == {"0": None}

    async def test_retry_sub_response_without_id_is_skipped(
        self, client: GraphClient, mock_router: respx.MockRouter
    ) -> None:
        responses = [
            httpx.Response(200, json={"responses": [{"id": "0", "status": 429, "headers": {"Retry-After": "0"}}]}),
            httpx.Response(200, json={"responses": [{"status": 200, "body": {"orphan": True}}]}),
        ]
        mock_router.post(f"{GRAPH_BASE}/$batch").mock(side_effect=responses)
        assert await client.batch([{"id": "0", "method": "GET", "url": "/zero"}]) == {}

    async def test_sub_response_without_id_is_skipped(self, client: GraphClient, mock_router: respx.MockRouter) -> None:
        mock_router.post(f"{GRAPH_BASE}/$batch").mock(
            return_value=httpx.Response(
                200,
                json={
                    "responses": [
                        {"status": 200, "body": {"orphan": True}},
                        {"id": "1", "status": 200, "body": {"v": 1}},
                    ]
                },
            )
        )
        result = await client.batch([{"id": "1", "method": "GET", "url": "/one"}])
        assert result == {"1": {"v": 1}}, "an id-less sub-response must not be filed under a None key"

    async def test_throttled_sub_response_without_id_is_not_retried(
        self, client: GraphClient, mock_router: respx.MockRouter
    ) -> None:
        route = mock_router.post(f"{GRAPH_BASE}/$batch").mock(
            return_value=httpx.Response(
                200,
                json={"responses": [{"status": 429, "headers": {"Retry-After": "0"}}]},
            )
        )
        assert await client.batch([{"id": "0", "method": "GET", "url": "/zero"}]) == {}
        assert route.call_count == 1, "there is no id to retry, so no second $batch should be sent"


class TestPimGroupListEndpoints:
    async def test_active_assignments_expands_related_objects(
        self, client: GraphClient, mock_router: respx.MockRouter
    ) -> None:
        route = mock_router.get(
            url__startswith=f"{GRAPH_BASE}/identityGovernance/privilegedAccess/group/assignmentScheduleInstances"
        ).mock(return_value=httpx.Response(200, json={"value": [{"id": "a1"}]}))
        assert await client.list_pim_group_active_assignments() == [{"id": "a1"}]
        url = str(route.calls[0].request.url)
        assert "filterByCurrentUser" in url
        assert "principal" in url

    async def test_inflight_requests_filter_covers_pending_statuses(
        self, client: GraphClient, mock_router: respx.MockRouter
    ) -> None:
        route = mock_router.get(
            url__startswith=f"{GRAPH_BASE}/identityGovernance/privilegedAccess/group/assignmentScheduleRequests"
        ).mock(return_value=httpx.Response(200, json={"value": [{"id": "r1"}]}))
        assert await client.list_pim_group_inflight_requests() == [{"id": "r1"}]
        url = unquote(str(route.calls[0].request.url))
        for status in ("PendingApproval", "Granted", "PendingAdminDecision"):
            assert f"status eq '{status}'" in url

    async def test_pending_approvals_uses_beta_approver_scope(
        self, client: GraphClient, mock_router: respx.MockRouter
    ) -> None:
        route = mock_router.get(
            url__startswith="https://graph.microsoft.com/beta/identityGovernance/privilegedAccess/group/assignmentScheduleRequests"
        ).mock(return_value=httpx.Response(200, json={"value": [{"id": "p1"}]}))
        assert await client.list_pim_group_pending_approvals() == [{"id": "p1"}]
        url = unquote(str(route.calls[0].request.url))
        assert "filterByCurrentUser(on='approver')" in url
        assert "status eq 'PendingApproval'" in url


class TestApprovalSteps:
    async def test_returns_steps(self, client: GraphClient, mock_router: respx.MockRouter) -> None:
        mock_router.get(url__startswith=f"{BETA_APPROVALS}/ap-1").mock(
            return_value=httpx.Response(200, json={"steps": [{"id": "s1"}]})
        )
        assert await client.get_pim_group_approval_steps("ap-1") == [{"id": "s1"}]

    async def test_missing_steps_returns_empty_list(self, client: GraphClient, mock_router: respx.MockRouter) -> None:
        mock_router.get(url__startswith=f"{BETA_APPROVALS}/ap-1").mock(return_value=httpx.Response(200, json={}))
        assert await client.get_pim_group_approval_steps("ap-1") == []


class TestApprovePimGroupRequest:
    async def test_patches_only_unreviewed_steps_with_an_id(
        self, client: GraphClient, mock_router: respx.MockRouter
    ) -> None:
        mock_router.get(url__startswith=f"{BETA_APPROVALS}/ap-1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "steps": [
                        {"id": "s1", "reviewResult": "NotReviewed"},
                        {"id": "s2", "reviewResult": "Approve"},
                        {"reviewResult": "NotReviewed"},
                        {"id": "s4"},
                    ]
                },
            )
        )
        patch_route = mock_router.patch(url__startswith=f"{BETA_APPROVALS}/ap-1/steps/").mock(
            return_value=httpx.Response(204)
        )

        await client.approve_pim_group_request("ap-1", "because")

        patched = [str(c.request.url).rsplit("/", 1)[-1] for c in patch_route.calls]
        assert patched == ["s1", "s4"]
        assert json.loads(patch_route.calls[0].request.content) == {
            "reviewResult": "Approve",
            "justification": "because",
        }

    async def test_no_steps_sends_no_patch(self, client: GraphClient, mock_router: respx.MockRouter) -> None:
        mock_router.get(url__startswith=f"{BETA_APPROVALS}/ap-1").mock(
            return_value=httpx.Response(200, json={"steps": []})
        )
        patch_route = mock_router.patch(url__startswith=f"{BETA_APPROVALS}/ap-1/steps/")
        await client.approve_pim_group_request("ap-1", "because")
        assert patch_route.call_count == 0
