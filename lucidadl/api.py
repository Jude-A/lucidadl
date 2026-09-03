"""
lucida.to client over plain HTTP (httpx). The browser is NOT used here at all — it
only ever runs (elsewhere) to obtain/refresh the Cloudflare cf_clearance cookie, which
is then carried by httpx. This keeps downloads fast and RAM-light (no Chromium open).

Flow (all httpx, like the Rust jelni client):
  search(query)          -> GET /search, parse the SvelteKit JSON5 blob -> results
  fetch_page_data(url)    -> GET /?url=..., parse blob -> token + every track (csrf)
  start_download(track)   -> POST /api/load -> {handoff, server}
  run_job(handoff,server) -> poll <server>.lucida.to (Cloudflare-free) -> stream file

Apple Music playlist scraping (applemusic_tracklist) DOES need a page — it's not a
lucida service — so it's a module function taking a Playwright page.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from . import paths, utils

LUCIDA = "https://lucida.to"
STREAM_ACTION = "/api/fetch/stream/v2"

# SvelteKit data blob delimiters in the RAW HTML (item page AND search page).
_PD_START = ',{"type":"data","data":'
_PD_END = ',"uses":{"url":1}}];'

SERVICE_ALIASES = {"amazon_music": "amazon", "yandex_music": "yandex"}
# Qobuz on lucida.to currently only accepts "US"; Amazon works WITHOUT a country.
COUNTRY_DEFAULTS = {"qobuz": "US", "amazon": "", "deezer": "FR"}
# Services tried (in order) when the primary one finds nothing (unless --strict).
FALLBACK_SERVICES = ["qobuz", "amazon"]

# Values of the #convert <select> (also the lucida downscale strings).
DOWNSCALE_CHOICES = ["original", "flac", "mp3", "ogg-vorbis", "opus", "m4a-aac", "wav"]

_EXT_BY_CTYPE = {
    "audio/flac": "flac", "audio/x-flac": "flac", "audio/mpeg": "mp3", "audio/mp3": "mp3",
    "audio/mp4": "m4a", "audio/m4a": "m4a", "audio/aac": "m4a", "audio/ogg": "ogg",
    "audio/opus": "opus", "audio/wav": "wav", "audio/x-wav": "wav", "application/zip": "zip",
}


class LucidaError(RuntimeError):
    pass


def normalize_service(service: str) -> str:
    s = (service or "").lower()
    return SERVICE_ALIASES.get(s, s)


def default_country(service: str) -> str:
    return COUNTRY_DEFAULTS.get(normalize_service(service), "US")


class LucidaClient:
    """All lucida.to calls over httpx, carrying the cf_clearance cookie + Chrome UA."""

    def __init__(self, cf_clearance: Optional[str], user_agent: str,
                 acquire: Optional[Callable[[], Awaitable[Tuple[str, str]]]] = None,
                 country: str = "US", downscale: str = "original", metadata: bool = True,
                 private: bool = False, jobs: int = 6, log=print):
        self.cf = cf_clearance
        self.ua = user_agent
        self.acquire = acquire           # async () -> (cf, ua), opens a browser briefly
        self.country = country
        self.downscale = downscale
        self.metadata = metadata
        self.private = private
        self.jobs = jobs
        self.log = log
        self.http = None
        self._claimed = set()
        self._refresh_lock = asyncio.Lock()
        self._cf_gen = 0  # bumped on each successful refresh (dedupes concurrent 403s)

    async def start_http(self) -> None:
        import httpx

        self.http = httpx.AsyncClient(
            headers=self._headers(), http2=True, follow_redirects=True,
            timeout=httpx.Timeout(60.0, read=600.0),
            limits=httpx.Limits(max_connections=max(8, self.jobs * 2),
                                max_keepalive_connections=max(8, self.jobs)),
        )

    def _headers(self) -> Dict[str, str]:
        h = {"User-Agent": self.ua}
        if self.cf:
            h["Cookie"] = f"cf_clearance={self.cf}"
        return h

    async def aclose(self) -> None:
        if self.http is not None:
            try:
                await self.http.aclose()
            except Exception:
                pass

    async def _refresh_creds(self) -> bool:
        """Re-obtain cf_clearance via the browser. Deduped: N concurrent 403s open the
        browser exactly ONCE (a generation counter short-circuits late callers)."""
        if not self.acquire:
            return False
        gen = self._cf_gen  # snapshot before queueing on the lock
        async with self._refresh_lock:
            if self._cf_gen != gen:
                return True  # another task already refreshed while we waited
            try:
                cf, ua = await self.acquire()
            except Exception as e:
                self.log(f"  ⚠ Cloudflare refresh failed: {e}")
                return False
            self.cf, self.ua = cf, ua
            if self.http is not None:
                self.http.headers["User-Agent"] = ua
                if cf:
                    self.http.headers["Cookie"] = f"cf_clearance={cf}"
                else:
                    self.http.headers.pop("Cookie", None)
            self._cf_gen += 1
            return True

    async def _get(self, url: str, **kw):
        """GET with one Cloudflare-refresh retry on 403."""
        r = await self.http.get(url, **kw)
        if r.status_code == 403 and await self._refresh_creds():
            r = await self.http.get(url, **kw)
        return r

    # -- search (httpx) ------------------------------------------------------

    async def search(self, query: str, service: str) -> Dict[str, List[Dict[str, Any]]]:
        import pyjson5

        svc = normalize_service(service)
        cc = default_country(svc)
        params = {"service": svc}
        if cc:
            params["country"] = cc
        params["query"] = query
        self.log(f"  searching: {query!r} on {svc}" + (f" ({cc})" if cc else ""))
        r = await self._get(LUCIDA + "/search", params=params)
        blob = _between(r.text, _PD_START, _PD_END)
        if not blob:
            self.log(f"  (no search data; status {r.status_code})")
            return {"tracks": [], "albums": [], "artists": []}
        try:
            data = pyjson5.loads(blob)
        except Exception as e:
            self.log(f"  (search parse: {e})")
            return {"tracks": [], "albums": [], "artists": []}
        return _extract_search_results(data)

    # -- item page -> token + tracks ----------------------------------------

    async def fetch_page_data(self, svc_url: str, country: Optional[str] = None) -> Dict[str, Any]:
        import pyjson5

        cc = country if country is not None else self.country
        params = {"url": svc_url}
        if cc:
            params["country"] = cc
        r = await self._get(LUCIDA + "/", params=params)
        blob = _between(r.text, _PD_START, _PD_END)
        if not blob:
            raise LucidaError(f"token not found (status {r.status_code}, format changed?)")
        try:
            return pyjson5.loads(blob)
        except Exception as e:
            raise LucidaError(f"parse page data: {e}")

    @staticmethod
    def tracks_from_pd(pd: Dict[str, Any]) -> List[Dict[str, Any]]:
        info = pd.get("info", {}) or {}
        if info.get("type") == "album":
            return list(info.get("tracks", []) or [])
        t = dict(info)
        t.setdefault("csrf", pd.get("token"))
        t.setdefault("csrfFallback", None)
        return [t]

    # -- download (POST /api/load -> poll -> stream) ------------------------

    async def start_download(self, track: Dict[str, Any], expiry: Any,
                             country: Optional[str] = None) -> Tuple[str, str]:
        cc = country if country is not None else self.country
        body = {
            "account": {"id": cc or "auto", "type": "country"}, "compat": False,
            "downscale": self.downscale, "handoff": True, "metadata": self.metadata,
            "private": self.private,
            "token": {"expiry": expiry, "primary": track.get("csrf"),
                      "secondary": track.get("csrfFallback")},
            "upload": {"enabled": False}, "url": track["url"],
        }
        for attempt in range(5):
            r = await self.http.post(LUCIDA + "/api/load", params={"url": STREAM_ACTION}, json=body)
            if r.status_code == 403:
                if not await self._refresh_creds():
                    break
                await asyncio.sleep(2)
                continue
            try:
                j = r.json()
            except Exception:
                j = {}
            if r.status_code == 200 and j.get("handoff") and j.get("server"):
                return j["handoff"], j["server"]
            err = (j.get("error") if isinstance(j, dict) else None) or r.text[:150]
            self.log(f"    /api/load ({r.status_code}): {err}")
            await asyncio.sleep(5)
        raise LucidaError("/api/load failed")

    async def run_job(self, handoff: str, server: str, dest_dir: str, base_name: str,
                      title: str = "", timeout: int = 1800,
                      on_status=None, on_bytes=None) -> str:
        base = f"https://{server}.lucida.to/api/fetch/request/{handoff}"
        deadline = time.time() + timeout
        last_msg = None
        last_state = None
        last_change = time.time()
        while time.time() < deadline:
            s = await self.http.get(base)
            if s.status_code in (404, 500):
                raise LucidaError(f"poll HTTP {s.status_code}")
            try:
                st = s.json()
            except Exception:
                st = {}
            status = str(st.get("status", ""))
            msg = str(st.get("message", "")).replace("{item}", title)
            if status == "completed":
                break
            if status == "error":
                raise LucidaError(f"lucida server: {msg or 'error'}")
            if msg and msg != last_msg:
                last_msg = msg
                if on_status:
                    on_status(msg)
                else:
                    self.log(f"    … {msg}")
            state = (status, msg)
            if state != last_state:
                last_state, last_change = state, time.time()
            elif time.time() - last_change >= 40:
                raise LucidaError("stuck (>40s without progress)")
            await asyncio.sleep(1)
        else:
            raise LucidaError("poll: timed out")

        os.makedirs(_long(dest_dir), exist_ok=True)
        async with self.http.stream("GET", base + "/download") as resp:
            if resp.status_code != 200:
                raise LucidaError(f"download HTTP {resp.status_code}")
            headers = {k.lower(): v for k, v in resp.headers.items()}
            ext = _EXT_BY_CTYPE.get(headers.get("content-type", "").split(";")[0].strip().lower(), "flac")
            fname = _filename_from_cd(headers.get("content-disposition", "")) or f"{base_name}.{ext}"
            if "." not in os.path.basename(fname):
                fname = f"{fname}.{ext}"
            dest = self._unique_dest(dest_dir, fname)
            part = _long(dest + ".part")
            try:
                total = None
                try:
                    total = int(headers.get("content-length")) or None
                except (TypeError, ValueError):
                    total = None
                done = 0
                if on_bytes:
                    on_bytes(0, total)
                with open(part, "wb") as f:
                    async for chunk in resp.aiter_bytes(1 << 16):
                        f.write(chunk)
                        if on_bytes:
                            done += len(chunk)
                            on_bytes(done, total)
                os.replace(part, _long(dest))
            except BaseException as e:  # OSError, httpx errors, CancelledError…
                self._claimed.discard(dest)  # free the name so a retry reuses it
                try:
                    os.remove(part)
                except OSError:
                    pass
                if isinstance(e, OSError):
                    raise LucidaError(f"write: {e}")
                raise
        return dest

    def _unique_dest(self, dest_dir: str, fname: str) -> str:
        base = os.path.join(dest_dir, utils.sanitize_filename(fname))
        root, ext = os.path.splitext(base)
        cand, i = base, 1
        while cand in self._claimed or os.path.exists(_long(cand)):
            cand = f"{root} ({i}){ext}"
            i += 1
        self._claimed.add(cand)
        return cand


# --- search blob navigation -------------------------------------------------

def _extract_search_results(data: Any) -> Dict[str, List[Dict[str, Any]]]:
    out = {"tracks": [], "albums": [], "artists": []}
    node = _find_results_node(data)
    if not node:
        return out
    for kind in ("tracks", "albums"):
        for it in node.get(kind, []) or []:
            if not isinstance(it, dict) or not it.get("url"):
                continue
            artist = ", ".join(a.get("name", "") for a in (it.get("artists") or [])
                               if isinstance(a, dict) and a.get("name"))
            alb = it.get("album") if isinstance(it.get("album"), dict) else {}
            album = alb.get("title", "")
            out[kind].append({
                "url": it["url"], "title": it.get("title", ""), "artist": artist,
                "album": album, "context": f"{it.get('title', '')} {artist} {album}".strip(),
            })
    return out


def _find_results_node(obj: Any, depth: int = 0) -> Optional[Dict[str, Any]]:
    """Find the dict holding the search result lists, wherever it sits in the blob."""
    if depth > 8:
        return None
    if isinstance(obj, dict):
        for k in ("tracks", "albums"):
            v = obj.get(k)
            if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                return obj
        for v in obj.values():
            r = _find_results_node(v, depth + 1)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_results_node(v, depth + 1)
            if r:
                return r
    return None


# --- Apple Music playlist scraping (needs a Playwright page) ----------------

_APPLE_ROWS_JS = """() => Array.from(document.querySelectorAll('.songs-list-row')).map(r => {
  const n = r.querySelector('.songs-list-row__song-name');
  const b = r.querySelector('.songs-list-row__by-line');
  return { title: n ? n.textContent : '', artist: b ? b.textContent : '' };
})"""

_APPLE_SCROLL_JS = """() => {
  let el = document.querySelector('.songs-list-row');
  while (el) {
    const s = getComputedStyle(el);
    if (/(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight + 10) break;
    el = el.parentElement;
  }
  const t = el || document.scrollingElement || document.documentElement;
  const step = Math.max(200, Math.round((t.clientHeight || window.innerHeight) * 0.7));
  t.scrollTop += step;
  return { top: t.scrollTop, h: t.scrollHeight, c: t.clientHeight };
}"""


async def applemusic_tracklist(page, url: str, log=print):
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    except Exception as e:
        log(f"  Apple Music navigation: {e}")
    await page.wait_for_timeout(1500)
    await _dismiss_consent(page)
    try:
        await page.wait_for_selector(".songs-list-row", timeout=30_000)
    except Exception:
        pass

    name = await _playlist_name(page)
    tracks: List[Dict[str, str]] = []
    seen = set()
    stable = 0
    for _ in range(600):
        try:
            rows = await page.evaluate(_APPLE_ROWS_JS)
        except Exception:
            rows = []
        new = 0
        for t in rows:
            title = (t.get("title") or "").strip()
            artist = (t.get("artist") or "").strip()
            if not title:
                continue
            key = (artist.lower(), title.lower())
            if key not in seen:
                seen.add(key)
                tracks.append({"title": title, "artist": artist})
                new += 1
        if new:
            log(f"    … {len(tracks)} titles")
        try:
            pos = await page.evaluate(_APPLE_SCROLL_JS)
        except Exception:
            pos = None
        await page.wait_for_timeout(300)
        at_bottom = bool(pos) and (pos["top"] + pos["c"] >= pos["h"] - 8)
        stable = stable + 1 if new == 0 else 0
        if (at_bottom and stable >= 3) or stable >= 30:
            break
    return name, tracks


async def _playlist_name(page) -> str:
    """Best-effort playlist title from the page (heading, else document.title)."""
    for sel in ('[data-testid="non-editable-product-title"]', ".headings__title",
                "h1.product-name", "h1"):
        try:
            el = page.locator(sel).first
            if await el.count():
                t = (await el.inner_text()).strip()
                if t:
                    return " ".join(t.split())[:120]
        except Exception:
            continue
    try:
        t = (await page.title()).strip()
        for suf in (" on Apple Music", " - playlist by "):
            i = t.find(suf)
            if i > 0:
                t = t[:i]
        return " ".join(t.split()).strip(" -|·")[:120]
    except Exception:
        return ""


async def _dismiss_consent(page) -> None:
    for lbl in ("Accepter", "Tout accepter", "J'accepte", "Accept", "Accept All",
                "Agree", "I Agree", "Continue", "Continuer"):
        try:
            btn = page.get_by_role("button", name=lbl, exact=False)
            if await btn.count():
                await btn.first.click(timeout=2000)
                await page.wait_for_timeout(800)
                return
        except Exception:
            continue


async def playlist_tracklist(page, url: str, log=print):
    """Scrape a public Apple Music playlist -> (name, [{title, artist}])."""
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if host != "music.apple.com" and not host.endswith(".music.apple.com"):
        log("  unsupported playlist URL — paste a public music.apple.com playlist link")
        return "", []
    name, tracks = await applemusic_tracklist(page, url, log)
    if not tracks:
        await _dump_playlist_debug(page)
    return name, tracks


async def _dump_playlist_debug(page) -> None:
    """Best-effort capture for support, kept with other app data (never in the cwd)."""
    try:
        out = os.path.join(paths.DATA_DIR, "applemusic_debug.html")
        with open(out, "w", encoding="utf-8") as f:
            f.write(await page.content())
        await page.screenshot(path=os.path.join(paths.DATA_DIR, "applemusic_debug.png"))
    except Exception:
        pass


# --- module helpers ---------------------------------------------------------

def _between(text: str, start: str, end: str) -> Optional[str]:
    i = text.find(start)
    if i < 0:
        return None
    i += len(start)
    j = text.find(end, i)
    return text[i:j] if j > 0 else None


def _filename_from_cd(cd: str) -> Optional[str]:
    from urllib.parse import unquote
    if not cd:
        return None
    m = re.search(r"filename\*\s*=\s*(?:UTF-8'')?\"?([^\";]+)", cd, re.I) or \
        re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
    return unquote(m.group(1)).strip() if m else None


def _long(path: str) -> str:
    """Windows extended-length path so total length can exceed MAX_PATH (260)."""
    if os.name == "nt":
        ap = os.path.abspath(path)
        if not ap.startswith("\\\\?\\"):
            return "\\\\?\\" + ap
    return path


def _apple_tracks_from_obj(obj: Any, out: List[Dict[str, str]]) -> None:
    """Collect songs from an Apple Music amp-api JSON object (kept for tests)."""
    if isinstance(obj, dict):
        attrs = obj.get("attributes")
        if isinstance(attrs, dict):
            name, artist = attrs.get("name"), attrs.get("artistName")
            if name and artist and obj.get("type") in (None, "songs", "library-songs", "music-videos"):
                out.append({"title": str(name), "artist": str(artist)})
        for v in obj.values():
            _apple_tracks_from_obj(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _apple_tracks_from_obj(v, out)
