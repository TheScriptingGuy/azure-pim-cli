"""Grab a Microsoft Graph bearer token from an authenticated Azure Portal session.

Launches Playwright with a persistent user-data dir (so login survives across runs).
Two parallel strategies race:
  1. Network sniff: intercept the Authorization header off a graph.microsoft.com XHR
  2. sessionStorage scrape: read MSAL access token cache from the portal SPA

Portal app c44b4083 is CA-exempt in the tenant, so this succeeds where
Connect-MgGraph / az CLI / MSAL public client fail.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

# Entra blades that typically fire graph.microsoft.com XHRs. If neither triggers
# one, the sessionStorage scrape picks up the token portal cached during login.
PORTAL_URLS = [
    # PIM activation blade first: portal requests ReadWrite scope here (needed for POST)
    "https://portal.azure.com/#view/Microsoft_Azure_PIMCommon/ActivationMenuBlade/~/aadgroup",
    # PIM approvals blade: caches PrivilegedAssignmentSchedule.ReadWrite scope for approve_pim.py
    "https://portal.azure.com/#view/Microsoft_Azure_PIMCommon/ApproveRequestMenuBlade/~/aadgroup",
    # Fallback: Entra Groups (acquires READ scope only)
    "https://portal.azure.com/#view/Microsoft_AAD_IAM/GroupsManagementMenuBlade/~/AllGroups",
    # Fallback: Users blade
    "https://portal.azure.com/#view/Microsoft_AAD_UsersAndTenants/UserManagementMenuBlade/~/AllUsers",
]
GRAPH_HOSTS = ("graph.microsoft.com",)
GRAPH_TARGET_MARKERS = ("graph.microsoft.com",)

# Legacy PIM REST (portal's Approvals blade uses this, not Graph)
AZRBAC_HOSTS = ("api.azrbac.mspim.azure.com",)
AZRBAC_AUD_MARKERS = (
    "api.azrbac.mspim.azure.com",
    "01fc33a7-78ba-4d2f-a4b7-768e336e890e",  # PIM Microsoft.Azure.PIMCommon app id
)

# Playwright browser channels. "chrome" uses installed Chrome, "msedge" Edge.
# Empty string / None = bundled chromium.
DEFAULT_CHANNEL = "chrome"


def _profile_dir() -> Path:
    if os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"]) / "pim_activate"
    else:
        base = Path.home() / ".pim_activate"
    d = base / "browser_profile"
    d.mkdir(parents=True, exist_ok=True)
    return d


# Read-only PIM scope is sufficient for listing. Activation additionally needs
# ReadWrite; portal only caches that after visiting the PIM activation blade.
PIM_READ_MARKERS = (
    "privilegedeligibilityschedule.read.azureadgroup",
    "privilegedaccess.read.azureadgroup",
)
PIM_READWRITE_MARKERS = (
    "privilegedeligibilityschedule.readwrite.azureadgroup",
    "privilegedaccess.readwrite.azureadgroup",
    "privilegedassignmentschedule.readwrite.azureadgroup",
)
REQUIRED_SCOPE_MARKERS = PIM_READ_MARKERS + PIM_READWRITE_MARKERS


def _decode_payload(token: str) -> dict | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None


def _decode_exp(token: str) -> int | None:
    p = _decode_payload(token)
    return int(p.get("exp", 0)) if p else None


def _has_pim_scope(token: str, require_readwrite: bool = False, require_acrs: bool = False) -> bool:
    p = _decode_payload(token)
    if not p:
        return False
    scp = (p.get("scp") or "").lower()
    markers = PIM_READWRITE_MARKERS if require_readwrite else REQUIRED_SCOPE_MARKERS
    if not any(m in scp for m in markers):
        return False
    if require_acrs:
        acrs = p.get("acrs") or []
        if isinstance(acrs, str):
            acrs = [acrs]
        if "c1" not in [str(a).lower() for a in acrs]:
            return False
    return True


def _is_azrbac_token(token: str) -> bool:
    """Detect a token minted for the legacy PIM REST (api.azrbac.mspim.azure.com)."""
    p = _decode_payload(token)
    if not p:
        return False
    aud = str(p.get("aud") or "").lower()
    return any(m in aud for m in AZRBAC_AUD_MARKERS)


# JS run inside the portal page. Iterates all frames' sessionStorage / localStorage
# looking for MSAL access token cache entries whose target/scope contains
# graph.microsoft.com. Returns the first valid (unexpired) access token.
_STORAGE_SCRAPE_JS = r"""
() => {
    const now = Math.floor(Date.now() / 1000);
    const candidates = [];
    for (const store of [window.sessionStorage, window.localStorage]) {
        for (let i = 0; i < store.length; i++) {
            const key = store.key(i);
            if (!key) continue;
            const val = store.getItem(key);
            if (!val) continue;
            // MSAL v2 access token entries are JSON with fields: secret, target, expiresOn, credentialType
            try {
                const obj = JSON.parse(val);
                if (obj && obj.credentialType === "AccessToken" && obj.secret) {
                    const target = (obj.target || "").toLowerCase();
                    if (target.includes("graph.microsoft.com")) {
                        const exp = parseInt(obj.expiresOn || "0", 10);
                        if (!exp || exp > now + 30) {
                            // Prefer PIM-scoped, then ADIbizaUX broad scope, then anything Graph
                            let score = 0;
                            if (target.includes("privilegedeligibilityschedule")) score += 100;
                            if (target.includes("privilegedaccess")) score += 100;
                            if (target.includes("rolemanagement")) score += 50;
                            if (target.includes("group.readwrite.all")) score += 20;
                            if (target.includes("directory.read.all")) score += 10;
                            candidates.push({ secret: obj.secret, exp, target, score });
                        }
                    }
                }
            } catch (e) { /* not JSON */ }
        }
    }
    if (candidates.length === 0) return null;
    // Highest score first, then longest TTL
    candidates.sort((a, b) => (b.score - a.score) || ((b.exp || 0) - (a.exp || 0)));
    return candidates[0].secret;
}
"""


def _scrape_storage(page) -> str | None:
    """Scan every frame's storage for a graph.microsoft.com access token."""
    for frame in page.frames:
        try:
            tok = frame.evaluate(_STORAGE_SCRAPE_JS)
        except Exception:
            continue
        if tok:
            return tok
    return None


def _is_portal_tab(url: str) -> bool:
    """True only when the tab's hostname is portal.azure.com (not a login redirect containing it in a query param)."""
    try:
        return urlparse(url).hostname == "portal.azure.com"
    except Exception:
        return False


def _scrape_all_pages(ctx) -> str | None:
    """Scan storage across every tab in the browser context."""
    for pg in ctx.pages:
        try:
            url = pg.url
        except Exception:
            continue
        if not _is_portal_tab(url):
            continue
        tok = _scrape_storage(pg)
        if tok:
            return tok
    return None


def grab_token(
    headless: bool = False,
    keep_open: bool = False,
    sniff_timeout: int = 300,
    channel: str | None = DEFAULT_CHANNEL,
    require_readwrite: bool = False,
    resource: str = "graph",
    cdp_endpoint: str | None = None,
    require_acrs: bool = False,
) -> str:
    """Return a bearer token for graph.microsoft.com from the portal session.

    First run: shows a browser window for interactive login.
    Subsequent runs: reuses persisted cookies, silent if session still valid.
    """
    profile = _profile_dir()
    token_holder: dict[str, str] = {}
    token_event = threading.Event()

    target_hosts = AZRBAC_HOSTS if resource == "azrbac" else GRAPH_HOSTS

    def _accept(candidate: str) -> bool:
        if resource == "azrbac":
            return _is_azrbac_token(candidate)
        return _has_pim_scope(candidate, require_readwrite=require_readwrite, require_acrs=require_acrs)

    def on_request(request):
        if token_event.is_set():
            return
        try:
            url = request.url
        except Exception:
            return
        if not any(h in url for h in target_hosts):
            return
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return
        candidate = auth.split(" ", 1)[1]
        if not _accept(candidate):
            return  # skip wrong-scope / wrong-audience tokens
        token_holder["token"] = candidate
        token_holder["source"] = "network"
        token_event.set()

    with sync_playwright() as pw:
        cdp = cdp_endpoint or os.environ.get("PIM_CDP_ENDPOINT")
        ctx = None
        browser = None
        if cdp:
            print(f"[token] attaching to existing Chrome via CDP: {cdp}", file=sys.stderr)
            print(
                "[token] make sure Chrome was started with --remote-debugging-port=9222",
                file=sys.stderr,
            )
            try:
                browser = pw.chromium.connect_over_cdp(cdp, timeout=15000)
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            except Exception as e:
                print(
                    f"[token] CDP attach failed ({e}); killing Chrome and falling back to launch.",
                    file=sys.stderr,
                )
                if os.name == "nt":
                    import subprocess as _sp

                    _sp.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True)
                ctx = None
                browser = None

        if ctx is None:
            debug_port = os.environ.get("PIM_DEBUG_PORT", "9222")
            chrome_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-features=IsolateOrigins,site-per-process",
                f"--remote-debugging-port={debug_port}",
            ]
            launch_kwargs = dict(
                user_data_dir=str(profile),
                headless=headless,
                args=chrome_args,
            )
            if channel:
                launch_kwargs["channel"] = channel
            print(
                f"[token] chrome debug port: {debug_port} (attach via chrome://inspect or DevTools)",
                file=sys.stderr,
            )

            for _lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                try:
                    (profile / _lock).unlink()
                except FileNotFoundError:
                    pass

            try:
                ctx = pw.chromium.launch_persistent_context(**launch_kwargs)  # type: ignore[arg-type]
            except Exception as e:
                if channel:
                    print(
                        f"[token] {channel} unavailable ({e}); falling back to bundled chromium.",
                        file=sys.stderr,
                    )
                    launch_kwargs.pop("channel", None)
                    ctx = pw.chromium.launch_persistent_context(**launch_kwargs)  # type: ignore[arg-type]
                else:
                    raise

        # For CDP mode, register on ALL existing pages + new ones
        for existing_page in ctx.pages:
            existing_page.on("request", on_request)
        ctx.on("page", lambda p: p.on("request", on_request))

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("request", on_request)

        print(
            f"[token] navigating (channel={channel or 'bundled'}, headless={headless})...",
            file=sys.stderr,
        )
        print(
            f"[token] complete login/SSO in the browser window. Waiting up to {sniff_timeout}s.",
            file=sys.stderr,
        )

        # Approvals blade fires azrbac requests; put it first when we need that resource.
        if resource == "azrbac":
            ordered = [
                "https://portal.azure.com/#view/Microsoft_Azure_PIMCommon/ApproveRequestMenuBlade/~/aadgroup",
                "https://portal.azure.com/#view/Microsoft_Azure_PIMCommon/ActivationMenuBlade/~/aadgroup",
            ]
        else:
            ordered = PORTAL_URLS

        # In CDP mode: prefer scraping storage from an already-authenticated portal tab
        # (SSO cookies often don't carry over to a fresh new_page()). Only open a new
        # tab if no portal tab is present.
        opened_page = None
        if cdp:
            urls = [p.url[:80] for p in ctx.pages]
            print(f"[token] CDP tabs seen: {urls}", file=sys.stderr)
            # Try to scrape from existing portal tabs first
            early = _scrape_all_pages(ctx)
            if early and _accept(early) and not token_event.is_set():
                token_holder["token"] = early
                token_holder["source"] = "storage-existing-tab"
                token_event.set()
                print("[token] found token in existing portal tab storage.", file=sys.stderr)
            else:
                has_portal = any(_is_portal_tab(p.url or "") for p in ctx.pages)
                if has_portal:
                    # Reuse a portal tab; navigate it to the target blade to fire XHR
                    for pg in ctx.pages:
                        if _is_portal_tab(pg.url or ""):
                            page = pg
                            page.on("request", on_request)
                            print(f"[token] reusing portal tab: {pg.url[:80]}", file=sys.stderr)
                            try:
                                page.goto(ordered[0], wait_until="commit", timeout=30_000)
                            except Exception as e:
                                print(f"[token] reused tab nav warning: {e}", file=sys.stderr)
                            break
                else:
                    try:
                        opened_page = ctx.new_page()
                        opened_page.on("request", on_request)
                        page = opened_page
                        print(f"[token] opening new tab: {ordered[0]}", file=sys.stderr)
                        opened_page.goto(ordered[0], wait_until="commit", timeout=30_000)
                    except Exception as e:
                        print(
                            f"[token] CDP new-tab warning: {e}; falling back to existing tabs.",
                            file=sys.stderr,
                        )
                        print(
                            "[token] >>> click 'Vernieuwen' on the PIM tab to fire the API call.",
                            file=sys.stderr,
                        )
        else:
            try:
                page.goto(ordered[0], wait_until="commit", timeout=30_000)
            except Exception as e:
                print(f"[token] initial nav warning: {e}", file=sys.stderr)

        # Poll: race network sniff against storage scrape; nudge to next blade at intervals.
        nudge_at = [60, 150]  # seconds elapsed to nudge to blade[1], blade[2]
        nudge_targets = list(ordered[1:])
        started = time.time()
        deadline = started + sniff_timeout
        next_nudge_idx = 0

        while time.time() < deadline:
            if token_event.wait(timeout=5):
                break

            # Storage scrape (in case portal didn't fire a direct Graph XHR).
            # In CDP mode scan every portal tab, not just the one we opened.
            try:
                scraped = _scrape_all_pages(ctx) if cdp else _scrape_storage(page)
            except Exception as e:
                scraped = None
                print(f"[token] storage scrape error: {e}", file=sys.stderr)
            # Skip storage when acrs required — MSAL doesn't cache claims-enriched tokens.
            if not require_acrs and scraped and _accept(scraped) and not token_event.is_set():
                token_holder["token"] = scraped
                token_holder["source"] = "storage"
                token_event.set()
                break

            elapsed = int(time.time() - started)
            remaining = int(deadline - time.time())
            try:
                cur = page.url
            except Exception:
                cur = "?"
            if "login.microsoftonline.com" in cur or "login.microsoft.com" in cur:
                print(
                    f"[token] portal redirected to login — sign in at Chrome window to continue ({remaining}s left)",
                    file=sys.stderr,
                )
            else:
                print(f"[token] waiting... ({remaining}s left, at {cur[:70]})", file=sys.stderr)

            # Nudge to next blade if enough time has elapsed (skip in CDP mode — user's tab is authoritative)
            if not cdp and next_nudge_idx < len(nudge_at) and next_nudge_idx < len(nudge_targets):
                if elapsed >= nudge_at[next_nudge_idx]:
                    target = nudge_targets[next_nudge_idx]
                    next_nudge_idx += 1
                    print(f"[token] nudging to: {target}", file=sys.stderr)
                    try:
                        page.goto(target, wait_until="commit", timeout=30_000)
                    except Exception as e:
                        print(f"[token] nudge nav warning: {e}", file=sys.stderr)

        if not token_event.is_set():
            if not keep_open:
                ctx.close()
            raise RuntimeError(
                "No graph.microsoft.com token captured. "
                "If Chrome kept redirecting to login, the profile is stale — "
                "re-run with --refresh-chrome-profile to re-copy your real Chrome cookies. "
                "Alternatively, paste a token via --token."
            )

        token = token_holder["token"]
        source = token_holder.get("source", "?")
        exp = _decode_exp(token)
        pl = _decode_payload(token) or {}
        acrs_claim = pl.get("acrs")
        scp_claim = (pl.get("scp") or "")[:80]
        if exp:
            ttl = exp - int(time.time())
            print(
                f"[token] captured via {source} (expires in ~{ttl // 60}m, acrs={acrs_claim}, scp={scp_claim!r})",
                file=sys.stderr,
            )
        else:
            print(
                f"[token] captured via {source} (unknown expiry, acrs={acrs_claim})",
                file=sys.stderr,
            )

        if not keep_open:
            if browser is not None:
                # CDP mode — don't close the user's Chrome, just disconnect
                try:
                    browser.close()
                except Exception:
                    pass
            else:
                ctx.close()

    return token


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--headless", action="store_true")
    p.add_argument("--keep-open", action="store_true")
    p.add_argument(
        "--channel",
        default=DEFAULT_CHANNEL,
        help="chrome (default) | msedge | '' for bundled chromium",
    )
    args = p.parse_args()

    tok = grab_token(headless=args.headless, keep_open=args.keep_open, channel=(args.channel or None))
    print(tok)
