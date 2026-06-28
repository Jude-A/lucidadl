# lucidadl

[![PyPI](https://img.shields.io/pypi/v/lucidadl.svg)](https://pypi.org/project/lucidadl/)
[![CI](https://github.com/Jude-A/lucidadl/actions/workflows/ci.yml/badge.svg)](https://github.com/Jude-A/lucidadl/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python versions](https://img.shields.io/pypi/pyversions/lucidadl.svg)](https://pypi.org/project/lucidadl/)

Fast, parallel command-line music downloader for [lucida.to](https://lucida.to) — fetch
tracks, albums and playlists from **Qobuz** and **Amazon Music** in lossless **FLAC** (or
transcode to MP3 / AAC / Opus), neatly organized by tags. Like `yt-dlp`, but for lucida —
with an interactive terminal UI on top.

**A vibe-coded project.** lucidadl was built quickly and AI-assisted ("vibe coding"). It
wraps and automates downloading from [lucida.to](https://lucida.to) — inspired by two
existing open-source lucida downloaders we looked at (see [Credits](#credits)) and adding
the features I wanted on top: parallel downloads, local transcoding, tag-based
organization, playlist import, existence-aware dedup, and an interactive menu.

> **Disclaimer.** This is a personal-use tool, similar in spirit to `yt-dlp`. You are
> responsible for complying with the terms of service of lucida.to and of the source
> services, and with the copyright law of your jurisdiction. The authors are not
> affiliated with lucida.to or any streaming service. Use it for content you are
> entitled to download.

## Features

- **Search or URL** — `track "artist - title"`, `album "artist - album"`, or paste a
  Qobuz/Amazon URL.
- **Parallel downloads** over HTTP (`--jobs N`) — no browser kept open, low RAM.
- **Albums = track by track**, expanded and downloaded in parallel (faster than the
  native album zip).
- **Local transcoding** with ffmpeg (`--to mp3 --bitrate 320k`) — bundled, nothing to
  install. Tags and cover art preserved.
- **Tag-based organization** into `Artists/<Artist>/<Album>/…` (falls back to the
  source's artist/album metadata when a file has no embedded tags, so nothing lands in
  "Unknown"); playlists go under `Playlists/<name>/`, kept separate from artists.
- **Watchlists** with dedup (`tracks` / `albums` read a file, skip what's done — but a
  file you deleted is re-downloaded; `--force` ignores the memory entirely).
- **Playlist import** — paste a playlist link and get every track via lucida. Apple
  Music is wired up today; adding more sources is easy (PRs welcome).
- **Interactive search**, **service fallback** (Qobuz → Amazon), **retry** of failures.
- **Interactive menu** (`lucida ui`, or just `lucida`) and **live progress bars** — one
  bar per parallel download, in any real terminal.

## Demo

Run `lucida` with no arguments for the interactive menu:

```text
lucidadl  ·  8 concurrent downloads · qobuz · → flac
~/Downloads/music

► What do you want to do?  (↑/↓, Enter)
  🎵  Download a track
  💿  Download an album
  🅰️   Import an Apple Music playlist
  🔎  Interactive search
  📜  Watchlists (tracks / albums)
  ⚙   Settings
  🚪  Quit
```

…or go straight to a command and watch one progress bar per parallel download:

```text
$ lucida album "Red Hot Chili Peppers - Californication" --to flac -j 8

  ✓ Californication/Around the World.flac   (29.1 MB)
  ✓ Californication/Otherside.flac          (27.6 MB)
  Scar Tissue            ━━━━━━━━━━━━━━━━╸━━━━━   68%  ·  3.9 MB/s
  Get on Top             ━━━━━━━╸━━━━━━━━━━━━━━   31%  ·  4.2 MB/s
  Around the World       ━━━━━━━━━━━━━━━━━━━━━━  100%
  Done — OK:15  skipped:0  failed:0
  → Files in ~/Downloads/music · log: …/run.log
```

## How it works (Cloudflare)

lucida.to is behind Cloudflare and plain HTTP gets a `403`. lucidadl solves the
challenge **once** in a real browser (`setup`), caches the `cf_clearance` cookie, then
runs **everything else over `httpx`** — no browser stays open. The browser is only
re-opened briefly if the cookie expires. Solving the challenge needs a logged-in
desktop session (it can't run on a locked/headless server).

## Requirements

- Python **3.10+**
- A desktop session for the one-time Cloudflare `setup` (Windows/macOS/Linux).

## Install

`lucidadl` is on [PyPI](https://pypi.org/project/lucidadl/); installing it adds a global
**`lucida`** command (alias: `lucidadl`).

> **Already using [lucida-downloader](https://github.com/jelni/lucida-downloader)?** Its
> binary is also called `lucida`, so the two clash on your `PATH`. Just use the
> **`lucidadl`** alias for this tool (e.g. `lucidadl track "…"`, `lucidadl ui`) — every
> command below works the same with `lucidadl` in place of `lucida`.

**Recommended — isolated, on PATH ([pipx](https://pipx.pypa.io)):**

```bash
pip install --user pipx && python -m pipx ensurepath   # once, if you don't have pipx
pipx install lucidadl
pipx run playwright install chromium                    # one-time: download the browser
```

**Or with plain pip** (into your Python; its `Scripts`/`bin` must be on PATH):

```bash
pip install lucidadl
playwright install chromium
```

**From source** (for development — see [CONTRIBUTING.md](CONTRIBUTING.md)):

```bash
git clone https://github.com/Jude-A/lucidadl
cd lucidadl
pip install -e ".[dev]"
playwright install chromium
```

Open a new terminal afterwards so `lucida` is picked up. ffmpeg is bundled
(`imageio-ffmpeg`) — nothing to install.

## Quick start

```bash
lucida setup                                  # once: pass Cloudflare, cache the cookie
lucida                                         # interactive menu (same as `lucida ui`)
lucida track "Red Hot Chili Peppers - Otherside"
lucida album "Red Hot Chili Peppers - Californication" --to mp3 --bitrate 320k -j 8
```

Prefer a menu? Run `lucida` with no arguments (or `lucida ui`): pick an action, type a
query/URL, and watch one progress bar per parallel download. Your menu defaults
(jobs, service, format, folder) are remembered.

Files land in **one fixed folder** — `~/Downloads/music` by default (not the current
directory, so they never scatter). Change it once with `lucida config --music "D:/Music"`
(or the `LUCIDADL_MUSIC` env var), or per run with `-o`.

## Commands

Singular = ad-hoc (arguments, always downloads). Plural = watchlist (reads a file,
skips already-downloaded items — for unattended/scheduled runs).

| Command | Input | Dedup |
|---------|-------|-------|
| `lucida` / `lucida ui` | interactive menu | — |
| `lucida track "<query\|url>"` | argument(s) | no (force) |
| `lucida album "<query\|url>"` | argument(s) | no (force) |
| `lucida tracks` | `./inputs/tracks.txt` | yes |
| `lucida albums` | `./inputs/albums.txt` | yes |
| `lucida playlist "<apple music url>"` | public playlist | yes |
| `lucida search "<query>"` | interactive pick | no |
| `lucida retry` | failed list | yes |
| `lucida config` | show/set the music folder | — |
| `lucida setup` | — | — |
| `lucida doctor` | environment check | — |

A search takes the best-matching result (title + artist, avoiding remix/cover/karaoke/
live/… unless you ask for them). A playlist/album URL downloads all its tracks.

## Options (download commands)

- `-j, --jobs N` — parallel downloads, 1–20 (default 3).
- `-s, --service` — `qobuz` (default) or `amazon`. If the primary finds nothing, it
  falls back to the other automatically.
- `-F, --format` — format requested from lucida (server-side, no bitrate control):
  `original` (default) · `flac` · `mp3` · `ogg-vorbis` · `opus` · `m4a-aac` · `wav`.
- `--to` — **local ffmpeg transcode** (recommended for a precise format/bitrate):
  `mp3` · `aac`/`m4a` · `opus` · `ogg` · `flac` · `wav`. Downloads FLAC then converts.
- `--bitrate` — e.g. `320k`, `256k`, `192k` (for `--to`).
- `--keep-original` — keep the source FLAC next to the transcoded file.
- `--force` — ignore the dedup memory and re-download even items already recorded as
  done (handy if `state.json` drifted out of sync).
- `--organize / --flat` — tag-based `Artists/<Artist>/<Album>/` (default) vs everything
  flat in `<music folder>/Music/`.
- `--country` — country code (default `US` for Qobuz; Amazon needs none).
- `-o, --out` — output directory for this run (default: the configured music folder,
  `~/Downloads/music`).
- `--hidden / --visible` — if a Cloudflare refresh is needed, open the window
  off-screen (`--hidden`) instead of visible.

## Watchlists

Copy the example files and edit them — one item per line (a search `artist - title`,
or a direct URL):

```bash
cp inputs/tracks.txt.example inputs/tracks.txt
cp inputs/albums.txt.example inputs/albums.txt
```

then:

```bash
lucida tracks      # downloads everything new, skips what's already done
lucida albums
```

## Playlists

```bash
lucida playlist "https://music.apple.com/.../pl.xxxxxxxx" [--dry-run] [-j N]
```

Give it a playlist link: lucidadl reads the track list (title + artist) straight from the
page, then downloads each track through lucida (Qobuz) into `Playlists/<playlist name>/`.
`--dry-run` just lists them (and writes `./inputs/playlist.txt`) without downloading.

**Only Apple Music is implemented today — that's what I use.** Adding other sources
(Spotify, Deezer, Tidal…) is straightforward, and contributions are welcome. The playlist
scraping all lives in [`lucidadl/api.py`](lucidadl/api.py):

- `playlist_tracklist()` — picks a scraper based on the link's host.
- `applemusic_tracklist()` — the working Apple Music scraper (your reference example).
- `_scrape_playlist()` + `_PLAYLIST_SOURCES` — a generic scraper with per-site CSS
  selectors (best-guess starting points for Spotify/Deezer/Tidal), gated behind the
  `_PLAYLIST_OTHERS_ENABLED` flag.

To add a source: flip `_PLAYLIST_OTHERS_ENABLED = True`, fix the selectors for your
service in `_PLAYLIST_SOURCES`, test, and open a PR.

## Scheduling / "in the background"

The cookie is cached, so unattended runs open no browser (until it expires). Schedule a
watchlist with your OS scheduler. A Windows example is provided in `schedule.ps1`.

## Where files live

- **Music**: one fixed folder, `~/Downloads/music` by default. Set it with
  `lucida config --music "<path>"` or the `LUCIDADL_MUSIC` env var; `lucida config`
  (no args) prints every path. Everything is saved here and deduped against here only.
- **App data** (browser profile, `clearance.json`, dedup `state.json`, `config.json`,
  `run.log`, `failed.txt`): the OS user data dir (`%LOCALAPPDATA%\lucidadl` on Windows,
  `~/.local/share/lucidadl` on Linux, `~/Library/Application Support/lucidadl` on
  macOS). Override with `LUCIDADL_HOME`.
- **Watchlist inputs** (`tracks.txt`, `albums.txt`): `./inputs/` next to where you run
  the command, so you can keep them in your project. Override per command with `-f`.

## Troubleshooting

- **Everything lands in `Unknown Artist/Unknown Album`** → `mutagen` is missing in the
  Python that runs `lucida` (tags can't be read). `pip install mutagen` into that
  interpreter (it's a declared dependency, so a normal `pip install .`/`pipx` install
  pulls it). lucidadl now prints a warning when it's absent.
- **"Cloudflare not cleared"** → run `lucida setup` again (the cached cookie expired).
- **"Executable doesn't exist"** → run `playwright install chromium`.
- **Search finds nothing** → try a direct URL, or `-s amazon`.
- **`lucida doctor`** → checks Python, Playwright, and reachability.

## Credits

lucidadl takes inspiration from two existing open-source lucida.to downloaders we looked
at while building it:

- **[lucida-flow](https://github.com/ryanlong1004/lucida-flow)** — a Python CLI/API that
  drives lucida.to through browser automation; the starting point for the browser side.
- **[lucida-downloader](https://github.com/jelni/lucida-downloader)** — a fast,
  multithreaded Rust client; the inspiration for downloading many tracks concurrently.

It's a "vibe-coded" project (built quickly, AI-assisted), so expect rough edges — issues
and PRs that sharpen or extend it are very welcome.

## Contributing

Bug reports and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the dev setup
and how to run the offline self-tests. Notable changes are tracked in
[CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).
