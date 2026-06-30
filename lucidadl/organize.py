"""Post-download organization: place files into <music_root>/<Artist>/<Album>/
using embedded tags, and auto-extract album zips into the same structure."""

from __future__ import annotations

import os
import re
import shutil
import zipfile
from typing import Dict, List, Optional

from . import utils
from .api import _long

AUDIO_EXT = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".alac", ".aiff", ".aif"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}

# Top-level grouping under the music root: albums/tracks live under Artists/<Artist>/…,
# playlists live under Playlists/<Playlist name>/ — so the two never mix.
ARTISTS_DIR = "Artists"
PLAYLISTS_DIR = "Playlists"

try:  # mutagen is a hard dep; a missing/broken install must be VISIBLE, not silent
    import mutagen as _mutagen
except Exception:  # pragma: no cover
    _mutagen = None


def mutagen_available() -> bool:
    """False if mutagen can't be imported — tag-based organization is then impossible
    and every file would land in Unknown Artist/Album. Callers warn the user."""
    return _mutagen is not None


def read_tags(path: str) -> Dict[str, str]:
    """Best-effort read of artist/albumartist/album from embedded tags. Returns {} if
    mutagen is unavailable or the file's tags can't be read (caller falls back to
    API-supplied metadata, then to Unknown)."""
    if _mutagen is None:
        return {}
    try:
        f = _mutagen.File(_long(path), easy=True)
        if not f:
            return {}

        def first(key: str) -> str:
            v = f.get(key)
            if isinstance(v, list):
                return str(v[0]) if v else ""
            return str(v) if v else ""

        return {
            "artist": first("artist"),
            "albumartist": first("albumartist"),
            "album": first("album"),
            "title": first("title"),
        }
    except Exception:
        return {}


def album_dir(music_root: str, tags: Dict[str, str], meta: Dict[str, str] = None) -> str:
    """<music_root>/<Artist>/<Album>/. Embedded `tags` win; API-derived `meta` only
    fills a BLANK folder-artist or folder-album (so a file that already organizes
    correctly by its embedded artist is never relocated by meta), then 'Unknown'."""
    tags = tags or {}
    meta = meta or {}
    artist = (tags.get("albumartist") or tags.get("artist")
              or meta.get("albumartist") or meta.get("artist") or "Unknown Artist")
    album = tags.get("album") or meta.get("album") or "Unknown Album"
    return os.path.join(music_root, utils.sanitize(artist), utils.sanitize(album))


def _move_into(src: str, dest_dir: str, name: str = None) -> str:
    os.makedirs(_long(dest_dir), exist_ok=True)
    dest = os.path.join(dest_dir, name or os.path.basename(src))
    root, ext = os.path.splitext(dest)
    i = 1
    while os.path.exists(_long(dest)):
        dest = f"{root} ({i}){ext}"
        i += 1
    shutil.move(_long(src), _long(dest))
    return dest


def _title_and_ext(basename: str, meta: Dict[str, str] = None,
                   prefer_meta_title: bool = True) -> tuple:
    """The track title (artist prefix removed) + extension. lucida names files
    'Artist - Title.ext'; since we already sort into Artist/Album folders, the artist in
    the name is redundant. Use the API title when known; otherwise strip a leading
    '<artist> - ' (and never a bare ' - ', so titles like 'Iron Man (2012 - Remaster)'
    survive)."""
    stem, ext = os.path.splitext(basename)
    meta = meta or {}
    if prefer_meta_title and (meta.get("title") or "").strip():
        return meta["title"].strip(), ext
    artist = (meta.get("artist") or meta.get("albumartist") or "").strip()
    if artist and stem.lower().startswith(artist.lower() + " - "):
        rest = stem[len(artist) + 3:].strip()
        if rest:
            return rest, ext
    return stem, ext


def place_file(path: str, music_root: str, collection: str = None,
               meta: Dict[str, str] = None, track_no: str = None,
               prefer_meta_title: bool = True) -> str:
    """Move a single audio file into <music_root>/Artists/<Artist>/<Album>/, or, when a
    `collection` (playlist name) is given, into <music_root>/Playlists/<collection>/.
    The artist prefix is stripped from the filename; playlist tracks are prefixed with
    `track_no` so they keep the playlist order instead of sorting alphabetically.
    `meta` (API artist/album) is the fallback used when embedded tags are missing."""
    title, ext = _title_and_ext(os.path.basename(path), meta, prefer_meta_title)
    if collection:
        dest_dir = os.path.join(music_root, PLAYLISTS_DIR, utils.sanitize(collection))
        stem = f"{track_no} - {title}" if track_no else title
    else:
        dest_dir = album_dir(os.path.join(music_root, ARTISTS_DIR), read_tags(path), meta)
        stem = title
    return _move_into(path, dest_dir, utils.sanitize_filename(stem + ext))


def _m3u_title(filename: str) -> str:
    """Display title for an EXTINF line: the filename minus its extension and minus the
    leading 'NN - ' track-number prefix that playlist files carry (so the watch shows
    'Castle (feat. …)', not '01 - Castle …')."""
    stem = os.path.splitext(filename)[0]
    m = re.match(r"^\d+\s*-\s*(.+)$", stem)
    return ((m.group(1) if m else stem).strip()) or stem


def write_m3u8(folder: str) -> Optional[str]:
    """Write/refresh '<folder name>.m3u8' inside `folder`, listing every audio file it
    holds in filename order — playlist tracks are zero-padded 'NN - …', so that order IS
    the intended one. Returns the .m3u8 path, or None when the folder has no audio.

    A bare folder of tracks is NOT a playlist to most hardware players (Garmin watches,
    car head-units, phones): they only surface a playlist when such a sidecar file sits
    next to the songs. Entries are bare filenames (relative to the .m3u8), so the folder
    stays self-contained and survives being copied onto a device. UTF-8 (the whole point
    of the .m3u8 extension) + CRLF is the most broadly accepted encoding."""
    if not os.path.isdir(_long(folder)):
        return None
    name = os.path.basename(os.path.normpath(folder))
    tracks = sorted(fn for fn in os.listdir(_long(folder))
                    if os.path.splitext(fn)[1].lower() in AUDIO_EXT)
    if not tracks:
        return None
    lines = ["#EXTM3U"]
    for fn in tracks:
        lines.append(f"#EXTINF:-1,{_m3u_title(fn)}")
        lines.append(fn)
    m3u = os.path.join(folder, utils.sanitize_filename(name + ".m3u8"))
    with open(_long(m3u), "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(lines) + "\n")
    return m3u


def write_playlist_m3u(music_root: str, collection: str) -> Optional[str]:
    """Generate the .m3u8 sidecar for a downloaded playlist (Playlists/<collection>/)."""
    return write_m3u8(os.path.join(music_root, PLAYLISTS_DIR, utils.sanitize(collection)))


def process_download(path: str, music_root: str, collection: str = None,
                     meta: Dict[str, str] = None, track_no: str = None) -> List[str]:
    """Organize a finished download (single audio file or an album .zip).
    Returns the final file paths. Removes the source zip after extraction."""
    if path.lower().endswith(".zip"):
        return _extract_and_place(path, music_root, collection, meta)
    return [place_file(path, music_root, collection, meta, track_no)]


def _extract_and_place(zip_path: str, music_root: str, collection: str = None,
                       meta: Dict[str, str] = None) -> List[str]:
    tmp = zip_path + ".extract"
    placed: List[str] = []
    covers: List[str] = []
    try:
        with zipfile.ZipFile(_long(zip_path)) as z:
            z.extractall(_long(tmp))
        for root, _dirs, files in os.walk(tmp):
            for fn in files:
                fp = os.path.join(root, fn)
                ext = os.path.splitext(fn)[1].lower()
                if ext in AUDIO_EXT:
                    # zip tracks: one album-level meta for all, so strip the artist from
                    # each file's own name rather than reusing the album title.
                    placed.append(place_file(fp, music_root, collection, meta,
                                             prefer_meta_title=False))
                elif ext in IMAGE_EXT:
                    covers.append(fp)
        # drop cover art into the album folder of the first placed track
        if placed and covers:
            target = os.path.dirname(placed[0])
            dst = os.path.join(target, "cover" + os.path.splitext(covers[0])[1].lower())
            if not os.path.exists(_long(dst)):
                try:
                    shutil.copyfile(_long(covers[0]), _long(dst))
                except OSError:
                    pass
    finally:
        shutil.rmtree(_long(tmp), ignore_errors=True)
        try:
            os.remove(_long(zip_path))
        except OSError:
            pass
    return placed
