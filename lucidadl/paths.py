"""Filesystem locations.

App data that must persist across runs and is independent of where you launch the
tool — the Cloudflare cookie, the browser profile, the dedup state, the run log and
the failed-items list — lives in a per-user data directory. Downloaded music goes to a
single FIXED directory (so files never scatter across the disk depending on where you
launched the command); it defaults to ``~/Downloads/music`` and is configurable.

Overrides:
- ``LUCIDADL_HOME``  — the app-data directory (cookie/profile/state/logs).
- ``LUCIDADL_MUSIC`` — the music output directory (also settable via ``lucida config``).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP = "lucidadl"


def _data_dir() -> Path:
    env = os.environ.get("LUCIDADL_HOME")
    if env:
        d = Path(env)
    elif os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        d = Path(base) / APP
    else:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
        d = Path(base) / APP
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


DATA_DIR = _data_dir()
PROFILE_DIR = str(DATA_DIR / "profile")            # persistent browser profile
CLEARANCE_PATH = str(DATA_DIR / "clearance.json")  # cached cf_clearance + UA
STATE_PATH = str(DATA_DIR / "state.json")          # dedup (downloaded track URLs -> path)
CONFIG_PATH = str(DATA_DIR / "config.json")        # user settings (music dir, …)
LOG_PATH = str(DATA_DIR / "run.log")               # last run's log (fixed, not cwd)
FAILED_PATH = str(DATA_DIR / "failed.txt")         # last run's failures (for `retry`)
PLAYLIST_RUN_PATH = str(DATA_DIR / "playlist-run.json")  # resumable last playlist run
PLAYLIST_TEXT_PATH = str(DATA_DIR / "playlist.txt")      # last extracted track list


# --- user config (persisted) ------------------------------------------------

_config_warned = False  # warn at most once per process (load_config is called often)


def _warn_config(msg: str) -> None:
    global _config_warned
    if not _config_warned:
        sys.stderr.write(msg)
        _config_warned = True


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {}  # absent is normal — start empty, silently
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        # An EXISTING but corrupt config would silently send downloads to the wrong dir.
        _warn_config(f"⚠ config unreadable ({CONFIG_PATH}: {e}) — settings ignored.\n")
        return {}
    if not isinstance(data, dict):  # valid JSON but wrong shape — don't drop it silently
        _warn_config(f"⚠ invalid config ({CONFIG_PATH}: not a JSON object) — ignored.\n")
        return {}
    return data


def save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def _downloads_root() -> Path:
    """The user's Downloads folder (best-effort, cross-platform)."""
    home = Path.home()
    dl = home / "Downloads"
    return dl if dl.exists() else home


def default_music_dir() -> str:
    """The single fixed directory every download is saved to (and deduped against).
    Priority: $LUCIDADL_MUSIC > config.json 'music_dir' > ~/Downloads/music."""
    env = os.environ.get("LUCIDADL_MUSIC")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    cfg = load_config().get("music_dir")
    if cfg:
        return os.path.abspath(os.path.expanduser(cfg))
    return str(_downloads_root() / "music")


def set_music_dir(path: str) -> str:
    """Persist a new music directory; returns the absolute path stored."""
    abspath = os.path.abspath(os.path.expanduser(path))
    cfg = load_config()
    cfg["music_dir"] = abspath
    save_config(cfg)
    return abspath


def cwd(name: str) -> str:
    """A path in the current working directory (e.g. default batch inputs)."""
    return os.path.join(os.getcwd(), name)
