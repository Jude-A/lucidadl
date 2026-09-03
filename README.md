# lucidadl

[![PyPI](https://img.shields.io/pypi/v/lucidadl.svg)](https://pypi.org/project/lucidadl/)
[![CI](https://github.com/Jude-A/lucidadl/actions/workflows/ci.yml/badge.svg)](https://github.com/Jude-A/lucidadl/actions/workflows/ci.yml)
[![Python versions](https://img.shields.io/pypi/pyversions/lucidadl.svg)](https://pypi.org/project/lucidadl/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A small, fast music downloader for [lucida.to](https://lucida.to), with both direct
commands and an interactive terminal interface.

lucidadl downloads tracks and albums in parallel, organizes them from their metadata,
can convert them locally with ffmpeg, accepts large `.txt` batches, and imports public
Apple Music, Spotify, and Deezer playlists with match checking and reliable resume. It
intentionally remains a lightweight personal tool rather than a music-library or
streaming-account platform.

> Use lucidadl only for content you are entitled to download. You are responsible for
> complying with applicable law and with the terms of the services involved. This
> project is not affiliated with lucida.to, Apple, Spotify, Deezer, Qobuz, Amazon,
> or any streaming service.

## Highlights

- Track and album downloads from a search (`Artist - Title`) or a direct URL.
- Parallel HTTP downloads with no browser left running in the background.
- Qobuz search by default, with an automatic Amazon fallback.
- FLAC source downloads and optional local MP3, AAC, Opus, Ogg, WAV, or FLAC conversion.
- Tag-based organization under `Artists/<Artist>/<Album>/`.
- Batch downloads from any `.txt` file, without modifying the source list.
- Public Apple Music, Spotify, and Deezer playlist imports with match checking, resume,
  ordered tracks, and a portable `.m3u8` file.
- Existence-aware deduplication, safe matching, and one-command retry for failures.
- Guided first-run setup, diagnostics, progress bars, and a compact interactive menu.

## Install

Python 3.10 or newer and a normal desktop session are required. Using
[pipx](https://pipx.pypa.io) keeps the application isolated and available everywhere:

```bash
pipx install lucidadl
lucida setup
lucida
```

`lucida setup` installs the matching Playwright Chromium build when needed, opens
lucida.to once for the Cloudflare check, then saves the resulting access locally.

Plain pip also works:

```bash
pip install lucidadl
lucida setup
```

Installing the package creates both `lucida` and `lucidadl`. If another application
already owns the `lucida` command, use the `lucidadl` alias for every example below.

## Three ways to download

### 1. A track or album

```bash
lucida track "Daft Punk - Around the World"
lucida album "Daft Punk - Discovery"
```

A direct Qobuz or Amazon URL can be used in place of the search text. Albums are
expanded and downloaded track by track so the available parallelism is preserved.

Use interactive search when you want to choose the result yourself:

```bash
lucida search "Discovery Daft Punk"
```

### 2. Many tracks or albums from a text file

```bash
lucida tracks --file "D:/music-lists/tracks.txt"
lucida albums --file "D:/music-lists/albums.txt"
```

The format is deliberately simple: one search or direct URL per line. Blank lines and
comments beginning with `#` are ignored.

```text
# Road-trip additions
Daft Punk - Around the World
The Chemical Brothers - Galvanize
https://play.qobuz.com/track/24107150
```

The source file is never edited. Already downloaded items are skipped unless `--force`
is used. When `--file` is omitted, the plural commands use `./inputs/tracks.txt` or
`./inputs/albums.txt`; ready-to-copy examples are included in the repository.

### 3. A public streaming playlist

```bash
lucida playlist "https://music.apple.com/.../pl.xxxxxxxx"
lucida playlist "https://open.spotify.com/playlist/xxxxxxxx"
lucida playlist "https://www.deezer.com/playlist/xxxxxxxx"
```

lucidadl detects the service from the URL, reads its public track list, resolves each
title through lucida.to, and stores the result under `Playlists/<playlist name>/`. An
`.m3u8` file is written beside the tracks so players and devices recognize the folder as
an actual playlist.

Preview the extraction without downloading anything:

```bash
lucida playlist "https://music.apple.com/.../pl.xxxxxxxx" --dry-run
```

To verify what lucidadl will select on Qobuz or Amazon before starting a large download:

```bash
lucida playlist "https://music.apple.com/.../pl.xxxxxxxx" --check
```

The extracted list is saved in lucidadl's application-data folder. If a title is
ambiguous or unavailable, edit that file (or remove the line), then download the reviewed
version while preserving playlist order:

```bash
lucida playlist-file "C:/path/to/playlist.txt" --name "My playlist"
```

If a playlist is interrupted, `lucida retry` resumes it with its original folder,
settings, and track numbers. Existing files are skipped, and the `.m3u8` is rebuilt when
the run finishes. Repeating the same song at two different positions is supported.

Only public playlists are read: lucidadl does not connect to or modify an Apple Music,
Spotify, or Deezer account. Spotify's public player exposes at most 100 items; when the
declared playlist is longer, lucidadl stops with an explicit message instead of importing
an incomplete list. Cross-service playlist translation and account authorization remain
the responsibility of a separate companion project.

## Interactive menu

Run `lucida` without arguments (or `lucida ui`):

```text
╭────────────────── lucidadl ──────────────────╮
│ 3 concurrent downloads · qobuz · original    │
│ Music: ~/Downloads/music                     │
│ Access: prepared                             │
╰──────────────────────────────────────────────╯

► What do you want to do?
  ⬇   Download music
  🎶  Playlists — streaming link or an edited list
  📄  Download from a .txt file
  ⚙   Settings
  🧰  Help, access and diagnostics
  🚪  Quit
```

The menu remembers its download count, source service, conversion settings, and music
folder. Each run ends with a readable summary and offers failed items for retry from the
main menu.

## Output and formats

Music is saved to `~/Downloads/music` by default, independently of the directory from
which lucidadl is launched:

```text
music/
├── Artists/
│   └── Artist/
│       └── Album/
└── Playlists/
    └── Playlist name/
        ├── 01 - Track.flac
        └── Playlist name.m3u8
```

Change the main folder permanently or for one run:

```bash
lucida config --music "D:/Music"
lucida track "Artist - Title" --out "E:/Temporary music"
```

For local conversion, lucidadl downloads the best source first and invokes the bundled
ffmpeg executable:

```bash
lucida album "Artist - Album" --to mp3 --bitrate 320k --jobs 8
```

If conversion fails, the source audio is kept and the item is reported as failed rather
than silently counted as a success.

Useful download options:

| Option | Purpose |
|---|---|
| `-j, --jobs N` | Parallel downloads, from 1 to 100 (default: 3) |
| `-s, --service` | Primary search service: `qobuz` or `amazon` |
| `--to FORMAT` | Local conversion to MP3, AAC/M4A, Opus, Ogg, FLAC, or WAV |
| `--bitrate RATE` | Conversion bitrate such as `320k` or `192k` |
| `--keep-original` | Keep the source FLAC after conversion |
| `--flat` | Place files under `Music/` instead of organizing from tags |
| `--force` | Ignore download history and fetch the item again |
| `--hidden` | Move a necessary Cloudflare browser window off-screen |

Run `lucida <command> --help` for the complete options of a command.

## Access, failures, and diagnostics

Cloudflare access is prepared once in a real Chromium window. Downloads then use a
lightweight HTTP client. If the saved access expires, lucidadl briefly opens the browser
again and refreshes it.

```bash
lucida doctor          # quick local check; never opens a browser
lucida doctor --live   # browser and lucida.to connectivity check
lucida setup           # install/repair Chromium and refresh access
lucida retry           # retry failures or resume an interrupted playlist
lucida cleanup         # prune stale state and old partial downloads
```

Failed tracks and albums retain their original type; playlist failures also retain their
folder and original position. Automated and scheduled commands return a non-zero status
while work remains unresolved. The latest details are stored in `run.log`; `lucida
config` prints its exact location along with the extracted playlist and recovery data.

Common fixes:

- No confident automatic match: use `lucida search` and choose the result manually.
- Cloudflare or browser failure: run `lucida setup`, then `lucida doctor --live`.
- Unexpected output folder: run `lucida config` and check `LUCIDADL_MUSIC`.
- Another program uses the `lucida` command: call this application with `lucidadl`.

## Scheduling a batch

Once access has been prepared, a `.txt` batch can run unattended while its cached access
remains valid. A Windows Scheduled Task helper is included:

```powershell
.\schedule.ps1 -Mode tracks -Time 21:30 -WorkingDir "D:\music-lists"
```

The scheduled task uses `inputs/tracks.txt` or `inputs/albums.txt` under its working
directory. A logged-in desktop session is still required if Cloudflare access must be
renewed.

## Application data

The browser profile, access data, configuration, deduplication state, last log,
failed-item list, and playlist recovery data are stored outside the repository:

- Windows: `%LOCALAPPDATA%\lucidadl`
- Linux: `~/.local/share/lucidadl`
- macOS: `~/Library/Application Support/lucidadl`

Advanced overrides are available through `LUCIDADL_HOME` and `LUCIDADL_MUSIC`.

## Development

```bash
git clone https://github.com/Jude-A/lucidadl
cd lucidadl
python -m venv .venv
pip install -e ".[dev]"
python selftest.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for platform-specific setup and validation.
Release changes are recorded in [CHANGELOG.md](CHANGELOG.md).

## Credits

lucidadl takes inspiration from
[lucida-flow](https://github.com/ryanlong1004/lucida-flow) and
[lucida-downloader](https://github.com/jelni/lucida-downloader). The project started as
a small, AI-assisted personal tool and remains intentionally focused on that scale.

## License

[MIT](LICENSE)
