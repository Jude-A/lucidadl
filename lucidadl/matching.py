"""Pick the best search result for a query instead of blindly taking the first.

Search results often rank remixes / karaoke / tribute / "renditions" versions above
the real track, so we score each candidate on title+artist match and penalise
unwanted variants (unless the query explicitly asked for them)."""

from __future__ import annotations

import re
from typing import Dict, List, Optional

# Whole-word markers of an unwanted variant.
_BAD_TOKENS = {
    "remix", "mix", "karaoke", "cover", "covers", "instrumental", "nightcore",
    "acoustic", "rendition", "renditions", "tribute", "lullaby", "parody",
    "workout", "reverb", "slowed", "sped", "8d", "acapella", "acappella",
}
# Multi-word markers.
_BAD_PHRASES = (
    "sped up", "made famous", "originally performed", "in the style of",
    "made popular", "8d audio", "piano version", "string quartet", "tribute to",
)


def _norm(s: str) -> str:
    return re.sub(r"[^\w\s]", " ", (s or "").lower())


def _tokens(s: str) -> set:
    return set(_norm(s).split())


def _split_query(q: str):
    if " - " in q:
        a, t = q.split(" - ", 1)
        return a.strip(), t.strip()
    return "", q.strip()


def score(query: str, item: Dict[str, str]) -> float:
    artist_q, title_q = _split_query(query)
    q_tokens = _tokens(query)
    title = item.get("title", "")
    artist = item.get("artist", "")
    ctx = item.get("context") or title
    t_tokens = _tokens(title)
    a_tokens = _tokens(artist)                            # explicit artist field (h2)
    c_tokens = _tokens(ctx) | a_tokens

    s = 0.0
    tq = _tokens(title_q)
    if tq:
        s += 3.0 * len(tq & t_tokens) / len(tq)          # title word overlap
    if _norm(title_q) and _norm(title_q) in _norm(title):
        s += 2.0                                          # exact-ish title
    aq = _tokens(artist_q)
    if aq:
        # Prefer the explicit artist field; fall back to the row context.
        artist_hits = len(aq & a_tokens) / len(aq) if a_tokens else 0.0
        ctx_hits = len(aq & c_tokens) / len(aq)
        s += 3.0 * artist_hits + 1.0 * ctx_hits           # artist match (weighted)
        if a_tokens and not (aq & a_tokens):
            s -= 2.0                                       # wrong artist → strong penalty

    blob_tokens = t_tokens | c_tokens
    for b in _BAD_TOKENS:
        if b in blob_tokens and b not in q_tokens:
            s -= 1.5
    blob = _norm(title) + " " + _norm(ctx)
    nq = _norm(query)
    for ph in _BAD_PHRASES:
        if ph in blob and ph not in nq:
            s -= 1.5

    if title_q:
        s -= 0.01 * abs(len(title) - len(title_q))        # prefer closest length
    return s


def artist_matches(query: str, item: Dict[str, str]) -> bool:
    """True if the item's artist (or row context) overlaps the query's artist tokens.
    Used to reject wrong-artist hits when a search was broadened to the title alone."""
    artist_q, _ = _split_query(query)
    aq = _tokens(artist_q)
    if not aq:
        return True  # no artist asked for → nothing to reject
    pool = _tokens(item.get("artist", "")) | _tokens(item.get("context") or "")
    return bool(aq & pool)


def pick_best(query: str, items: List[Dict[str, str]],
              require_artist: bool = False, min_score: Optional[float] = None,
              min_margin: float = 0.0) -> Optional[str]:
    """Return the URL of the best-scoring candidate (ties → earliest result). When
    `require_artist` is set and the query names an artist, only candidates whose artist
    matches are considered. Optional score/margin guards let automatic downloads reject
    a weak or ambiguous match while interactive search can keep showing every result."""
    if not items:
        return None
    pool = items
    if require_artist:
        matched = [it for it in items if artist_matches(query, it)]
        if not matched:
            return None
        pool = matched
    ranked = sorted(enumerate(pool), key=lambda pair: score(query, pair[1]), reverse=True)
    best_i, best_item = ranked[0]
    best_s = score(query, best_item)
    if min_score is not None and best_s < min_score:
        return None
    if len(ranked) > 1:
        second_s = score(query, ranked[1][1])
        if best_s - second_s < min_margin:
            return None
    return pool[best_i].get("url")
