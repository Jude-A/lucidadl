"""Small helpers: filename sanitizing, metadata bits, dedup state."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from typing import Any, Dict, List, Optional

# Windows-forbidden filename chars + control chars.
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name: str, fallback: str = "untitled") -> str:
    name = (name or "").strip()
    name = _INVALID.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    if name.upper() in {"CON", "PRN", "AUX", "NUL"} or re.match(r"^(COM|LPT)\d$", name.upper()):
        name = "_" + name
    return name[:180] or fallback


def sanitize_filename(fname: str, fallback: str = "download") -> str:
    """Sanitize a real on-disk filename WITHOUT truncating its extension."""
    root, ext = os.path.splitext(fname or "")
    ext = ext[:12]  # guard against absurd 'extensions'
    stem = sanitize(root, fallback)
    budget = max(1, 180 - len(ext))
    return (stem[:budget] or fallback) + ext


def artists_str(artists: Optional[List[Dict[str, Any]]]) -> str:
    names = [a.get("name", "") for a in (artists or []) if a.get("name")]
    return ", ".join(names) or "Unknown Artist"


def year_of(rfc3339: Optional[str]) -> Optional[str]:
    if not rfc3339:
        return None
    m = re.match(r"(\d{4})", str(rfc3339))
    return m.group(1) if m else None


def _path_exists(path: str) -> bool:
    """os.path.exists with Windows extended-length prefix (utils can't import api)."""
    if not path:
        return False
    try:
        if os.name == "nt":
            ap = os.path.abspath(path)
            if not ap.startswith("\\\\?\\"):
                ap = "\\\\?\\" + ap
            return os.path.exists(ap)
        return os.path.exists(path)
    except OSError:
        return False


def _as_paths(v) -> List[str]:
    if isinstance(v, list):
        return [str(x) for x in v if x]
    return [str(v)] if v else []


def _is_under(path: str, directory: str) -> bool:
    """True if `path` lives inside `directory` (case-insensitive on Windows)."""
    try:
        p = os.path.normcase(os.path.abspath(path))
        d = os.path.normcase(os.path.abspath(directory))
        return p == d or p.startswith(d + os.sep)
    except Exception:
        return False


class State:
    """Remembers which track URLs were downloaded, AND where each copy landed, so a
    re-run skips an item only while a copy still exists on disk (delete it → re-download).
    A track may be recorded in several places (e.g. an album folder AND a playlist folder):
    `under` scopes the check to one destination, so a track present elsewhere is still
    fetched into a playlist folder that is missing it."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self.done: Dict[str, List[str]] = {}   # url -> list of final file paths
        self._inflight = set()
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f).get("done", [])
                if isinstance(raw, dict):                 # {url: path} or {url: [paths]}
                    self.done = {str(k): _as_paths(v) for k, v in raw.items()}
                else:                                     # oldest: a plain list of URLs
                    self.done = {str(k): [] for k in raw}
            except Exception as e:
                # The file EXISTS but is unreadable: don't silently drop the whole dedup
                # history (the next add() would overwrite it). Back it up and warn.
                self.done = {}
                bak = path + ".bad"
                try:
                    os.replace(path, bak)
                except OSError:
                    bak = path
                sys.stderr.write(
                    f"⚠ dedup state unreadable ({path}: {e}) — backed up to {bak} then "
                    f"reset; duplicates are possible this run.\n")

    def has(self, key: str, under: Optional[str] = None) -> bool:
        """True if this URL counts as already-downloaded. A recorded path that no longer
        exists does NOT count (deleted → re-download). When `under` is given, only a copy
        inside that directory counts."""
        if key not in self.done:
            return False
        paths = [p for p in self.done[key] if p]
        if not paths:
            # legacy entry, no recorded path: can't verify location. Done only for an
            # unscoped check; for a specific destination, re-download to be safe.
            return under is None
        if under is not None:
            return any(_path_exists(p) and _is_under(p, under) for p in paths)
        return any(_path_exists(p) for p in paths)

    def reserve(self, key: str, under: Optional[str] = None) -> bool:
        """Atomically claim a key (asyncio single-thread: no await inside).
        Returns False if already downloaded (and present) or currently in flight."""
        if key in self._inflight or self.has(key, under):
            return False
        self._inflight.add(key)
        return True

    def release(self, key: str) -> None:
        self._inflight.discard(key)

    def add(self, key: str, path: str = "") -> None:
        with self._lock:
            paths = self.done.setdefault(key, [])
            if path and path not in paths:
                paths.append(path)
            self._inflight.discard(key)
            tmp = self.path + f".{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"done": self.done}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

    def prune(self) -> tuple[int, int]:
        """Forget recorded file paths that no longer exist.

        URL-only legacy entries are retained because their location cannot be checked.
        Returns ``(removed_paths, removed_items)``.
        """
        removed_paths = 0
        removed_items = 0
        changed = False
        with self._lock:
            for key in list(self.done):
                old = self.done[key]
                if not old:  # oldest state format: keep its conservative dedup behaviour
                    continue
                live = [path for path in old if _path_exists(path)]
                removed_paths += len(old) - len(live)
                if live:
                    if live != old:
                        self.done[key] = live
                        changed = True
                else:
                    del self.done[key]
                    removed_items += 1
                    changed = True
            if changed:
                tmp = self.path + f".{os.getpid()}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"done": self.done}, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
        return removed_paths, removed_items
