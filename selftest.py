"""Offline self-test of the pure logic (no browser, no network)."""

import asyncio as _aio

from lucidadl import utils, matching
from lucidadl.api import (
    LucidaClient, normalize_service, default_country, _long, _apple_tracks_from_obj,
    _apple_playlist_from_scripts, is_apple_playlist_url, playlist_source,
    _spotify_playlist_from_html, _spotify_total_from_html,
    _deezer_playlist_from_obj, DOWNSCALE_CHOICES,
)
from lucidadl.models import FailedItem

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
check("Apple URL validation accepts playlists only",
      is_apple_playlist_url("https://music.apple.com/fr/playlist/mix/pl.123")
      and not is_apple_playlist_url("https://music.apple.com/fr/album/record/123")
      and not is_apple_playlist_url("https://example.com/playlist/mix/pl.123"))
check("public playlist source detection",
      playlist_source("https://music.apple.com/fr/playlist/mix/pl.123") == "apple"
      and playlist_source("https://open.spotify.com/playlist/abc?si=share") == "spotify"
      and playlist_source("https://www.deezer.com/fr/playlist/123") == "deezer"
      and playlist_source("https://open.spotify.com/album/abc") == "")

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

_apple_name, _apple_structured = _apple_playlist_from_scripts([
    "not-json",
    __import__("json").dumps(sample),
])
check("apple structured extractor: playlist name + ordered songs",
      _apple_name == "My PL" and _apple_structured == [
          {"title": "Otherside", "artist": "RHCP"},
          {"title": "Scar Tissue", "artist": "RHCP"},
      ])

_duplicate_sample = {"data": [{"type": "playlists", "attributes": {"name": "Loop"},
                     "relationships": {"tracks": {"data": [
                         {"type": "songs", "attributes": {
                             "name": "Again", "artistName": "Artist"}},
                         {"type": "songs", "attributes": {
                             "name": "Again", "artistName": "Artist"}},
                     ]}}}]}
_duplicate_name, _duplicate_tracks = _apple_playlist_from_scripts([
    __import__("json").dumps(_duplicate_sample)])
check("apple structured extractor preserves intentional duplicate positions",
      _duplicate_name == "Loop" and len(_duplicate_tracks) == 2)

# Spotify and Deezer importers parse public metadata only; downloads still use lucida.
_spotify_obj = {
    "props": {"pageProps": {"state": {"data": {"entity": {
        "type": "playlist", "name": "My Spotify Mix", "trackList": [
            {"entityType": "track", "title": "One", "subtitle": "Artist\xa0A"},
            {"entityType": "episode", "title": "Podcast", "subtitle": "Host"},
            {"entityType": "track", "title": "One", "subtitle": "Artist\xa0A"},
        ],
    }}}}},
}
_spotify_html = ('<script id="__NEXT_DATA__" type="application/json">'
                 + __import__("json").dumps(_spotify_obj) + '</script>')
_spotify_name, _spotify_tracks = _spotify_playlist_from_html(_spotify_html)
check("spotify extractor keeps order/duplicates and skips episodes",
      _spotify_name == "My Spotify Mix"
      and _spotify_tracks == [
          {"title": "One", "artist": "Artist A"},
          {"title": "One", "artist": "Artist A"},
      ])
_spotify_ld = ('<script type="application/ld+json">'
               + __import__("json").dumps({
                   "description": "Playlist · Top Hits 2026 · 198 items · 2 saves"})
               + '</script>')
check("spotify extractor detects playlists beyond the public 100-item window",
      _spotify_total_from_html(_spotify_ld) == 198)

_deezer_name, _deezer_tracks, _deezer_next = _deezer_playlist_from_obj({
    "title": "My Deezer Mix",
    "tracks": {"data": [
        {"title": "First", "artist": {"name": "Primary"},
         "contributors": [{"name": "Primary"}, {"name": "Guest"}]},
        {"title": "Again", "artist": {"name": "Primary"}},
    ], "next": "https://api.deezer.com/next"},
})
check("deezer extractor keeps order, contributors and pagination",
      _deezer_name == "My Deezer Mix"
      and _deezer_tracks == [
          {"title": "First", "artist": "Primary, Guest"},
          {"title": "Again", "artist": "Primary"},
      ] and _deezer_next.endswith("/next"))

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
s3.done["gone"] = [_os.path.join(_d3, "missing.flac")]
_removed_paths, _removed_items = s3.prune()
check("state prune removes missing paths/items",
      _removed_paths == 2 and _removed_items == 1 and "gone" not in s3.done
      and s3.done.get("urlE") == [_af])
check("state prune preserves unverifiable legacy entries", "urlLegacy" in s3.done)
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
check("m3u8 numeric ordering survives mixed zero-padding",
      sorted(["100 - Last.flac", "02 - Second.flac", "005 - Fifth.flac"],
             key=_org._playlist_sort_key) ==
      ["02 - Second.flac", "005 - Fifth.flac", "100 - Last.flac"])
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
check("matching: automatic mode rejects a secondary-artist cover",
      matching.pick_best(
          "Red Hot Chili Peppers - Under the Bridge",
          [{"url": "cover", "title": "Under The Bridge",
            "artist": "Rhythms Del Mundo, Red Hot Chili Peppers"}],
          min_score=5.5, min_margin=0.25) is None)
check("matching: automatic mode rejects an unrequested sped-up version",
      matching.pick_best(
          "Manu Chao - Bongo Bong",
          [{"url": "sped", "title": "Bongo Bong - Sped Up (Manu Chao)",
            "artist": "Manu Chao, spedup trends"}],
          min_score=5.5, min_margin=0.25) is None)
check("matching: automatic mode accepts a matching primary artist",
      matching.pick_best(
          "La Fine Equipe, Saneyes - Lying With You",
          [{"url": "right", "title": "Lying With You", "artist": "La Fine Equipe"}],
          min_score=5.5, min_margin=0.25) == "right")

# resolver query variants (specific -> loose) + artist-gated broadening
from lucidadl.downloader import _query_variants as _qv
_v = _qv("Sinyo' - Enfant Perdu")
check("variants: full query first", _v[0] == "Sinyo' - Enfant Perdu")
check("variants: title-only included", "Enfant Perdu" in _v)
_v2 = _qv("Ptite Soeur, FEMTOGO - PUKE SOMETHING")
check("variants: title-only + primary-artist forms",
      "PUKE SOMETHING" in _v2 and "Ptite Soeur PUKE SOMETHING" in _v2)
check("variants: no separator -> just the line", _qv("Madonna") == ["Madonna"])

from lucidadl.downloader import friendly_error as _friendly_error
check("friendly error: access guidance", "lucida setup" in _friendly_error(Exception("HTTP 403")))
check("friendly error: timeout retry guidance", "safe to retry" in _friendly_error(Exception("timed out")))

from lucidadl.downloader import _playlist_download_key, _existing_playlist_copy
_legacy_playlist_dir = tempfile.mkdtemp(prefix="lucidadl_playlist_state_")
_legacy_file = _os.path.join(_legacy_playlist_dir, "02 - Song.flac")
with open(_legacy_file, "wb") as _handle:
    _handle.write(b"x")
_legacy_state = utils.State(_os.path.join(_legacy_playlist_dir, "state.json"))
_legacy_state.add("track-url", _legacy_file)
check("playlist state: each duplicate position gets a distinct key",
      _playlist_download_key("track-url", "Mix", "02") !=
      _playlist_download_key("track-url", "Mix", "07"))
check("playlist state: v1.1 copy recognized only at its original position",
      _existing_playlist_copy(_legacy_state, "track-url", _legacy_playlist_dir, "02") ==
      _legacy_file and
      not _existing_playlist_copy(_legacy_state, "track-url", _legacy_playlist_dir, "07"))
_sh0.rmtree(_legacy_playlist_dir, ignore_errors=True)

from lucidadl.downloader import preview_tracks as _preview_tracks
class _PreviewClient:
    async def search(self, query, _service):
        if "Missing" in query:
            return {"tracks": []}
        title = query.split(" - ", 1)[-1]
        return {"tracks": [{"url": "https://qobuz/" + title, "title": title,
                            "artist": "Artist", "context": title + " Artist"}]}

    async def fetch_page_data(self, url, _country):
        title = url.rsplit("/", 1)[-1]
        return {"info": {"type": "track", "url": url, "title": title,
                         "artists": [{"name": "Artist"}], "producers": ["p"]},
                "token": "token", "tokenExpiry": 1}

    tracks_from_pd = staticmethod(LucidaClient.tracks_from_pd)

_preview = _aio.run(_preview_tracks(
    _PreviewClient(), ["Artist - One", "Artist - Missing", "Artist - Two"],
    "qobuz", "US", jobs=3, log=lambda *_: None))
check("playlist check: preserves order and reports missing matches",
      [row["index"] for row in _preview] == [1, 2, 3]
      and [row["status"] for row in _preview] == ["matched", "not found", "matched"])

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
check("artist_matches checks the primary artist",
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
from lucidadl.api import _between, _filename_from_cd, _retry_delay
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

class _RetryResponse:
    headers = {"Retry-After": "99"}
check("network retry delay is bounded", _retry_delay(_RetryResponse(), 0) == 30)

# refresh dedup: N concurrent 403-refreshes must call acquire() exactly once
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
     _patch.object(_cli, "_music_health", return_value=(True, "ready")), \
     _patch.object(_cli, "_stale_partials", return_value=[]), \
     _patch.object(_cli, "_load_playlist_run", return_value={}), \
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
with _patch.object(_tui, "load_clearance", return_value=(None, None)):
    check("tui: corrupt/empty access is not shown as prepared", not _tui._access_ready())
with _patch.object(_tui, "load_clearance", return_value=("CF", "UA")):
    check("tui: complete access is shown as prepared", _tui._access_ready())

# corrupt or hand-edited settings must not prevent the TUI from starting
with _patch.object(_tui.paths, "load_config", return_value={"jobs": "many"}):
    check("tui: invalid jobs setting falls back safely", _tui._settings()["jobs"] == 3)

# ordinary downloads use the shared runner without depending on playlist-only locals
class _TuiConsole:
    def print(self, *_args, **_kwargs):
        pass


class _TuiCli:
    @staticmethod
    async def _run(*_args, **_kwargs):
        return {"ok": 1, "skip": 0, "fail": 0}, []


def _invoke_tui_go(_s, _console, _cli_obj, _questionary, go):
    go(["Artist - Song"], "track", False)
    return True


with _patch.object(_tui, "_download_action", side_effect=_invoke_tui_go), \
     _patch.object(_tui, "_access_ready", return_value=True), \
     _patch.object(_tui, "_show_run_summary"):
    _tui_download_ok = _tui._dispatch(
        "download",
        {"service": "qobuz", "jobs": 1, "to": None, "bitrate": None,
         "keep_orig": False, "force": False},
        _TuiConsole(), _TuiCli(), object(),
    )
check("tui: ordinary downloads do not depend on playlist choices", _tui_download_ok)

# failures preserve album/track intent, while old files stay backwards-compatible
_failed_dir = tempfile.mkdtemp(prefix="lucidadl_failed_")
_failed_path = _os.path.join(_failed_dir, "failed.txt")
_playlist_run_path = _os.path.join(_failed_dir, "playlist-run.json")
with _patch.object(_cli, "FAILED_PATH", _failed_path), \
     _patch.object(_cli, "PLAYLIST_RUN_PATH", _playlist_run_path):
    _cli._write_failed([("album", "Artist - Album"), ("track", "Artist - Song")])
    check("retry: typed failures round-trip", _cli._read_failed() == [
        FailedItem("album", "Artist - Album"), FailedItem("track", "Artist - Song")])
    _cli._write_failed([
        FailedItem("track", "Artist - Playlist Song", "My Mix", "07")])
    check("retry: playlist context round-trips", _cli._read_failed() == [
        FailedItem("track", "Artist - Playlist Song", "My Mix", "07")])
    with open(_failed_path, "w", encoding="utf-8") as _legacy:
        _legacy.write("Legacy Artist - Song\n")
    check("retry: legacy failures remain track retries",
          _cli._read_failed() == [FailedItem("track", "Legacy Artist - Song")])

    _retry_calls = []
    async def _fake_retry_run(values, kind, *args, **kwargs):
        _retry_calls.append((kind, values))
        if kind == "track":
            return ({"ok": 0, "skip": 0, "fail": 1}, [FailedItem("track", values[0])])
        return ({"ok": 1, "skip": 0, "fail": 0}, [])

    with _patch.object(_cli, "_run", side_effect=_fake_retry_run):
        with _ctxlib.redirect_stdout(_io.StringIO()):
            _retry_result = _aio.run(_cli._retry(
                [("album", "Artist - Album"), ("track", "Artist - Song")],
                "qobuz", None, "original", _failed_dir, False, 3))
    check("retry: albums and tracks are dispatched with their original type",
          _retry_calls == [
              ("album", ["Artist - Album"]), ("track", ["Artist - Song"])])
    check("retry: only unresolved items remain",
          _retry_result[1] == [FailedItem("track", "Artist - Song")]
          and _cli._read_failed() == [FailedItem("track", "Artist - Song")])

    _playlist_retry = []
    async def _fake_playlist_retry(values, kind, *args, **kwargs):
        _playlist_retry.append((values, kind, kwargs))
        return ({"ok": 1, "skip": 0, "fail": 0}, [])

    with _patch.object(_cli, "_run", side_effect=_fake_playlist_retry):
        with _ctxlib.redirect_stdout(_io.StringIO()):
            _aio.run(_cli._retry(
                [FailedItem("track", "Artist - Song", "My Mix", "07")],
                "qobuz", None, "original", _failed_dir, False, 3))
    check("retry: playlist stays in its collection with original number",
          len(_playlist_retry) == 1
          and _playlist_retry[0][2].get("collection") == "My Mix"
          and _playlist_retry[0][2].get("track_numbers") == ["07"])

    _interrupted = {
        "status": "running", "collection": "Interrupted Mix",
        "tracks": [
            {"track_no": "01", "query": "Artist - One"},
            {"track_no": "02", "query": "Artist - Two"},
        ],
        "options": {"service": "amazon", "out": _failed_dir, "jobs": 5},
    }
    _cli._save_playlist_run(_interrupted)
    _resume_calls = []
    async def _fake_resume(values, kind, *args, **kwargs):
        _resume_calls.append((values, kind, args, kwargs))
        return ({"ok": 1, "skip": 1, "fail": 0}, [])
    with _patch.object(_cli, "_run", side_effect=_fake_resume):
        with _ctxlib.redirect_stdout(_io.StringIO()):
            _aio.run(_cli._resume_playlist_run(_cli._pending_playlist_run()))
    check("playlist recovery: interrupted run resumes full ordered list",
          len(_resume_calls) == 1
          and _resume_calls[0][0] == ["Artist - One", "Artist - Two"]
          and _resume_calls[0][3].get("track_numbers") == ["01", "02"]
          and _resume_calls[0][3].get("collection") == "Interrupted Mix")
    check("playlist recovery: successful resume marked complete",
          _cli._load_playlist_run().get("status") == "complete")
_sh0.rmtree(_failed_dir)

# unsupported playlist URLs fail immediately without launching Playwright
with _patch.object(_cli, "lucida_context") as _playlist_browser:
    with _ctxlib.redirect_stdout(_io.StringIO()):
        _unsupported_ok = _aio.run(_cli._playlist(
            "https://example.com/list", True, "qobuz", None, "original",
            tempfile.gettempdir(), False, 3))
check("playlist: unsupported links fail before opening a browser",
      not _unsupported_ok and not _playlist_browser.called)

_remote_list = _os.path.join(tempfile.gettempdir(), "lucidadl_remote_playlist.txt")
with _patch.object(_cli.api, "public_playlist_tracklist", _AsyncMock(return_value=(
        "Public Mix", [{"artist": "Artist", "title": "Song"}]))), \
     _patch.object(_cli, "PLAYLIST_TEXT_PATH", _remote_list), \
     _patch.object(_cli, "lucida_context") as _remote_browser:
    with _ctxlib.redirect_stdout(_io.StringIO()):
        _remote_ok = _aio.run(_cli._playlist(
            "https://open.spotify.com/playlist/abc", True, "qobuz", None,
            "original", tempfile.gettempdir(), False, 3))
check("playlist: Spotify dry-run does not launch a browser",
      _remote_ok and not _remote_browser.called and _os.path.exists(_remote_list))
try:
    _os.remove(_remote_list)
except OSError:
    pass

check("playlist: failures before download retain recovery context",
      _cli._failed_result(
          ["Artist - One", "Artist - Two"], "track", "My Mix", ["01", "02"]
      )[1] == [
          FailedItem("track", "Artist - One", "My Mix", "01"),
          FailedItem("track", "Artist - Two", "My Mix", "02"),
      ])

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
_version = _runner.invoke(_cli.cli, ["--version"])
check("cli: source version matches release metadata",
      _version.exit_code == 0 and "1.3.0" in _version.output)

print()
if fails:
    print(f"{len(fails)} FAILURE(S): {fails}")
    raise SystemExit(1)
print("ALL OFFLINE TESTS PASSED")
