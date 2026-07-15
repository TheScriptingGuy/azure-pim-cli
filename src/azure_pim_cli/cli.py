"""PIM group activation + approval CLI.

Fetches my eligible PIM group assignments and PIM group requests I can approve,
merges them into a single interactive picker, then activates/approves the selection.

Auth: Playwright grabs a bearer token from an authenticated Azure Portal session.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from datetime import UTC, datetime, timedelta

import questionary
from rich.console import Console
from rich.table import Table

from . import cache as cache_mod
from .chrome_launcher import DEFAULT_COPY_PROFILE, DEFAULT_PORT, launch_debug_chrome
from .graph_client import GraphClient, GraphError, PermissionDenied, TokenExpired
from .token_grabber import DEFAULT_CHANNEL, grab_token

console = Console(width=500, soft_wrap=True)

POLL_TIMEOUT = 180
POLL_INTERVAL = 5
TERMINAL_STATES = {"Provisioned", "Failed", "Canceled", "Denied"}
# Requests needing human approval settle here immediately — don't poll further.
AWAITING_APPROVAL_STATES = {
    "PendingApproval",
    "PendingApprovalProvisioning",
    "PendingAdminDecision",
}


# ------------- Fetchers ---------------------------------------------------------


async def fetch_me(gc: GraphClient) -> dict:
    return await gc.get("/me?$select=id,displayName,userPrincipalName")


async def _enrich_eligibility(gc: GraphClient, entry: dict, sem: asyncio.Semaphore) -> dict:
    from urllib.parse import quote

    gid = entry["groupId"]
    access_id = entry["accessId"]

    display_name = gid
    description = ""
    policy_max_h = 8
    req_j = True
    req_t = False
    req_mfa = False

    pflt = quote(f"scopeId eq '{gid}' and scopeType eq 'Group' and roleDefinitionId eq '{access_id}'")

    async with sem:
        grp_task = asyncio.create_task(gc.get(f"/groups/{gid}?$select=id,displayName,description"))
        assigns_task = asyncio.create_task(gc.get_paged(f"/policies/roleManagementPolicyAssignments?$filter={pflt}"))

        try:
            grp = await grp_task
            display_name = grp.get("displayName") or gid
            description = grp.get("description") or ""
        except GraphError:
            pass

        try:
            assigns = await assigns_task
            if assigns:
                policy_id = assigns[0]["policyId"]
                rules = await gc.get_paged(f"/policies/roleManagementPolicies/{policy_id}/rules")
                for r in rules:
                    rid = r.get("id", "")
                    if rid == "Expiration_EndUser_Assignment":
                        dur = r.get("maximumDuration")
                        if dur:
                            policy_max_h = _iso8601_hours(dur) or policy_max_h
                    elif rid == "Enablement_EndUser_Assignment":
                        enabled = r.get("enabledRules") or []
                        req_j = "Justification" in enabled
                        req_t = "Ticketing" in enabled
                        req_mfa = "MultiFactorAuthentication" in enabled
        except GraphError:
            pass

    end_dt = "Permanent"
    sched = entry.get("scheduleInfo") or {}
    exp = sched.get("expiration") or {}
    if exp.get("endDateTime"):
        end_dt = exp["endDateTime"]

    return {
        "groupId": gid,
        "displayName": display_name,
        "description": description,
        "accessId": access_id,
        "endDateTime": end_dt,
        "policyMaxDurationHours": policy_max_h,
        "requiresJustification": req_j,
        "requiresTicket": req_t,
        "requiresMfa": req_mfa,
    }


async def fetch_eligibilities(gc: GraphClient, principal_id: str, fetch_workers: int) -> list[dict]:
    from urllib.parse import quote

    flt = quote(f"principalId eq '{principal_id}'")
    raw = await gc.get_paged(f"/identityGovernance/privilegedAccess/group/eligibilityScheduleInstances?$filter={flt}")
    console.print(f"[dim]Found {len(raw)} eligible assignment(s).[/dim]")

    sem = asyncio.Semaphore(fetch_workers)
    return list(await asyncio.gather(*[_enrich_eligibility(gc, e, sem) for e in raw]))


async def fetch_active_group_ids(gc: GraphClient) -> set[str]:
    """Return groupIds where I have an active assignment OR an in-flight request.

    Portal-parity: matches 'Actieve toewijzingen' tab (assignmentScheduleInstances)
    combined with any in-flight assignmentScheduleRequests. Skipping these
    pre-activation avoids RoleAssignmentExists / PendingRoleAssignmentRequest.
    Surfaces Graph errors instead of silently returning empty.
    """
    active_res, inflight_res = await asyncio.gather(
        gc.list_pim_group_active_assignments(),
        gc.list_pim_group_inflight_requests(),
        return_exceptions=True,
    )

    out: set[str] = set()
    if isinstance(active_res, GraphError):
        console.print(f"[yellow]Could not list active assignments: {active_res}.[/yellow]")
        active_res = []
    elif isinstance(active_res, BaseException):
        raise active_res
    for r in active_res:
        gid = (r.get("group") or {}).get("id") or r.get("groupId")
        if gid:
            out.add(gid)

    if isinstance(inflight_res, GraphError):
        console.print(f"[yellow]Could not list in-flight requests: {inflight_res}.[/yellow]")
        inflight_res = []
    elif isinstance(inflight_res, BaseException):
        raise inflight_res
    for r in inflight_res:
        gid = (r.get("group") or {}).get("id") or r.get("groupId")
        if gid:
            out.add(gid)
    return out


async def fetch_pending_approvals(gc: GraphClient) -> list[dict]:
    """Requests awaiting my approval — Graph beta filterByCurrentUser(on='approver').

    Portal-parity: server-side scoped, only requests current user can act on.
    """
    try:
        raw = await gc.list_pim_group_pending_approvals()
    except GraphError as e:
        console.print(f"[yellow]Could not fetch pending approvals: {e}[/yellow]")
        return []

    out: list[dict] = []
    for r in raw:
        principal = r.get("principal") or {}
        group = r.get("group") or {}
        sched = r.get("scheduleInfo") or {}
        exp = sched.get("expiration") or {}
        out.append(
            {
                "requestId": r.get("id") or "",
                "approvalId": r.get("approvalId") or r.get("id") or "",
                "groupId": group.get("id") or r.get("groupId") or "",
                "displayName": group.get("displayName") or "?",
                "accessId": r.get("accessId") or "member",
                "requester": principal.get("userPrincipalName") or principal.get("displayName") or "?",
                "justification": r.get("justification") or "",
                "duration": exp.get("duration") or "",
            }
        )
    return out


def _iso8601_hours(dur: str) -> int | None:
    m = re.match(r"^PT(\d+)([HM])$", dur)
    if not m:
        return None
    n = int(m.group(1))
    return n if m.group(2) == "H" else max(1, n // 60)


# ------------- Actions ----------------------------------------------------------


async def activate(
    gc: GraphClient,
    principal_id: str,
    item: dict,
    justification: str,
    hours: int,
    ticket: str | None,
) -> tuple[str, str]:
    eff_hours = min(hours, int(item["policyMaxDurationHours"] or hours))
    body = {
        "accessId": item["accessId"],
        "principalId": principal_id,
        "groupId": item["groupId"],
        "action": "selfActivate",
        "scheduleInfo": {
            "startDateTime": datetime.now(UTC).isoformat(),
            "expiration": {"type": "afterDuration", "duration": f"PT{eff_hours}H"},
        },
        "justification": justification,
    }
    if item.get("requiresTicket") and ticket:
        body["ticketInfo"] = {"ticketNumber": ticket, "ticketSystem": "Provided"}

    try:
        resp = await gc.post("/identityGovernance/privilegedAccess/group/assignmentScheduleRequests", body)
    except GraphError as e:
        emsg = str(e)
        if re.search(
            r"RoleAssignmentExists|DuplicateRoleCreated|ActiveDurationTooShort|"
            r"MatchingRoleAssignmentExists|SubjectHasActiveAssignment",
            emsg,
        ):
            return "AlreadyActive", ""
        if re.search(r"MfaRequired|StrongAuthenticationRequired", emsg):
            return "MfaRequired", ""
        if "PendingApproval" in emsg:
            return "PendingApproval", ""
        if "PendingRoleAssignmentRequest" in emsg:
            return "PendingRequest", "prior POST still in flight server-side"
        sys.stderr.write(f"[activate {item['displayName']}] {emsg}\n")
        return "Failed", emsg

    req_id = resp.get("id")
    status = resp.get("status", "Unknown")

    if req_id and status not in TERMINAL_STATES and status not in AWAITING_APPROVAL_STATES:
        deadline = time.time() + POLL_TIMEOUT
        while time.time() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                poll = await gc.get(f"/identityGovernance/privilegedAccess/group/assignmentScheduleRequests/{req_id}")
                status = poll.get("status", status)
            except GraphError:
                break
            remaining = int(deadline - time.time())
            sys.stderr.write(f"[activate] {item['displayName']}: status={status} ({remaining}s left)\n")
            if status in TERMINAL_STATES or status in AWAITING_APPROVAL_STATES:
                break
        else:
            status = "Timeout"

    if status in AWAITING_APPROVAL_STATES:
        return "AwaitingApproval", f"reqId={req_id}"
    if status == "Provisioned":
        expires = (datetime.now() + timedelta(hours=eff_hours)).strftime("%Y-%m-%d %H:%M")
        return status, expires
    if status == "Timeout":
        return status, f"reqId={req_id}"
    return status, ""


async def approve(gc: GraphClient, item: dict, justification: str) -> tuple[str, str]:
    """Approve a pending PIM group request via Graph beta assignmentApprovals steps."""
    try:
        await gc.approve_pim_group_request(item.get("approvalId") or item["requestId"], justification)
        return "Approved", ""
    except GraphError as e:
        return "Failed", str(e)


# ------------- Picker -----------------------------------------------------------


def build_choices(eligible: list[dict], pending: list[dict]) -> list[questionary.Choice]:
    choices: list[questionary.Choice] = []

    # Pending approvals first, alpha by group displayName.
    for p in sorted(pending, key=lambda x: (x["displayName"] or "").lower()):
        title = f"APPROVE   {p['displayName']:<40s} <- {p['requester']}   {p['duration']}"
        choices.append(questionary.Choice(title=title, value=("APPROVE", p)))

    # Then eligibles, alpha by group displayName (NEW flag still shown in title).
    for e in sorted(eligible, key=lambda x: (x["displayName"] or "").lower()):
        title = _elig_title(e, mark_new=bool(e.get("isNew")))
        choices.append(questionary.Choice(title=title, value=("ACTIVATE", e)))

    return choices


def _elig_title(e: dict, mark_new: bool) -> str:
    flag = "*NEW*" if mark_new else "     "
    return (
        f"ACTIVATE {flag} {e['displayName']:<40s} ({e['accessId']:<6s})  "
        f"max {e['policyMaxDurationHours']}h  MFA={e['requiresMfa']}"
    )


# ------------- Orchestrator -----------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    # Auth
    # Activation POST needs ReadWrite scope; --list-only can use READ.
    need_rw = not args.list_only

    # --auto-cdp: kill Chrome, copy real profile (once), launch Chrome with debug port,
    # then attach via CDP. Fixes CA "U kunt op dit moment geen toegang krijgen" when
    # Playwright's own profile lacks Intune compliance cookies.
    cdp_endpoint = args.cdp_endpoint
    if args.auto_cdp and not args.token:
        from pathlib import Path as _Path

        cdp_endpoint = await asyncio.to_thread(
            launch_debug_chrome,
            port=args.auto_cdp_port,
            copy_profile=_Path(args.auto_cdp_profile),
            force_profile_refresh=args.refresh_chrome_profile,
        )

    if args.token:
        token = args.token
    else:
        token = await asyncio.to_thread(
            grab_token,
            headless=args.headless,
            keep_open=args.keep_open,
            channel=(args.channel or None),
            require_readwrite=need_rw,
            cdp_endpoint=cdp_endpoint,
        )

    async with GraphClient(token) as gc:
        return await _run_with_client(args, gc, cdp_endpoint)


async def _run_with_client(args: argparse.Namespace, gc: GraphClient, cdp_endpoint: str | None) -> int:
    me = await fetch_me(gc)
    principal_id = me["id"]
    console.print(f"[dim]User: {me.get('userPrincipalName')} ({principal_id})[/dim]")

    # Cache setup — decides whether eligibility fetch is needed at all.
    cached = cache_mod.load()
    previous: list[dict] | None = cached["eligible"] if (cached and cached.get("eligible")) else None
    use_cache = not args.approvals_only and cached and not args.refresh and cache_mod.is_fresh(cached, principal_id)

    # Fan out top-level fetches: pending, eligibilities, active_ids — all concurrent.
    pending_task: asyncio.Task | None = None
    elig_task: asyncio.Task | None = None
    active_task: asyncio.Task | None = None

    if not args.eligibilities_only:
        console.print("[cyan]Fetching pending PIM group approvals (Graph)...[/cyan]")
        pending_task = asyncio.create_task(fetch_pending_approvals(gc))

    if not args.approvals_only and not use_cache:
        console.print("[cyan]Fetching eligible PIM group assignments...[/cyan]")
        elig_task = asyncio.create_task(fetch_eligibilities(gc, principal_id, args.fetch_workers))

    # Active-ids fetch only matters if we have eligibilities to filter.
    if not args.approvals_only:
        console.print("[cyan]Fetching my active/in-flight PIM group requests...[/cyan]")
        active_task = asyncio.create_task(fetch_active_group_ids(gc))

    pending = await pending_task if pending_task else []
    if pending:
        console.print(f"[yellow]>> {len(pending)} pending approval(s) awaiting your decision.[/yellow]")
    elif not args.eligibilities_only:
        console.print("[dim]No pending approvals for you.[/dim]")

    eligible: list[dict] = []
    if not args.approvals_only:
        if use_cache:
            console.print("[dim]Using cached eligible list. -Refresh to update.[/dim]")
            eligible = cached["eligible"]
        else:
            eligible = await elig_task  # type: ignore[assignment]
            cache_mod.save(principal_id, eligible)
        eligible = cache_mod.mark_new(eligible, previous)
        new_count = sum(1 for e in eligible if e.get("isNew"))
        if new_count:
            console.print(f"[yellow]>> {new_count} NEW eligible group(s) since last fetch![/yellow]")

    # Skip groups already active or with in-flight requests (portal-parity).
    # Avoids RoleAssignmentExists / PendingRoleAssignmentRequest on reruns.
    if eligible and active_task:
        active_ids = await active_task
        if active_ids:
            skipped = [e["displayName"] for e in eligible if e["groupId"] in active_ids]
            eligible = [e for e in eligible if e["groupId"] not in active_ids]
            if skipped:
                console.print(
                    f"[dim]Skipping {len(skipped)} already-active/in-flight group(s): "
                    f"{', '.join(sorted(skipped))}[/dim]"
                )
    elif active_task and not active_task.done():
        # Nothing to filter, but drain the task so it doesn't leak.
        active_task.cancel()
        try:
            await active_task
        except (asyncio.CancelledError, GraphError):
            pass

    # --group regex filter
    if args.group:
        rx = re.compile(args.group, re.I)
        eligible = [e for e in eligible if rx.search(e["displayName"])]
        pending = [p for p in pending if rx.search(p["displayName"])]

    if args.list_only:
        _print_list(eligible, pending)
        return 0

    if not eligible and not pending:
        console.print("[red]Nothing to activate or approve.[/red]")
        return 1

    # Non-interactive: --group with no picker
    selected: list[tuple[str, dict]] = []
    if args.group:
        selected = [("ACTIVATE", e) for e in eligible] + [("APPROVE", p) for p in pending]
        console.print(f"[cyan]Matched {len(selected)} item(s) via --group filter.[/cyan]")
    else:
        choices = build_choices(eligible, pending)
        if not choices:
            console.print("[red]No items to pick.[/red]")
            return 1
        picks = await asyncio.to_thread(
            lambda: questionary.checkbox(
                "Select PIM items to activate/approve:",
                choices=choices,
            ).ask()
        )
        if not picks:
            console.print("No selection. Exiting.")
            return 0
        selected = picks

    justification = args.justification
    if not justification:
        justification = await asyncio.to_thread(lambda: questionary.text("Justification (required):").ask())
        if not justification:
            console.print("[red]Justification cannot be empty.[/red]")
            return 1

    async def _do(kind: str, item: dict) -> tuple[str, str, str, str, str]:
        if kind == "ACTIVATE":
            s, e = await activate(gc, principal_id, item, justification, args.hours, args.ticket)
            return (kind, item["displayName"], item.get("accessId", ""), s, e)
        s, err = await approve(gc, item, justification)
        if err:
            console.print(f"  [red]{item['displayName']}: {err}[/red]")
        return (kind, item["displayName"], item.get("accessId", ""), s, err)

    async def _run_batch(batch: list[tuple[str, dict]]) -> list[tuple[str, str, str, str, str]]:
        out: list[tuple[str, str, str, str, str]] = []
        w = max(1, int(args.parallel or 1))
        if w == 1:
            for kind, item in batch:
                console.print(f"\n[cyan]{kind}: {item['displayName']}[/cyan]")
                out.append(await _do(kind, item))
            return out
        console.print(f"\n[cyan]Running {len(batch)} item(s) in parallel (workers={w})...[/cyan]")
        sem = asyncio.Semaphore(w)

        async def _guarded(kind: str, item: dict) -> tuple[str, str, str, str, str]:
            async with sem:
                return await _do(kind, item)

        coros = [_guarded(k, i) for k, i in batch]
        for coro in asyncio.as_completed(coros):
            r = await coro
            if r[3] in ("Provisioned", "Approved", "AlreadyActive", "PendingRequest"):
                color = "green"
            elif r[3] == "AwaitingApproval":
                color = "cyan"
            else:
                color = "yellow"
            detail = (
                f" [dim]({r[4]})[/dim]" if r[4] and r[3] not in ("Provisioned", "AlreadyActive", "Approved") else ""
            )
            console.print(f"[{color}]{r[0]}[/] {r[1]} -> {r[3]}{detail}")
            out.append(r)
        return out

    results = await _run_batch(selected)

    # Detect ACRS step-up failures + retry with fresh token after user does portal MFA.
    def _needs_acrs(r: tuple[str, str, str, str, str]) -> bool:
        return r[3] == "Failed" and "AcrsValidationFailed" in (r[4] or "")

    acrs_names = {r[1] for r in results if _needs_acrs(r)}
    if acrs_names:
        console.print(
            f"\n[yellow]{len(acrs_names)} group(s) blocked by step-up MFA "
            "(RoleAssignmentRequestAcrsValidationFailed).[/yellow]"
        )
        console.print("[cyan]Auto-priming acrs=c1 via portal (driving one dummy activation)...[/cyan]")

        new_token: str | None = None
        if cdp_endpoint:
            try:
                from .acrs_primer import prime_acrs

                new_token = await asyncio.to_thread(prime_acrs, cdp_endpoint, justification="acrs prime", timeout=180)
            except Exception as e:
                console.print(f"[red]Auto-prime failed: {e}[/red]")

        if not new_token:
            console.print("[yellow]Falling back to manual prime.[/yellow]")
            console.print(
                "In portal, open PIM -> My Roles -> Groups -> click 'Activate' on ANY ONE group "
                "with this restriction and complete MFA. Then rerun this script."
            )
            _print_summary(results)
            return 0

        gc.set_token(new_token)

        retry_batch = [(k, i) for k, i in selected if i["displayName"] in acrs_names]
        console.print(f"[cyan]Retrying {len(retry_batch)} failed activation(s) with primed token...[/cyan]")
        retry_results = await _run_batch(retry_batch)

        # Replace old rows with new results
        keep = [r for r in results if r[1] not in acrs_names]
        results = keep + retry_results

    _print_summary(results)
    return 0


def _print_list(eligible: list[dict], pending: list[dict]) -> None:
    if eligible:
        t = Table(title="Eligible", show_lines=False, expand=True)
        t.add_column("NEW", width=3)
        t.add_column("Group", overflow="fold", no_wrap=False, max_width=60)
        t.add_column("Role", width=8)
        t.add_column("MaxH", width=5)
        t.add_column("MFA", width=5)
        t.add_column("Ticket", width=6)
        t.add_column("Until", overflow="fold")
        for e in eligible:
            t.add_row(
                "*" if e.get("isNew") else "",
                e["displayName"],
                e["accessId"],
                str(e["policyMaxDurationHours"]),
                str(e["requiresMfa"]),
                str(e["requiresTicket"]),
                str(e["endDateTime"])[:19],
            )
        console.print(t)
    if pending:
        t = Table(title="Pending approvals (I can approve)")
        for col in ("Group", "Role", "Requester", "Duration"):
            t.add_column(col)
        for p in pending:
            t.add_row(p["displayName"], p.get("accessId", ""), p["requester"], p["duration"])
        console.print(t)
    if not eligible and not pending:
        console.print("[yellow]Nothing to show.[/yellow]")


def _print_summary(results: list[tuple[str, str, str, str, str]]) -> None:
    t = Table(title="Summary", show_lines=False, expand=True)
    t.add_column("Kind", width=8)
    t.add_column("Group", overflow="fold", no_wrap=False, max_width=60)
    t.add_column("Role", width=8)
    t.add_column("Status", width=14)
    t.add_column("Detail", overflow="fold")
    for row in results:
        t.add_row(*row)
    console.print(t)


# ------------- CLI --------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Activate PIM groups + approve pending PIM requests.")
    p.add_argument("--refresh", action="store_true", help="Bypass 24h cache.")
    p.add_argument("--list-only", action="store_true", help="Print eligibilities and pending approvals, exit.")
    p.add_argument("--group", help="Regex filter against displayName (both kinds).")
    p.add_argument("--justification", help="Justification text (prompted if omitted).")
    p.add_argument("--hours", type=int, default=8, help="Activation duration hours (clamped to policy max).")
    p.add_argument("--ticket", help="Ticket number for policies that require it.")
    p.add_argument("--approvals-only", action="store_true", help="Skip eligibilities feed.")
    p.add_argument("--eligibilities-only", action="store_true", help="Skip pending approvals feed.")
    p.add_argument("--headless", action="store_true", help="Run Playwright headless (needs prior login).")
    p.add_argument("--keep-open", action="store_true", help="Keep browser context alive after grabbing token.")
    p.add_argument("--token", help="Skip Playwright; use provided bearer token.")
    p.add_argument(
        "--channel",
        default=DEFAULT_CHANNEL,
        help="Browser channel: msedge (default), chrome, or '' for bundled chromium.",
    )
    p.add_argument(
        "--parallel",
        type=int,
        default=8,
        help="Max concurrent activation/approval workers (default 8). Set 1 for serial; higher risks Graph 429.",
    )
    p.add_argument(
        "--fetch-workers",
        type=int,
        default=32,
        help="Max concurrent read-side Graph fetches for eligibility enrichment (default 32).",
    )
    p.add_argument(
        "--cdp-endpoint",
        help="Attach to existing Chrome via CDP (e.g. http://localhost:9222). "
        "Launch Chrome with --remote-debugging-port=9222 first.",
    )
    p.add_argument(
        "--auto-cdp",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Auto-launch Chrome with debug port on a copied real profile "
        "(bypasses CA 'geen toegang' block on Playwright's own profile). "
        "Kills running Chrome first. On by default; pass --no-auto-cdp to skip.",
    )
    p.add_argument(
        "--auto-cdp-port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Debug port for --auto-cdp (default {DEFAULT_PORT}).",
    )
    p.add_argument(
        "--auto-cdp-profile",
        default=str(DEFAULT_COPY_PROFILE),
        help=f"Target dir for real-profile copy used by --auto-cdp (default {DEFAULT_COPY_PROFILE}).",
    )
    p.add_argument(
        "--refresh-chrome-profile",
        action="store_true",
        help="Force re-copy real Chrome profile even if the target dir exists.",
    )
    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(run(args))
    except TokenExpired:
        console.print("[red]Token expired mid-run. Re-run without --token to refresh via Playwright.[/red]")
        return 2
    except PermissionDenied as e:
        console.print(f"[red]Permission denied: {e}[/red]")
        return 3
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
