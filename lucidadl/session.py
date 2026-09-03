"""
Async browser session manager for lucida.to.

lucida.to is behind Cloudflare; headless is challenged, so we drive a *headed*,
*persistent* Chromium (profile in .userdata/profile) whose cf_clearance cookie is
reused across runs. One context is shared by all tabs, so concurrent downloads
ride the same Cloudflare clearance.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional, Tuple

from playwright.async_api import (
    async_playwright, BrowserContext, Page, Error as PlaywrightError,
)

from . import paths

LUCIDA = "https://lucida.to"
USER_DATA_DIR = paths.PROFILE_DIR
CLEARANCE_PATH = paths.CLEARANCE_PATH

_CHALLENGE_TITLES = (
    "just a moment", "attention required", "un instant", "checking your browser",
    "verifying", "verification",
)
# Strong interstitial markers only (NOT the persistent /cdn-cgi/challenge-platform/
# script which Cloudflare also injects on already-cleared pages).
_CHALLENGE_BODY = (
    'id="challenge-form"', 'cf-chl-widget', 'window._cf_chl_opt',
    'enable javascript and cookies to continue',
)


class BrowserClosed(RuntimeError):
    """The browser window/context closed unexpectedly."""


async def chromium_installed() -> bool:
    """Return whether Playwright's matching Chromium build is available."""
    try:
        async with async_playwright() as pw:
            return os.path.exists(pw.chromium.executable_path)
    except Exception:
        return False


async def install_chromium() -> bool:
    """Install Chromium into the current Python/pipx environment."""
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "playwright", "install", "chromium"
        )
        return await proc.wait() == 0
    except Exception:
        return False


def _title_is_challenge(title: str) -> bool:
    t = (title or "").lower()
    return any(s in t for s in _CHALLENGE_TITLES) or t.strip() == "moment"


async def is_challenged(page: Page) -> bool:
    try:
        if _title_is_challenge(await page.title()):
            return True
    except Exception:
        return True
    try:
        html = (await page.content() or "").lower()
    except Exception:
        return True
    return any(m in html for m in _CHALLENGE_BODY)


@asynccontextmanager
async def lucida_context(headless: bool = False, channel: Optional[str] = None,
                        downloads_dir: Optional[str] = None, hidden: bool = False
                        ) -> AsyncIterator[BrowserContext]:
    """A real (headed) Chromium passes Cloudflare; true headless is challenged and
    fails. `hidden=True` keeps it headed but moves the window off-screen, so it runs
    invisibly (in the background) while still clearing Cloudflare. It still needs a
    logged-in desktop session."""
    channel = channel or os.environ.get("LUCIDA_CHANNEL") or None
    os.makedirs(USER_DATA_DIR, exist_ok=True)
    if downloads_dir:
        os.makedirs(downloads_dir, exist_ok=True)

    args = ["--disable-blink-features=AutomationControlled", "--no-first-run"]
    if hidden:
        headless = False  # off-screen headed clears Cloudflare; true headless does not
        args += ["--window-position=-32000,-32000", "--window-size=1366,900"]

    base_kwargs = dict(
        user_data_dir=USER_DATA_DIR,
        headless=headless,
        accept_downloads=True,
        viewport={"width": 1366, "height": 900},
        args=args,
    )
    async with async_playwright() as pw:
        ctx = None
        if channel:
            try:
                ctx = await pw.chromium.launch_persistent_context(channel=channel, **base_kwargs)
            except Exception:
                ctx = None  # fall back to bundled Chromium
        if ctx is None:
            ctx = await pw.chromium.launch_persistent_context(**base_kwargs)
        ctx.set_default_timeout(60_000)
        try:
            yield ctx
        finally:
            try:
                await ctx.close()
            except Exception:
                pass


async def ensure_cleared(ctx: BrowserContext, timeout: int = 150) -> bool:
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto(LUCIDA + "/", wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightError:
            pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not await is_challenged(page):
                return True
            await page.wait_for_timeout(1500)
        return not await is_challenged(page)
    except PlaywrightError as e:
        if any(s in str(e) for s in ("closed", "Closed", "crash")):
            raise BrowserClosed(str(e))
        raise


async def get_page(ctx: BrowserContext) -> Page:
    return ctx.pages[0] if ctx.pages else await ctx.new_page()


# --- Cloudflare clearance store (so downloads run over httpx, no browser open) ---

def save_clearance(cf: Optional[str], ua: str) -> None:
    try:
        os.makedirs(os.path.dirname(CLEARANCE_PATH), exist_ok=True)
        with open(CLEARANCE_PATH, "w", encoding="utf-8") as f:
            json.dump({"cf_clearance": cf, "ua": ua, "ts": time.time()}, f)
    except Exception:
        pass


def load_clearance() -> Tuple[Optional[str], Optional[str]]:
    try:
        with open(CLEARANCE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d.get("cf_clearance"), d.get("ua")
    except Exception:
        return None, None


async def acquire_clearance(hidden: bool = False) -> Tuple[str, str]:
    """Open the browser briefly, clear Cloudflare, harvest cf_clearance + UA, save, close.
    Raises BrowserClosed / RuntimeError on failure."""
    async with lucida_context(hidden=hidden) as ctx:
        if not await ensure_cleared(ctx, timeout=180):
            raise RuntimeError("Cloudflare not cleared")
        cookies = await ctx.cookies("https://lucida.to")
        cf = next((c["value"] for c in cookies if c["name"] == "cf_clearance"), None)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        ua = (await page.evaluate("() => navigator.userAgent")).replace("HeadlessChrome", "Chrome")
    if not cf:
        raise RuntimeError("cf_clearance missing after clearing Cloudflare")
    save_clearance(cf, ua)
    return cf, ua
