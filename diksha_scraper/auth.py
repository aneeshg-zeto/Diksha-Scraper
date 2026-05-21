"""Authentication module.

Strategy
--------
1. Kills any existing Brave/Chrome process that would block a fresh launch.
2. Relaunches the browser with ``--remote-debugging-port=9222`` so Playwright
   can connect to it via CDP (Chrome DevTools Protocol).
3. Navigates to DIKSHA, waits for the user to log in manually with their real
   Google account (saved passwords work because we use the real profile).
4. Captures all cookies + the ``Authorization: Bearer <jwt>`` token.
5. Saves them to ``auth/cookies.json`` and ``auth/token.json``.

On subsequent runs ``get_authenticated_session`` loads those files into a
``requests.Session`` — no browser needed.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import winreg
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from .config import ScraperConfig
from .logger import get_logger

log = get_logger(__name__)

_REQUIRED_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://diksha.gov.in",
    "Referer": "https://diksha.gov.in/",
}

_DEBUG_PORT = 9222


# ── Browser detection ──────────────────────────────────────────────────────────

_BROWSER_MAP: Dict[str, Tuple[str, str, str]] = {
    "BraveHTML": (
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"BraveSoftware\Brave-Browser\User Data"),
        "Default",
    ),
    "ChromeHTML": (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\User Data"),
        "Default",
    ),
    "MSEdgeHTM": (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Microsoft\Edge\User Data"),
        "Default",
    ),
    "OperaStable": (
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Programs\Opera\opera.exe"),
        os.path.join(os.environ.get("APPDATA", ""), r"Opera Software\Opera Stable"),
        "Default",
    ),
    "VivaldiHTM": (
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Vivaldi\Application\vivaldi.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Vivaldi\User Data"),
        "Default",
    ),
}

# Process names to kill before relaunching (so the profile isn't locked)
_PROCESS_NAMES: Dict[str, str] = {
    "BraveHTML":   "brave.exe",
    "ChromeHTML":  "chrome.exe",
    "MSEdgeHTM":   "msedge.exe",
    "OperaStable": "opera.exe",
    "VivaldiHTM":  "vivaldi.exe",
}


def _get_default_prog_id() -> Optional[str]:
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
        )
        prog_id, _ = winreg.QueryValueEx(key, "ProgId")
        winreg.CloseKey(key)
        return prog_id
    except Exception:
        return None


def _resolve_browser() -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Return (exe, user_data_dir, profile, process_name)."""
    prog_id = _get_default_prog_id()
    if prog_id:
        log.info("Default browser ProgId: %s", prog_id)
        for key, (exe, data_dir, profile) in _BROWSER_MAP.items():
            if prog_id.startswith(key):
                proc_name = _PROCESS_NAMES.get(key)
                if Path(exe).exists():
                    return exe, data_dir, profile, proc_name
                log.warning("Expected browser exe not found: %s", exe)
    return None, None, None, None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _kill_browser(process_name: str) -> None:
    """Kill all instances of the browser process so the profile is not locked."""
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/IM", process_name],
            capture_output=True, text=True
        )
        if "SUCCESS" in result.stdout:
            log.info("Closed existing %s processes.", process_name)
            time.sleep(2)   # give OS time to release the profile lock
    except Exception as exc:
        log.warning("Could not kill %s: %s", process_name, exc)


def _launch_browser_with_debug_port(exe: str, user_data_dir: str) -> subprocess.Popen:
    """Launch the browser with remote debugging enabled, using the real profile."""
    cmd = [
        exe,
        f"--remote-debugging-port={_DEBUG_PORT}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "about:blank",
    ]
    log.info("Launching browser: %s", " ".join(cmd[:3]) + " ...")
    return subprocess.Popen(cmd)


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except json.JSONDecodeError:
            log.warning("Could not parse %s — ignoring.", path)
    return None


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    log.debug("Saved %s", path)


# ── Public API ─────────────────────────────────────────────────────────────────

def login_and_save_cookies(config: ScraperConfig) -> None:
    """Kill the running browser, relaunch it with remote debugging, connect
    via Playwright CDP, wait for manual login, save cookies + Bearer token.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "Playwright is not installed.\n"
            "Run: pip install playwright && playwright install chromium"
        )

    cookies_path = Path(config.auth.cookies_file)
    token_path = Path(config.auth.token_file)
    cookies_path.parent.mkdir(parents=True, exist_ok=True)

    exe, user_data_dir, profile, proc_name = _resolve_browser()

    if not exe:
        raise RuntimeError(
            "Could not detect your default browser.\n"
            "Please set the browser path manually in config.yaml."
        )

    # ── Step 1: close existing browser so profile isn't locked ────────────
    print("\n" + "=" * 70)
    print(f"  Detected browser: {Path(exe).name}")
    print("  Closing any open browser windows (needed to attach to your profile).")
    print("  They will reopen automatically.")
    print("=" * 70)

    if proc_name:
        _kill_browser(proc_name)

    # ── Step 2: relaunch with remote debugging port ────────────────────────
    browser_proc = _launch_browser_with_debug_port(exe, user_data_dir)
    time.sleep(3)   # wait for browser to start and open the debug port

    captured_token: Dict[str, str] = {}

    with sync_playwright() as pw:
        # Connect to the already-running browser via CDP
        try:
            browser = pw.chromium.connect_over_cdp(f"http://localhost:{_DEBUG_PORT}")
        except Exception as exc:
            raise RuntimeError(
                f"Could not connect to browser on port {_DEBUG_PORT}.\n"
                f"Error: {exc}\n"
                "Make sure no other app is using that port."
            )

        log.info("Connected to browser via CDP on port %d.", _DEBUG_PORT)

        # Use the first context (your real profile's context)
        context = browser.contexts[0] if browser.contexts else browser.new_context()

        def _on_request(request: Any) -> None:
            auth = request.headers.get("authorization", "")
            if auth.startswith("Bearer ") and "token" not in captured_token:
                captured_token["token"] = auth.split(" ", 1)[1]
                log.info("[green]Bearer token captured.[/green]")

        context.on("request", _on_request)

        # Open DIKSHA in a new tab
        page = context.new_page()

        print("\n" + "=" * 70)
        print("  Your browser is open with your real profile.")
        print("  Navigating to DIKSHA — please log in with Google.")
        print(f"  You have {config.auth.login_timeout_seconds} seconds.")
        print("  Once you see the DIKSHA home page, come back here.")
        print("=" * 70 + "\n")

        try:
            page.goto(config.auth.login_url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            log.warning("Page load warning (non-fatal): %s", exc)

        # ── Step 3: wait for login ─────────────────────────────────────────
        deadline = time.time() + config.auth.login_timeout_seconds
        logged_in = False
        while time.time() < deadline:
            try:
                current_url = page.url
                if (
                    "diksha.gov.in" in current_url
                    and not any(
                        x in current_url.lower()
                        for x in ("login", "signin", "auth", "accounts.google")
                    )
                ):
                    log.info("Login confirmed at: %s", current_url)
                    logged_in = True
                    break
            except Exception:
                pass
            time.sleep(3)

        if not logged_in:
            log.warning("Login timeout — saving whatever session state exists.")

        # Give SPA time to fire API calls so we capture the token
        time.sleep(4)

        # ── Step 4: save cookies ───────────────────────────────────────────
        raw_cookies: List[Dict[str, Any]] = context.cookies()
        _save_json(cookies_path, raw_cookies)
        log.info("Saved %d cookies to %s", len(raw_cookies), cookies_path)

        # ── Step 5: save token ─────────────────────────────────────────────
        if captured_token:
            _save_json(token_path, captured_token)
            log.info("Saved Bearer token to %s", token_path)
        else:
            log.warning(
                "No Bearer token captured — API will rely on cookies only.\n"
                "If you get 401 errors, re-run --login."
            )

        browser.close()

    # Kill the debug-port instance; user can reopen Brave normally after this
    try:
        browser_proc.terminate()
    except Exception:
        pass

    # ── Step 6: verify ─────────────────────────────────────────────────────
    session = get_authenticated_session(config)
    _verify_session(session, config)


def get_authenticated_session(config: ScraperConfig) -> requests.Session:
    """Build a ``requests.Session`` loaded with saved cookies and JWT token."""
    cookies_path = Path(config.auth.cookies_file)
    token_path = Path(config.auth.token_file)

    session = requests.Session()
    session.headers.update(_REQUIRED_HEADERS)
    session.headers["User-Agent"] = config.scraper.user_agent

    raw_cookies = _load_json(cookies_path)
    if raw_cookies:
        for c in raw_cookies:
            session.cookies.set(
                c["name"],
                c["value"],
                domain=c.get("domain", ".diksha.gov.in"),
                path=c.get("path", "/"),
            )
        log.info("Loaded %d cookies into session.", len(raw_cookies))
    else:
        log.warning("No cookies found at %s — run --login first.", cookies_path)

    token_data = _load_json(token_path)
    if token_data and token_data.get("token"):
        session.headers["Authorization"] = f"Bearer {token_data['token']}"
        log.info("Bearer token loaded into session.")
    else:
        log.warning("No Bearer token found at %s.", token_path)

    return session


def _verify_session(session: requests.Session, config: ScraperConfig) -> bool:
    """Quick sanity-check against the search API."""
    payload = {
        "request": {
            "filters": {"primaryCategory": "Digital Textbook"},
            "limit": 1,
            "offset": 0,
        }
    }
    try:
        resp = session.post(
            config.search_url,
            json=payload,
            timeout=config.scraper.request_timeout_seconds,
        )
        if resp.status_code == 200:
            log.info("[green]Session verified — API returned HTTP 200.[/green]")
            return True
        elif resp.status_code == 401:
            log.error(
                "API returned 401 Unauthorized.\n"
                "Please complete login fully (including any MFA) and re-run --login."
            )
        else:
            log.warning("Session check returned HTTP %d — proceeding anyway.", resp.status_code)
    except requests.RequestException as exc:
        log.warning("Session verification failed: %s", exc)
    return False
