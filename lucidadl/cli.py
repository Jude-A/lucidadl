"""Command-line interface for lucidadl (async download core)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
from typing import Dict, List, Optional, Tuple

import click

from . import __version__
from . import api, organize, paths, progress, transcode, utils
from .api import (LucidaClient, default_country, normalize_service, DOWNSCALE_CHOICES,
                  playlist_source, playlist_source_name, LUCIDA)
from .downloader import preview_tracks, run_batch
from .models import FailedItem
from .session import (lucida_context, ensure_cleared, get_page, BrowserClosed,
                      acquire_clearance, load_clearance, chromium_installed,
                      install_chromium)

# App data (cookie/profile/state/log/failed list) lives in the fixed user data dir.
# Downloads go to ONE fixed, configurable music directory (never the cwd). Default
# batch input files stay next to you, and any text file can be selected with --file.
STATE_PATH = paths.STATE_PATH
DEFAULT_OUT = paths.default_music_dir()
INPUTS = paths.cwd("inputs")
LOG_PATH = paths.LOG_PATH
FAILED_PATH = paths.FAILED_PATH
PLAYLIST_RUN_PATH = paths.PLAYLIST_RUN_PATH
PLAYLIST_TEXT_PATH = paths.PLAYLIST_TEXT_PATH


RunResult = Tuple[Dict[str, int], List[FailedItem]]


def _as_failed_item(value) -> FailedItem:
    if isinstance(value, FailedItem):
        return value
    parts = list(value) if isinstance(value, (tuple, list)) else ["track", str(value)]
    return FailedItem(*(parts + ["", ""])[:4])


def _write_failed(items: List[FailedItem]) -> None:
    try:
        if not items:
            try:
                os.remove(FAILED_PATH)
            except FileNotFoundError:
                pass
            return
        with open(FAILED_PATH, "w", encoding="utf-8") as f:
            f.write("# Failed items — re-run with: lucidadl retry\n")
            f.write("# kind<TAB>query or URL<TAB>playlist<TAB>track number\n")
            for raw in items:
                item = _as_failed_item(raw)
                f.write(f"{item.kind}\t{item.item}\t{item.collection}\t{item.track_no}\n")
    except Exception as e:
        # don't fail silently: the user is told to `retry`, but the list wasn't saved.
        click.secho(f"⚠ couldn't write {FAILED_PATH} ({e}) — `retry` won't have these "
                    f"items. Re-run them manually:", fg="yellow")
        for raw in items:
            item = _as_failed_item(raw)
            click.echo(f"    {item.kind}: {item.item}")

_CLOSED_HINT = (
    "The browser closed on its own. Try: (1) re-run; (2) close any open Chrome "
    "windows; (3) to force your real Chrome: $env:LUCIDA_CHANNEL='chrome'."
)


def _read_lines(path: str) -> List[str]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


def _read_failed() -> List[FailedItem]:
    """Read typed failures; old untyped failed.txt entries remain track retries."""
    out: List[FailedItem] = []
    for line in _read_lines(FAILED_PATH):
        parts = line.split("\t")
        kind = parts[0] if parts else ""
        item = parts[1] if len(parts) > 1 else ""
        if kind in ("track", "album") and item:
            out.append(FailedItem(kind, item,
                                  parts[2] if len(parts) > 2 else "",
                                  parts[3] if len(parts) > 3 else ""))
        else:
            out.append(FailedItem("track", line))
    return out


def _load_playlist_run() -> dict:
    try:
        with open(PLAYLIST_RUN_PATH, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _save_playlist_run(value: dict) -> None:
    value = dict(value)
    value["updated_at"] = int(time.time())
    tmp = PLAYLIST_RUN_PATH + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, PLAYLIST_RUN_PATH)


def _pending_playlist_run() -> dict:
    value = _load_playlist_run()
    return value if value.get("status") == "running" and value.get("tracks") else {}


def _playlist_run_options(value: dict) -> dict:
    """Validated options from a saved run; corrupt values fall back conservatively."""
    opts = value.get("options") if isinstance(value.get("options"), dict) else {}
    try:
        jobs = max(1, min(100, int(opts.get("jobs", 3))))
    except (TypeError, ValueError):
        jobs = 3
    return {
        "service": opts.get("service") or "qobuz",
        "country": opts.get("country"),
        "downscale": opts.get("downscale") or "original",
        "out": opts.get("out") or paths.default_music_dir(),
        "hidden": bool(opts.get("hidden", False)),
        "jobs": jobs,
        "organize_on": bool(opts.get("organize_on", True)),
        "to_fmt": opts.get("to_fmt") or None,
        "bitrate": opts.get("bitrate") or None,
        "keep_orig": bool(opts.get("keep_orig", False)),
        "force": bool(opts.get("force", False)),
    }


async def _resume_playlist_run(value: dict) -> RunResult:
    tracks = value.get("tracks") if isinstance(value.get("tracks"), list) else []
    items = [str(item.get("query") or "") for item in tracks if isinstance(item, dict)]
    numbers = [str(item.get("track_no") or "") for item in tracks if isinstance(item, dict)]
    if not items or any(not item for item in items):
        click.secho("The saved playlist run is unreadable; paste its URL again.", fg="red")
        return _failed_result(["saved playlist"], "track")
    opts = _playlist_run_options(value)
    collection = str(value.get("collection") or "Playlist")
    click.secho(f'Resuming playlist "{collection}" ({len(items)} tracks)…', fg="cyan")
    result = await _run(
        items, "track", opts["service"], opts["country"], opts["downscale"],
        opts["out"], opts["hidden"], opts["jobs"], dedup=True,
        organize_on=opts["organize_on"], to_fmt=opts["to_fmt"],
        bitrate=opts["bitrate"], keep_orig=opts["keep_orig"],
        collection=collection, force=opts["force"], quiet_resolve=True,
        track_numbers=numbers,
    )
    value["status"] = "incomplete" if result[0]["fail"] or result[1] else "complete"
    value["summary"] = result[0]
    _save_playlist_run(value)
    return result


def _read_batch(path: str, label: str) -> List[str]:
    """Read a batch input while distinguishing a missing file from an empty one."""
    if not os.path.isfile(path):
        click.secho(f"Batch file not found: {os.path.abspath(path)}", fg="red")
        click.echo(f"Choose one with: lucida {label} --file \"path/to/{label}.txt\"")
        raise click.exceptions.Exit(1)
    items = _read_lines(path)
    if not items:
        click.secho(f"Batch file is empty: {os.path.abspath(path)}", fg="yellow")
    return items


def _failed_result(items: List[str], kind: str, collection: Optional[str] = None,
                   track_numbers: Optional[List[str]] = None) -> RunResult:
    failed = [
        FailedItem(
            kind, item, collection or "",
            str(track_numbers[index])
            if collection and track_numbers and index < len(track_numbers) else "",
        )
        for index, item in enumerate(items)
    ]
    return {"ok": 0, "skip": 0, "fail": len(items)}, failed


def _exit_if_failed(result: RunResult) -> None:
    if result[0]["fail"] or result[1]:
        raise click.exceptions.Exit(1)


def _service_opts(f):
    f = click.option("-s", "--service", default="qobuz",
                     help="Source service (qobuz by default, amazon).")(f)
    f = click.option("--country", default=None, help="Country code (def: US for qobuz).")(f)
    f = click.option("-F", "--format", "downscale", default="original",
                     type=click.Choice(DOWNSCALE_CHOICES),
                     help="Format requested from lucida (server-side conversion, no bitrate "
                          "control). For a precise format + bitrate, prefer --to.")(f)
    f = click.option("-o", "--out", default=DEFAULT_OUT, help="Output folder.")(f)
    f = click.option("--organize/--flat", "organize_on", default=True,
                     help="Sort by tags into Artists/<Artist>/<Album>/ (default); "
                          "--flat = everything flat in <music folder>/Music/.")(f)
    f = click.option("-j", "--jobs", default=3, type=click.IntRange(1, 100),
                     help="Parallel downloads (1–100, def 3).")(f)
    f = click.option("--to", "to_fmt", default=None, type=click.Choice(transcode.CHOICES),
                     help="Local ffmpeg transcoding (recommended): download as FLAC then "
                          "convert to this format. Bitrate adjustable via --bitrate.")(f)
    f = click.option("--bitrate", default=None,
                     help="Bitrate for --to (e.g. 320k, 256k, 192k).")(f)
    f = click.option("--keep-original", "keep_orig", is_flag=True,
                     help="Keep the original FLAC alongside the transcoded file.")(f)
    f = click.option("--force", "force", is_flag=True,
                     help="Ignore the dedup memory and (re)download, even if already "
                          "done (useful after deleting files).")(f)
    f = click.option("--hidden/--visible", "hidden", default=False,
                     help="--hidden = off-screen window if a Cloudflare pass is required "
                          "(otherwise no browser opens). Default: visible.")(f)
    return f


@click.group(invoke_without_command=True)
@click.version_option(version=__version__, message="lucidadl %(version)s")
@click.pass_context
def cli(ctx):
    """Parallel-HTTP lucida.to downloader (browser only needed for Cloudflare).

    With no argument, opens the interactive menu (`lucida ui`)."""
    if ctx.invoked_subcommand is None:
        from . import tui
        tui.run()


@cli.command("ui")
def ui_cmd():
    """Interactive menu: download, search, import a playlist, configure."""
    from . import tui
    tui.run()


# --- shared async runners ---------------------------------------------------

async def _run(items: List[str], kind: str, service: str, country: Optional[str],
               downscale: str, out: str, hidden: bool, jobs: int, dedup: bool,
               organize_on: bool = True, to_fmt: Optional[str] = None,
               bitrate: Optional[str] = None, keep_orig: bool = False,
               collection: Optional[str] = None, force: bool = False,
               quiet_resolve: bool = False, save_failures: bool = True,
               track_numbers: Optional[List[str]] = None) -> RunResult:
    if not items:
        click.secho("Nothing to download.", fg="yellow")
        return {"ok": 0, "skip": 0, "fail": 0}, []
    if force:
        dedup = False  # re-download even what state.json remembers
    cc = country or default_country(service)
    # When transcoding locally, pull the best source (FLAC) from lucida.
    tx = None
    if to_fmt:
        downscale = "original"
        tx = {"fmt": to_fmt, "bitrate": bitrate, "keep": keep_orig}
        if not transcode.available():
            click.secho("⚠ ffmpeg not found — install it (pip install imageio-ffmpeg) "
                        "or drop --to.", fg="red")
            result = _failed_result(items, kind, collection, track_numbers)
            if save_failures:
                _write_failed(result[1])
            return result
    if organize_on and not organize.mutagen_available():
        click.secho("⚠ mutagen not found — tags can't be read; sorting by "
                    "artist/album will rely on the API metadata (otherwise "
                    "\"Unknown\"). Install it: pip install mutagen", fg="yellow")
    os.makedirs(out, exist_ok=True)
    state = utils.State(STATE_PATH)
    logf = open(LOG_PATH, "w", encoding="utf-8")
    reporter = progress.make_reporter(echo=click.echo, logfile=logf)
    log = reporter.log
    result = _failed_result(items, kind, collection, track_numbers)

    log(f"# lucidadl — kind={kind} service={service} country={cc!r} "
        f"format={downscale} jobs={jobs} dedup={dedup} "
        f"transcode={to_fmt or '-'}{('@'+bitrate) if (to_fmt and bitrate) else ''}")
    try:
        # Get a Cloudflare cookie: reuse the saved one (no browser at all), else open
        # the browser briefly to solve it. Downloads then run over httpx (RAM-light).
        cf, ua = load_clearance()
        if not (cf and ua):
            log("No Cloudflare cookie — briefly opening the browser…")
            try:
                cf, ua = await acquire_clearance(hidden=hidden)
            except BrowserClosed:
                log(_CLOSED_HINT)
                if save_failures:
                    _write_failed(result[1])
                return result
            except Exception as e:
                log(f"Couldn't clear Cloudflare: {e}. Run `setup`.")
                if save_failures:
                    _write_failed(result[1])
                return result

        async def _acquire():
            return await acquire_clearance(hidden=hidden)

        client = LucidaClient(cf, ua, acquire=_acquire, country=cc, downscale=downscale,
                              metadata=True, jobs=jobs, log=log)
        await client.start_http()
        log(f"Downloading {len(items)} item(s) — {jobs} in parallel (no browser)…")
        try:
            totals, failed = await run_batch(client, state, items, kind, service, cc, out,
                                             jobs, dedup, organize_on, tx,
                                             collection=collection, reporter=reporter,
                                             quiet_resolve=quiet_resolve,
                                             track_numbers=track_numbers)
        finally:
            await client.aclose()
        log(f"\nDone — OK:{totals['ok']}  skipped:{totals['skip']}  failed:{totals['fail']}")
        result = totals, failed
        if save_failures:
            _write_failed(failed)
        if failed and save_failures:
            log(f"  → {len(failed)} failure(s) written to {FAILED_PATH} "
                f"(re-run: lucida retry)")
    except Exception as e:
        log(f"FATAL ERROR: {e}")
        log(traceback.format_exc())
        if save_failures:
            _write_failed(result[1])
    finally:
        try:
            reporter.close()
        except Exception:
            pass
        try:
            logf.close()
        except Exception:
            pass
    click.secho(f"→ Files in {out}  ·  log: {LOG_PATH}", fg="cyan")
    return result


# --- ad-hoc (singular): args, no dedup -------------------------------------

@cli.command("track")
@click.argument("items", nargs=-1, required=True)
@_service_opts
def track_cmd(items, service, country, downscale, out, organize_on, jobs, to_fmt,
              bitrate, keep_orig, force, hidden):
    """One or more tracks now: track "artist - title" (or URL). Forces the DL."""
    result = asyncio.run(_run(list(items), "track", service, country, downscale, out,
                              hidden, jobs, dedup=False, organize_on=organize_on,
                              to_fmt=to_fmt, bitrate=bitrate, keep_orig=keep_orig,
                              force=force))
    _exit_if_failed(result)


@cli.command("album")
@click.argument("items", nargs=-1, required=True)
@_service_opts
def album_cmd(items, service, country, downscale, out, organize_on, jobs,
              to_fmt, bitrate, keep_orig, force, hidden):
    """One or more albums now: album "artist - album" (or URL), expanded track by track."""
    result = asyncio.run(_run(list(items), "album", service, country, downscale, out,
                              hidden, jobs, dedup=False, organize_on=organize_on,
                              to_fmt=to_fmt, bitrate=bitrate, keep_orig=keep_orig,
                              force=force))
    _exit_if_failed(result)


# --- batch files (plural commands): file input, with dedup ------------------

@cli.command("tracks")
@click.option("-f", "--file", "file", default=os.path.join(INPUTS, "tracks.txt"),
              help="File of titles/URLs, one per line (def: inputs/tracks.txt).")
@_service_opts
def tracks_cmd(file, service, country, downscale, out, organize_on, jobs, to_fmt,
               bitrate, keep_orig, force, hidden):
    """Download tracks listed in a text file (dedup enabled)."""
    result = asyncio.run(_run(_read_batch(file, "tracks"), "track", service, country,
                              downscale, out,
                              hidden, jobs, dedup=True, organize_on=organize_on,
                              to_fmt=to_fmt, bitrate=bitrate, keep_orig=keep_orig,
                              force=force))
    _exit_if_failed(result)


@cli.command("albums")
@click.option("-f", "--file", "file", default=os.path.join(INPUTS, "albums.txt"),
              help="File of albums/URLs, one per line (def: inputs/albums.txt).")
@_service_opts
def albums_cmd(file, service, country, downscale, out, organize_on, jobs,
               to_fmt, bitrate, keep_orig, force, hidden):
    """Download albums listed in a text file (dedup enabled)."""
    result = asyncio.run(_run(_read_batch(file, "albums"), "album", service, country,
                              downscale, out,
                              hidden, jobs, dedup=True, organize_on=organize_on,
                              to_fmt=to_fmt, bitrate=bitrate, keep_orig=keep_orig,
                              force=force))
    _exit_if_failed(result)


@cli.command("retry")
@_service_opts
def retry_cmd(service, country, downscale, out, organize_on, jobs, to_fmt,
              bitrate, keep_orig, force, hidden):
    """Resume an interrupted playlist or re-run the last failed items."""
    pending = _pending_playlist_run()
    if pending:
        result = asyncio.run(_resume_playlist_run(pending))
        _exit_if_failed(result)
        return
    items = _read_failed()
    if not items:
        click.secho("No failures to re-run (failed.txt is empty).", fg="yellow")
        return
    result = asyncio.run(_retry(items, service, country, downscale, out, hidden, jobs,
                                organize_on, to_fmt, bitrate, keep_orig, force))
    _exit_if_failed(result)


async def _retry(items: List[FailedItem], service: str, country: Optional[str],
                 downscale: str, out: str, hidden: bool, jobs: int,
                 organize_on: bool = True, to_fmt: Optional[str] = None,
                 bitrate: Optional[str] = None, keep_orig: bool = False,
                 force: bool = False) -> RunResult:
    """Retry failures by their original type, then keep only what still failed."""
    totals = {"ok": 0, "skip": 0, "fail": 0}
    remaining: List[FailedItem] = []
    normalized = [_as_failed_item(item) for item in items]
    manifest = _load_playlist_run()
    groups = {}
    for item in normalized:
        groups.setdefault((item.kind, item.collection), []).append(item)
    for (kind, collection), group in groups.items():
        values = [item.item for item in group]
        if not values or kind not in ("track", "album"):
            continue
        label = f' in playlist "{collection}"' if collection else ""
        click.secho(
            f"\nRetrying {len(values)} {kind}{'s' if len(values) != 1 else ''}{label}…",
            fg="cyan",
        )
        saved = (_playlist_run_options(manifest)
                 if collection and manifest.get("collection") == collection else {})
        result = await _run(values, kind, saved.get("service", service),
                            saved.get("country", country), saved.get("downscale", downscale),
                            saved.get("out", out), saved.get("hidden", hidden),
                            saved.get("jobs", jobs), dedup=True,
                            organize_on=saved.get("organize_on", organize_on),
                            to_fmt=saved.get("to_fmt", to_fmt),
                            bitrate=saved.get("bitrate", bitrate),
                            keep_orig=saved.get("keep_orig", keep_orig),
                            force=saved.get("force", force),
                            collection=collection or None, save_failures=False,
                            track_numbers=[item.track_no for item in group]
                            if collection else None)
        for key in totals:
            totals[key] += result[0][key]
        remaining.extend(result[1])
    _write_failed(remaining)
    if (manifest.get("status") == "incomplete" and manifest.get("collection") and
            not any(item.collection == manifest.get("collection") for item in remaining)):
        manifest["status"] = "complete"
        manifest["summary"] = totals
        _save_playlist_run(manifest)
    if not remaining:
        click.secho("\n✓ All previous failures were resolved.", fg="green")
    return totals, remaining


# --- interactive search ----------------------------------------------------

async def _search_entries(query, service, hidden=False, country=None):
    """Run a search and return a flat list of (kind, item) entries (albums then tracks).
    Shared by the CLI `search` prompt and the TUI's arrow-key picker."""
    cc = country or default_country(service)
    cf, ua = load_clearance()
    if not (cf and ua):
        cf, ua = await acquire_clearance(hidden=hidden)
    client = LucidaClient(cf, ua, acquire=lambda: acquire_clearance(hidden=hidden),
                          country=cc, log=click.echo)
    await client.start_http()
    try:
        res = await client.search(query, service)
    finally:
        await client.aclose()
    entries = [("album", it) for it in (res.get("albums") or [])[:15]]
    entries += [("track", it) for it in (res.get("tracks") or [])[:15]]
    return entries


async def _search(query, service, country, downscale, out, organize_on, jobs,
                  to_fmt, bitrate, keep_orig, hidden,
                  force=False) -> Optional[RunResult]:
    try:
        entries = await _search_entries(query, service, hidden=hidden, country=country)
    except BrowserClosed:
        click.secho(_CLOSED_HINT, fg="red")
        return {"ok": 0, "skip": 0, "fail": 1}, []
    except Exception as e:
        click.secho(f"Search failed: {e}. Try `lucida setup` or `lucida doctor --live`.",
                    fg="red")
        return {"ok": 0, "skip": 0, "fail": 1}, []

    albums = [it for kind, it in entries if kind == "album"]
    tracks = [it for kind, it in entries if kind == "track"]
    numbered = []
    if albums:
        click.secho("\nAlbums:", fg="cyan")
        for it in albums:
            numbered.append(("album", it))
            click.echo(f"  {len(numbered):>2}. {it.get('title', '?')} — "
                       f"{it.get('artist', '?')}")
    if tracks:
        click.secho("\nTracks:", fg="cyan")
        for it in tracks:
            numbered.append(("track", it))
            alb = f"  [{it.get('album', '')}]" if it.get("album") else ""
            click.echo(f"  {len(numbered):>2}. {it.get('title', '?')} — "
                       f"{it.get('artist', '?')}{alb}")
    if not numbered:
        click.secho("No results.", fg="yellow")
        return {"ok": 0, "skip": 0, "fail": 1}, []

    try:
        sel = (await asyncio.to_thread(input, "\nNumber to download (Enter = cancel): ")).strip()
    except EOFError:
        sel = ""
    if not sel:
        click.echo("Cancelled.")
        return None
    if not sel.isdigit() or not (1 <= int(sel) <= len(numbered)):
        click.secho("Invalid selection.", fg="red")
        return {"ok": 0, "skip": 0, "fail": 1}, []
    kind, item = numbered[int(sel) - 1]
    return await _run([item["url"]], kind, service, country, downscale, out, hidden,
                      jobs, dedup=False, organize_on=organize_on, to_fmt=to_fmt,
                      bitrate=bitrate, keep_orig=keep_orig, force=force)


@cli.command("search")
@click.argument("query", nargs=-1, required=True)
@_service_opts
def search_cmd(query, service, country, downscale, out, organize_on, jobs,
               to_fmt, bitrate, keep_orig, force, hidden):
    """Interactive search: lists the results, downloads the one you pick."""
    result = asyncio.run(_search(" ".join(query), service, country, downscale, out,
                                 organize_on, jobs, to_fmt, bitrate, keep_orig, hidden,
                                 force=force))
    if result is not None:
        _exit_if_failed(result)


# --- public playlist import -------------------------------------------------

def _render_playlist(collection: str, tracks: list, dry_run: bool) -> None:
    """Compact, pretty playlist summary: a rich table for --dry-run, a single header
    line for a download (the per-track progress bars take over from there). Falls back
    to plain text when not attached to a terminal (pipes, redirects)."""
    n = len(tracks)
    console = None
    try:
        from rich.console import Console
        c = Console()
        if c.is_terminal:
            console = c
    except Exception:
        console = None

    if console is None:                       # plain output (piped / non-tty)
        click.secho(f'Playlist "{collection}" — {n} tracks', fg="green")
        if dry_run:
            for i, t in enumerate(tracks, 1):
                click.echo(f"  {i:>3}. {t['artist']} - {t['title']}")
        return

    from rich.markup import escape
    coll = escape(collection)  # names may contain '[' etc. — never let them be markup
    if dry_run:
        from rich.table import Table
        table = Table(title=f'Playlist "{coll}" — {n} tracks',
                      title_style="bold green", header_style="bold", box=None, pad_edge=False)
        table.add_column("#", justify="right", style="dim", width=4)
        table.add_column("Artist", style="cyan")
        table.add_column("Title")
        for i, t in enumerate(tracks, 1):
            table.add_row(str(i), escape(t["artist"]), escape(t["title"]))
        console.print(table)
    else:
        console.print(f'\n[bold green]Playlist "{coll}"[/]  ·  [bold]{n}[/] tracks  '
                      f'·  →  [dim]Playlists/{coll}/[/]\n')


def _render_playlist_check(collection: str, rows: list) -> None:
    matched = sum(row.get("status") == "matched" for row in rows)
    missing = len(rows) - matched
    console = None
    try:
        from rich.console import Console
        candidate = Console()
        if candidate.is_terminal:
            console = candidate
    except Exception:
        pass
    if console is None:
        click.echo(f'Match check for "{collection}" — {matched} ready, {missing} unresolved')
        for row in rows:
            mark = "OK" if row.get("status") == "matched" else "MISS"
            detail = (f"{row.get('artist')} - {row.get('title')}"
                      if row.get("status") == "matched" else
                      (row.get("error") or row.get("status")))
            click.echo(f"  {row['index']:>3}. {mark:<4} {row['query']} → {detail}")
        return
    from rich.markup import escape
    from rich.table import Table
    table = Table(title=f'Match check — {escape(collection)}', box=None, pad_edge=False)
    table.add_column("#", justify="right", style="dim", width=4)
    table.add_column("Status", width=9)
    table.add_column("Requested")
    table.add_column("Matched on lucida")
    for row in rows:
        ok = row.get("status") == "matched"
        matched_text = (f"{row.get('artist')} - {row.get('title')}" if ok else
                        (row.get("error") or row.get("status") or "unresolved"))
        table.add_row(str(row["index"]), "[green]ready[/]" if ok else "[red]unresolved[/]",
                      escape(row["query"]), escape(matched_text))
    console.print(table)
    console.print(f"[green]{matched} ready[/]  ·  "
                  f"[{'red' if missing else 'green'}]{missing} unresolved[/]")


async def _check_playlist_matches(collection: str, items: List[str], service: str,
                                  country: Optional[str], hidden: bool, jobs: int,
                                  edit_path: Optional[str] = None) -> bool:
    cc = country or default_country(service)
    cf, ua = load_clearance()
    if not (cf and ua):
        click.echo("Preparing lucida.to access for the match check…")
        try:
            cf, ua = await acquire_clearance(hidden=hidden)
        except Exception as exc:
            click.secho(f"Could not prepare lucida.to access: {exc}", fg="red")
            return False

    async def _acquire():
        return await acquire_clearance(hidden=hidden)

    client = LucidaClient(cf, ua, acquire=_acquire, country=cc, jobs=jobs, log=click.echo)
    try:
        await client.start_http()
        rows = await preview_tracks(client, items, service, cc, jobs=jobs, log=click.echo)
    except Exception as exc:
        click.secho(f"Match check failed: {exc}", fg="red")
        return False
    finally:
        await client.aclose()
    _render_playlist_check(collection, rows)
    unresolved = sum(row.get("status") != "matched" for row in rows)
    if unresolved:
        editable = edit_path or PLAYLIST_TEXT_PATH
        click.secho(
            f"Edit {editable}, then download the corrected list with:\n"
            f'  lucida playlist-file "{editable}" --name "{collection}"',
            fg="yellow",
        )
    return unresolved == 0


async def _download_playlist_items(collection: str, items: List[str], source: str,
                                   service: str, country: Optional[str], downscale: str,
                                   out: str, hidden: bool, jobs: int,
                                   organize_on: bool = True, to_fmt=None, bitrate=None,
                                   keep_orig: bool = False, force: bool = False,
                                   tracks: Optional[List[dict]] = None) -> bool:
    pad = max(2, len(str(len(items))))
    track_numbers = [f"{index:0{pad}d}" for index in range(1, len(items) + 1)]
    track_rows = tracks or [{} for _ in items]
    manifest = {
        "version": 1, "status": "running", "source_url": source,
        "collection": collection,
        "tracks": [{"track_no": number, "artist": track.get("artist", ""),
                    "title": track.get("title", ""), "query": item}
                   for number, track, item in zip(track_numbers, track_rows, items)],
        "options": {
            "service": service, "country": country, "downscale": downscale,
            "out": os.path.abspath(out), "hidden": hidden, "jobs": jobs,
            "organize_on": organize_on, "to_fmt": to_fmt, "bitrate": bitrate,
            "keep_orig": keep_orig, "force": force,
        },
    }
    try:
        _save_playlist_run(manifest)
    except Exception as e:
        click.secho(f"Could not save playlist recovery data: {e}", fg="yellow")
    result = await _run(items, "track", service, country, downscale, out, hidden, jobs,
                        dedup=True, organize_on=organize_on, to_fmt=to_fmt,
                        bitrate=bitrate, keep_orig=keep_orig, collection=collection,
                        force=force, quiet_resolve=True, track_numbers=track_numbers)
    manifest["status"] = "incomplete" if result[0]["fail"] or result[1] else "complete"
    manifest["summary"] = result[0]
    try:
        _save_playlist_run(manifest)
    except Exception as e:
        click.secho(f"Could not update playlist recovery data: {e}", fg="yellow")
    return not (result[0]["fail"] or result[1])


async def _playlist(url, dry_run, service, country, downscale, out, hidden,
                     jobs, organize_on=True, to_fmt=None, bitrate=None, keep_orig=False,
                     force=False, check_matches=False) -> bool:
    source = playlist_source(url)
    if not source:
        click.secho(
            "Paste a public Apple Music, Spotify, or Deezer playlist link.", fg="red"
        )
        return False

    name, tracks = "", []
    try:
        if source == "apple":
            async def _scrape(headless: bool):
                # Apple Music uses a dynamic page, so it still needs Playwright. `hidden`
                # only matters for the visible fallback.
                async with lucida_context(headless=headless,
                                          hidden=(hidden and not headless)) as ctx:
                    page = await get_page(ctx)
                    return await api.playlist_tracklist(page, url, click.echo)

            click.echo("Reading the Apple Music playlist (headless browser)…")
            try:
                name, tracks = await _scrape(headless=True)
            except BrowserClosed:
                raise
            except Exception as e:
                click.secho(f"  headless: {e}", fg="yellow")
            if not tracks:
                click.secho("Headless unsuccessful — retrying with a visible window…",
                            fg="yellow")
                name, tracks = await _scrape(headless=False)
        else:
            click.echo(f"Reading the public {playlist_source_name(source)} playlist…")
            try:
                name, tracks = await api.public_playlist_tracklist(url, click.echo)
            except api.SpotifyPlaylistWindow as limited:
                detail = (f"reports {limited.total} titles" if limited.total
                          else "returned its 100-item public window")
                click.echo(f"Spotify {detail} — reading the full list in a headless browser…")
                async with lucida_context(headless=True) as ctx:
                    page = await get_page(ctx)
                    name, tracks = await api.spotify_browser_tracklist(
                        page, url, limited.name, limited.total, click.echo
                    )
    except BrowserClosed:
        click.secho(_CLOSED_HINT, fg="red")
        return False
    except Exception as e:
        click.secho(f"✗ {playlist_source_name(source)} playlist extraction: {e}", fg="red")
        return False

    if not tracks:
        detail = (" Diagnostic files were saved in the app data folder; run `lucida "
                  "config` to see its location." if source == "apple" else "")
        click.secho(
            f"No public tracks were found. The playlist may be empty or private.{detail}",
            fg="red",
        )
        return False

    collection = name or "Playlist"
    items = [f"{t['artist']} - {t['title']}" for t in tracks]
    try:
        with open(PLAYLIST_TEXT_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(items) + "\n")
    except Exception as e:
        click.secho(f"Could not save the extracted list: {e}", fg="yellow")

    _render_playlist(collection, tracks, dry_run)
    if dry_run:
        click.secho(f"--dry-run: nothing downloaded (list saved to {PLAYLIST_TEXT_PATH}).",
                    fg="yellow")
        return True
    if check_matches:
        click.secho("Checking automatic matches without downloading…", fg="cyan")
        return await _check_playlist_matches(collection, items, service, country, hidden, jobs)
    return await _download_playlist_items(
        collection, items, url, service, country, downscale, out, hidden, jobs,
        organize_on, to_fmt, bitrate, keep_orig, force, tracks,
    )


@cli.command("playlist")
@click.argument("url")
@click.option("--dry-run", is_flag=True, help="List the tracks without downloading.")
@click.option("--check", "check_matches", is_flag=True,
              help="Resolve every title through lucida without downloading.")
@_service_opts
def playlist_cmd(url, dry_run, check_matches, service, country, downscale, out, organize_on, jobs,
                 to_fmt, bitrate, keep_orig, force, hidden):
    """Import a public Apple Music, Spotify, or Deezer playlist."""
    if dry_run and check_matches:
        raise click.UsageError("Choose either --dry-run or --check, not both.")
    ok = asyncio.run(_playlist(url, dry_run, service, country, downscale, out, hidden,
                               jobs, organize_on, to_fmt=to_fmt, bitrate=bitrate,
                               keep_orig=keep_orig, force=force,
                               check_matches=check_matches))
    if not ok:
        raise click.exceptions.Exit(1)


@cli.command("playlist-file")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@click.option("--name", required=True, help="Playlist name used for its folder and .m3u8.")
@click.option("--check", "check_matches", is_flag=True,
              help="Resolve every title through lucida without downloading.")
@_service_opts
def playlist_file_cmd(file, name, check_matches, service, country, downscale, out,
                      organize_on, jobs, to_fmt, bitrate, keep_orig, force, hidden):
    """Download a corrected text list as an ordered playlist."""
    items = _read_lines(file)
    if not items:
        click.secho(f"Playlist file is empty: {os.path.abspath(file)}", fg="yellow")
        raise click.exceptions.Exit(1)
    collection = " ".join(name.split()).strip()
    if not collection:
        raise click.UsageError("--name cannot be empty.")
    if check_matches:
        ok = asyncio.run(_check_playlist_matches(
            collection, items, service, country, hidden, jobs,
            edit_path=os.path.abspath(file)))
    else:
        _render_playlist(collection, [
            {"artist": item.partition(" - ")[0] if " - " in item else "",
             "title": item.partition(" - ")[2] if " - " in item else item}
            for item in items
        ], dry_run=False)
        ok = asyncio.run(_download_playlist_items(
            collection, items, os.path.abspath(file), service, country, downscale,
            out, hidden, jobs, organize_on, to_fmt, bitrate, keep_orig, force))
    if not ok:
        raise click.exceptions.Exit(1)


# --- config -----------------------------------------------------------------

@cli.command("config")
@click.option("--music", "music", default=None,
              help="Set the download folder (saved). E.g. \"D:/Music\".")
def config_cmd(music):
    """Show/edit the configuration (music folder, data paths)."""
    if music:
        newdir = paths.set_music_dir(music)
        try:
            os.makedirs(newdir, exist_ok=True)
        except Exception as e:
            click.secho(f"⚠ couldn't create: {e}", fg="yellow")
        click.secho(f"✓ Music folder → {newdir}", fg="green")
    click.echo(f"Music       : {paths.default_music_dir()}")
    click.echo(f"Data        : {paths.DATA_DIR}")
    click.echo(f"State/dedup : {paths.STATE_PATH}")
    click.echo(f"Log         : {paths.LOG_PATH}")
    click.echo(f"Failures    : {paths.FAILED_PATH}")
    click.echo(f"Playlist    : {paths.PLAYLIST_TEXT_PATH}")
    click.echo(f"Playlist run: {paths.PLAYLIST_RUN_PATH}")
    click.echo(f"Config      : {paths.CONFIG_PATH}")
    if not music:
        click.secho("Tip: `lucida config --music \"D:/Music\"` to change the folder "
                    "(or the LUCIDADL_MUSIC variable).", fg="cyan")


# --- setup / doctor / debug -------------------------------------------------

def _music_health(music_dir: str) -> tuple[bool, str]:
    """Check that the configured destination can be created and written."""
    marker = os.path.join(music_dir, f".lucidadl-write-check-{os.getpid()}")
    try:
        os.makedirs(music_dir, exist_ok=True)
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.remove(marker)
        return True, "ready"
    except Exception as exc:
        try:
            os.remove(marker)
        except OSError:
            pass
        return False, str(exc)


def _stale_partials(music_dir: str, older_than: int = 24 * 3600) -> List[str]:
    """Find old temporary downloads only in lucidadl's two staging locations."""
    cutoff = time.time() - older_than
    out: List[str] = []
    for folder in (os.path.join(music_dir, ".incoming"),
                   os.path.join(music_dir, "Music")):
        try:
            for name in os.listdir(folder):
                path = os.path.join(folder, name)
                if name.endswith(".part") and os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    out.append(path)
        except OSError:
            continue
    return out


def _cleanup() -> tuple[int, int, int]:
    state = utils.State(STATE_PATH)
    removed_paths, removed_items = state.prune()
    partials = _stale_partials(paths.default_music_dir())
    removed_partials = 0
    for path in partials:
        try:
            os.remove(path)
            removed_partials += 1
        except OSError as exc:
            click.secho(f"Could not remove {path}: {exc}", fg="yellow")
    click.secho(
        f"Cleanup complete — {removed_paths} missing file reference(s), "
        f"{removed_items} empty item(s), {removed_partials} stale partial file(s) removed.",
        fg="green",
    )
    return removed_paths, removed_items, removed_partials


@cli.command("cleanup")
def cleanup_cmd():
    """Prune missing download records and partial files older than 24 hours."""
    _cleanup()

async def _setup() -> bool:
    click.secho("\nlucidadl setup", bold=True)
    click.echo("[1/2] Checking the browser…")
    if not await chromium_installed():
        click.echo("      Chromium is missing. Downloading it once for lucidadl…")
        if not await install_chromium():
            click.secho("✗ Chromium could not be installed.", fg="red")
            click.echo(f"  Try manually: \"{sys.executable}\" -m playwright install chromium")
            return False
        click.secho("      ✓ Chromium installed", fg="green")
    else:
        click.secho("      ✓ Chromium is ready", fg="green")

    click.echo("[2/2] Opening lucida.to…")
    click.echo("      Complete the Cloudflare check in the browser if it appears.")
    try:
        await acquire_clearance(hidden=False)  # clears CF and SAVES the cookie to disk
    except BrowserClosed:
        click.secho(_CLOSED_HINT, fg="red")
        return False
    except Exception as e:
        click.secho(f"⚠ {e} — try `setup` again.", fg="yellow")
        return False
    click.secho("\n✓ Setup complete. You can now download music with `lucida`.", fg="green")
    click.echo("  The browser will stay closed until Cloudflare asks for a new check.")
    return True


@cli.command()
def setup():
    """Install the browser if needed, then prepare access to lucida.to."""
    if not asyncio.run(_setup()):
        raise click.exceptions.Exit(1)


async def _doctor(live: bool = False) -> bool:
    browser_ok = await chromium_installed()
    ffmpeg_ok = transcode.available()
    cf, ua = load_clearance()
    access_ok = bool(cf and ua)
    music_ok, music_detail = _music_health(paths.default_music_dir())
    stale_partials = _stale_partials(paths.default_music_dir())
    playlist_run = _load_playlist_run()
    playlist_status = playlist_run.get("status") or "none"

    click.secho("\nlucidadl doctor", bold=True)
    click.echo(f"Python      : {sys.version.split()[0]}")
    click.secho(f"Browser     : {'ready' if browser_ok else 'missing'}",
                fg="green" if browser_ok else "yellow")
    click.secho(f"ffmpeg      : {'ready' if ffmpeg_ok else 'missing'}",
                fg="green" if ffmpeg_ok else "yellow")
    click.secho(f"Access      : {'saved' if access_ok else 'not prepared'}",
                fg="green" if access_ok else "yellow")
    click.echo(f"Music       : {paths.default_music_dir()}")
    click.secho(f"Music write : {music_detail if not music_ok else 'ready'}",
                fg="green" if music_ok else "red")
    click.secho(f"Partial files: {len(stale_partials)} stale",
                fg="yellow" if stale_partials else "green")
    click.secho(f"Playlist run: {playlist_status}",
                fg="yellow" if playlist_status in ("running", "incomplete") else "green")
    click.echo(f"App data    : {paths.DATA_DIR}")

    live_ok = True
    if live:
        if not browser_ok:
            click.secho("\nLive check skipped: Chromium is missing.", fg="yellow")
            live_ok = False
        else:
            click.echo("\nOpening the browser to test lucida.to…")
            try:
                async with lucida_context(headless=False) as ctx:
                    live_ok = await ensure_cleared(ctx, timeout=120)
                click.secho("✓ lucida.to is reachable" if live_ok
                            else "⚠ Cloudflare was not cleared",
                            fg="green" if live_ok else "yellow")
            except Exception as e:
                live_ok = False
                click.secho(f"✗ Browser launch failed: {e}", fg="red")
    else:
        click.echo("Live check  : not run (use `lucida doctor --live`)")

    if not browser_ok or not access_ok:
        click.secho("\nNext step: run `lucida setup`.", fg="cyan")
    if stale_partials:
        click.secho("Run `lucida cleanup` to remove stale temporary downloads.", fg="cyan")
    if playlist_status in ("running", "incomplete"):
        click.secho("Run `lucida retry` to continue the unfinished playlist.", fg="cyan")
    elif not live:
        click.secho("\nEverything needed is present.", fg="green")
    return browser_ok and ffmpeg_ok and access_ok and music_ok and live_ok


@cli.command()
@click.option("--live", is_flag=True,
              help="Open the browser and verify access to lucida.to.")
def doctor(live):
    """Check the installation. Add --live for a browser/network test."""
    if not asyncio.run(_doctor(live=live)):
        raise click.exceptions.Exit(1)


async def _debug(query, service, country, item, headless):
    from urllib.parse import urlencode

    svc = normalize_service(service)
    cc = country or default_country(svc)
    q = " ".join(query) or "red hot chili peppers"
    if item:
        target, tag = f"{LUCIDA}/?{urlencode({'url': item, 'country': cc})}", "item"
    else:
        target = f"{LUCIDA}/search?{urlencode({'service': svc, 'country': cc, 'query': q})}"
        tag = "search"

    async with lucida_context(headless=headless, downloads_dir=DEFAULT_OUT) as ctx:
        try:
            if not await ensure_cleared(ctx, timeout=180):
                click.secho("Cloudflare not cleared.", fg="red")
                return
        except BrowserClosed:
            click.secho(_CLOSED_HINT, fg="red")
            return
        page = await get_page(ctx)
        click.echo(f"Navigating: {target}")
        try:
            await page.goto(target, wait_until="networkidle", timeout=60_000)
        except Exception as e:
            click.echo(f"goto: {e}")
        await page.wait_for_timeout(4000)
        html = await page.content()
        with open(paths.cwd(f"{tag}_debug.html"), "w", encoding="utf-8") as f:
            f.write(html)
        try:
            await page.screenshot(path=paths.cwd(f"{tag}_debug.png"), full_page=True)
        except Exception:
            pass
        click.echo(f"  HTML {len(html)} bytes -> {tag}_debug.html (+ .png)")
        for m in ('const data = [', 'songs-list-row', 'button.download-button',
                  'download-button'):
            click.echo(f"  marker {m!r}: {'YES' if m in html else 'no'}")
        click.secho("Window left OPEN. Press Enter to close.", fg="yellow")
        try:
            await asyncio.to_thread(input)
        except Exception:
            pass


@cli.command("debug", hidden=True)
@click.argument("query", nargs=-1)
@click.option("-s", "--service", default="qobuz", help="Service to diagnose (def: qobuz).")
@click.option("--country", default=None, help="Country code (def: US for qobuz).")
@click.option("--item", default=None, help="Load this item URL instead of a search.")
@click.option("--headless", is_flag=True, help="(dev) force headless, normally blocked by CF.")
def debug_cmd(query, service, country, item, headless):
    """(dev) Diagnostic: open a page, capture HTML + screenshot, keep the window open."""
    asyncio.run(_debug(query, service, country, item, headless))


if __name__ == "__main__":
    cli()
