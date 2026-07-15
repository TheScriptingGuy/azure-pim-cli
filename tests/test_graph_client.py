from __future__ import annotations

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


@pytest.fixture()
def client() -> GraphClient:
    return GraphClient(token="test-token")


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
    @respx.mock
    async def test_success_returns_json(self, client: GraphClient) -> None:
        respx.get(f"{GRAPH_BASE}/me").mock(return_value=httpx.Response(200, json={"id": "u1"}))
        result = await client.get("/me")
        assert result["id"] == "u1"

    @respx.mock
    async def test_401_raises_token_expired(self, client: GraphClient) -> None:
        respx.get(f"{GRAPH_BASE}/me").mock(return_value=httpx.Response(401))
        with pytest.raises(TokenExpired):
            await client.get("/me")

    @respx.mock
    async def test_403_raises_permission_denied(self, client: GraphClient) -> None:
        body = {"error": {"code": "Forbidden", "message": "Access denied"}}
        respx.get(f"{GRAPH_BASE}/me").mock(return_value=httpx.Response(403, json=body))
        with pytest.raises(PermissionDenied) as exc_info:
            await client.get("/me")
        assert exc_info.value.status == 403

    @respx.mock
    async def test_500_raises_graph_error(self, client: GraphClient) -> None:
        body = {"error": {"code": "InternalError", "message": "server problem"}}
        respx.get(f"{GRAPH_BASE}/me").mock(return_value=httpx.Response(500, json=body))
        with pytest.raises(GraphError) as exc_info:
            await client.get("/me")
        assert exc_info.value.status == 500
        assert exc_info.value.code == "InternalError"

    @respx.mock
    async def test_empty_200_returns_empty_dict(self, client: GraphClient) -> None:
        respx.get(f"{GRAPH_BASE}/me").mock(return_value=httpx.Response(200, content=b""))
        result = await client.get("/me")
        assert result == {}

    @respx.mock
    async def test_absolute_url_passthrough(self, client: GraphClient) -> None:
        url = "https://graph.microsoft.com/beta/something"
        respx.get(url).mock(return_value=httpx.Response(200, json={"ok": True}))
        result = await client.get(url)
        assert result["ok"] is True

    @respx.mock
    async def test_429_retries_once(self, client: GraphClient) -> None:
        respx.get(f"{GRAPH_BASE}/me").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(200, json={"id": "u1"}),
            ]
        )
        result = await client.get("/me")
        assert result["id"] == "u1"


class TestGraphClientGetPaged:
    @respx.mock
    async def test_single_page(self, client: GraphClient) -> None:
        respx.get(f"{GRAPH_BASE}/items").mock(
            return_value=httpx.Response(200, json={"value": [{"id": "1"}, {"id": "2"}]})
        )
        result = await client.get_paged("/items")
        assert len(result) == 2

    @respx.mock
    async def test_follows_next_link(self, client: GraphClient) -> None:
        page1_url = f"{GRAPH_BASE}/items"
        page2_url = f"{GRAPH_BASE}/items?$skiptoken=abc"
        respx.get(page1_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [{"id": "1"}],
                    "@odata.nextLink": page2_url,
                },
            )
        )
        respx.get(page2_url).mock(return_value=httpx.Response(200, json={"value": [{"id": "2"}]}))
        result = await client.get_paged("/items")
        assert [r["id"] for r in result] == ["1", "2"]


class TestSetToken:
    async def test_set_token_updates_header(self, client: GraphClient) -> None:
        client.set_token("new-token")
        assert client.client.headers["Authorization"] == "Bearer new-token"
