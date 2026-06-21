# lucidadl

Fast, parallel command-line music downloader built on top of
[lucida.to](https://lucida.to). Search or paste a URL, download tracks and albums in
parallel over plain HTTP, organize them by tags, transcode to the format/bitrate you
want, and import playlists.

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
- **Tag-based organization** into `Artist/Album/…`.
- **Watchlists** with dedup (`tracks` / `albums` read a file, skip what's done).
- **Apple Music playlist import** (public link → tracklist → download via Qobuz).
- **Interactive search**, **service fallback** (Qobuz → Amazon), **retry** of failures.

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

`pip install` adds a global **`lucida`** command (alias: `lucidadl`).

**Recommended — isolated, on PATH ([pipx](https://pipx.pypa.io)):**

```bash
pip install --user pipx && python -m pipx ensurepath   # once, if you don't have pipx
git clone https://github.com/your-username/lucidadl
pipx install ./lucidadl
pipx run playwright install chromium                    # one-time: download the browser
```

**Or with plain pip** (into your Python; its `Scripts`/`bin` must be on PATH):

```bash
git clone https://github.com/your-username/lucidadl
cd lucidadl
pip install .            # or `pip install -e .` to keep editing the code
playwright install chromium
```

Open a new terminal afterwards so `lucida` is picked up. ffmpeg is bundled
(`imageio-ffmpeg`) — nothing to install.

## Quick start

```bash
lucida setup                                  # once: pass Cloudflare, cache the cookie
lucida track "Red Hot Chili Peppers - Otherside"
lucida album "Red Hot Chili Peppers - Californication" --to mp3 --bitrate 320k -j 8
```

Files land in `./downloads/` by default.

## Commands

Singular = ad-hoc (arguments, always downloads). Plural = watchlist (reads a file,
skips already-downloaded items — for unattended/scheduled runs).

| Command | Input | Dedup |
|---------|-------|-------|
| `lucida track "<query\|url>"` | argument(s) | no (force) |
| `lucida album "<query\|url>"` | argument(s) | no (force) |
| `lucida tracks` | `./inputs/tracks.txt` | yes |
| `lucida albums` | `./inputs/albums.txt` | yes |
| `lucida playlist "<apple music url>"` | public playlist | yes |
| `lucida search "<query>"` | interactive pick | no |
| `lucida retry` | `./failed.txt` | yes |
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
- `--organize / --flat` — tag-based `Artist/Album/` (default) vs everything in
  `downloads/Music/`.
- `--country` — country code (default `US` for Qobuz; Amazon needs none).
- `-o, --out` — output directory (default `./downloads`).
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

## Apple Music playlist

```bash
lucida playlist "https://music.apple.com/.../pl.xxxxxxxx" [--dry-run] [-j N]
```

Apple Music is not a lucida source: lucidadl reads the playlist's tracklist (title +
artist) from the public page, then downloads each via Qobuz. `--dry-run` only lists
(and writes `./inputs/playlist.txt`). *(Spotify/Deezer/Tidal are coded but disabled —
flip `_PLAYLIST_OTHERS_ENABLED` in `lucidadl/api.py`.)*

## Scheduling / "in the background"

The cookie is cached, so unattended runs open no browser (until it expires). Schedule a
watchlist with your OS scheduler. A Windows example is provided in `schedule.ps1`.

## Where files live

- **Downloads / inputs / logs**: the current directory (`./downloads`, `./inputs`,
  `./run.log`, `./failed.txt`).
- **App data** (browser profile, `clearance.json`, dedup `state.json`): the OS user
  data dir (`%LOCALAPPDATA%\lucidadl` on Windows, `~/.local/share/lucidadl` on Linux,
  `~/Library/Application Support/lucidadl` on macOS). Override with `LUCIDADL_HOME`.

## Troubleshooting

- **"Cloudflare non franchi"** → run `lucida setup` again (the cached cookie expired).
- **"Executable doesn't exist"** → run `playwright install chromium`.
- **Search finds nothing** → try a direct URL, or `-s amazon`.
- **`lucida doctor`** → checks Python, Playwright, and reachability.

## License

MIT — see [LICENSE](LICENSE).
