#!/usr/bin/env python
"""Probe MusicBrainz for song metadata (genre tags, year) from artist + title strings only.

Design: docs/notes/song-context.md. This is a prototype, not a scraper: one documented search
call per song, 1 request/second, a descriptive User-Agent, results cached locally.

PRIVACY: only the artist and title strings leave the machine. Never audio, never file names,
never paths. `--dry-run` is the DEFAULT and makes no network call: it prints the exact URLs it
would request. A live run needs BOTH `--live` and `--accept-privacy` (the opt-in), plus a contact
address in TONEMATCH_CONTACT or --contact (MusicBrainz asks for contact info in the User-Agent).

TERMS CHECKED 2026-10-17 (read, not run against the network by the author):
  * MusicBrainz API: https://musicbrainz.org/doc/MusicBrainz_API  ("Non-commercial use of this web
    service is free", no key, meaningful User-Agent required)
  * Rate limit: https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting (1 request/s average)
  * Data licence: https://musicbrainz.org/doc/About/Data_License (core data CC0; tags/genres are
    supplementary, CC BY-NC-SA 3.0: attribute and keep non-commercial; do not redistribute).
Results are cached under cache/song_meta/ (gitignored) and must not be committed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "cache" / "song_meta"
ENDPOINT = "https://musicbrainz.org/ws/2/recording"
VERSION = "0.1"
EXAMPLES = [("Metallica", "Enter Sandman"), ("Nirvana", "Come as You Are"),
            ("Pink Floyd", "Comfortably Numb")]
MIN_INTERVAL_S = 1.1  # MusicBrainz: 1 request per second on average


def _escape(s: str) -> str:
    """Escape Lucene special characters and quotes inside a quoted phrase."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def build_url(artist: str, title: str, limit: int = 100) -> str:
    # No release-type filter: a classic recording also sits on compilations, so excluding those
    # drops the original. Instead fetch the API maximum (100) and take the oldest date (parse).
    q = f'recording:"{_escape(title)}" AND artist:"{_escape(artist)}"'
    return f"{ENDPOINT}?{urllib.parse.urlencode({'query': q, 'fmt': 'json', 'limit': limit})}"


def _year(s) -> int | None:
    y = str(s or "")[:4]
    return int(y) if y.isdigit() else None


def _recording_years(r: dict) -> list[int]:
    ys = [_year(r.get("first-release-date"))] + [_year(x.get("date")) for x in r.get("releases") or []]
    return [y for y in ys if y]


def parse_recordings(payload: dict, top_tags: int = 6, min_score: int = 90) -> dict:
    """Reduce a MusicBrainz recording-search response to what the prior needs. Pure function.

    The year is the OLDEST release among all strong matches (score >= ``min_score``), not the
    best match's own date: songs are re-released on compilations, live albums and remasters, and
    the original release is the one that dates the tone (human, 2026-10-17)."""
    recs = payload.get("recordings") or []
    if not recs:
        return {"found": False}
    best = max(recs, key=lambda r: int(r.get("score", 0)))
    strong = [r for r in recs if int(r.get("score", 0)) >= min_score] or [best]
    dated = [(min(ys), r) for r in strong if (ys := _recording_years(r))]
    oldest_year, oldest = min(dated, key=lambda d: d[0]) if dated else (None, best)
    tags = sorted(best.get("tags") or [], key=lambda t: -int(t.get("count", 0)))
    return {
        "found": True,
        "score": int(best.get("score", 0)),
        "mbid": best.get("id"),
        "first_release_year": oldest_year,
        "year_from_mbid": oldest.get("id"),
        "matches_considered": len(strong),
        "tags": [t["name"] for t in tags[:top_tags]],
        "labels_not_in_search": True,  # label needs a release lookup (inc=labels); not done here
    }


def _key(artist: str, title: str) -> str:
    return hashlib.sha256(f"{artist.lower()}|{title.lower()}".encode()).hexdigest()[:16]


def fetch(url: str, user_agent: str, retries: int = 3, backoff_s: float = 2.0) -> dict:
    """GET one MusicBrainz URL. A 503 is MusicBrainz's rate-limit answer: wait and retry
    (2, 4, 8 s), then give up."""
    import urllib.error
    req = urllib.request.Request(url, headers={"User-Agent": user_agent,
                                               "Accept": "application/json"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 503 or attempt == retries:
                raise
            time.sleep(backoff_s * 2 ** attempt)
    raise RuntimeError("unreachable")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--song", nargs=2, action="append", metavar=("ARTIST", "TITLE"),
                    help="repeatable; default = 3 built-in examples")
    ap.add_argument("--dry-run", dest="dry", action="store_true", default=True,
                    help="(default) print the URLs and send nothing")
    ap.add_argument("--live", action="store_true", help="really call MusicBrainz")
    ap.add_argument("--accept-privacy", action="store_true",
                    help="opt in: artist and title strings are sent to musicbrainz.org")
    ap.add_argument("--contact", default=os.environ.get("TONEMATCH_CONTACT", ""))
    a = ap.parse_args(argv)
    songs = [tuple(s) for s in a.song] if a.song else EXAMPLES

    live = a.live
    if live and not a.accept_privacy:
        print("refused: --live also needs --accept-privacy (artist and title strings will be "
              "sent to musicbrainz.org; no audio, no file names).", file=sys.stderr)
        return 2
    if live and not a.contact:
        print("refused: set TONEMATCH_CONTACT or --contact (MusicBrainz wants contact info in "
              "the User-Agent).", file=sys.stderr)
        return 2

    ua = f"tonematch-song-probe/{VERSION} ( {a.contact or 'contact-unset'} )"
    CACHE.mkdir(parents=True, exist_ok=True)
    last = 0.0
    for artist, title in songs:
        url = build_url(artist, title)
        if not live:
            print(f"[dry-run] would GET {url}\n          User-Agent: {ua}")
            continue
        cached = CACHE / f"{_key(artist, title)}.json"
        raw = json.loads(cached.read_text()) if cached.exists() else None
        if isinstance(raw, dict) and "recordings" in raw and raw.get("_url") == url:
            print(f"{artist} - {title}: cached {parse_recordings(raw)}")   # raw response cached:
            continue                                                        # parsing changes need no refetch
        wait = MIN_INTERVAL_S - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.monotonic()
        raw = fetch(url, ua)
        raw["_url"] = url
        cached.write_text(json.dumps(raw))
        print(f"{artist} - {title}: {parse_recordings(raw)}")
    if not live:
        print("dry run only: nothing was sent. Live needs --live --accept-privacy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
