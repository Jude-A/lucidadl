"""Interactive terminal menu (`lucida ui`).

A small, dependency-light front-end over the existing async runners. Each menu action
gathers a couple of inputs with questionary, then dispatches to the very same code the
plain commands use (so behaviour is identical, just easier to drive). Download progress
is rendered by the rich reporter in :mod:`lucidadl.progress`.
"""

from __future__ import annotations

import asyncio
import os
import sys

from . import paths, transcode
from .session import load_clearance

_TO_NONE = "(none — keep the source format)"
_SERVICES = ["qobuz", "amazon"]
# actions whose output is worth reading before the menu redraws (we pause after them)
_PAUSE_AFTER = {"download", "playlist", "batch", "retry", "tools", "onboarding"}


def _isatty(stream) -> bool:
    # stdin/stdout can be None under pythonw / detached GUI contexts
    return bool(getattr(stream, "isatty", lambda: False)())


def _onoff(b: bool) -> str:
    return "yes" if b else "no"


def _is_first_run() -> bool:
    return not (os.path.exists(paths.CONFIG_PATH) or os.path.exists(paths.CLEARANCE_PATH))


def _access_ready() -> bool:
    cf, user_agent = load_clearance()
    return bool(cf and user_agent)


# --- persisted settings -----------------------------------------------------

def _settings() -> dict:
    cfg = paths.load_config()
    try:
        jobs = max(1, min(100, int(cfg.get("jobs", 3) or 3)))
    except (TypeError, ValueError):
        jobs = 3
    return {
        "jobs": jobs,
        "service": cfg.get("service") if cfg.get("service") in _SERVICES else "qobuz",
        "to": cfg.get("to") or None,
        "bitrate": cfg.get("bitrate") or None,
        "force": bool(cfg.get("force", False)),
        "keep_orig": bool(cfg.get("keep_orig", False)),
    }


def _save_settings(s: dict) -> None:
    cfg = paths.load_config()
    for k in ("jobs", "service", "to", "bitrate", "force", "keep_orig"):
        cfg[k] = s[k]
    paths.save_config(cfg)


def _to_label(s: dict) -> str:
    if s["to"] and s["bitrate"]:
        return f"{s['to']} @ {s['bitrate']}"
    return s["to"] or "original format (no transcoding)"


def _opts_line(s: dict) -> str:
    bits = [f"{s['jobs']} concurrent downloads", s["service"], _to_label(s)]
    if s["force"]:
        bits.append("force")
    if s["keep_orig"]:
        bits.append("keep FLAC")
    return "  ·  ".join(bits)


# --- small input helpers ----------------------------------------------------

def _ask_text(questionary, message: str, instruction: str = "", default: str = "") -> str:
    """Text prompt that collapses cancel/empty/whitespace into '' (caller returns)."""
    v = questionary.text(message, instruction=instruction, default=default).ask()
    return (v or "").strip()


def _open_path(path: str, console) -> None:
    try:
        if os.name == "nt":
            os.startfile(path)  # noqa: F821 (Windows only)
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", path])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path])
        console.print(f"[green]Opened: {path}[/]")
    except Exception as e:
        console.print(f"[yellow]Could not open {path}: {e}[/]")


def _show_run_summary(result, out: str, console) -> None:
    """Give menu users one stable, readable outcome after the live progress output."""
    if not result:
        return
    from rich.panel import Panel
    totals, remaining = result
    failed = max(totals.get("fail", 0), len(remaining))
    color = "red" if failed else "green"
    next_step = ("\n[bold]Next:[/] choose Retry failures from the main menu."
                 if failed else "")
    console.print(Panel(
        f"[green]{totals.get('ok', 0)} downloaded[/]  ·  "
        f"{totals.get('skip', 0)} already present  ·  "
        f"[{color}]{failed} failed[/]\n"
        f"[dim]Files:[/] {out}{next_step}",
        title="Download complete" if not failed else "Download incomplete",
        border_style=color,
        expand=False,
    ))


# --- main loop --------------------------------------------------------------

def run() -> None:
    if not (_isatty(sys.stdin) and _isatty(sys.stdout)):
        print("`lucida ui` needs an interactive terminal. "
              "Use the direct commands instead (e.g. lucida track \"…\").")
        return
    try:
        import questionary
        from questionary import Choice
        from rich.console import Console
        from rich.panel import Panel
    except Exception as e:  # pragma: no cover
        print(f"UI unavailable ({e}). Install: pip install rich questionary")
        return

    console = Console()
    from . import cli  # deferred: cli imports tui for the command

    while True:
        s = _settings()
        access_saved = _access_ready()
        access = "[green]prepared[/]" if access_saved else "[yellow]setup needed[/]"
        console.print(Panel(
            f"[bold]{_opts_line(s)}[/]\n"
            f"[dim]Music:[/] {paths.default_music_dir()}\n"
            f"[dim]Access:[/] {access}",
            title="[bold cyan]lucidadl[/]", border_style="cyan", expand=False,
        ))

        if _is_first_run():
            console.print(Panel(
                "Welcome! Start with [bold]Set up lucidadl[/]. It checks the browser, "
                "lets you confirm the music folder, and prepares access to lucida.to.",
                title="First time here?", border_style="yellow", expand=False,
            ))

        menu = []
        if not access_saved:
            menu.append(Choice("✨  Set up lucidadl (recommended)", "onboarding"))
        menu += [
            Choice("⬇   Download music", "download"),
            Choice("🎶  Playlists — streaming link or an edited list", "playlist"),
            Choice("📄  Download from a .txt file", "batch"),
        ]
        failed = cli._read_failed()
        pending_playlist = cli._pending_playlist_run()
        if pending_playlist:
            menu.append(Choice(
                f"🔁  Resume {pending_playlist.get('collection', 'playlist')}", "retry"))
        elif failed:
            menu.append(Choice(f"🔁  Retry failures ({len(failed)})", "retry"))
        menu += [
            Choice("⚙   Settings", "settings"),
            Choice("🧰  Help, access and diagnostics", "tools"),
            Choice("🚪  Quit", "quit"),
        ]
        action = questionary.select("What do you want to do?", choices=menu,
                                    qmark="►", instruction="(↑/↓, Enter)").ask()

        if action in (None, "quit"):
            console.print("See you soon.")
            return
        ran = False
        try:
            ran = _dispatch(action, s, console, cli, questionary)
        except KeyboardInterrupt:
            console.print("[yellow]Interrupted.[/]")
            ran = True
        except Exception as e:
            console.print(f"[red]Error: {e}[/]")
            ran = True
        if ran and action in _PAUSE_AFTER:
            try:
                questionary.text("Enter to return to the menu…").ask()
            except Exception:
                pass


def _dispatch(action, s, console, cli, questionary) -> bool:
    """Returns True if an action actually ran (worth pausing on), False if cancelled."""
    out = paths.default_music_dir()

    def _warn_browser():
        if not _access_ready():
            console.print("[dim]A browser may open briefly to get past "
                          "Cloudflare — that's normal.[/]")

    def go(items, kind, dedup, collection=None):
        _warn_browser()
        result = asyncio.run(cli._run(
            items, kind, s["service"], None, "original", out, hidden=False,
            jobs=s["jobs"], dedup=dedup, organize_on=True, to_fmt=s["to"],
            bitrate=s["bitrate"], keep_orig=s["keep_orig"], collection=collection,
            force=s["force"],
        ))
        _show_run_summary(result, out, console)
        return result

    if action == "onboarding":
        return _onboarding_action(console, cli, questionary)

    if action == "download":
        return _download_action(s, console, cli, questionary, go)

    if action == "track":
        q = _ask_text(questionary, "Track or URL:",
                      "(e.g. Daft Punk - Around the World ; empty = back)")
        if not q:
            return False
        go([q], "track", dedup=False)
        return True

    if action == "album":
        q = _ask_text(questionary, "Album or URL:",
                      "(e.g. Daft Punk - Discovery ; empty = back)")
        if not q:
            return False
        go([q], "album", dedup=False)
        return True

    if action == "playlist":
        from questionary import Choice
        source = questionary.select("Playlist source:", choices=[
            Choice("🔗  Public streaming link", "remote"),
            Choice("📄  Edited .txt list", "file"),
            Choice("← Back", "back"),
        ]).ask()
        if source in (None, "back"):
            return False
        if source == "remote":
            url = _ask_text(
                questionary, "Playlist URL:",
                "(public Apple Music, Spotify or Deezer link; empty = back)",
            )
            if not url:
                return False
        else:
            raw_path = _ask_text(
                questionary, "Playlist text file:", "(one artist - title per line; empty = back)",
                default=cli.PLAYLIST_TEXT_PATH,
            )
            if not raw_path:
                return False
            file_path = os.path.abspath(os.path.expandvars(os.path.expanduser(
                raw_path.strip('"'))))
            if not os.path.isfile(file_path):
                console.print(f"[red]File not found: {file_path}[/]")
                return True
            items = cli._read_lines(file_path)
            if not items:
                console.print("[yellow]The playlist file is empty.[/]")
                return True
            name = _ask_text(questionary, "Playlist name:", "(empty = back)")
            if not name:
                return False
        mode = questionary.select("What should lucidadl do?", choices=[
            Choice("⬇   Download or resume this playlist", "download"),
            Choice("✓   Check every automatic match first", "check"),
            *([Choice("📄  Only extract and save the track list", "list")]
              if source == "remote" else []),
            Choice("← Back", "back"),
        ]).ask()
        if mode in (None, "back"):
            return False
        if not (source == "remote" and mode == "list"):
            _warn_browser()
        if source == "remote":
            asyncio.run(cli._playlist(
                url, mode == "list", s["service"], None, "original", out,
                hidden=False, jobs=s["jobs"], organize_on=True, to_fmt=s["to"],
                bitrate=s["bitrate"], keep_orig=s["keep_orig"], force=s["force"],
                check_matches=mode == "check"))
        elif mode == "check":
            asyncio.run(cli._check_playlist_matches(
                name, items, s["service"], None, hidden=False, jobs=s["jobs"],
                edit_path=file_path))
        else:
            asyncio.run(cli._download_playlist_items(
                name, items, file_path, s["service"], None, "original", out,
                hidden=False, jobs=s["jobs"], organize_on=True, to_fmt=s["to"],
                bitrate=s["bitrate"], keep_orig=s["keep_orig"], force=s["force"]))
        return True

    if action == "batch":
        return _batch_action(console, cli, questionary, go)

    if action == "retry":
        pending = cli._pending_playlist_run()
        if pending:
            _warn_browser()
            result = asyncio.run(cli._resume_playlist_run(pending))
            _show_run_summary(result, cli._playlist_run_options(pending)["out"], console)
            return True
        items = cli._read_failed()
        if not items:
            console.print("[yellow]No failures to retry.[/]")
            return False
        _warn_browser()
        result = asyncio.run(cli._retry(
            items, s["service"], None, "original", out, hidden=False, jobs=s["jobs"],
            organize_on=True, to_fmt=s["to"], bitrate=s["bitrate"],
            keep_orig=s["keep_orig"], force=s["force"],
        ))
        _show_run_summary(result, out, console)
        return True

    if action == "settings":
        _settings_menu(s, console, questionary)
        return False

    if action == "tools":
        return _tools_action(console, cli, questionary)

    return False


def _onboarding_action(console, cli, questionary) -> bool:
    """Small first-run flow: confirm the destination, then prepare browser access."""
    folder = paths.default_music_dir()
    console.print(f"\nMusic will be saved in:\n[bold]{folder}[/]")
    keep = questionary.confirm("Use this folder?", default=True).ask()
    if keep is None:
        return False
    if not keep:
        chosen = _ask_text(questionary, "Music folder:", "(empty = cancel)")
        if not chosen:
            return False
        folder = paths.set_music_dir(chosen)
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception as e:
            console.print(f"[red]Could not create the music folder: {e}[/]")
            return True
        console.print(f"[green]✓ Music folder saved: {folder}[/]")
    console.print("\nNext, lucidadl will prepare its browser access once.")
    return bool(asyncio.run(cli._setup()))


def _download_action(s, console, cli, questionary, go) -> bool:
    """Group the three everyday download paths behind one clear menu."""
    from questionary import Choice
    action = questionary.select("Download music:", choices=[
        Choice("🎵  Track — enter an artist, title or URL", "track"),
        Choice("💿  Album — enter an artist, album or URL", "album"),
        Choice("🔎  Search — browse results before downloading", "search"),
        Choice("← Back", "back"),
    ]).ask()
    if action in (None, "back"):
        return False
    if action == "search":
        return _search_action(s, console, cli, questionary, go)
    prompt = "Track or URL:" if action == "track" else "Album or URL:"
    example = ("(e.g. Daft Punk - Around the World ; empty = back)"
               if action == "track" else
               "(e.g. Daft Punk - Discovery ; empty = back)")
    value = _ask_text(questionary, prompt, example)
    if not value:
        return False
    go([value], action, dedup=False)
    return True


def _tools_action(console, cli, questionary) -> bool:
    from questionary import Choice
    action = questionary.select("Help and diagnostics:", choices=[
        Choice("🛡   Prepare or refresh access", "setup"),
        Choice("🩺  Check the installation", "doctor"),
        Choice("🌐  Test browser + lucida.to", "doctor-live"),
        Choice("🧹  Clean stale state and temporary files", "cleanup"),
        Choice("📂  Open the music folder", "openfolder"),
        Choice("📄  Open the last run log", "log"),
        Choice("← Back", "back"),
    ]).ask()
    if action in (None, "back"):
        return False
    if action == "setup":
        return bool(asyncio.run(cli._setup()))
    if action in ("doctor", "doctor-live"):
        asyncio.run(cli._doctor(live=action == "doctor-live"))
        return True
    if action == "cleanup":
        confirmed = questionary.confirm(
            "Remove missing state entries and .part files older than 24 hours?",
            default=True,
        ).ask()
        if not confirmed:
            return False
        cli._cleanup()
        return True
    if action == "openfolder":
        _open_path(paths.default_music_dir(), console)
        return False
    if os.path.exists(paths.LOG_PATH):
        _open_path(paths.LOG_PATH, console)
    else:
        console.print("[yellow]No download log yet.[/]")
    return False


def _search_action(s, console, cli, questionary, go) -> bool:
    from questionary import Choice
    q = _ask_text(questionary, "Search:", "(empty = back)")
    if not q:
        return False
    if not os.path.exists(paths.CLEARANCE_PATH):
        console.print("[dim]A browser may open briefly to get past "
                      "Cloudflare — that's normal.[/]")
    try:
        entries = asyncio.run(cli._search_entries(q, s["service"]))
    except Exception as e:
        console.print(f"[red]Search failed: {e} (try \"Prepare access\").[/]")
        return True
    if not entries:
        console.print("[yellow]No results.[/]")
        return True
    choices = []
    for kind, it in entries:
        tag = "💿" if kind == "album" else "🎵"
        alb = f"  [{it.get('album')}]" if it.get("album") else ""
        choices.append(Choice(f"{tag} {it.get('title', '?')} — {it.get('artist', '?')}{alb}",
                              (kind, it)))
    choices.append(Choice("← Cancel", "cancel"))
    pick = questionary.select("Result to download:", choices=choices).ask()
    if pick in (None, "cancel"):
        return False
    kind, item = pick
    go([item["url"]], kind, dedup=False)
    return True


def _batch_action(console, cli, questionary, go) -> bool:
    """Download a one-off batch from a user-owned text file; never edit that file."""
    from questionary import Choice
    which = questionary.select("What does the file contain?", choices=[
        Choice("🎵  Tracks — one search or URL per line", "tracks"),
        Choice("💿  Albums — one search or URL per line", "albums"),
        Choice("← Back", "back"),
    ]).ask()
    if which in (None, "back"):
        return False
    kind = "track" if which == "tracks" else "album"
    default = os.path.abspath(os.path.join(cli.INPUTS, f"{which}.txt"))
    raw_path = _ask_text(
        questionary, "Text file:",
        "(comments starting with # and blank lines are ignored; empty = back)",
        default=default,
    )
    if not raw_path:
        return False
    file_path = os.path.abspath(os.path.expandvars(os.path.expanduser(
        raw_path.strip('"'))))
    if not os.path.isfile(file_path):
        console.print(f"[red]File not found: {file_path}[/]")
        return True
    lines = cli._read_lines(file_path)
    if not lines:
        console.print("[yellow]The file contains no items to download.[/]")
        return True
    label = "tracks" if kind == "track" else "albums"
    confirmed = questionary.confirm(
        f"Download {len(lines)} {label} from {os.path.basename(file_path)}?",
        default=True,
    ).ask()
    if not confirmed:
        return False
    console.print(f"[dim]The source file will not be modified: {file_path}[/]")
    go(lines, kind, dedup=True)
    return True


# --- settings : scroll a list, edit ONE row, ← Back to leave ----------------

def _settings_menu(s, console, questionary) -> None:
    from questionary import Choice
    while True:
        choice = questionary.select(
            "Settings — choose an item to change:",
            choices=[
                Choice(f"Parallel downloads: {s['jobs']}", "jobs"),
                Choice(f"Service: {s['service']}", "service"),
                Choice(f"Transcoding: {_to_label(s)}", "to"),
                Choice(f"Keep the original FLAC: {_onoff(s['keep_orig'])}", "keep"),
                Choice(f"Force re-download: {_onoff(s['force'])}", "force"),
                Choice(f"Music folder: {paths.default_music_dir()}", "music"),
                Choice("← Back", "back"),
            ],
            qmark="⚙", instruction="(↑/↓, Enter ; Esc = back)",
        ).ask()
        if choice in (None, "back"):
            return
        _edit_setting(choice, s, console, questionary)


def _edit_setting(key, s, console, questionary) -> None:
    if key == "jobs":
        v = questionary.text("Parallel downloads (1–100):", default=str(s["jobs"]),
                             validate=lambda x: x.isdigit() and 1 <= int(x) <= 100).ask()
        if not v:
            return
        s["jobs"] = int(v)
        _save_settings(s)
    elif key == "service":
        v = questionary.select("Service:",
                               default=s["service"] if s["service"] in _SERVICES else "qobuz",
                               choices=_SERVICES).ask()
        if not v:
            return
        s["service"] = v
        _save_settings(s)
    elif key == "to":
        cur = s["to"] if s["to"] in transcode.CHOICES else _TO_NONE
        to = questionary.select("Local transcoding (ffmpeg):", default=cur,
                                choices=[_TO_NONE] + list(transcode.CHOICES)).ask()
        if to is None:
            return
        s["to"] = None if to == _TO_NONE else to
        if s["to"]:
            br = questionary.text("Bitrate (e.g. 320k, 256k, 192k ; empty = default):",
                                  default=s["bitrate"] or "").ask()
            s["bitrate"] = (br or "").strip() or None
        else:
            s["bitrate"] = None
        _save_settings(s)
    elif key == "keep":
        v = questionary.confirm("Keep the original FLAC next to the transcoded file?",
                                default=s["keep_orig"]).ask()
        if v is None:
            return
        s["keep_orig"] = bool(v)
        _save_settings(s)
    elif key == "force":
        v = questionary.confirm("Force re-download (ignore dedup)?",
                                default=s["force"]).ask()
        if v is None:
            return
        s["force"] = bool(v)
        _save_settings(s)
    elif key == "music":
        m = questionary.text("Music folder:", default=paths.default_music_dir()).ask()
        if not m or not m.strip():
            return
        paths.set_music_dir(m.strip())
        try:
            os.makedirs(paths.default_music_dir(), exist_ok=True)
        except Exception:
            pass
    console.print("[green]✓ Saved.[/]")
