"""Command-line interface for lucidadl (async download core)."""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from typing import List, Optional

import click

from . import api, organize, paths, progress, transcode, utils
from .api import LucidaClient, default_country, normalize_service, DOWNSCALE_CHOICES, LUCIDA
from .downloader import run_batch
from .session import (lucida_context, ensure_cleared, get_page, BrowserClosed,
                      acquire_clearance, load_clearance)

# App data (cookie/profile/state/log/failed list) lives in the fixed user data dir.
# Downloads go to ONE fixed, configurable music directory (never the cwd). Only the
# watchlist inputs stay next to you, so you can edit them where you work.
STATE_PATH = paths.STATE_PATH
DEFAULT_OUT = paths.default_music_dir()
INPUTS = paths.cwd("inputs")
LOG_PATH = paths.LOG_PATH
FAILED_PATH = paths.FAILED_PATH


def _write_failed(items) -> None:
    try:
        with open(FAILED_PATH, "w", encoding="utf-8") as f:
            f.write("# Failed items — re-run with: lucidadl retry\n")
            f.write("\n".join(items) + "\n")
    except Exception as e:
        # don't fail silently: the user is told to `retry`, but the list wasn't saved.
        click.secho(f"⚠ couldn't write {FAILED_PATH} ({e}) — `retry` won't have these "
                    f"items. Re-run them manually:", fg="yellow")
        for it in items:
            click.echo(f"    {it}")

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
    f = click.option("-j", "--jobs", default=3, type=click.IntRange(1, 20),
                     help="Parallel downloads (1–20, def 3).")(f)
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
@click.version_option(message="lucidadl %(version)s")
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
               quiet_resolve: bool = False) -> None:
    if not items:
        click.secho("Nothing to download.", fg="yellow")
        return
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
            return
    if organize_on and not organize.mutagen_available():
        click.secho("⚠ mutagen not found — tags can't be read; sorting by "
                    "artist/album will rely on the API metadata (otherwise "
                    "\"Unknown\"). Install it: pip install mutagen", fg="yellow")
    os.makedirs(out, exist_ok=True)
    state = utils.State(STATE_PATH)
    logf = open(LOG_PATH, "w", encoding="utf-8")
    reporter = progress.make_reporter(echo=click.echo, logfile=logf)
    log = reporter.log

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
                return
            except Exception as e:
                log(f"Couldn't clear Cloudflare: {e}. Run `setup`.")
                return

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
                                             quiet_resolve=quiet_resolve)
        finally:
            await client.aclose()
        log(f"\nDone — OK:{totals['ok']}  skipped:{totals['skip']}  failed:{totals['fail']}")
        if failed:
            _write_failed(failed)
            log(f"  → {len(failed)} failure(s) written to {FAILED_PATH} "
                f"(re-run: lucida retry)")
    except Exception as e:
        log(f"FATAL ERROR: {e}")
        log(traceback.format_exc())
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


# --- ad-hoc (singular): args, no dedup -------------------------------------

@cli.command("track")
@click.argument("items", nargs=-1, required=True)
@_service_opts
def track_cmd(items, service, country, downscale, out, organize_on, jobs, to_fmt,
              bitrate, keep_orig, force, hidden):
    """One or more tracks now: track "artist - title" (or URL). Forces the DL."""
    asyncio.run(_run(list(items), "track", service, country, downscale, out,
                     hidden, jobs, dedup=False, organize_on=organize_on,
                     to_fmt=to_fmt, bitrate=bitrate, keep_orig=keep_orig, force=force))


@cli.command("album")
@click.argument("items", nargs=-1, required=True)
@_service_opts
def album_cmd(items, service, country, downscale, out, organize_on, jobs,
              to_fmt, bitrate, keep_orig, force, hidden):
    """One or more albums now: album "artist - album" (or URL), expanded track by track."""
    asyncio.run(_run(list(items), "album", service, country, downscale, out,
                     hidden, jobs, dedup=False, organize_on=organize_on,
                     to_fmt=to_fmt, bitrate=bitrate, keep_orig=keep_orig, force=force))


# --- watchlist (plural): file, with dedup ----------------------------------

@cli.command("tracks")
@click.option("-f", "--file", "file", default=os.path.join(INPUTS, "tracks.txt"),
              help="File of titles/URLs, one per line (def: inputs/tracks.txt).")
@_service_opts
def tracks_cmd(file, service, country, downscale, out, organize_on, jobs, to_fmt,
               bitrate, keep_orig, force, hidden):
    """Tracks watchlist: download inputs/tracks.txt (dedup enabled)."""
    asyncio.run(_run(_read_lines(file), "track", service, country, downscale, out,
                     hidden, jobs, dedup=True, organize_on=organize_on,
                     to_fmt=to_fmt, bitrate=bitrate, keep_orig=keep_orig, force=force))


@cli.command("albums")
@click.option("-f", "--file", "file", default=os.path.join(INPUTS, "albums.txt"),
              help="File of albums/URLs, one per line (def: inputs/albums.txt).")
@_service_opts
def albums_cmd(file, service, country, downscale, out, organize_on, jobs,
               to_fmt, bitrate, keep_orig, force, hidden):
    """Albums watchlist: download inputs/albums.txt (dedup enabled)."""
    asyncio.run(_run(_read_lines(file), "album", service, country, downscale, out,
                     hidden, jobs, dedup=True, organize_on=organize_on,
                     to_fmt=to_fmt, bitrate=bitrate, keep_orig=keep_orig, force=force))


@cli.command("retry")
@_service_opts
def retry_cmd(service, country, downscale, out, organize_on, jobs, to_fmt,
              bitrate, keep_orig, force, hidden):
    """Re-run the failed items from the last run (failed.txt)."""
    items = _read_lines(FAILED_PATH)
    if not items:
        click.secho("No failures to re-run (failed.txt is empty).", fg="yellow")
        return
    asyncio.run(_run(items, "track", service, country, downscale, out,
                     hidden, jobs, dedup=True, organize_on=organize_on,
                     to_fmt=to_fmt, bitrate=bitrate, keep_orig=keep_orig, force=force))


# --- interactive search ----------------------------------------------------

async def _search_entries(query, service, hidden=False):
    """Run a search and return a flat list of (kind, item) entries (albums then tracks).
    Shared by the CLI `search` prompt and the TUI's arrow-key picker."""
    cc = default_country(service)
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
                  to_fmt, bitrate, keep_orig, hidden, force=False):
    cc = country or default_country(service)
    cf, ua = load_clearance()
    if not (cf and ua):
        try:
            cf, ua = await acquire_clearance(hidden=hidden)
        except Exception as e:
            click.secho(f"Cloudflare: {e}. Run `setup`.", fg="red")
            return
    client = LucidaClient(cf, ua, acquire=lambda: acquire_clearance(hidden=hidden),
                          country=cc, downscale=downscale, log=click.echo)
    await client.start_http()
    try:
        res = await client.search(query, service)
    finally:
        await client.aclose()

    entries = []
    albums, tracks = res.get("albums") or [], res.get("tracks") or []
    if albums:
        click.secho("\nAlbums:", fg="cyan")
        for it in albums[:15]:
            entries.append(("album", it))
            click.echo(f"  {len(entries):>2}. {it.get('title', '?')} — {it.get('artist', '?')}")
    if tracks:
        click.secho("\nTracks:", fg="cyan")
        for it in tracks[:15]:
            entries.append(("track", it))
            alb = f"  [{it.get('album', '')}]" if it.get("album") else ""
            click.echo(f"  {len(entries):>2}. {it.get('title', '?')} — {it.get('artist', '?')}{alb}")
    if not entries:
        click.secho("No results.", fg="yellow")
        return

    try:
        sel = (await asyncio.to_thread(input, "\nNumber to download (Enter = cancel): ")).strip()
    except EOFError:
        sel = ""
    if not sel.isdigit() or not (1 <= int(sel) <= len(entries)):
        click.echo("Cancelled.")
        return
    kind, item = entries[int(sel) - 1]
    await _run([item["url"]], kind, service, country, downscale, out, hidden, jobs,
               dedup=False, organize_on=organize_on, to_fmt=to_fmt, bitrate=bitrate,
               keep_orig=keep_orig, force=force)


@cli.command("search")
@click.argument("query", nargs=-1, required=True)
@_service_opts
def search_cmd(query, service, country, downscale, out, organize_on, jobs,
               to_fmt, bitrate, keep_orig, force, hidden):
    """Interactive search: lists the results, downloads the one you pick."""
    asyncio.run(_search(" ".join(query), service, country, downscale, out, organize_on,
                        jobs, to_fmt, bitrate, keep_orig, hidden, force=force))


# --- playlist import (Apple Music / Spotify / Deezer / Tidal) --------------

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


async def _playlist(url, dry_run, service, country, downscale, out, hidden,
                    jobs, organize_on=True, to_fmt=None, bitrate=None, keep_orig=False,
                    force=False):
    cc = country or default_country(service)
    name, tracks = "", []

    async def _scrape(headless: bool):
        # Apple Music is NOT behind Cloudflare, so the tracklist can be scraped headless
        # (no visible window). `hidden` only matters for the headed fallback.
        async with lucida_context(headless=headless,
                                  hidden=(hidden and not headless)) as ctx:
            page = await get_page(ctx)
            return await api.playlist_tracklist(page, url, click.echo)

    try:
        click.echo("Reading the playlist (headless browser)…")
        try:
            name, tracks = await _scrape(headless=True)
        except BrowserClosed:
            raise
        except Exception as e:
            click.secho(f"  headless: {e}", fg="yellow")  # fall through to the headed retry
        if not tracks:
            click.secho("Headless unsuccessful — retrying with a visible window…",
                        fg="yellow")
            name, tracks = await _scrape(headless=False)
    except BrowserClosed:
        click.secho(_CLOSED_HINT, fg="red")
        return
    except Exception as e:
        click.secho(f"✗ playlist extraction: {e}", fg="red")
        return

    if not tracks:
        click.secho("No tracks extracted (a <source>_debug.html may have been written).", fg="red")
        return

    collection = name or "Playlist"
    items = [f"{t['artist']} - {t['title']}" for t in tracks]
    try:
        os.makedirs(INPUTS, exist_ok=True)
        with open(os.path.join(INPUTS, "playlist.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(items) + "\n")
    except Exception:
        pass

    _render_playlist(collection, tracks, dry_run)
    if dry_run:
        click.secho("--dry-run: nothing downloaded (list saved to inputs/playlist.txt).",
                    fg="yellow")
        return
    await _run(items, "track", service, country, downscale, out, hidden, jobs,
               dedup=True, organize_on=organize_on, to_fmt=to_fmt, bitrate=bitrate,
               keep_orig=keep_orig, collection=collection, force=force, quiet_resolve=True)


@cli.command("playlist")
@click.argument("url")
@click.option("--dry-run", is_flag=True, help="List the tracks without downloading.")
@_service_opts
def playlist_cmd(url, dry_run, service, country, downscale, out, organize_on, jobs,
                 to_fmt, bitrate, keep_orig, force, hidden):
    """Import an Apple Music playlist (public link) → download each track via lucida.
    (Spotify/Deezer/Tidal coded but disabled for now.)"""
    asyncio.run(_playlist(url, dry_run, service, country, downscale, out, hidden,
                          jobs, organize_on, to_fmt=to_fmt, bitrate=bitrate,
                          keep_orig=keep_orig, force=force))


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
    click.echo(f"Config      : {paths.CONFIG_PATH}")
    if not music:
        click.secho("Tip: `lucida config --music \"D:/Music\"` to change the folder "
                    "(or the LUCIDADL_MUSIC variable).", fg="cyan")


# --- setup / doctor / debug -------------------------------------------------

async def _setup():
    click.echo("Opening the browser… (solve any captcha)")
    try:
        await acquire_clearance(hidden=False)  # clears CF and SAVES the cookie to disk
    except BrowserClosed:
        click.secho(_CLOSED_HINT, fg="red")
        return
    except Exception as e:
        click.secho(f"⚠ {e} — try `setup` again.", fg="yellow")
        return
    click.secho("✓ Cloudflare passed, cookie saved (downloads won't open a browser "
                "anymore as long as it's valid).", fg="green")


@cli.command()
def setup():
    """Open the browser once to pass Cloudflare and save the cookie."""
    asyncio.run(_setup())


async def _doctor():
    click.echo(f"Python      : {sys.version.split()[0]}")
    try:
        import playwright  # noqa
        click.echo("Playwright  : installed")
    except Exception as e:
        click.secho(f"Playwright  : MISSING ({e})", fg="red")
    click.echo(f"Data        : {paths.DATA_DIR}")
    click.echo(f"Profile     : {paths.PROFILE_DIR}")
    click.echo(f"Music       : {DEFAULT_OUT}")
    click.echo(f"State/dedup : {STATE_PATH}")
    click.echo(f"Log         : {LOG_PATH}")
    click.echo("Testing browser + Cloudflare…")
    try:
        async with lucida_context(headless=False) as ctx:
            ok = await ensure_cleared(ctx, timeout=120)
            click.secho("✓ lucida.to reachable" if ok else "⚠ Cloudflare not cleared",
                        fg="green" if ok else "yellow")
    except Exception as e:
        click.secho(f"✗ Browser launch failed: {e}", fg="red")


@cli.command()
def doctor():
    """Diagnose the environment and lucida.to reachability."""
    asyncio.run(_doctor())


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


@cli.command("debug")
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
