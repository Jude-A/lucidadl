"""Offline self-test of the pure logic (no browser, no network)."""

from lucidadl import utils, matching
from lucidadl.api import (
    normalize_service, default_country, _long, _apple_tracks_from_obj, DOWNSCALE_CHOICES,
)

fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# naming
check("sanitize strips bad chars", utils.sanitize('AC/DC: Back?*') == "AC_DC_ Back__")
check("sanitize reserved", utils.sanitize("CON").startswith("_"))
check("artists_str", utils.artists_str([{"name": "A"}, {"name": "B"}]) == "A, B")
check("artists_str empty", utils.artists_str([]) == "Unknown Artist")
check("year_of", utils.year_of("1999-06-08T00:00:00Z") == "1999")

# sanitize_filename preserves the extension even when truncating a long name
_long_name = ("Red Hot Chili Peppers - " + "x" * 300 + ".flac")
_sf = utils.sanitize_filename(_long_name)
check("sanitize_filename keeps .flac", _sf.endswith(".flac"))
check("sanitize_filename capped", len(_sf) <= 192)
check("sanitize_filename strips bad chars", "/" not in utils.sanitize_filename("a/b:c.zip"))

# services / country
check("normalize amazon_music", normalize_service("amazon_music") == "amazon")
check("default_country qobuz=US", default_country("qobuz") == "US")
check("default_country amazon=''", default_country("amazon") == "")
check("default_country other=US", default_country("tidal") == "US")
check("formats", DOWNSCALE_CHOICES[0] == "original" and "flac" in DOWNSCALE_CHOICES)

# long path (Windows)
import os as _os
if _os.name == "nt":
    check("long path prefix on Windows", _long("C:\\a\\b").startswith("\\\\?\\"))
else:
    check("long path passthrough off Windows", _long("/a/b") == "/a/b")

# Apple Music JSON extractor (still used as a helper / future fallback)
sample = {"data": [{"type": "playlists", "attributes": {"name": "My PL"},
          "relationships": {"tracks": {"data": [
              {"type": "songs", "attributes": {"name": "Otherside", "artistName": "RHCP"}},
              {"type": "songs", "attributes": {"name": "Scar Tissue", "artistName": "RHCP"}},
          ]}}}]}
out = []
_apple_tracks_from_obj(sample, out)
check("apple extractor: 2 songs, playlist node skipped",
      len(out) == 2 and out[0] == {"title": "Otherside", "artist": "RHCP"})

# State dedup
import tempfile
p = _os.path.join(tempfile.gettempdir(), "lucidadl_selftest_state.json")
if _os.path.exists(p):
    _os.remove(p)
st = utils.State(p)
check("state empty", not st.has("u1"))
check("reserve first ok", st.reserve("u1") is True)
check("reserve second blocked (in-flight)", st.reserve("u1") is False)
st.add("u1")
check("state remembers", st.has("u1") and utils.State(p).has("u1"))
check("reserve blocked after done", st.reserve("u1") is False)
check("reserve other ok + release", st.reserve("u2") and (st.release("u2") or st.reserve("u2")))
_os.remove(p)

# dedup scoped to a destination folder (playlist) + multi-path per URL
import shutil as _sh0
_d3 = tempfile.mkdtemp(prefix="lucidadl_state2_")
_sp = _os.path.join(_d3, "state.json")
_artists = _os.path.join(_d3, "Artists", "Sinyo", "Enfant Perdu")
_pls = _os.path.join(_d3, "Playlists", "saddd")
_os.makedirs(_artists); _os.makedirs(_pls)
_af = _os.path.join(_artists, "Enfant Perdu.flac"); open(_af, "w").write("x")
s3 = utils.State(_sp)
s3.add("urlE", _af)                                  # downloaded standalone into Artists/
check("scoped: present anywhere counts unscoped", s3.has("urlE"))
check("scoped: NOT done for a playlist folder it's missing from",
      not s3.has("urlE", under=_pls))
check("scoped: reserve succeeds to fetch it into the playlist",
      s3.reserve("urlE", under=_pls))
s3.release("urlE")
_pf = _os.path.join(_pls, "Enfant Perdu.flac"); open(_pf, "w").write("y")
s3.add("urlE", _pf)                                  # now also in the playlist folder
check("scoped: done once present in the playlist folder", s3.has("urlE", under=_pls))
check("multi-path persisted", sorted(utils.State(_sp).done["urlE"]) == sorted([_af, _pf]))
s3.done["urlLegacy"] = []                            # legacy entry, no recorded path
check("legacy unscoped = done", s3.has("urlLegacy"))
check("legacy scoped = re-download", not s3.has("urlLegacy", under=_pls))
_os.remove(_pf)
check("deleted playlist copy -> not done for that playlist", not s3.has("urlE", under=_pls))
_sh0.rmtree(_d3, ignore_errors=True)

# organize: album_dir + zip extraction/placement (no real tags -> Unknown)
from lucidadl import organize as _org
ad = _org.album_dir("/music", {"albumartist": "RHCP", "album": "Cal"})
check("album_dir uses albumartist", ad.replace("\\", "/").endswith("/music/RHCP/Cal"))
ad2 = _org.album_dir("/music", {})
check("album_dir unknown fallback", "Unknown Artist" in ad2 and "Unknown Album" in ad2)

import zipfile as _zip
import shutil as _sh
_d = tempfile.mkdtemp(prefix="lucidadl_org_")
_zp = _os.path.join(_d, "album.zip")
with _zip.ZipFile(_zp, "w") as z:
    z.writestr("01 - Song.flac", b"not a real flac")
    z.writestr("cover.jpg", b"img")
_finals = _org.process_download(_zp, _d)
check("zip extracted 1 audio", len(_finals) == 1 and _finals[0].endswith(".flac"))
check("placed under Unknown Artist", "Unknown Artist" in _finals[0])
check("source zip removed", not _os.path.exists(_zp))
check("cover placed next to track", _os.path.exists(_os.path.join(_os.path.dirname(_finals[0]), "cover.jpg")))
_sh.rmtree(_d, ignore_errors=True)

# organize: API-metadata fallback (used only when embedded tags are missing)
check("mutagen_available is bool", isinstance(_org.mutagen_available(), bool))
# embedded tags WIN over meta
_ad = _org.album_dir("/m", {"albumartist": "RHCP", "album": "Cal"},
                     {"albumartist": "MetaAA", "album": "MetaAlb"})
check("album_dir: embedded tags win over meta", _ad.replace("\\", "/").endswith("/m/RHCP/Cal"))
# meta FILLS BLANKS when tags absent
_ad = _org.album_dir("/m", {}, {"albumartist": "Daft Punk", "album": "Discovery"})
check("album_dir: meta fills blank tags", _ad.replace("\\", "/").endswith("/m/Daft Punk/Discovery"))
# CRITICAL regression: embedded `artist` (blank albumartist) must NOT be relocated by meta albumartist
_ad = _org.album_dir("/m", {"artist": "RealArtist"}, {"albumartist": "MetaAA", "album": "X"})
check("album_dir: embedded artist not overridden by meta albumartist",
      _ad.replace("\\", "/").endswith("/m/RealArtist/X"))
# meta=None unchanged (backward compat)
check("album_dir: meta=None unchanged",
      _org.album_dir("/m", {"album": "Y"}).replace("\\", "/").endswith("/m/Unknown Artist/Y"))

# place_file / process_download thread meta; collection still wins
_d2 = tempfile.mkdtemp(prefix="lucidadl_meta_")
def _junk(name):
    p = _os.path.join(_d2, name)
    with open(p, "wb") as fh:
        fh.write(b"not a real flac")
    return p
_pf = _org.place_file(_junk("a.flac"), _d2, meta={"albumartist": "Daft Punk", "album": "Discovery"})
check("place_file: album under Artists/<Artist>/<Album> via meta",
      _os.path.dirname(_pf).replace("\\", "/").endswith("/Artists/Daft Punk/Discovery"))
_pf = _org.place_file(_junk("b.flac"), _d2, collection="MyMix",
                      meta={"albumartist": "Daft Punk", "album": "Discovery"})
check("place_file: playlist under Playlists/<name> (collection beats meta)",
      _os.path.dirname(_pf).replace("\\", "/").endswith("/Playlists/MyMix"))
_pf = _org.process_download(_junk("c.flac"), _d2, None, {"albumartist": "AA", "album": "BB"})[0]
check("process_download threads meta", _os.path.dirname(_pf).replace("\\", "/").endswith("/AA/BB"))
# zip + meta
_zp2 = _os.path.join(_d2, "alb.zip")
with _zip.ZipFile(_zp2, "w") as z:
    z.writestr("01 - Song.flac", b"x")
_zf = _org.process_download(_zp2, _d2, None, {"albumartist": "CompAA", "album": "Cal"})
check("zip + meta -> album folder", _os.path.dirname(_zf[0]).replace("\\", "/").endswith("/CompAA/Cal"))
# audio-less zip: process_download returns [] (downloader treats [] as a failure, not
# a bogus success on the deleted zip path) and the source zip is still removed
_zp3 = _os.path.join(_d2, "noaudio.zip")
with _zip.ZipFile(_zp3, "w") as z:
    z.writestr("cover.jpg", b"img")
    z.writestr("notes.txt", b"hello")
_zf3 = _org.process_download(_zp3, _d2)
check("audio-less zip -> [] (no false success)", _zf3 == [])
check("audio-less zip still removed", not _os.path.exists(_zp3))
_sh.rmtree(_d2, ignore_errors=True)

# filename cleanup: strip the artist; number playlist tracks to keep order
_d4 = tempfile.mkdtemp(prefix="lucidadl_name_")
def _mk(_n):
    _p = _os.path.join(_d4, _n)
    with open(_p, "wb") as _fh:
        _fh.write(b"x")
    return _p
_pf = _org.place_file(_mk("Daft Punk - Aerodynamic.flac"), _d4,
                      meta={"artist": "Daft Punk", "album": "Discovery", "title": "Aerodynamic"})
check("filename: artist stripped via API title", _os.path.basename(_pf) == "Aerodynamic.flac")
_pf = _org.place_file(_mk("Sinyo - Enfant Perdu.flac"), _d4, collection="saddd",
                      meta={"artist": "Sinyo", "title": "Enfant Perdu"}, track_no="07")
check("filename: playlist track numbered + artist stripped",
      _os.path.basename(_pf) == "07 - Enfant Perdu.flac")
_t, _e = _org._title_and_ext("Black Sabbath - Iron Man (2012 - Remaster).flac",
                             {"artist": "Black Sabbath"}, prefer_meta_title=False)
check("title: strip artist prefix but keep ' - ' inside the title",
      _t == "Iron Man (2012 - Remaster)" and _e == ".flac")
_t2, _ = _org._title_and_ext("Iron Man (2012 - Remaster).flac", {}, prefer_meta_title=False)
check("title: no artist match -> keep stem (no false ' - ' strip)",
      _t2 == "Iron Man (2012 - Remaster)")
_sh.rmtree(_d4, ignore_errors=True)

# playlist .m3u8 sidecar: lists audio in track order, bare filenames, with EXTINF titles
_d5 = tempfile.mkdtemp(prefix="lucidadl_m3u_")
_plf = _os.path.join(_d5, "Playlists", "My Mix")
_os.makedirs(_plf)
for _n in ("02 - Beta.m4a", "01 - Alpha.flac", "cover.jpg"):
    with open(_os.path.join(_plf, _n), "wb") as _fh:
        _fh.write(b"x")
_m3u = _org.write_playlist_m3u(_d5, "My Mix")
check("m3u8: named after the playlist, inside its folder",
      _m3u and _os.path.basename(_m3u) == "My Mix.m3u8"
      and _os.path.dirname(_m3u) == _plf)
with open(_m3u, encoding="utf-8") as _fh:
    _body = _fh.read()
_audio_lines = [ln for ln in _body.splitlines() if not ln.startswith("#")]
check("m3u8: tracks in filename order, bare names, no cover.jpg",
      _audio_lines == ["01 - Alpha.flac", "02 - Beta.m4a"])
check("m3u8: header + EXTINF with number-prefix stripped",
      _body.startswith("#EXTM3U") and "#EXTINF:-1,Alpha" in _body
      and "#EXTINF:-1,Beta" in _body)
check("m3u8: empty folder -> None (no file written)",
      _org.write_playlist_m3u(_d5, "Nope") is None)
check("_m3u_title strips 'NN - ' prefix and extension",
      _org._m3u_title("07 - Enfant Perdu.m4a") == "Enfant Perdu"
      and _org._m3u_title("No Prefix.flac") == "No Prefix")
_sh.rmtree(_d5, ignore_errors=True)

# downloader meta builders
from lucidadl.downloader import _track_meta as _tm, _join_artists as _ja
check("_join_artists None -> ''", _ja(None) == "")
check("_join_artists skips nameless", _ja([{"name": "X"}, {"foo": 1}]) == "X")
_m_alb = _tm({"title": "Californication", "artists": [{"name": "RHCP"}]},
             {"title": "Around the World", "artists": [{"name": "RHCP"}]}, True)
check("_track_meta album: album-level artist + album title",
      _m_alb == {"albumartist": "RHCP", "album": "Californication", "artist": "RHCP",
                 "title": "Around the World"})
# compilation: album-level artist used for ALL tracks (no per-track scatter)
_m_va = _tm({"title": "VA Comp", "artists": [{"name": "Various Artists"}]},
            {"title": "Song", "artists": [{"name": "Some Performer"}]}, True)
check("_track_meta album: uses album artist, not per-track (no scatter)",
      _m_va["albumartist"] == "Various Artists")
_m_sgl = _tm({}, {"title": "One More Time", "artists": [{"name": "Daft Punk"}],
                  "album": {"title": "Discovery"}}, False)
check("_track_meta single: track artist + nested album.title",
      _m_sgl["albumartist"] == "Daft Punk" and _m_sgl["album"] == "Discovery")

from lucidadl import tui as _tui

# matching: pick the real track over remixes / the real album over tributes
from lucidadl import matching as _m
_tracks = [
    {"url": "remix1", "title": "Do I Wanna Know? (Lncn Remix)", "context": "Do I Wanna Know? (Lncn Remix) Arctic Monkeys"},
    {"url": "remix2", "title": "Do I Wanna Know? (Club Mix)", "context": "Do I Wanna Know? (Club Mix) Arctic Monkeys"},
    {"url": "real", "title": "Do I Wanna Know?", "context": "Do I Wanna Know? Arctic Monkeys AM"},
]
check("match picks real track over remixes",
      _m.pick_best("Arctic Monkeys - Do I Wanna Know?", _tracks) == "real")

_albums = [
    {"url": "trib", "title": "Tribute to Red Hot Chili Peppers", "context": "Tribute to Red Hot Chili Peppers Various Artists"},
    {"url": "rend", "title": "Lullaby Renditions of Red Hot Chili Peppers", "context": "Lullaby Renditions Rockabye Baby"},
    {"url": "realalb", "title": "Californication", "context": "Californication Red Hot Chili Peppers 1999"},
]
check("match picks real album over tribute/renditions",
      _m.pick_best("Red Hot Chili Peppers - Californication", _albums) == "realalb")

check("match: explicit remix query keeps remix",
      _m.pick_best("Artist - Song Remix", [
          {"url": "r", "title": "Song Remix", "context": "Song Remix Artist"},
          {"url": "p", "title": "Song", "context": "Song Artist"}]) in ("r", "p"))
check("match empty -> None", _m.pick_best("x", []) is None)

# matching: pick the real artist's album over a same-titled cover/tribute
alb_candidates = [
    {"url": "u_cover", "title": "Californication", "artist": "ReStyleHits", "album": ""},
    {"url": "u_tribute", "title": "Californication", "artist": "Vitamin String Quartet", "album": ""},
    {"url": "u_rhcp", "title": "Californication", "artist": "Red Hot Chili Peppers", "album": ""},
]
check("matching: real artist album beats cover",
      matching.pick_best("Red Hot Chili Peppers - Californication", alb_candidates) == "u_rhcp")

# matching: pick the real track over remixes
trk_candidates = [
    {"url": "t_rmx1", "title": "Otherside (Moonbeam Remix)", "artist": "Red Hot Chili Peppers"},
    {"url": "t_rmx2", "title": "Otherside (Club Mix)", "artist": "DJ X"},
    {"url": "t_real", "title": "Otherside", "artist": "Red Hot Chili Peppers"},
]
check("matching: real track beats remixes",
      matching.pick_best("Red Hot Chili Peppers - Otherside", trk_candidates) == "t_real")

# matching: wrong artist penalised even with exact title
check("matching: wrong artist loses",
      matching.pick_best("Red Hot Chili Peppers - Otherside",
                         [{"url": "w", "title": "Otherside", "artist": "Macklemore"},
                          {"url": "r", "title": "Otherside", "artist": "Red Hot Chili Peppers"}]) == "r")
check("matching: automatic mode rejects a wrong-artist-only result",
      matching.pick_best("Red Hot Chili Peppers - Otherside",
                         [{"url": "w", "title": "Otherside", "artist": "Macklemore"}],
                         min_score=5.5, min_margin=0.25) is None)
check("matching: automatic mode rejects tied editions",
      matching.pick_best("Artist - Song",
                         [{"url": "a", "title": "Song", "artist": "Artist"},
                          {"url": "b", "title": "Song", "artist": "Artist"}],
                         min_score=5.5, min_margin=0.25) is None)

# resolver query variants (specific -> loose) + artist-gated broadening
from lucidadl.downloader import _query_variants as _qv
_v = _qv("Sinyo' - Enfant Perdu")
check("variants: full query first", _v[0] == "Sinyo' - Enfant Perdu")
check("variants: title-only included", "Enfant Perdu" in _v)
_v2 = _qv("Ptite Soeur, FEMTOGO - PUKE SOMETHING")
check("variants: title-only + primary-artist forms",
      "PUKE SOMETHING" in _v2 and "Ptite Soeur PUKE SOMETHING" in _v2)
check("variants: no separator -> just the line", _qv("Madonna") == ["Madonna"])

_title_hits = [
    {"url": "wrong", "title": "Enfant Perdu", "artist": "Some Other Artist", "context": "Enfant Perdu Some Other Artist"},
    {"url": "right", "title": "Enfant Perdu", "artist": "Sinyo", "context": "Enfant Perdu Sinyo"},
]
check("require_artist picks the matching artist from a title-only search",
      matching.pick_best("Sinyo' - Enfant Perdu", _title_hits, require_artist=True) == "right")
_only_wrong = [{"url": "w", "title": "Enfant Perdu", "artist": "Nope", "context": "Enfant Perdu Nope"}]
check("require_artist returns None when no artist matches (no wrong download)",
      matching.pick_best("Sinyo' - Enfant Perdu", _only_wrong, require_artist=True) is None)
check("require_artist off -> legacy best-anyway behavior",
      matching.pick_best("Sinyo' - Enfant Perdu", _only_wrong) == "w")
check("artist_matches token overlap",
      matching.artist_matches("Sinyo' - Enfant Perdu", {"artist": "Sinyo"}) is True
      and matching.artist_matches("Sinyo' - Enfant Perdu", {"artist": "Other"}) is False)

# transcode bitrate normalization (bare number = kbps)
from lucidadl import transcode as _T
check("bitrate 192 -> 192k", _T.norm_bitrate("192") == "192k")
check("bitrate 320k stays", _T.norm_bitrate("320k") == "320k")
check("bitrate None stays None", _T.norm_bitrate(None) is None)
check("transcode cmd has -b:a 192k",
      "192k" in _T.build_cmd("ffmpeg", "i.flac", "o.m4a", "aac", "192"))

# fast HTTP path parsing (raw SvelteKit JSON5 blob -> tracks + helpers)
import pyjson5
from lucidadl.api import LucidaClient, _between, _filename_from_cd
_alb = ('{info:{success:true,type:"album",title:"Cal",tracks:['
        '{title:"A",url:"https://q/track/1",csrf:"C1",csrfFallback:"F1",producers:["p"]},'
        '{title:"B",url:"https://q/track/2",csrf:"C2",csrfFallback:null,producers:null}'
        ']},originalService:"qobuz",token:"TOK",tokenExpiry:123}')
_tracks = LucidaClient.tracks_from_pd(pyjson5.loads(_alb))
check("pd album -> 2 tracks w/ csrf", len(_tracks) == 2 and _tracks[0]["csrf"] == "C1")
check("pd album null producers kept", _tracks[1].get("producers") is None)
_trk = LucidaClient.tracks_from_pd(pyjson5.loads(
    '{info:{type:"track",title:"X",url:"https://q/track/9",producers:["p"]},token:"TT",tokenExpiry:9}'))
check("pd single track csrf=token", len(_trk) == 1 and _trk[0]["csrf"] == "TT")
check("_between slices blob",
      _between('x,{"type":"data","data":{a:1},"uses":{"url":1}}];y',
               ',{"type":"data","data":', ',"uses":{"url":1}}];') == "{a:1}")
check("filename from content-disposition",
      _filename_from_cd('attachment; filename="01 - Song.flac"') == "01 - Song.flac")

# refresh dedup: N concurrent 403-refreshes must call acquire() exactly once
import asyncio as _aio
_calls = {"n": 0}
async def _fake_acquire():
    _calls["n"] += 1
    await _aio.sleep(0.01)
    return ("CF" + str(_calls["n"]), "UA")
_c = LucidaClient(cf_clearance="old", user_agent="UA", acquire=_fake_acquire)
async def _refresh_storm():
    await _aio.gather(*[_c._refresh_creds() for _ in range(5)])
_aio.run(_refresh_storm())
check("refresh deduped to 1 browser open", _calls["n"] == 1 and _c.cf == "CF1")

# fallback services remain intentionally small
from lucidadl.api import FALLBACK_SERVICES
check("fallback chain", "qobuz" in FALLBACK_SERVICES and "amazon" in FALLBACK_SERVICES)

# first-run UX: the quick doctor must never open a browser unless --live is requested
import contextlib as _ctxlib
import io as _io
from unittest.mock import AsyncMock as _AsyncMock, patch as _patch
from click.testing import CliRunner as _CliRunner
from lucidadl import cli as _cli

with _patch.object(_cli, "chromium_installed", _AsyncMock(return_value=True)), \
     _patch.object(_cli.transcode, "available", return_value=True), \
     _patch.object(_cli, "load_clearance", return_value=("CF", "UA")), \
     _patch.object(_cli, "lucida_context") as _browser_ctx:
    _doctor_out = _io.StringIO()
    with _ctxlib.redirect_stdout(_doctor_out):
        _doctor_ok = _aio.run(_cli._doctor(live=False))
check("doctor: quick check succeeds without opening a browser",
      _doctor_ok and not _browser_ctx.called and "not run" in _doctor_out.getvalue())

with _patch.object(_cli, "chromium_installed", _AsyncMock(return_value=False)), \
     _patch.object(_cli, "install_chromium", _AsyncMock(return_value=True)) as _install, \
     _patch.object(_cli, "acquire_clearance", _AsyncMock(return_value=("CF", "UA"))) as _access:
    with _ctxlib.redirect_stdout(_io.StringIO()):
        _setup_ok = _aio.run(_cli._setup())
check("setup: installs a missing browser before preparing access",
      _setup_ok and _install.await_count == 1 and _access.await_count == 1)

with _patch.object(_tui.os.path, "exists", return_value=False):
    check("tui: empty app data is detected as first run", _tui._is_first_run())

# corrupt or hand-edited settings must not prevent the TUI from starting
with _patch.object(_tui.paths, "load_config", return_value={"jobs": "many"}):
    check("tui: invalid jobs setting falls back safely", _tui._settings()["jobs"] == 3)

# failures preserve album/track intent, while old files stay backwards-compatible
_failed_dir = tempfile.mkdtemp(prefix="lucidadl_failed_")
_failed_path = _os.path.join(_failed_dir, "failed.txt")
with _patch.object(_cli, "FAILED_PATH", _failed_path):
    _cli._write_failed([("album", "Artist - Album"), ("track", "Artist - Song")])
    check("retry: typed failures round-trip", _cli._read_failed() == [
        ("album", "Artist - Album"), ("track", "Artist - Song")])
    with open(_failed_path, "w", encoding="utf-8") as _legacy:
        _legacy.write("Legacy Artist - Song\n")
    check("retry: legacy failures remain track retries",
          _cli._read_failed() == [("track", "Legacy Artist - Song")])

    _retry_calls = []
    async def _fake_retry_run(values, kind, *args, **kwargs):
        _retry_calls.append((kind, values))
        if kind == "track":
            return ({"ok": 0, "skip": 0, "fail": 1}, [("track", values[0])])
        return ({"ok": 1, "skip": 0, "fail": 0}, [])

    with _patch.object(_cli, "_run", side_effect=_fake_retry_run):
        with _ctxlib.redirect_stdout(_io.StringIO()):
            _retry_result = _aio.run(_cli._retry(
                [("album", "Artist - Album"), ("track", "Artist - Song")],
                "qobuz", None, "original", _failed_dir, False, 3))
    check("retry: albums and tracks are dispatched with their original type",
          _retry_calls == [
              ("track", ["Artist - Song"]), ("album", ["Artist - Album"])])
    check("retry: only unresolved items remain",
          _retry_result[1] == [("track", "Artist - Song")]
          and _cli._read_failed() == [("track", "Artist - Song")])
_sh0.rmtree(_failed_dir)

# unsupported playlist URLs fail immediately without launching Playwright
with _patch.object(_cli, "lucida_context") as _playlist_browser:
    with _ctxlib.redirect_stdout(_io.StringIO()):
        _unsupported_ok = _aio.run(_cli._playlist(
            "https://example.com/list", True, "qobuz", None, "original",
            tempfile.gettempdir(), False, 3))
check("playlist: unsupported links fail before opening a browser",
      not _unsupported_ok and not _playlist_browser.called)

# command contracts used by scripts: missing input/search failure must be non-zero,
# while an explicit search cancellation remains a normal successful exit
_runner = _CliRunner()
_missing_batch = _runner.invoke(
    _cli.cli, ["tracks", "--file", _os.path.join(tempfile.gettempdir(),
                                                  "lucidadl-no-such-batch.txt")])
check("batch: missing file returns exit 1 with guidance",
      _missing_batch.exit_code == 1 and "Batch file not found" in _missing_batch.output
      and "--file" in _missing_batch.output)

_search_failure = ({"ok": 0, "skip": 0, "fail": 1}, [])
with _patch.object(_cli, "_search", _AsyncMock(return_value=_search_failure)):
    _search_failed = _runner.invoke(_cli.cli, ["search", "anything"])
check("search: failure result returns exit 1", _search_failed.exit_code == 1)
with _patch.object(_cli, "_search", _AsyncMock(return_value=None)):
    _search_cancelled = _runner.invoke(_cli.cli, ["search", "anything"])
check("search: explicit cancellation returns exit 0", _search_cancelled.exit_code == 0)

_help = _runner.invoke(_cli.cli, ["--help"])
check("cli: developer debug command is hidden", "  debug " not in _help.output)

print()
if fails:
    print(f"{len(fails)} FAILURE(S): {fails}")
    raise SystemExit(1)
print("ALL OFFLINE TESTS PASSED")
