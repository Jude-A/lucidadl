"""
lucida.to client over plain HTTP (httpx). The browser is NOT used here at all — it
only ever runs (elsewhere) to obtain/refresh the Cloudflare cf_clearance cookie, which
is then carried by httpx. This keeps downloads fast and RAM-light (no Chromium open).

Flow (all httpx, like the Rust jelni client):
  search(query)          -> GET /search, parse the SvelteKit JSON5 blob -> results
  fetch_page_data(url)    -> GET /?url=..., parse blob -> token + every track (csrf)
  start_download(track)   -> POST /api/load -> {handoff, server}
  run_job(handoff,server) -> poll <server>.lucida.to (Cloudflare-free) -> stream file

Public Spotify and Deezer playlist metadata is normally read directly over HTTP.
Apple Music and Spotify playlists beyond the public player's first window use a
Playwright page because their complete track lists are rendered dynamically.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import os
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

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
PLAYLIST_SOURCE_NAMES = {
    "apple": "Apple Music",
    "spotify": "Spotify",
    "deezer": "Deezer",
}

# Values of the #convert <select> (also the lucida downscale strings).
DOWNSCALE_CHOICES = ["original", "flac", "mp3", "ogg-vorbis", "opus", "m4a-aac", "wav"]

_EXT_BY_CTYPE = {
    "audio/flac": "flac", "audio/x-flac": "flac", "audio/mpeg": "mp3", "audio/mp3": "mp3",
    "audio/mp4": "m4a", "audio/m4a": "m4a", "audio/aac": "m4a", "audio/ogg": "ogg",
    "audio/opus": "opus", "audio/wav": "wav", "audio/x-wav": "wav", "application/zip": "zip",
}


class LucidaError(RuntimeError):
    pass


class SpotifyPlaylistWindow(LucidaError):
    """The fast public player exposed only its first 100 playlist items."""

    def __init__(self, name: str, total: int):
        self.name = name
        self.total = total
        detail = f"100 of {total} titles" if total else "its first 100 titles"
        super().__init__(f"Spotify's public player exposed {detail}")


def _retry_delay(response, attempt: int) -> int:
    """Small bounded backoff, honoring Retry-After when a server supplies it."""
    try:
        return min(30, max(1, int(response.headers.get("Retry-After", ""))))
    except (TypeError, ValueError):
        return min(8, 2 ** attempt)


def normalize_service(service: str) -> str:
    s = (service or "").lower()
    return SERVICE_ALIASES.get(s, s)


def default_country(service: str) -> str:
    return COUNTRY_DEFAULTS.get(normalize_service(service), "US")


def playlist_source(url: str) -> str:
    """Return the supported public-playlist source identified by its canonical URL."""
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    parts = [part.lower() for part in parsed.path.split("/") if part]
    if ((host == "music.apple.com" or host.endswith(".music.apple.com")) and
            "playlist" in parts):
        return "apple"
    if host in ("open.spotify.com", "spotify.com", "www.spotify.com") and "playlist" in parts:
        return "spotify"
    if host in ("deezer.com", "www.deezer.com") and "playlist" in parts:
        return "deezer"
    return ""


def playlist_source_name(source: str) -> str:
    return PLAYLIST_SOURCE_NAMES.get(source, source or "unknown source")


def is_apple_playlist_url(url: str) -> bool:
    return playlist_source(url) == "apple"


def _playlist_id(url: str) -> str:
    parts = [part for part in urlparse((url or "").strip()).path.split("/") if part]
    for index, part in enumerate(parts[:-1]):
        if part.lower() == "playlist":
            return parts[index + 1]
    return ""


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
        """GET with one Cloudflare refresh and bounded transient retries."""
        refreshed = False
        for attempt in range(3):
            try:
                r = await self.http.get(url, **kw)
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** attempt)
                continue
            if r.status_code == 403 and not refreshed:
                refreshed = True
                if await self._refresh_creds():
                    continue
            if r.status_code == 429 or r.status_code in (500, 502, 503, 504):
                if attempt < 2:
                    await asyncio.sleep(_retry_delay(r, attempt))
                    continue
            return r
        raise LucidaError("GET failed after retrying")

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
        last_error = "/api/load failed"
        refreshed = False
        for attempt in range(5):
            try:
                r = await self.http.post(
                    LUCIDA + "/api/load", params={"url": STREAM_ACTION}, json=body)
            except Exception as exc:
                last_error = f"/api/load network error: {exc}"
                if attempt == 4:
                    break
                await asyncio.sleep(min(8, 2 ** attempt))
                continue
            if r.status_code == 403 and not refreshed:
                refreshed = True
                if await self._refresh_creds():
                    await asyncio.sleep(1)
                    continue
            try:
                j = r.json()
            except Exception:
                j = {}
            if r.status_code == 200 and j.get("handoff") and j.get("server"):
                return j["handoff"], j["server"]
            err = (j.get("error") if isinstance(j, dict) else None) or r.text[:150]
            last_error = f"/api/load HTTP {r.status_code}: {err}"
            self.log(f"    /api/load ({r.status_code}): {err}")
            if r.status_code in (400, 401, 404, 422) or (r.status_code == 403 and refreshed):
                break
            await asyncio.sleep(_retry_delay(r, attempt))
        raise LucidaError(last_error)

    async def run_job(self, handoff: str, server: str, dest_dir: str, base_name: str,
                      title: str = "", timeout: int = 1800,
                      on_status=None, on_bytes=None) -> str:
        base = f"https://{server}.lucida.to/api/fetch/request/{handoff}"
        deadline = time.time() + timeout
        last_msg = None
        last_state = None
        last_change = time.time()
        transient_errors = 0
        while time.time() < deadline:
            s = await self.http.get(base)
            if s.status_code == 429 or s.status_code in (500, 502, 503, 504):
                transient_errors += 1
                if transient_errors > 3:
                    raise LucidaError(f"poll HTTP {s.status_code} after retrying")
                if on_status:
                    on_status(f"server HTTP {s.status_code} — retrying")
                await asyncio.sleep(_retry_delay(s, transient_errors - 1))
                continue
            transient_errors = 0
            if s.status_code == 404:
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
                        done += len(chunk)
                        if on_bytes:
                            on_bytes(done, total)
                if total is not None and done != total:
                    raise LucidaError(f"incomplete download ({done}/{total} bytes)")
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


# --- public playlist sources ------------------------------------------------

async def _public_get(url: str):
    """Fetch a public playlist page/API with the same small retry policy as lucida."""
    import httpx

    headers = {
        # Spotify's public SEO response includes the declared playlist size for this
        # neutral browser UA; a detailed Chrome UA receives only the web-app shell.
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "en-US,en;q=0.8",
    }
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30.0) as client:
        for attempt in range(3):
            try:
                response = await client.get(url)
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** attempt)
                continue
            if response.status_code == 429 or response.status_code in (500, 502, 503, 504):
                if attempt < 2:
                    await asyncio.sleep(_retry_delay(response, attempt))
                    continue
            response.raise_for_status()
            return response
    raise LucidaError("public playlist request failed after retrying")


def _spotify_playlist_from_html(raw: str) -> Tuple[str, List[Dict[str, str]]]:
    """Parse the stable JSON payload shipped with Spotify's public embed player."""
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        raw or "", re.I | re.S,
    )
    if not match:
        raise LucidaError("Spotify's public playlist data was not found (page format changed?)")
    try:
        data = json.loads(html_lib.unescape(match.group(1)))
        entity = data["props"]["pageProps"]["state"]["data"]["entity"]
    except (KeyError, TypeError, ValueError) as exc:
        raise LucidaError(f"Spotify playlist data could not be read: {exc}") from exc
    if not isinstance(entity, dict) or str(entity.get("type") or "").lower() != "playlist":
        raise LucidaError("Spotify did not return a public playlist")
    name = " ".join(str(entity.get("name") or entity.get("title") or "").split())
    tracks: List[Dict[str, str]] = []
    for item in entity.get("trackList") or []:
        if not isinstance(item, dict) or item.get("entityType") not in (None, "track"):
            continue
        title = " ".join(str(item.get("title") or "").split())
        artist = " ".join(str(item.get("subtitle") or "").replace("\xa0", " ").split())
        if title and artist:
            tracks.append({"title": title, "artist": artist})
    return name, tracks


def _spotify_total_from_html(raw: str) -> Optional[int]:
    """Read the public page's declared item count, used to detect the embed's 100 cap."""
    for match in re.finditer(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            raw or "", re.I | re.S):
        try:
            data = json.loads(html_lib.unescape(match.group(1)))
        except (TypeError, ValueError):
            continue
        description = str(data.get("description") or "") if isinstance(data, dict) else ""
        count = re.search(r"\b(\d[\d ,.]*)\s+(?:items?|songs?|tracks?)\b",
                          description, re.I)
        if count:
            digits = re.sub(r"\D", "", count.group(1))
            if digits:
                return int(digits)
    return None


async def spotify_tracklist(url: str, log=print) -> Tuple[str, List[Dict[str, str]]]:
    playlist_id = _playlist_id(url)
    if not playlist_id:
        raise LucidaError("Spotify playlist ID not found in the URL")
    embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
    response = await _public_get(embed_url)
    name, tracks = _spotify_playlist_from_html(response.text)
    if len(tracks) == 100:
        public = await _public_get(f"https://open.spotify.com/playlist/{playlist_id}")
        total = _spotify_total_from_html(public.text)
        if total and total > len(tracks):
            raise SpotifyPlaylistWindow(name, total)
        if total is None:
            raise SpotifyPlaylistWindow(name, 0)
    return name, tracks


_SPOTIFY_VISIBLE_ROWS_JS = """() => {
    const grid = document.querySelector('[data-testid="playlist-tracklist"][role="grid"]');
    if (!grid) return [];
    return Array.from(grid.querySelectorAll('[role="row"][aria-rowindex]')).map(row => {
        const position = Number(row.getAttribute('aria-rowindex')) - 1;
        const track = row.querySelector(
            'a[data-testid="internal-track-link"], a[href*="/track/"]'
        );
        const episode = row.querySelector('a[href*="/episode/"]');
        const artists = Array.from(row.querySelectorAll(
            '[role="gridcell"][aria-colindex="2"] a[href*="/artist/"]'
        )).map(node => (node.textContent || '').trim()).filter(Boolean);
        return {
            position,
            kind: track ? 'track' : (episode ? 'episode' : 'pending'),
            title: track ? (track.textContent || '').trim() : '',
            artist: [...new Set(artists)].join(', '),
        };
    });
}"""

_SPOTIFY_SCROLL_JS = """target => {
    const grid = document.querySelector('[data-testid="playlist-tracklist"][role="grid"]');
    if (!grid) return null;
    let node = grid.parentElement;
    while (node && !(node.scrollHeight > node.clientHeight + 50 &&
           ['auto', 'scroll'].includes(getComputedStyle(node).overflowY))) {
        node = node.parentElement;
    }
    if (!node) return null;
    node.scrollTop = Math.min(target, node.scrollHeight - node.clientHeight);
    node.dispatchEvent(new Event('scroll', {bubbles: true}));
    return {top: node.scrollTop, height: node.scrollHeight, viewport: node.clientHeight};
}"""


async def spotify_browser_tracklist(page, url: str, name: str, total: int,
                                     log=print) -> Tuple[str, List[Dict[str, str]]]:
    """Read a long public playlist by scrolling Spotify's own paginated web view."""
    await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    try:
        await page.wait_for_selector(
            '[data-testid="playlist-tracklist"] a[href*="/track/"]', timeout=30_000
        )
        await page.wait_for_function("""() => {
            const grid = document.querySelector(
                '[data-testid="playlist-tracklist"][role="grid"]'
            );
            let node = grid && grid.parentElement;
            while (node && !(node.scrollHeight > node.clientHeight + 50 &&
                   ['auto', 'scroll'].includes(getComputedStyle(node).overflowY))) {
                node = node.parentElement;
            }
            return Boolean(node);
        }""", timeout=30_000)
    except Exception as exc:
        raise LucidaError("Spotify's public track list did not become available") from exc

    try:
        rendered_count = await page.locator(
            '[data-testid="playlist-tracklist"]'
        ).get_attribute("aria-rowcount")
        rendered_total = max(0, int(rendered_count or 0) - 1)
    except (TypeError, ValueError):
        rendered_total = 0
    if rendered_total:
        total = rendered_total
    if not total:
        raise LucidaError("Spotify's public playlist size could not be determined")

    found: Dict[int, Dict[str, str]] = {}
    observed = set()
    target = 0
    last_top = -1
    stagnant = 0
    reported = 0
    while len(observed) < total and stagnant < 5:
        rows = await page.evaluate(_SPOTIFY_VISIBLE_ROWS_JS)
        for row in rows or []:
            position = row.get("position")
            if not isinstance(position, int) or position < 1 or position > total:
                continue
            if row.get("kind") not in ("track", "episode"):
                continue
            observed.add(position)
            if row.get("kind") == "track" and row.get("title") and row.get("artist"):
                found[position] = {
                    "title": " ".join(str(row["title"]).split()),
                    "artist": " ".join(str(row["artist"]).split()),
                }
        metrics = await page.evaluate(_SPOTIFY_SCROLL_JS, target)
        if not metrics:
            raise LucidaError("Spotify's public playlist could not be scrolled")
        top = int(metrics.get("top") or 0)
        if top == last_top:
            stagnant += 1
        else:
            stagnant = 0
            last_top = top
        if len(observed) >= reported + 100:
            reported = len(observed) // 100 * 100
            log(f"  read {len(observed)} of {total} Spotify positions")
        step = max(400, int(metrics.get("viewport") or 700) * 3 // 4)
        target = min(target + step,
                     int(metrics.get("height") or 0) - int(metrics.get("viewport") or 0))
        await page.wait_for_timeout(250)

    if len(observed) < total:
        raise LucidaError(
            f"Spotify exposed only {len(observed)} of {total} playlist positions"
        )
    tracks = [found[position] for position in sorted(found)]
    if not tracks:
        raise LucidaError("Spotify's public playlist contained no music tracks")
    return name, tracks


def _deezer_playlist_from_obj(data: Any) -> Tuple[str, List[Dict[str, str]], str]:
    if not isinstance(data, dict):
        raise LucidaError("Deezer returned unreadable playlist data")
    if isinstance(data.get("error"), dict):
        message = data["error"].get("message") or "playlist unavailable"
        raise LucidaError(f"Deezer: {message}")
    name = " ".join(str(data.get("title") or "").split())
    block = data.get("tracks") if isinstance(data.get("tracks"), dict) else data
    rows = block.get("data") if isinstance(block, dict) else []
    tracks: List[Dict[str, str]] = []
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or item.get("title_short") or "").split())
        contributors = (item.get("contributors")
                        if isinstance(item.get("contributors"), list) else [])
        artists = [str(artist.get("name") or "").strip() for artist in contributors
                   if isinstance(artist, dict) and artist.get("name")]
        primary = item.get("artist") if isinstance(item.get("artist"), dict) else {}
        artist = ", ".join(dict.fromkeys(artists)) or str(primary.get("name") or "").strip()
        if title and artist:
            tracks.append({"title": title, "artist": artist})
    return name, tracks, str(block.get("next") or "") if isinstance(block, dict) else ""


async def deezer_tracklist(url: str, log=print) -> Tuple[str, List[Dict[str, str]]]:
    playlist_id = _playlist_id(url)
    if not playlist_id:
        raise LucidaError("Deezer playlist ID not found in the URL")
    response = await _public_get(f"https://api.deezer.com/playlist/{playlist_id}")
    name, tracks, next_url = _deezer_playlist_from_obj(response.json())
    pages = 1
    while next_url and pages < 100:
        response = await _public_get(next_url)
        _, more, next_url = _deezer_playlist_from_obj(response.json())
        tracks.extend(more)
        pages += 1
    if next_url:
        log("  Deezer pagination stopped after 100 pages")
    return name, tracks


async def public_playlist_tracklist(url: str, log=print) -> Tuple[str, List[Dict[str, str]]]:
    source = playlist_source(url)
    if source == "spotify":
        return await spotify_tracklist(url, log)
    if source == "deezer":
        return await deezer_tracklist(url, log)
    raise LucidaError("This public playlist source is not supported by the HTTP importer")


# --- Apple Music playlist scraping (needs a Playwright page) ----------------

_APPLE_ROWS_JS = """() => Array.from(document.querySelectorAll(
  '[data-testid="track-list-item"], .songs-list-row'
)).map(r => {
  const n = r.querySelector('[data-testid="track-title"], .songs-list-row__song-name');
  const b = r.querySelector('[data-testid="track-column-secondary"] a, '
    + '[data-testid="track-title-by-line"] a, .songs-list-row__by-line a, '
    + '.songs-list-row__by-line');
  return { index: r.dataset.row || '', title: n ? n.textContent : '',
    artist: b ? b.textContent : '' };
})"""

_APPLE_SCROLL_JS = """() => {
  let el = document.querySelector('[data-testid="track-list-item"], .songs-list-row');
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
        await page.wait_for_selector(
            '[data-testid="track-list-item"], .songs-list-row', timeout=30_000
        )
    except Exception:
        pass

    name = await _playlist_name(page)
    structured_name, structured_tracks = await _apple_structured_playlist(page)
    if structured_name and (not name or name.lower() in ("apple music", "playlist")):
        name = structured_name
    if structured_tracks:
        log(f"    ✓ {len(structured_tracks)} titles found in Apple page data")
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
            row_index = str(t.get("index") or "").strip()
            key = (("row", row_index) if row_index else
                   ("track", artist.casefold(), title.casefold()))
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
    # Apple has used both a complete JSON bootstrap and a virtualized visual list over
    # time. Keep both readers and prefer whichever produced the most complete sequence.
    if len(structured_tracks) > len(tracks):
        tracks = structured_tracks
    return name, tracks


async def _apple_structured_playlist(page) -> Tuple[str, List[Dict[str, str]]]:
    """Read JSON bootstrap scripts without depending on Apple's CSS class names."""
    try:
        scripts = await page.locator('script[type="application/json"]').all_text_contents()
    except Exception:
        return "", []
    return _apple_playlist_from_scripts(scripts)


def _apple_playlist_from_scripts(scripts: List[str]) -> Tuple[str, List[Dict[str, str]]]:
    best_name, best_tracks = "", []
    for raw in scripts or []:
        if not raw or len(raw) > 20_000_000:  # avoid pathological pages / support dumps
            continue
        try:
            obj = json.loads(raw)
        except (TypeError, ValueError):
            continue
        name, tracks = _apple_playlist_from_obj(obj)
        if len(tracks) > len(best_tracks):
            best_name, best_tracks = name, tracks
    return best_name, best_tracks


def _apple_playlist_from_obj(obj: Any) -> Tuple[str, List[Dict[str, str]]]:
    """Find the most complete playlist resource inside an Apple bootstrap object."""
    candidates: List[Tuple[str, List[Dict[str, str]]]] = []

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 40:
            return
        if isinstance(value, dict):
            resource_type = str(value.get("type") or "").lower()
            attrs = value.get("attributes") if isinstance(value.get("attributes"), dict) else {}
            if resource_type in ("playlists", "library-playlists"):
                extracted: List[Dict[str, str]] = []
                relationships = value.get("relationships") or {}
                track_rel = relationships.get("tracks") if isinstance(relationships, dict) else {}
                data = track_rel.get("data") if isinstance(track_rel, dict) else None
                _apple_tracks_from_obj(data, extracted)
                if extracted:
                    candidates.append((str(attrs.get("name") or "").strip(), extracted))
            for child in value.values():
                visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, depth + 1)

    visit(obj)
    return max(candidates, key=lambda candidate: len(candidate[1])) if candidates else ("", [])


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
    if not is_apple_playlist_url(url):
        log("  unsupported playlist URL — paste a public music.apple.com playlist link")
        return "", []
    name, tracks = await applemusic_tracklist(page, url, log)
    if not tracks:
        log("  " + await _apple_failure_reason(page))
        await _dump_playlist_debug(page)
    return name, tracks


async def _apple_failure_reason(page) -> str:
    """Best-effort explanation without relying on one locale or one page layout."""
    try:
        text = ((await page.title()) + "\n" + (await page.locator("body").inner_text())).lower()
    except Exception:
        return "Apple Music returned no readable playlist content."
    unavailable = (
        "not available in your country", "not available in your region",
        "indisponible dans votre pays", "indisponible dans votre région",
        "item not available", "contenu indisponible",
    )
    missing = (
        "playlist not found", "page not found", "playlist introuvable",
        "page introuvable", "content is no longer available",
    )
    if any(marker in text for marker in unavailable):
        return "This playlist is unavailable in the current Apple Music region."
    if any(marker in text for marker in missing):
        return "This playlist no longer exists or is not publicly accessible."
    return ("No public tracks were found. The playlist may be empty/private, or Apple "
            "may have changed the page format.")


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
