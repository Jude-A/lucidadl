"""Async orchestration over the FAST HTTP path (no browser).

Phase 1 (resolve): each line -> item URL (search via httpx, with service fallback for
free-text queries), then ONE httpx GET of the item page -> token + every track.
Phase 2 (download): per track, httpx POST /api/load + poll + stream the file from the
Cloudflare-free <server>.lucida.to. Failed items are collected for an easy retry."""

from __future__ import annotations

import asyncio
import os
import re
from typing import Dict, List, Optional, Tuple

from . import matching, organize, progress, transcode, utils
from .api import LucidaClient, LucidaError, FALLBACK_SERVICES, normalize_service, _long
from .models import FailedItem


def _query_variants(line: str) -> List[str]:
    """Search queries to try, specific → loose. lucida/Qobuz often returns nothing for a
    literal "Artist - Title" (niche or multi-artist tracks), but matches the title alone,
    so we fall back to the title and to a primary-artist + title form. Broadened variants
    are artist-gated by the caller so they can't pick a wrong-artist track."""
    if " - " in line:
        artist, title = (p.strip() for p in line.split(" - ", 1))
    else:
        artist, title = "", line.strip()
    out = [line]
    if artist and title:
        primary = re.split(r"\s*(?:,|&|/| feat\.?| ft\.?| x | vs\.?| with )\s*",
                           artist, maxsplit=1, flags=re.I)[0].strip()
        for cand in (title, f"{primary} {title}" if primary else "",
                     f"{title} {primary}" if primary else ""):
            if cand:
                out.append(cand)
    seen, uniq = set(), []
    for v in out:
        if v and v.lower() not in seen:
            seen.add(v.lower())
            uniq.append(v)
    return uniq


async def _resolve_url(client: LucidaClient, line: str, service: str, kind: str,
                       log, strict: bool = False, quiet: bool = False) -> Optional[str]:
    line = line.strip()
    if line.lower().startswith("http"):
        return line
    services = [service]
    if not strict:
        for s in FALLBACK_SERVICES:
            if normalize_service(s) != normalize_service(service):
                services.append(s)
    bucket = "albums" if kind == "album" else "tracks"
    variants = _query_variants(line)
    explicit_artist = " - " in line
    uncertain = False
    for i, svc in enumerate(services):
        for v_idx, q in enumerate(variants):
            res = await client.search(q, svc)
            items = res.get(bucket) or []
            if not items:
                continue
            # Automatic matching always checks the primary artist. Broadened variants
            # request it explicitly too, keeping the intent obvious at the call site.
            url = matching.pick_best(
                line, items, require_artist=(v_idx > 0),
                min_score=5.5 if explicit_artist else None,
                min_margin=0.25 if explicit_artist else 0.0,
            )
            if not url:
                uncertain = uncertain or explicit_artist
                continue
            if not quiet:  # playlists (many items) suppress this per-track confirmation
                chosen = next((it for it in items if it.get("url") == url), {})
                tag = f" [fallback {svc}]" if i else ""
                via = "" if v_idx == 0 else f' [via "{q}"]'
                log(f"  ↳ chosen: \"{chosen.get('title', '?')}\" — "
                    f"{chosen.get('artist', '?')}{tag}{via} (among {len(items)} results)")
            return url
    if uncertain and not quiet:
        log(f"  ↳ no confident match for {line!r}; "
            "use `lucida search` to choose manually")
    return None


def _dest_dir(out: str, organize_on: bool) -> str:
    return os.path.join(out, ".incoming") if organize_on else os.path.join(out, "Music")


def _join_artists(artists) -> str:
    """Join a lucida `artists` list of {name} dicts. Returns '' (NOT 'Unknown Artist')
    when empty, so it never masks a usable embedded tag in organize.album_dir."""
    if isinstance(artists, str):
        return artists
    return ", ".join(a.get("name", "") for a in (artists or [])
                     if isinstance(a, dict) and a.get("name"))


def _track_meta(info: Dict, t: Dict, is_album: bool) -> Dict[str, str]:
    """API-derived artist/album fallback for organization when a file has no embedded
    tags. For an ALBUM, every track uses the album-level artist + album title so the
    whole album groups into ONE folder (never per-track artist → no compilation scatter).
    For a single track, uses the track's own artist + its album."""
    if is_album:
        who = _join_artists(info.get("artists"))
        album = info.get("title") or ""
    else:
        who = _join_artists(t.get("artists")) or _join_artists(info.get("artists"))
        alb = t.get("album") if isinstance(t.get("album"), dict) else None
        album = (alb.get("title") if alb else t.get("album")) or ""
    return {"albumartist": who, "artist": who, "album": album, "title": t.get("title") or ""}


def _filesize_mb(path: Optional[str]) -> float:
    try:
        return os.path.getsize(_long(path)) / 1_048_576 if path else 0.0
    except OSError:
        return 0.0


def friendly_error(error: Exception) -> str:
    """Turn low-level failures into a short category plus an actionable next step."""
    text = str(error).strip() or error.__class__.__name__
    lowered = text.lower()
    if "403" in lowered or "cloudflare" in lowered or "clearance" in lowered:
        return f"access expired or refused — run `lucida setup` ({text})"
    if "429" in lowered or "rate limit" in lowered or "too many" in lowered:
        return f"service temporarily rate-limited — retry later ({text})"
    if "timed out" in lowered or "timeout" in lowered or "stuck" in lowered:
        return f"service timed out — safe to retry ({text})"
    if lowered.startswith("write:") or "permission denied" in lowered:
        return f"could not write the file — check the music folder ({text})"
    return text


def _playlist_download_key(url: str, collection: str, track_no: str) -> str:
    return f"{url}|playlist|{utils.sanitize(collection)}|{track_no}"


def _existing_playlist_copy(state: utils.State, url: str, folder: str,
                            track_no: str) -> str:
    """Find a v1.1 URL-keyed file at this exact playlist position, if one exists."""
    prefix = f"{track_no} - ".casefold()
    for path in state.done.get(url, []):
        if (utils._path_exists(path) and utils._is_under(path, folder) and
                os.path.basename(path).casefold().startswith(prefix)):
            return path
    return ""


async def _resolve_targets(client, line, kind, service, country, strict, log,
                           quiet=False) -> Optional[List[Dict]]:
    url = await _resolve_url(client, line, service, kind, log, strict, quiet=quiet)
    if not url:
        return None
    pd = await client.fetch_page_data(url, country)        # ONE httpx GET
    info = pd.get("info", {}) or {}
    expiry = pd.get("tokenExpiry")
    is_album = info.get("type") == "album"
    targets = []
    for t in client.tracks_from_pd(pd):
        if t.get("producers", "x") is None or not t.get("url"):  # unavailable
            continue
        targets.append({"url": t["url"], "label": t.get("title") or line,
                        "csrf": t.get("csrf"), "csrfFallback": t.get("csrfFallback"),
                        "expiry": expiry, "meta": _track_meta(info, t, is_album)})
    if not targets:
        return None
    if is_album:
        log(f"  ⤷ album \"{line}\" → {len(targets)} tracks")
    return targets


async def preview_tracks(client: LucidaClient, items: List[str], service: str,
                         country: Optional[str], jobs: int = 3,
                         log=print) -> List[Dict]:
    """Resolve playlist entries without starting a download, in source order."""
    sem = asyncio.Semaphore(max(1, jobs))

    async def resolve(line: str, index: int) -> Dict:
        try:
            async with sem:
                targets = await _resolve_targets(
                    client, line, "track", service, country, False, log, quiet=True
                )
            if not targets:
                return {"index": index, "query": line, "status": "not found"}
            target = targets[0]
            meta = target.get("meta") or {}
            return {
                "index": index, "query": line, "status": "matched",
                "url": target.get("url") or "", "artist": meta.get("artist") or "",
                "title": meta.get("title") or target.get("label") or "",
            }
        except Exception as exc:
            return {"index": index, "query": line, "status": "error",
                    "error": friendly_error(exc)}

    rows = await asyncio.gather(*(resolve(line, index)
                                  for index, line in enumerate(items, 1)))
    return sorted(rows, key=lambda row: row["index"])


async def _download_target(client, state, target, country, out, dedup, organize_on,
                           tx, reporter, totals, failed, lock, collection=None) -> None:
    url, label = target["url"], target["label"]
    log = reporter.log
    # For a playlist, dedup is scoped to the playlist folder: a track already in
    # Artists/ (or another playlist) is still fetched into THIS playlist if it's missing.
    under = (os.path.join(out, organize.PLAYLISTS_DIR, utils.sanitize(collection))
             if collection else None)
    state_key = url
    task_key = url
    if collection and target.get("track_no"):
        state_key = _playlist_download_key(url, collection, target["track_no"])
        task_key = state_key
        # Seed the new occurrence-aware state from an existing 1.1 playlist copy. This
        # migrates lazily and still lets a duplicate URL at another position download.
        legacy_path = _existing_playlist_copy(state, url, under, target["track_no"])
        if dedup and not state.has(state_key, under) and legacy_path:
            state.add(state_key, legacy_path)
    reserved = False
    if dedup and not state.reserve(state_key, under):
        log(f"  ⏭ already downloaded / in progress, skipped: {label}")
        async with lock:
            totals["skip"] += 1
        return
    reserved = dedup
    reporter.start(task_key, label)
    track = {"url": url, "csrf": target["csrf"], "csrfFallback": target.get("csrfFallback")}
    path, last_err = None, None
    for attempt in range(2):
        try:
            handoff, server = await client.start_download(track, target["expiry"], country)
            path = await client.run_job(
                handoff, server, _dest_dir(out, organize_on), utils.sanitize(label),
                title=label,
                on_status=lambda m, k=task_key: reporter.status(k, m),
                on_bytes=lambda d, t, k=task_key: reporter.progress(k, d, t))
            break
        except Exception as e:
            last_err = e
            if attempt == 0:
                reporter.status(task_key, f"{friendly_error(e)} — retrying")
                await asyncio.sleep(3)
    if path is None:
        if reserved:
            state.release(state_key)
        reporter.finish(task_key, False, f"  ✗ {label}: {friendly_error(last_err)}")
        async with lock:
            totals["fail"] += 1
            failed.append(FailedItem(
                "track", target.get("source_line") if collection else url,
                collection or "", target.get("track_no") if collection else "",
            ))
        return

    finals = [path]
    if organize_on:
        placed = None
        try:
            placed = await asyncio.to_thread(
                organize.process_download, path, out, collection, target.get("meta"),
                target.get("track_no"))
        except Exception as e:
            log(f"  ⚠ organizing failed ({os.path.basename(path)}): {e}")
        if placed:
            finals = placed
        else:
            # process_download consumed the source (moved the file / deleted the zip) but
            # produced nothing usable (empty or audio-less archive, or it raised). Do NOT
            # report a bogus success or record a non-existent path in the dedup state
            # (that would loop forever): fail it so `retry` re-attempts.
            if reserved:
                state.release(state_key)
            reporter.finish(task_key, False,
                            f"  ✗ {label}: organized with no file (empty archive?)")
            async with lock:
                totals["fail"] += 1
                failed.append(FailedItem(
                    "track", target.get("source_line") if collection else url,
                    collection or "", target.get("track_no") if collection else "",
                ))
            return
    if tx and tx.get("fmt"):
        converted = []
        transcode_error = None
        for fp in finals:
            try:
                converted.append(await asyncio.to_thread(
                    transcode.transcode, fp, tx["fmt"], tx.get("bitrate"),
                    tx.get("keep", False), lambda *_: None))
            except Exception as e:
                log(f"  ⚠ transcode failed ({os.path.basename(fp)}): {e}")
                converted.append(fp)
                transcode_error = e
        finals = converted
        if transcode_error is not None:
            if reserved:
                state.release(state_key)
            reporter.finish(
                task_key, False,
                f"  ✗ {label}: downloaded, but {tx['fmt']} conversion failed "
                f"(original kept): {transcode_error}",
            )
            async with lock:
                totals["fail"] += 1
                failed.append(FailedItem(
                    "track", target.get("source_line") if collection else url,
                    collection or "", target.get("track_no") if collection else "",
                ))
            return

    async with lock:
        totals["ok"] += 1
    shown = os.path.relpath(finals[0], out) if finals else os.path.basename(path)
    extra = f" (+{len(finals) - 1})" if len(finals) > 1 else ""
    reporter.finish(task_key, True,
                    f"  ✓ {shown}{extra}  ({_filesize_mb(finals[0]):.1f} MB)")
    try:
        state.add(state_key, finals[0] if finals else path)
    except Exception as e:
        log(f"  ⚠ state not saved ({url}): {e}")


async def run_batch(client: LucidaClient, state: utils.State, items: List[str],
                    kind: str, service: str, country: Optional[str], out: str,
                    jobs: int, dedup: bool, organize_on: bool = True,
                    tx: Optional[Dict] = None, strict: bool = False,
                    collection: Optional[str] = None, reporter=None,
                    quiet_resolve: bool = False,
                    track_numbers: Optional[List[str]] = None
                    ) -> Tuple[Dict[str, int], List[FailedItem]]:
    if reporter is None:
        reporter = progress.TextReporter(print)
    log = reporter.log
    totals = {"ok": 0, "skip": 0, "fail": 0}
    # (kind, query-or-URL): resolution failures keep their original kind; failures
    # after an album was expanded are direct track URLs and can be retried as tracks.
    failed: List[FailedItem] = []
    sem = asyncio.Semaphore(max(1, jobs))
    lock = asyncio.Lock()

    # Phase 1 — resolve + expand into a flat track list (one httpx GET per item).
    targets: List[Dict] = []
    pad = max(2, len(str(len(items))))  # zero-pad width for playlist track numbers

    async def resolve_worker(line: str, idx: int) -> None:
        track_no = (str(track_numbers[idx]) if track_numbers and idx < len(track_numbers)
                    else f"{idx + 1:0{pad}d}")
        async with sem:
            try:
                tg = await _resolve_targets(client, line, kind, service, country, strict,
                                            log, quiet=quiet_resolve)
            except Exception as e:
                log(f"  ✗ resolving \"{line}\": {friendly_error(e)}")
                async with lock:
                    totals["fail"] += 1
                    failed.append(FailedItem(kind, line, collection or "",
                                             track_no if collection else ""))
                return
            if tg is None:
                log(f"  ✗ not found: {line}")
                async with lock:
                    totals["fail"] += 1
                    failed.append(FailedItem(kind, line, collection or "",
                                             track_no if collection else ""))
                return
            for t in tg:  # keep the source order (used to number playlist tracks)
                t["track_no"] = track_no
                t["source_line"] = line
            async with lock:
                targets.extend(tg)

    await asyncio.gather(*(resolve_worker(line, idx) for idx, line in enumerate(items)),
                         return_exceptions=True)
    if not targets:
        return totals, failed
    log(f"→ {len(targets)} track(s) to download ({jobs} in parallel)…")

    # Phase 2 — download every track concurrently over httpx (no browser).
    async def dl_worker(target: Dict) -> None:
        async with sem:
            try:
                await _download_target(client, state, target, country, out, dedup,
                                       organize_on, tx, reporter, totals, failed, lock,
                                       collection)
            except Exception as e:
                log(f"  ✗ {target.get('label')}: {friendly_error(e)}")
                async with lock:
                    totals["fail"] += 1
                    failed.append(FailedItem(
                        "track",
                        target.get("source_line") if collection else
                        (target.get("url") or target.get("label")),
                        collection or "", target.get("track_no") if collection else "",
                    ))

    await asyncio.gather(*(dl_worker(t) for t in targets), return_exceptions=True)

    # A downloaded playlist is only a real "playlist" to music players/devices (Garmin
    # watches, phones, car units…) when an .m3u8 sits next to the tracks — a bare folder
    # isn't one. Write it now that every track has been placed. Best-effort: a failure
    # here (e.g. odd filesystem) must not turn a successful batch into a failure.
    if collection and organize_on:
        try:
            m3u = await asyncio.to_thread(organize.write_playlist_m3u, out, collection)
            if m3u:
                log(f"→ playlist file: {os.path.relpath(m3u, out)}")
        except Exception as e:
            log(f"  ⚠ playlist .m3u8 not written: {e}")

    return totals, failed
