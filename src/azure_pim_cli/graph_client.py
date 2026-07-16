"""Async httpx wrapper around Microsoft Graph for PIM group ops. HTTP/2 enabled."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class GraphError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"[{status}] {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class TokenExpired(GraphError):
    pass


class PermissionDenied(GraphError):
    pass


class GraphClient:
    def __init__(self, token: str, *, _http2: bool = True):
        self.client = httpx.AsyncClient(
            http2=_http2,
            timeout=30,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=32),
        )

    def set_token(self, token: str) -> None:
        self.client.headers["Authorization"] = f"Bearer {token}"

    async def aclose(self) -> None:
        await self.client.aclose()

    async def __aenter__(self) -> GraphClient:
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()

    async def _request(self, method: str, path: str, json_body: Any = None) -> dict:
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        for _attempt in range(2):
            resp = await self.client.request(method, url, json=json_body)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "5"))
                await asyncio.sleep(retry_after)
                continue
            break

        if resp.status_code == 401:
            raise TokenExpired(401, "TokenExpired", "Bearer token rejected")

        if 200 <= resp.status_code < 300:
            if not resp.content:
                return {}
            return resp.json()

        try:
            err = resp.json().get("error", {})
            code = err.get("code", "Unknown")
            message = err.get("message", resp.text[:500])
        except Exception:
            code = "Unknown"
            message = resp.text[:500]

        if resp.status_code == 403:
            raise PermissionDenied(403, code, message)

        raise GraphError(resp.status_code, code, message)

    async def get(self, path: str) -> dict:
        return await self._request("GET", path)

    async def post(self, path: str, body: dict) -> dict:
        return await self._request("POST", path, json_body=body)

    async def get_paged(self, path: str) -> list[dict]:
        out: list[dict] = []
        page = await self.get(path)
        out.extend(page.get("value") or [])
        while page.get("@odata.nextLink"):
            page = await self.get(page["@odata.nextLink"])
            out.extend(page.get("value") or [])
        return out

    async def list_pim_group_active_assignments(self) -> list[dict]:
        """Portal's 'Actieve toewijzingen' (Active assignments) endpoint.

        Returns currently-assigned group memberships (both permanent 'Assigned'
        and activated 'Activated'). Any groupId returned here MUST NOT be
        re-activated — POST would fail with RoleAssignmentExists.

        Uses filterByCurrentUser(on='principal') so no admin scope is required.
        """
        path = (
            "/identityGovernance/privilegedAccess/group/"
            "assignmentScheduleInstances/filterByCurrentUser(on='principal')"
            "?$expand=group,principal,activatedUsing"
        )
        return await self.get_paged(path)

    async def list_pim_group_inflight_requests(self) -> list[dict]:
        """Requests still in-flight (pending approval, pending provisioning, etc.).

        Any groupId returned here MUST NOT be re-activated — POST would fail
        with PendingRoleAssignmentRequest.
        """
        from urllib.parse import quote

        statuses = (
            "Accepted",
            "PendingEvaluation",
            "Granted",
            "PendingProvisioning",
            "PendingApprovalProvisioning",
            "PendingApproval",
            "PendingAdminDecision",
            "PendingScheduleCreation",
            "PostApprovalExtensionPendingEvaluation",
        )
        flt = quote("(" + " or ".join(f"status eq '{s}'" for s in statuses) + ")")
        path = (
            "/identityGovernance/privilegedAccess/group/"
            f"assignmentScheduleRequests/filterByCurrentUser(on='principal')"
            f"?$filter={flt}&$expand=group,principal"
        )
        return await self.get_paged(path)

    async def list_pim_group_pending_approvals(self) -> list[dict]:
        """Portal's PIM Approvals blade uses Graph beta with filterByCurrentUser(on='approver').

        Returns only requests the current user is an approver for — server-side
        scoped, matches portal exactly.
        """
        path = (
            "https://graph.microsoft.com/beta/identityGovernance/privilegedAccess/group/"
            "assignmentScheduleRequests/filterByCurrentUser(on='approver')"
            "?$filter=status eq 'PendingApproval'&$expand=principal,group"
        )
        return await self.get_paged(path)

    async def get_pim_group_approval_steps(self, approval_id: str) -> list[dict]:
        """Fetch the approval object to discover step id(s) needed to submit a decision."""
        path = (
            "https://graph.microsoft.com/beta/identityGovernance/privilegedAccess/group/"
            f"assignmentApprovals/{approval_id}?$expand=steps"
        )
        obj = await self.get(path)
        return obj.get("steps") or []

    async def approve_pim_group_request(self, approval_id: str, justification: str) -> None:
        """Approve every InProgress / NotReviewed step on the given approval."""
        steps = await self.get_pim_group_approval_steps(approval_id)
        for step in steps:
            step_id = step.get("id")
            if not step_id:
                continue
            if step.get("reviewResult") not in (None, "NotReviewed"):
                continue
            path = (
                "https://graph.microsoft.com/beta/identityGovernance/privilegedAccess/group/"
                f"assignmentApprovals/{approval_id}/steps/{step_id}"
            )
            await self._request(
                "PATCH",
                path,
                json_body={
                    "reviewResult": "Approve",
                    "justification": justification,
                },
            )
