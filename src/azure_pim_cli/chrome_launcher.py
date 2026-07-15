"""Launch Chrome with remote debugging port, using a copy of the user's real profile.

Why: Intune device compliance cookies live in the user's real Chrome profile. Playwright's
own persistent profile lacks them, so Conditional Access refuses portal sign-in with
"U kunt op dit moment geen toegang krijgen". Copying the real profile into a debug-
friendly directory sidesteps both Intune's remote-debugging-port block (which only fires
on the managed real profile) and the CA compliance check (cookies came along in the copy).

Flow:
    1. If debug port already responding -> reuse (fast path).
    2. Else kill running chrome.exe (debug port requires exclusive use).
    3. Copy real profile -> C:\\temp\\chrome_pim_profile (skip if target exists unless refresh).
    4. Launch chrome.exe --remote-debugging-port=<port> --user-data-dir=<copy> <start-url>.
    5. Poll /json/version until ready, return CDP endpoint URL.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_PORT = 9222
DEFAULT_COPY_PROFILE = Path(r"C:\temp\chrome_pim_profile")
DEFAULT_START_URL = "https://portal.azure.com/#view/Microsoft_Azure_PIMCommon/ActivationMenuBlade/~/aadgroup"


def _chrome_exe() -> str:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    raise RuntimeError(f"chrome.exe not found in any of: {candidates}")


def _default_source_profile() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data"


def _port_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=1) as r:
            return r.status == 200
    except (urllib.error.URLError, ConnectionResetError, OSError):
        return False


def _wait_ready(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_alive(port):
            return True
        time.sleep(0.5)
    return False


def _kill_chrome() -> None:
    subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True)


def _copy_profile(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    # robocopy /E all subdirs, /XJ skip junctions (avoids symlink loops in Chrome cache),
    # /R:1 /W:1 minimise retries on locked files, /NFL /NDL /NJH /NJS quiet output.
    subprocess.run(
        [
            "robocopy",
            str(src),
            str(dst),
            "/E",
            "/XJ",
            "/R:1",
            "/W:1",
            "/NFL",
            "/NDL",
            "/NJH",
            "/NJS",
            "/NC",
            "/NS",
            "/NP",
        ],
        capture_output=True,
    )


def launch_debug_chrome(
    port: int = DEFAULT_PORT,
    copy_profile: Path = DEFAULT_COPY_PROFILE,
    source_profile: Path | None = None,
    start_url: str = DEFAULT_START_URL,
    force_profile_refresh: bool = False,
) -> str:
    """Ensure a Chrome with debug port is running against a profile that has Intune cookies.

    Returns CDP endpoint URL (http://localhost:<port>).
    """
    if _port_alive(port) and not force_profile_refresh:
        print(f"[chrome] debug port {port} already responding; reusing.", file=sys.stderr)
        return f"http://localhost:{port}"
    if _port_alive(port) and force_profile_refresh:
        print(
            "[chrome] --refresh-chrome-profile set; restarting Chrome to apply fresh profile.",
            file=sys.stderr,
        )

    exe = _chrome_exe()
    src = source_profile or _default_source_profile()

    print("[chrome] killing chrome.exe (need exclusive debug port)...", file=sys.stderr)
    _kill_chrome()
    time.sleep(1)

    need_copy = force_profile_refresh or not copy_profile.exists() or not any(copy_profile.iterdir())
    if need_copy:
        if not src.exists():
            raise RuntimeError(f"source profile not found: {src}")
        print(
            f"[chrome] copying profile {src} -> {copy_profile} (may take 30-90s)...",
            file=sys.stderr,
        )
        _copy_profile(src, copy_profile)
    else:
        print(
            f"[chrome] reusing existing profile copy at {copy_profile} (pass --refresh-chrome-profile to re-copy).",
            file=sys.stderr,
        )

    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={copy_profile}",
        "--no-first-run",
        "--no-default-browser-check",
        start_url,
    ]
    print(
        f"[chrome] launching: {exe} --remote-debugging-port={port} --user-data-dir={copy_profile}",
        file=sys.stderr,
    )

    # DETACHED_PROCESS = 0x00000008; keeps Chrome alive after Python exits.
    creationflags = 0x00000008 if os.name == "nt" else 0
    subprocess.Popen(args, creationflags=creationflags, close_fds=True)

    print(f"[chrome] waiting for debug port {port}...", file=sys.stderr)
    if not _wait_ready(port):
        raise RuntimeError(
            f"Chrome debug port {port} not responding after 30s. Chrome may be blocked by policy or failed to launch."
        )
    print(f"[chrome] ready at http://localhost:{port}", file=sys.stderr)
    print("[chrome] complete portal sign-in in the new Chrome window if prompted.", file=sys.stderr)
    return f"http://localhost:{port}"


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Launch Chrome with debug port on a copied profile.")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--copy-profile", default=str(DEFAULT_COPY_PROFILE))
    p.add_argument("--source-profile", default=None)
    p.add_argument("--refresh", action="store_true", help="Re-copy real profile even if target exists.")
    p.add_argument("--url", default=DEFAULT_START_URL)
    a = p.parse_args()
    endpoint = launch_debug_chrome(
        port=a.port,
        copy_profile=Path(a.copy_profile),
        source_profile=Path(a.source_profile) if a.source_profile else None,
        start_url=a.url,
        force_profile_refresh=a.refresh,
    )
    print(endpoint)
