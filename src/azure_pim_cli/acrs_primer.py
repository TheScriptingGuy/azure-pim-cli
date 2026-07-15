"""Prime an acrs=c1 (step-up MFA) Graph token via portal.

The Graph PIM activation POST refuses tokens lacking the `acrs=c1` claim
(`RoleAssignmentRequestAcrsValidationFailed`). Portal handles this by calling
MSAL with a claims challenge; if the user has a cached MFA session
(`kmsi`/`dvc_cmp`), portal upgrades silently and retries. We can't do that
step-up in raw Python (no refresh token), but we CAN drive portal to do it for
us via Playwright over CDP.

Flow:
  1. Attach to authenticated Chrome (debug port 9222).
  2. Navigate to PIM Activation blade.
  3. Click Activate on the first eligible row.
  4. Fill reason, submit.
  5. Sniff the resulting Graph POST — its Authorization header contains a fresh
     token with `acrs=c1`. Return that token.

If cached MFA has lapsed, portal shows an MFA modal in the visible browser
window — user completes it, script picks up the token afterwards. No signal
file / TEMP handshake needed.
"""

from __future__ import annotations

import base64
import json
import sys
import threading
import time

from playwright.sync_api import sync_playwright

ACTIVATION_URL = "https://portal.azure.com/#view/Microsoft_Azure_PIMCommon/ActivationMenuBlade/~/aadgroup"


def _has_c1(token: str) -> bool:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return False
    acrs = claims.get("acrs") or []
    if isinstance(acrs, str):
        acrs = [acrs]
    return "c1" in [str(a).lower() for a in acrs]


def prime_acrs(cdp_endpoint: str, justification: str = "acrs prime", timeout: int = 180) -> str:
    """Drive portal to fire a Graph POST bearing an acrs=c1 token; return that token.

    Raises RuntimeError on timeout / capture failure.
    """
    token_holder: dict[str, str] = {}
    token_event = threading.Event()

    def on_req(request):
        if token_event.is_set():
            return
        try:
            url = request.url
            method = request.method
        except Exception:
            return
        if "graph.microsoft.com" not in url:
            return
        if method != "POST":
            return
        if "assignmentScheduleRequests" not in url:
            return
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return
        candidate = auth.split(" ", 1)[1]
        if not _has_c1(candidate):
            return
        token_holder["token"] = candidate
        token_event.set()

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp_endpoint)
        ctx = browser.contexts[0]

        # Register on ALL current + future pages (portal iframes fire XHRs)
        for existing in ctx.pages:
            existing.on("request", on_req)
        ctx.on("page", lambda p: p.on("request", on_req))

        # Prefer a portal tab; else new tab
        page = None
        for pg in ctx.pages:
            if "portal.azure.com" in (pg.url or ""):
                page = pg
                break
        if page is None:
            page = ctx.new_page()
        page.on("request", on_req)

        print("[acrs] navigating portal tab to activation blade...", file=sys.stderr)
        try:
            page.goto(ACTIVATION_URL, wait_until="commit", timeout=60_000)
        except Exception as e:
            print(f"[acrs] nav warning: {e}", file=sys.stderr)

        # Wait for eligibility grid to render
        try:
            page.wait_for_selector('button:has-text("Activeren")', timeout=30_000)
        except Exception as e:
            raise RuntimeError(f"activation blade did not load: {e}")

        # Click first grid-row Activate (skip sidebar tab button by scoping to gridcell)
        print("[acrs] clicking first Activate on eligibility row...", file=sys.stderr)
        try:
            page.locator('[role="gridcell"] button:has-text("Activeren")').first.click(timeout=10_000)
        except Exception:
            # Fallback: any visible Activeren, but skip the sidebar tab (has "in-/uitschakelen")
            btns = page.locator('button:has-text("Activeren"):not(:has-text("in-/uitschakelen"))').all()
            if not btns:
                raise RuntimeError("could not find any Activate button on grid.")
            btns[0].click()

        # Wait for confirmation panel with reason textbox
        try:
            page.wait_for_selector(
                'textbox[aria-label*="Reden"], textarea[aria-label*="Reden"], [role="textbox"][aria-label*="Reden"]',
                timeout=15_000,
            )
        except Exception:
            # Portal panel loads a bit slower sometimes
            time.sleep(2)

        # Fill reason
        print("[acrs] filling reason + submitting...", file=sys.stderr)
        try:
            page.get_by_role("textbox", name="Reden").first.fill(justification)
        except Exception:
            try:
                page.locator('[role="textbox"][aria-label*="Reden"]').first.fill(justification)
            except Exception as e:
                print(
                    f"[acrs] warn: could not fill reason ({e}); portal may accept empty for silent step-up.",
                    file=sys.stderr,
                )

        # Click the panel Activate (bottom-most button in the aside/complementary panel)
        try:
            # Aside panel Activate — usually the last visible Activate button
            panel_buttons = page.get_by_role("button", name="Activeren").all()
            # Pick a panel button (not the row buttons). The confirmation panel
            # button follows all row buttons; taking the last one is reliable.
            panel_buttons[-1].click()
        except Exception as e:
            raise RuntimeError(f"could not click confirmation Activate button: {e}")

        print(f"[acrs] waiting for acrs=c1 POST (up to {timeout}s)...", file=sys.stderr)
        print(
            "[acrs] if MFA prompt appears in the browser, complete it — script will pick up the token.",
            file=sys.stderr,
        )

        deadline = time.time() + timeout
        while time.time() < deadline:
            if token_event.wait(timeout=2):
                break

        if not token_event.is_set():
            raise RuntimeError("no acrs=c1 token captured after portal prime (timeout).")

        token = token_holder["token"]
        print("[acrs] captured acrs=c1 token.", file=sys.stderr)
        return token


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp-endpoint", default="http://localhost:9222")
    ap.add_argument("--justification", default="acrs prime")
    ap.add_argument("--timeout", type=int, default=180)
    a = ap.parse_args()
    tok = prime_acrs(a.cdp_endpoint, a.justification, a.timeout)
    print(tok)
