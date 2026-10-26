"""Song bookkeeping for the end-to-end matcher (T217): song id or file -> stems, rebuilt-dry folders, match folders, windows.

No song id is written in code. A song is found through the separate.py cache manifests
(`cache/stems/<hash>/<model>/manifest.json`, field `source`), by one of:

  - a path to the song file (its id is the file name as a slug; the stems come from `scripts/separate.py`, which cache-hits),
  - a stems hash (full or a unique prefix of at least 6 characters), or
  - an id: the file name as a slug (the id transcribe_stems.find_stems_all uses), or the lowercase letters and digits of the id must appear in those of the manifest `source` file name
    ("teen-spirit" matches "Nirvana - Smells Like Teen Spirit (Official Music Video).mp3"). More than one match is an error.

Layout (kept compatible with the folders the T200-T211 runs already made):

  data/songs/<id>-rebuilt/transcription.mid        Basic Pitch notes (shared by every guitar)
  data/songs/<id>-rebuilt/profiled-<guitar>/       rebuilt dry track with a guitar profile
  data/songs/<id>-rebuilt/playlevel<variant>/      the dry track at the chosen level + the alignment json
  cache/matches/<id>-dryrebuild<variant>/          the search (match_block)
  cache/matches/<id>-finetune<variant>/            the knob fine-tune (finetune_knobs)

Local only: nothing here reads or writes outside the repository except the song file the caller names.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STEMS_ROOT = ROOT / "cache" / "stems"
SONGS_ROOT = ROOT / "data" / "songs"
MATCHES_ROOT = ROOT / "cache" / "matches"
MODEL = "htdemucs_6s"
AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aif", ".aiff"}


class SongNotFound(LookupError):
    pass


def norm(text: str) -> str:
    """Lowercase letters and digits only: the form ids are compared in."""
    return re.sub(r"[^a-z0-9]+", "", str(text).lower())


def slug(text: str) -> str:
    """File-name safe id: lowercase, runs of other characters become one '-'."""
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "song"


@dataclass(frozen=True)
class SongRef:
    id: str
    stems_dir: Path
    source: str = ""
    song_hash: str = ""

    @property
    def guitar_wav(self) -> Path:
        return self.stems_dir / "guitar.wav"

    @property
    def mix_wav(self) -> Path:
        return self.stems_dir / "mix.wav"


def stem_sets(stems_root: Path | None = None, model: str = MODEL) -> list[dict]:
    """Every separated song in the cache: {dir, hash, source, name}."""
    root = Path(stems_root) if stems_root else STEMS_ROOT
    out = []
    for man in sorted(root.glob(f"*/{model}/manifest.json")):
        try:
            d = json.loads(man.read_text())
        except (OSError, ValueError):
            continue
        if not (man.parent / "guitar.wav").exists():
            continue
        src = str(d.get("source", ""))
        out.append({"dir": man.parent, "hash": str(d.get("song_hash") or man.parent.parent.name), "source": src,
                    "name": Path(src.replace("\\", "/")).name})
    return out


def find_stems(ref: str, stems_root: Path | None = None) -> dict:
    """The one stem set an id or hash names. Raises SongNotFound (none) or ValueError (several)."""
    sets = stem_sets(stems_root)
    ref = str(ref)
    exact = [s for s in sets if s["hash"] == ref]
    if exact:
        return exact[0]
    if len(ref) >= 6 and re.fullmatch(r"[0-9a-f]+", ref):
        pre = [s for s in sets if s["hash"].startswith(ref)]
        if len(pre) == 1:
            return pre[0]
    slugged = [s for s in sets if slug(Path(s["name"]).stem) == slug(ref)]    # the id scripts/transcribe_stems.py find_stems_all() uses
    if len(slugged) == 1:
        return slugged[0]
    key = norm(ref)
    if not key:
        raise SongNotFound(f"empty song id {ref!r}")
    hits = [s for s in sets if key in norm(Path(s["name"]).stem)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise SongNotFound(f"no separated song matches {ref!r} (known: {', '.join(s['name'] for s in sets) or 'none'}); "
                           "pass the song file so it is separated first")
    raise ValueError(f"{ref!r} matches {len(hits)} separated songs: " + ", ".join(f"{s['name']} ({s['hash'][:8]})" for s in hits)
                     + "; use the stems hash or a longer id")


def is_song_file(ref: str) -> bool:
    p = Path(ref)
    return p.suffix.lower() in AUDIO_SUFFIXES and p.is_file()


def resolve(ref: str, song_id: str | None = None, stems_root: Path | None = None, separate=None) -> SongRef:
    """Song id, stems hash or song file -> SongRef.

    A file is looked up by its manifest `source` name first; when absent, `separate(path) -> stems_dir` is called
    (song_match passes scripts/separate.py); without it SongNotFound is raised."""
    if is_song_file(ref):
        p = Path(ref)
        sid = song_id or slug(p.stem)
        for s in stem_sets(stems_root):
            if s["name"] == p.name:
                return SongRef(sid, s["dir"], s["source"], s["hash"])
        if separate is None:
            raise SongNotFound(f"{ref} has not been separated yet")
        d = Path(separate(p))
        mf = d / "manifest.json"
        info = json.loads(mf.read_text()) if mf.exists() else {}
        return SongRef(sid, d, str(info.get("source", p)), str(info.get("song_hash", d.parent.name)))
    s = find_stems(ref, stems_root)
    sid = song_id or (ref if not re.fullmatch(r"[0-9a-f]{6,}", ref) else slug(Path(s["name"]).stem))
    return SongRef(slug(sid), s["dir"], s["source"], s["hash"])


# ------------------------------------------------------------------------------------------ folders

def _variant(v: str | None) -> str:
    return v or ""


def rebuilt_dir(song_id: str, songs_root: Path | None = None) -> Path:
    return (Path(songs_root) if songs_root else SONGS_ROOT) / f"{song_id}-rebuilt"


def transcription_mid(song_id: str, songs_root: Path | None = None) -> Path:
    return rebuilt_dir(song_id, songs_root) / "transcription.mid"


def profiled_dir(song_id: str, guitar_id: str, songs_root: Path | None = None) -> Path:
    return rebuilt_dir(song_id, songs_root) / f"profiled-{guitar_id}"


def plain_dir(song_id: str, songs_root: Path | None = None) -> Path:
    """Where the unprofiled rebuild goes (library bank notes only)."""
    return rebuilt_dir(song_id, songs_root) / "plain"


def playlevel_dir(song_id: str, variant: str | None = "", songs_root: Path | None = None) -> Path:
    return rebuilt_dir(song_id, songs_root) / f"playlevel{_variant(variant)}"


def match_dir(song_id: str, variant: str | None = "", matches_root: Path | None = None) -> Path:
    return (Path(matches_root) if matches_root else MATCHES_ROOT) / f"{song_id}-dryrebuild{_variant(variant)}"


def finetune_dir(song_id: str, variant: str | None = "", matches_root: Path | None = None) -> Path:
    return (Path(matches_root) if matches_root else MATCHES_ROOT) / f"{song_id}-finetune{_variant(variant)}"


# ------------------------------------------------------------------------------------------ windows

def parse_window(text: str) -> tuple[float, float]:
    """'START:END' in song seconds."""
    try:
        a, b = (float(x) for x in str(text).split(":"))
    except ValueError as e:
        raise ValueError(f"window {text!r}: expected START:END in seconds") from e
    if not (0 <= a < b):
        raise ValueError(f"window {text!r}: need 0 <= START < END")
    return a, b


def pick_window(stems_dir: str | Path, dur: float = 10.0) -> tuple[float, float]:
    """The T200 rule (scripts/t200_windows.py): a loud, distorted, high-confidence guitar window of `dur` seconds."""
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    sys.path.insert(0, str(ROOT / "src"))
    import t200_windows as TW

    best = TW.pick(str(stems_dir), dur)["best"]
    return round(best["start"], 2), round(best["start"] + dur, 2)
