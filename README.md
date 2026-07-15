# azure-pim-cli

[![PyPI version](https://img.shields.io/pypi/v/azure-pim-cli)](https://pypi.org/project/azure-pim-cli/)
[![CI](https://github.com/wesselvdlinden/azure-pim-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/wesselvdlinden/azure-pim-cli/actions/workflows/ci.yml)
[![Python](https://img.shields.io/pypi/pyversions/azure-pim-cli)](https://pypi.org/project/azure-pim-cli/)

Activate eligible Azure PIM group memberships **and** approve pending PIM group
requests you can approve, from one interactive picker.

Uses a real browser session (Playwright over CDP) to grab a Microsoft Graph
bearer token from an authenticated Azure Portal, bypassing the Conditional
Access blocks that break `Connect-MgGraph` / `az CLI` / Cloud Shell in the
tenant.

> **Windows only.** Relies on `robocopy`, `taskkill`, and Chrome paths specific to Windows.

## Install

```powershell
pip install azure-pim-cli
playwright install chromium
```

## First run

```powershell
pim-activate
```

What happens:

1. `--auto-cdp` (on by default) kills any running Chrome, copies your real
   Chrome profile to `C:\temp\chrome_pim_profile` (preserves Intune compliance
   cookies), launches Chrome with `--remote-debugging-port=9222`.
2. Playwright attaches over CDP to that Chrome and opens the PIM Approvals
   blade.
3. If you're not signed in, complete portal SSO in the Chrome window that pops
   up. The script waits (up to 300s).
4. The script sniffs a `graph.microsoft.com` Bearer token from the portal
   session, or scrapes MSAL storage on an already-authenticated tab.
5. Prints pending approvals (if any) and eligibilities, then shows the
   interactive checkbox picker.

Subsequent runs skip login (session cookies persist in the copied Chrome
profile).

## Typical output flow

```
[chrome] launching Chrome with debug port 9222 ...
[token] captured via storage-existing-tab (expires in ~40m, ...)
User: user@example.com (05a0a5ea-...)
Fetching pending PIM group approvals (Graph)...
>> 2 pending approval(s) awaiting your decision.
Fetching eligible PIM group assignments...
>> 3 NEW eligible group(s) since last fetch!
? Select PIM items to activate/approve:
  ❯ ◯ APPROVE   sg-prj-ExampleProject-...   <- user@example.com   PT2H
    ◯ ACTIVATE *NEW* sg-app-reporting-reader     (member)  max 8h  MFA=True
    ◯ ACTIVATE       sg-db-warehouse-contributor (member)  max 8h  MFA=True
    ...
```

If no approvals are pending you'll see:

```
[dim]No pending approvals for you.[/dim]
```

and it moves straight to the eligibilities list.

## Common commands

```powershell
# Combined interactive picker — approvals FIRST, then eligibilities
pim-activate

# List everything, no action (approvals + eligibilities tables)
pim-activate --list-only

# Refresh 24h eligibility cache
pim-activate --refresh

# Only my eligibilities (skip approvals feed)
pim-activate --eligibilities-only

# Only pending approvals I can approve
pim-activate --approvals-only

# Skip Playwright entirely — paste a Graph bearer token from DevTools
pim-activate --token "eyJ0eXAi..."
```

## Bulk activation via `--group` regex

`--group REGEX` skips the picker and activates every eligibility whose
`displayName` matches (case-insensitive). Combine with `--parallel N` for
concurrent POSTs.

```powershell
# Activate ALL sg-* eligibilities, 8 workers, 8h
pim-activate --group "^sg-" --justification "bulk activate" --hours 8 --eligibilities-only --parallel 8

# Only analytics groups
pim-activate --group "-app-" --justification "analytics work" --hours 4 --parallel 4

# Dry-run (list matches, no action)
pim-activate --group "db-warehouse" --list-only
```

- Regex matches `displayName` on both eligibilities and pending approvals.
- Groups already active are auto-skipped (avoids `PendingRoleAssignmentRequest`
  errors on reruns).
- `--parallel` default is 8. Higher risks Graph 429.
- Polls each request up to 180s; `Timeout` status usually still succeeds — verify
  with `--list-only`.

## Auto-CDP flags

`--auto-cdp` is on by default and handles the Chrome profile copy + debug-port
launch automatically. Override when needed:

```powershell
# Disable auto-cdp (attach to a manually-launched debug Chrome, or use bundled Playwright)
pim-activate --no-auto-cdp

# Force re-copy real Chrome profile (e.g. cookies stale)
pim-activate --refresh-chrome-profile

# Non-default debug port
pim-activate --auto-cdp-port 9333

# Non-default profile-copy target
pim-activate --auto-cdp-profile "D:\pim_chrome"

# Attach to already-running debug Chrome instead
pim-activate --no-auto-cdp --cdp-endpoint "http://localhost:9222"
```

## All flags

| Flag | Default | Description |
|---|---|---|
| `--refresh` | off | Bypass 24h eligibility cache |
| `--list-only` | off | Show tables, no activation/approval |
| `--group REGEX` | - | Filter both feeds by displayName |
| `--justification TEXT` | - | Prompted if omitted |
| `--hours N` | 8 | Duration (clamped per group policy) |
| `--ticket N` | - | Ticket number for policies that require it |
| `--approvals-only` | off | Skip eligibilities feed |
| `--eligibilities-only` | off | Skip pending approvals feed |
| `--headless` | off | Run Chromium headless (needs prior login) |
| `--keep-open` | off | Keep browser alive after token grab (debug) |
| `--token TOKEN` | - | Skip Playwright; use this Graph bearer token |
| `--parallel N` | 8 | Concurrent workers for bulk actions |
| `--channel STR` | chrome | Browser channel: `chrome`, `msedge`, `''` for bundled chromium |
| `--auto-cdp` | on | Auto-launch Chrome with debug port on copied real profile |
| `--no-auto-cdp` | - | Disable auto-cdp |
| `--auto-cdp-port` | 9222 | Debug port |
| `--auto-cdp-profile` | `C:\temp\chrome_pim_profile` | Profile-copy target |
| `--refresh-chrome-profile` | off | Force re-copy real Chrome profile |
| `--cdp-endpoint URL` | - | Attach to existing debug Chrome |

## Cache

- Eligibilities cached 24h at `%LOCALAPPDATA%\pim_activate\eligible_cache.json`.
- Pending approvals are **never** cached (time-sensitive).
- NEW entries flagged `*NEW*` in picker and `*` in list mode.

## Auth model

Portal PIM app (`c44b4083-...` / `Microsoft_Azure_PIMCommon`) sits inside the
authenticated Portal SPA. Its Graph token has all PIM scopes we need. We grab
that token via Playwright over CDP (attached to a Chrome that runs against a
copy of your real profile, so Intune compliance cookies come along).

- `az account get-access-token --resource https://graph.microsoft.com` fails —
  first-party az CLI cannot request PIM Graph scopes in the tenant.
- `Connect-MgGraph -UseDeviceCode` fails — CA blocks the Graph PowerShell app.
- Cloud Shell fails — CA blocks the Cloud Shell session.
- Portal-in-browser + sniff works.

### Scope caveat

Portal caches READ scope on first load of Entra ID blades. ReadWrite scope for
activation POST is cached only when portal visits the PIM Groups activation
blade. `--auto-cdp` opens that blade automatically.

Approval action needs `PrivilegedAssignmentSchedule.ReadWrite.AzureADGroup`,
which portal caches when you visit the approvals blade. If Graph 403s on
approve, open PIM Approvals blade in portal once, then rerun.

## Endpoints used

- `GET /v1.0/me`
- `GET /v1.0/identityGovernance/privilegedAccess/group/eligibilityScheduleInstances`
- `GET /v1.0/groups/{id}`
- `GET /v1.0/policies/roleManagementPolicyAssignments`
- `GET /v1.0/policies/roleManagementPolicies/{id}/rules`
- `POST /v1.0/identityGovernance/privilegedAccess/group/assignmentScheduleRequests` (`selfActivate`)
- `GET /v1.0/identityGovernance/privilegedAccess/group/assignmentScheduleRequests/{id}` (poll)
- `GET /beta/identityGovernance/privilegedAccess/group/assignmentScheduleRequests/filterByCurrentUser(on='approver')` (pending approvals — portal-parity server-side scoping)
- `GET /beta/identityGovernance/privilegedAccess/group/assignmentApprovals/{approvalId}` (approval steps)
- `PATCH /beta/identityGovernance/privilegedAccess/group/assignmentApprovals/{approvalId}/steps/{stepId}` (approve)

## Fallback: manual token

If Playwright breaks (browser update, CA change), grab a token manually:

1. Open portal.azure.com, sign in.
2. F12 → Network tab.
3. Navigate to PIM Groups. Find a request to `graph.microsoft.com/*`.
4. Copy the `Authorization` header value (drop the `Bearer ` prefix).
5. `pim-activate --token "<paste>"`.

## Known limitations

- **Denying approvals**: not supported. Use portal.
- **Non-group PIM** (AAD roles, Azure resource roles): different endpoints.
- **Scheduled/future activations**: only immediate `selfActivate`.
- **bash/xterm terminal + interactive picker**: `questionary` needs a Win32
  console — run from cmd.exe, PowerShell, or Windows Terminal. Bash/Cygwin
  raises `NoConsoleScreenBufferError`. Non-interactive flags (`--group`,
  `--list-only`) work in any shell.

## Development

```powershell
git clone https://github.com/wesselvdlinden/azure-pim-cli
cd azure-pim-cli
python -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check . && ruff format --check .

# Type check
mypy src/azure_pim_cli
```

### Publishing a release

1. On PyPI, create a new project `azure-pim-cli` and configure a **Trusted Publisher**:
   - Owner: `wesselvdlinden`
   - Repo: `azure-pim-cli`
   - Workflow: `release.yml`
   - Environment: `pypi`
2. Tag and push: `git tag v0.1.0 && git push origin v0.1.0`
3. The `release.yml` workflow builds and publishes automatically via OIDC — no API token needed.
