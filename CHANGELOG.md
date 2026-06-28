# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-06-28

First stable release. Feature-complete and validated end to end; no code changes since
0.1.1 — promoted to 1.0 and marked Production/Stable.

## [0.1.1] - 2026-06-28

### Added
- Playlist tracks are numbered (`NN - Title`) so they keep the playlist order in their
  flat folder instead of sorting alphabetically.

### Changed
- Filenames drop the redundant artist prefix (just `Title.ext`), since tracks are already
  organized under `Artists/<Artist>/<Album>/`.
- Cleaner playlist output: a table for `--dry-run` and a single header line + progress
  bars for a download, instead of dumping the whole tracklist and a per-track log line.

### Fixed
- Find niche and multi-artist tracks. When a literal `"Artist - Title"` query returns
  nothing, retry with the title alone (and a primary-artist + title form), verifying the
  artist before accepting — so a broadened search can't silently grab the wrong song.
- Playlist dedup is scoped to the playlist folder: a track already in `Artists/` (or
  another playlist) is still fetched into a playlist that's missing it, without
  re-downloading copies already there.

## [0.1.0] - 2026-06-27

First public release.

### Added
- **Fast parallel downloads** over HTTP (`-j/--jobs`) — the Cloudflare challenge is
  solved once in a real browser (`setup`), the `cf_clearance` cookie is cached, and
  everything else runs over `httpx`, so no browser stays open.
- **Search or URL** for `track` / `album` (singular, ad-hoc) and `tracks` / `albums`
  (watchlists with dedup), plus automatic **service fallback** (Qobuz → Amazon).
- **Albums downloaded track-by-track** in parallel.
- **Local transcoding** with bundled ffmpeg (`--to`, `--bitrate`); tags and cover art
  preserved. `-F/--format` requests a server-side format from lucida.
- **Tag-based organization** into `Artists/<Artist>/<Album>/`, with an API-metadata
  fallback when a file has no embedded tags. Playlists go under `Playlists/<name>/`.
- **Apple Music playlist import** (`playlist`) — the tracklist is scraped headless, with
  a visible-window fallback, then each track is downloaded via Qobuz.
- **Interactive menu** (`lucida ui`, or bare `lucida`) and **live progress bars** (one
  per parallel download).
- **Interactive search** (`search`), **retry** of failures (`retry`), and a `config`
  command for the fixed music directory.
- **Existence-aware dedup**: an item is skipped only while its file still exists; delete
  it and it is re-downloaded. `--force` ignores the dedup memory.
- **Fixed, configurable download directory** (`~/Downloads/music` by default;
  `lucida config --music`, or the `LUCIDADL_MUSIC` env var).

[Unreleased]: https://github.com/Jude-A/lucidadl/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Jude-A/lucidadl/compare/v0.1.1...v1.0.0
[0.1.1]: https://github.com/Jude-A/lucidadl/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Jude-A/lucidadl/releases/tag/v0.1.0
