#!/usr/bin/env python3
"""End-to-end match of ONE song section on Helix Native (T041). LOCAL ONLY: no song audio leaves the machine.

    TONEMATCH_NATIVE_PYTHON=cache/native/hostvenv/bin/python \\
      .venv/bin/python scripts/match_song.py --stems-dir cache/stems/<hash>/htdemucs_6s \\
      --song DS-Sample --out cache/matches/DS-Sample --wall-min 25

Stages (each a function, separable and unit-tested with a fake engine):
  1. load stems, sections and the confidence mask; reduce the time-frequency mask to a per-sample
     confidence (guitar-power-weighted); pick the most guitar-dominant ~10 s of one snapshot;
  2. pick DI clips (tonematch.dry.pick_di) by the target's register, style and density;
  3. candidates = Native chains that already have templates (templates_files/{K,A,B0,B,C,H},
     templates/ A30), free parameters = mapped, catalog-[0,1], not power-supply minutiae;
  4. short beam over chains (CMA-ES, few generations each), then CMA-ES refine on the best;
  5. final check of the top 3 on the full excerpt with stems and mask, DI-sensitivity check;
  6. write the best preset as .hlx (fw 3.50, HX Stomp), validate it, build the listening pack and report.md.

Every loss number is the PROVISIONAL, UNADMITTED loss (equal weights on the three no-DI terms).
Outputs go under a gitignored directory (cache/matches/...). The Stomp is never touched.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import dataclasses
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import soundfile as sf
from scipy.signal import stft

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from tonematch import param_policy

SR = 48_000
N_FFT, HOP = 2048, 512                      # the separator's mask grid (tonematch.separation.confidence)
LABEL = "unadmitted loss"
DI_PEAK_DBFS = -12.5                        # same DI level convention as scripts/parity_null.py
# Power-supply and hum minutiae: not worth search dimensions, and they invite odd settings.
# "Time" is NOT here any more (N1, 2026-10-16): delay Time is searched on the template's 10 ms grid.
# "Level" (a block's output level, dB) stays fixed: loudness is calibrated by the level guard and by
# the amp/channel volume (ChVol, which stays free), and a free block Level only trades against them
# without changing the tone. Offset and Spread stay fixed as before (minutiae).
FIXED_PARAMS = {"Hum", "Ripple", "Bias", "BiasX", "Offset", "Spread", "Level"}
# Parity findings: parameters whose Native 3.82 render does not match the Stomp fw 3.50, frozen at the
# given value in Stomp-verified mode (default). Add future findings here: (category, param) -> value.
# Read from the per-engine policy (src/tonematch/param_policy.json); first entry: cab EarlyReflections 0.0
# (benchmark 2026-10-15: ER 0 nulls -31.4 dB, 0.783 -3.6 dB).
STOMP_FROZEN = param_policy.frozen_for("stomp")
NATIVE_FROZEN = param_policy.frozen_for("native")   # Hum/Ripple at 0 (human 2026-10-16)
TEMPLATE_SETS = {                           # name -> (hlx, templates dir) relative to the repo root
    "K": ("cache/stomp/inject/K.hlx", "cache/native/templates_files/K"),
    "A": ("cache/stomp/inject/A.hlx", "cache/native/templates_files/A"),
    "B0": ("cache/stomp/inject/B0.hlx", "cache/native/templates_files/B0"),
    "B": ("cache/stomp/inject/B.hlx", "cache/native/templates_files/B"),
    "A30": ("cache/native/drop/4d54671b96a6_model_a30.hlx", "cache/native/templates"),
    "C": ("cache/stomp/inject/C.hlx", "cache/native/templates_files/C"),
    "H": ("cache/stomp/inject/H.hlx", "cache/native/templates_files/H"),
}


# ----------------------------------------------------------------------------------------- logging


class Log:
    def __init__(self, path: Path | None = None):
        self.t0 = time.perf_counter()
        self.path = path
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")

    def __call__(self, msg: str) -> None:
        line = f"[{time.perf_counter() - self.t0:7.1f}s] {msg}"
        print(line, flush=True)
        if self.path:
            with self.path.open("a") as f:
                f.write(line + "\n")


# ----------------------------------------------------------------------------------- stems and mask


def _mono(path: Path) -> np.ndarray:
    x, sr = sf.read(str(path), dtype="float32", always_2d=True)
    if sr != SR:
        raise ValueError(f"{path}: expected {SR} Hz, got {sr}")
    return np.ascontiguousarray(x.mean(axis=1), dtype=np.float32)


@dataclass
class Song:
    guitar: np.ndarray
    others: np.ndarray
    mask_tf: np.ndarray          # [freq, time] separator confidence
    sections: dict
    manifest: dict


def load_song(stems_dir: str | Path) -> Song:
    d = Path(stems_dir)
    return Song(_mono(d / "guitar.wav"), _mono(d / "others.wav"),
                np.load(d / "confidence.npy").astype(np.float32),
                json.loads((d / "sections.json").read_text()),
                json.loads((d / "manifest.json").read_text()) if (d / "manifest.json").exists() else {})


def frame_confidence(mask_tf: np.ndarray, guitar: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-STFT-frame confidence (mask averaged over frequency, weighted by the guitar stem's power in
    each bin) and per-frame guitar power. The mask is [freq, time] on the grid (n_fft 2048, hop 512)."""
    g = stft(guitar, fs=SR, nperseg=N_FFT, noverlap=N_FFT - HOP)[2]
    p = (np.abs(g) ** 2).astype(np.float32)
    t = min(p.shape[1], mask_tf.shape[1])
    p, m = p[:, :t], mask_tf[:, :t]
    fp = p.sum(axis=0)
    return ((m * p).sum(axis=0) / (fp + 1e-20)).astype(np.float32), fp


def sample_confidence(fc: np.ndarray, n_samples: int) -> np.ndarray:
    """Per-sample confidence in [0, 1] (what the loss kit takes as `mask`), frame k centred at k*HOP."""
    return np.clip(np.interp(np.arange(n_samples), np.arange(len(fc)) * HOP, fc), 0.0, 1.0).astype(
        np.float32)


def snapshot_span(sections: dict, snapshot: int, total_s: float) -> tuple[float, float]:
    bars, snaps = sections["bar_starts"], sections["bar_snapshot"]
    idx = [i for i, s in enumerate(snaps) if s == snapshot]
    if not idx:
        raise ValueError(f"snapshot {snapshot} has no bars")
    t0 = bars[idx[0]]
    t1 = bars[idx[-1] + 1] if idx[-1] + 1 < len(bars) else total_s
    return float(t0), float(t1)


def _union_overlap(a: float, b: float, spans) -> float:
    """Seconds of [a, b] covered by the union of `spans` (exposed windows overlap each other)."""
    cov = 0.0
    cur = a
    for s, e in sorted((max(s, a), min(e, b)) for s, e in spans if min(e, b) > max(s, a)):
        if e > cur:
            cov += e - max(s, cur)
            cur = e
    return cov


def pick_window(fc: np.ndarray, fp: np.ndarray, sections: dict, snapshot: int, dur_s: float,
                total_s: float, step_s: float = 0.5, min_active: float = 0.6,
                tol: float = 0.01) -> dict:
    """The `dur_s` window inside the snapshot with the highest guitar-power-weighted mean confidence
    among windows whose frames are mostly active. Windows within `tol` of the best confidence are
    treated as ties; the tie goes to the one most covered by the separation stage's exposed-guitar
    windows, then to one that starts on a bar line."""
    t0, t1 = snapshot_span(sections, snapshot, total_s)
    t1 = min(t1, total_s)
    dur_s = min(dur_s, t1 - t0)
    active = fp > np.percentile(fp, 95) * 10 ** (-35 / 10)
    exposed = [tuple(w) for w in sections.get("exposed_windows", [])]
    bars = [b for b in sections["bar_starts"] if t0 - 1e-6 <= b <= t1 - dur_s + 1e-6]

    def stats(a: float) -> tuple[float, float]:
        i, j = int(a * SR / HOP), int((a + dur_s) * SR / HOP)
        w = fp[i:j]
        return float((fc[i:j] * w).sum() / (w.sum() + 1e-20)), float(active[i:j].mean())

    starts = sorted({*np.arange(t0, t1 - dur_s + 1e-9, step_s).tolist(), *bars})
    scored = [(a, *stats(a)) for a in starts]
    ok = [s for s in scored if s[2] >= min_active] or scored
    top = max(s[1] for s in ok)
    pool = [s for s in ok if s[1] >= top - tol]
    start, conf, act = max(pool, key=lambda s: (round(_union_overlap(s[0], s[0] + dur_s, exposed), 1),
                                                any(abs(s[0] - b) < 1e-6 for b in bars), s[1]))
    return {"snapshot": snapshot, "start_s": float(start), "end_s": float(start + dur_s),
            "dur_s": float(dur_s), "mean_confidence": conf, "active_fraction": act,
            "best_confidence_any_window": top, "span_s": [t0, t1],
            "exposed_overlap_s": _union_overlap(start, start + dur_s, exposed)}


def manual_window(fc, fp, sections, start_s: float, end_s: float, total_s: float) -> dict:
    """A user-chosen window (song seconds) in the same dict form as pick_window."""
    if not 0 <= start_s < end_s <= total_s:
        raise ValueError(f"--target {start_s}:{end_s} outside the song (0..{total_s:.1f} s)")
    i, j = int(start_s * SR / HOP), int(end_s * SR / HOP)
    w = fp[i:j]
    active = fp > np.percentile(fp, 95) * 10 ** (-35 / 10)
    exposed = [tuple(x) for x in sections.get("exposed_windows", [])]
    bars = sections["bar_starts"]
    k = max((n for n, b in enumerate(bars) if b <= start_s), default=0)
    return {"snapshot": sections["bar_snapshot"][min(k, len(sections["bar_snapshot"]) - 1)],
            "start_s": float(start_s), "end_s": float(end_s), "dur_s": float(end_s - start_s),
            "mean_confidence": float((fc[i:j] * w).sum() / (w.sum() + 1e-20)),
            "active_fraction": float(active[i:j].mean()), "best_confidence_any_window": float("nan"),
            "span_s": [float(start_s), float(end_s)], "manual": True,
            "exposed_overlap_s": _union_overlap(start_s, end_s, exposed)}


def parse_target(text: str) -> tuple[float, float]:
    a, _, b = text.partition(":")
    return float(a), float(b)


def user_di_clip(wav: str | Path, take_json: str | Path | None, win: dict):
    """The user's DI cut to the target window. Returns (DIClip-like record, audio at true level).
    take_sample0_song_s places sample 0 of the take in song time; with no JSON the file is taken to
    start at the window start. The user's timing is loose, so no alignment is attempted."""
    x = _mono(Path(wav))
    if take_json:
        t0 = float(json.loads(Path(take_json).read_text())["alignment"]["take_sample0_song_s"])
    else:
        t0 = win["start_s"]
    a = round((win["start_s"] - t0) * SR)
    n = round(win["dur_s"] * SR)
    if a < 0 or a + n > len(x):
        raise ValueError(f"user DI covers song {t0:.1f}-{t0 + len(x) / SR:.1f} s; target "
                         f"{win['start_s']:.1f}-{win['end_s']:.1f} s is outside it")
    from tonematch import dry as _dry

    seg = np.ascontiguousarray(x[a:a + n])
    d = _dry.analyse(seg)
    clip = _dry.DIClip(path=str(wav), start_s=a / SR, dur_s=n / SR, pitch_lo=d["pitch_lo"],
                       pitch_hi=d["pitch_hi"], pitch_med=d["pitch_med"], density=d["density"],
                       style=d["style"], clarity=d["clarity"], rms_dbfs=d["rms_dbfs"],
                       source="user take", licence="user's own recording")
    return clip, seg


def build_section(song: Song, win: dict, fc: np.ndarray, name: str, dry: np.ndarray,
                  song_id: str):
    from tonematch.search import Section

    a, b = round(win["start_s"] * SR), round(win["end_s"] * SR)
    n = min(b - a, len(dry))
    mask = sample_confidence(fc, len(song.guitar))[a:a + n]
    return Section(song_id, name, song.guitar[a:a + n].copy(), dry[:n].copy(),
                   stems={"others": song.others[a:a + n].copy()}, mask=mask)


# ------------------------------------------------------------------------------ Stomp speed (T061)

STOMP_READBACK_EVERY = 10     # full readback on every 10th parameter-only load (legacy: every load)


def apply_stomp_speed(engine, legacy: bool = False) -> dict:
    """Set the T061 speed options on a Stomp engine (or the legacy behaviour). An option the user
    already set through its environment variable is left alone. Returns what is in force."""
    inner = getattr(engine, "inner", engine)
    if "TONEMATCH_STOMP_READBACK_EVERY" not in os.environ:
        inner.readback_every = 1 if legacy else STOMP_READBACK_EVERY
    if "TONEMATCH_STOMP_TAIL" not in os.environ:
        inner.tail_mode = "bound" if legacy else "measured"
    return {"readback_every": getattr(inner, "readback_every", None),
            "tail_mode": getattr(inner, "tail_mode", None)}


def search_view(section, seconds: float):
    """The section cut to its loudest `seconds` of DI (reamp.excerpt: the same window for every
    engine), target, stems and mask cut at the same samples. Unchanged when already that short."""
    from tonematch import reamp
    from tonematch.search import Section

    if seconds <= 0 or len(section.dry) <= round(seconds * SR):
        return section
    x, i = reamp.excerpt(section.dry, seconds, SR)
    j = i + len(x)

    def cut(v):
        return None if v is None else v[i:j].copy()

    stems = None if section.stems is None else {k: cut(v) for k, v in section.stems.items()}
    return Section(section.song, section.name, cut(section.target), x, stems=stems, mask=cut(section.mask))


# Clip policies (T064): what the search renders. The final check always uses the full excerpt.
CLIP_POLICIES = ("full", "loudest3", "loudest5", "beam3-refine-full", "spread")
STOMP_DEFAULT_POLICY = "full"       # chosen by the T064 study on Native, docs/notes/stomp-speed.md
SPREAD_N, SPREAD_CLIP_S = 3, 1.5    # "spread": this many clips of this length, one per equal part


def spread_view(section, n: int = SPREAD_N, seconds: float = SPREAD_CLIP_S):
    """`n` clips of `seconds` each, the loudest one inside each of `n` equal parts of the section,
    joined in time order (target, stems and mask cut at the same samples). Unchanged when the
    section is too short to hold them."""
    from tonematch import reamp
    from tonematch.search import Section

    L, m = len(section.dry), round(seconds * SR)
    if n <= 1 or L < n * m:
        return section
    idx = []
    for k in range(n):
        a, b = k * L // n, (k + 1) * L // n
        _, i = reamp.excerpt(section.dry[a:b], seconds, SR)
        idx.append(a + i)

    def cut(v):
        return None if v is None else np.concatenate([v[i:i + m] for i in idx]).astype(v.dtype)

    stems = None if section.stems is None else {k: cut(v) for k, v in section.stems.items()}
    return Section(section.song, section.name, cut(section.target), cut(section.dry), stems=stems,
                   mask=cut(section.mask))


def clip_policy_views(section, policy: str):
    """(search section, refine section or None). `refine` is only set by beam3-refine-full: the beam
    runs on the loudest 3 s, the refine on the whole excerpt."""
    if policy == "full":
        return section, None
    if policy == "loudest3":
        return search_view(section, 3.0), None
    if policy == "loudest5":
        return search_view(section, 5.0), None
    if policy == "beam3-refine-full":
        return search_view(section, 3.0), section
    if policy == "spread":
        return spread_view(section), None
    raise ValueError(f"clip policy must be one of {CLIP_POLICIES}, got {policy!r}")


# ------------------------------------------------------------------------------------- DI picking


def peak_normalise(x: np.ndarray, dbfs: float = DI_PEAK_DBFS) -> np.ndarray:
    return (x * (10 ** (dbfs / 20) / max(float(np.max(np.abs(x))), 1e-9))).astype(np.float32)


def load_di(clip, dur_s: float) -> np.ndarray:
    """`dur_s` of the clip's source file from the clip's start (shifted earlier if the file ends
    sooner), peak-normalised. Indexed clips are 4 s; the section needs ~10 s of continuous playing."""
    p = Path(clip.path) if Path(clip.path).is_absolute() else Path(clip.root) / clip.path
    total = sf.info(str(p)).duration if p.exists() else clip.start_s + clip.dur_s
    start = min(clip.start_s, max(0.0, total - dur_s))
    x = dataclasses.replace(clip, start_s=start, dur_s=dur_s).load()
    n = int(dur_s * SR)
    if len(x) < n:                                   # a file shorter than the section: repeat it
        x = np.tile(x, int(np.ceil(n / max(len(x), 1))))[:n]
    return peak_normalise(x[:n])


def choose_di(target: np.ndarray, dur_s: float, n: int = 3, library=None) -> tuple[list, dict]:
    """pick_di by the target's own register, style and onset density. Returns ([(clip, audio)], desc)."""
    from tonematch import dry

    desc = dry.analyse(target)
    clips = dry.pick_di((desc["pitch_lo"], desc["pitch_hi"]), desc["style"], density=desc["density"],
                        n=n, max_dur_s=4.0, library=library)
    return [(c, load_di(c, dur_s)) for c in clips], desc


# ----------------------------------------------------------------------------------- candidates


@dataclass
class Cand:
    name: str
    preset: object
    template: object = None          # ChainTemplate (Native) or None (fake engines)
    free: list | None = None         # ParamRefs the search may move; None = all numeric params
    note: str = ""


def param_range(template, catalog_param, key: str) -> tuple[float, float]:
    """(lo, hi) a free parameter moves over: the template's CC range (what set_params scales by),
    else the catalog's min/max, else [0, 1]."""
    r = (getattr(template, "ranges", None) or {}).get(key)
    if r is not None:
        return float(r[0]), float(r[1])
    if catalog_param and catalog_param.get("min") is not None and catalog_param.get("max") is not None:
        return float(catalog_param["min"]), float(catalog_param["max"])
    return 0.0, 1.0


def derive_free(preset, template, catalog, frozen=None) -> list:
    """Free parameters: numeric and continuous, mapped to an automation control (and CC-verified when
    the template says which are), not in FIXED_PARAMS or the frozen policy, not on a bypassed delay /
    reverb / mod block (the template's bypass state cannot be changed through CC), and whose value
    lies inside its range. The range is the template's own (`param_range`), so delay Time (0-1.27 s),
    LFO rates, reverb Decay and Hz parameters are free; make_space and set_params scale by the same
    range. Without a template (fake engines) the catalog range, or [0, 1], applies."""
    out = []
    models = (catalog or {}).get("models", {})
    for i, s in enumerate(preset.slots):
        if s.model_id is None or (s.category in ("delay", "reverb", "mod") and not s.enabled):
            continue
        for name, v in (s.params or {}).items():
            if name in FIXED_PARAMS or (s.category, name) in (frozen or {}) or isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            key = f"{i}:{name}"
            if template is not None and key not in template.map:
                continue
            ver = getattr(template, "verified", None)
            if ver and key not in ver:   # CC not verified to move the render: baked into the state
                continue
            pd = models.get(s.model_id, {}).get("params", {}).get(name) if catalog else None
            if pd is not None and pd.get("kind", "continuous") != "continuous":
                continue                 # switches and enums are not a continuous search dimension
            lo, hi = param_range(template, pd, key)
            if hi <= lo or not lo - 1e-9 <= v <= hi + 1e-9:
                continue
            out.append((i, name))
    return out


def pick_template(variants: list, preset):
    """The fingerprinted template whose baked values equal the preset's (native-engine's stale-template
    rule); None when there is none. Falls back to an unfingerprinted one only for engines without the rule."""
    try:
        from tonematch.engines.native import baked_fingerprint
    except ImportError:
        return variants[0] if variants else None
    for t in variants:
        if getattr(t, "baked_hash", "") and baked_fingerprint(preset, t.map, t.verified)[1] == t.baked_hash:
            return t
    return None


def freeze(preset, frozen):
    """Write the policy's frozen values into ``preset`` (in place; returned for chaining)."""
    for sl in preset.slots:
        for (cat, pn), val in (frozen or {}).items():
            if sl.category == cat and pn in (sl.params or {}):
                sl.params[pn] = val
    return preset


def build_candidates(root: Path, catalog, only: list[str] | None = None, frozen=None,
                     allow_time_variant: bool = False, time_variant: set | None = None) -> tuple[list[Cand], list[str]]:
    """Candidates for every template set that exists on disk. Returns (cands, notes on what is missing).
    A chain holding a time-variant library block (renders differently on every pass) is left out unless
    `allow_time_variant`; when kept, its note says the null-based loss is unreliable (N4, 2026-10-16)."""
    from tonematch.catalog import native_unavailable
    from tonematch.engines.native import ChainTemplate, chain_key
    from tonematch.preset import read_hlx
    from tonematch.search.pool import chain_time_variant, time_variant_models, warn_if_time_variant

    bad = native_unavailable()   # every candidate is searched on Native (lead, 2026-10-16)
    cands, notes = [], []
    for name, (hlx, tdir) in TEMPLATE_SETS.items():
        if only and name not in only:
            continue
        f, d = root / hlx, root / tdir
        if not f.is_file() or not d.is_dir():
            notes.append(f"{name}: no template ({d.relative_to(root)} or {hlx} missing), not searched")
            continue
        preset = freeze(read_hlx(f, catalog), frozen)   # before picking: a template baked at another value is stale
        refused = sorted({s.model_id for s in preset.slots if s.model_id in bad})
        if refused:
            notes.append(f"{name}: {refused} not available on Helix Native (refused models), not searched")
            continue
        tv = time_variant_models() if time_variant is None else set(time_variant)
        tvm = chain_time_variant(preset, tv)
        if tvm and not allow_time_variant:
            notes.append(f"{name}: time-variant block(s) {tvm} excluded (--allow-time-variant keeps them), not searched")
            continue
        tpl = pick_template(ChainTemplate.load_variants(d).get(chain_key(preset), []), preset)
        if tpl is None:
            notes.append(f"{name}: {tdir} holds no template for this chain at its frozen values, not searched")
            continue
        note = f"{hlx} + {tdir}"
        warn_if_time_variant(preset, tv, f"chain {name}", emit=lambda m: None)
        if tvm:
            note += f"; time-variant {tvm}: the null-based loss is unreliable for this chain"
        cands.append(Cand(name, preset, tpl, derive_free(preset, tpl, catalog, frozen), note))
    return cands, notes


# ------------------------------------------------------------------------------- engine plumbing


class TimedEngine:
    """Delegates to an engine, counts renders and render seconds, and survives a dead plugin host.

    The Native host child process can be killed from outside (seen once, code -9, with the machine at
    load 20+ and swap nearly full). On such an error the host is restarted, the last preset is
    loaded again with the parameters set since, and the call is retried once. Without this a dead host
    turns every remaining score into a failure and burns the budget in seconds.
    """

    def __init__(self, inner, log=print, rss_limit_mb: float | None = None, check_every: int = 8):
        self.inner, self.renders, self.render_s, self.restarts, self.log = inner, 0, 0.0, 0, log
        self.proactive = 0
        self.rss_limit_mb, self.check_every = rss_limit_mb, check_every
        self._preset = None
        self._applied: dict = {}

    def host_rss_mb(self) -> float | None:
        """Resident size of the plugin-host child process in MB (None when there is none)."""
        proc = getattr(getattr(self.inner, "host", None), "proc", None)
        if proc is None or proc.poll() is not None:
            return None
        r = subprocess.run(["ps", "-o", "rss=", "-p", str(proc.pid)], check=False,
                           capture_output=True, text=True)
        return float(r.stdout.strip()) / 1024 if r.stdout.strip() else None

    def _maybe_restart_for_memory(self) -> None:
        """Native host memory grows with use (5.9 GB fresh, 8 GB after the first chain loads) and the host
        was killed (code -9) at roughly 290 renders, with other jobs filling the 32 GB machine. A fresh host
        already sits at 5.5 to 6 GB, so the limit must be well above that."""
        if not self.rss_limit_mb or self.renders % self.check_every:
            return
        rss = self.host_rss_mb()
        if rss is not None and rss > self.rss_limit_mb:
            self.proactive += 1
            self.log(f"  host resident size {rss:.0f} MB > {self.rss_limit_mb:.0f} MB: restarting it")
            self._recover(quiet=True)

    @staticmethod
    def _dead(e: Exception) -> bool:
        m = str(e)
        return "host process exited" in m or "host died" in m

    def _recover(self, quiet: bool = False) -> None:
        inner = self.inner
        host = getattr(inner, "host", None)
        if host is not None:
            host.restart()
            with contextlib.suppress(Exception):  # bookkeeping only
                from tonematch.engines.native import _hc_mode

                st = getattr(inner, "_initial_state", b"")
                inner._hc_mode = _hc_mode(st) if st else None
            inner.current_chain = inner.current_template = None
        self.restarts += 1
        if not quiet:
            self.log(f"  engine host died; restarted it (restart {self.restarts}) and reloaded the chain")
        if self._preset is not None:
            inner.load(self._preset)
            if self._applied:
                inner.set_params(dict(self._applied))

    def _guard(self, fn, *a):
        from tonematch.engines.base import EngineError

        try:
            return fn(*a)
        except EngineError as e:
            if not self._dead(e):
                raise
            self._recover()
            return fn(*a)

    def load(self, preset):
        self._preset, self._applied = preset, {}
        return self._guard(self.inner.load, preset)

    def set_params(self, changes):
        out = self._guard(self.inner.set_params, changes)
        self._applied.update(changes)
        return out

    def render(self, dry):
        self._maybe_restart_for_memory()
        t = time.perf_counter()
        try:
            return self._guard(self.inner.render, dry)
        finally:
            self.renders += 1
            self.render_s += time.perf_counter() - t

    def __getattr__(self, k):
        return getattr(self.inner, k)


def load_candidate(engine, cand: Cand, preset=None) -> None:
    """Load a candidate's chain, forcing the right template (K/A and B0/B share a chain key)."""
    inner = getattr(engine, "inner", engine)
    if cand.template is not None and hasattr(inner, "templates"):
        inner.templates[cand.template.chain] = cand.template
        if hasattr(inner, "_variants"):
            inner._variants[cand.template.chain] = [cand.template]
        inner.current_chain = None
        inner.current_template = None
    engine.load(preset if preset is not None else cand.preset)


class MaskedKit:
    """LossKit that always applies the section's confidence mask (search passes it only at the final check)."""

    def __init__(self, kit, mask):
        self.kit, self.mask = kit, mask

    def score(self, target, candidate, *, stems=None, mask=None, weights=None):
        m = mask
        if m is None and self.mask is not None and len(self.mask) >= len(target):
            m = self.mask[:len(target)]
        return self.kit.score(target, candidate, stems=stems, mask=m, weights=weights)


# Known-DI level guard (lead Ruling 3, 2026-10-16). mrstft_v2: truth ~0.001, near miss ~0.08, random ~0.7,
# so 1 dB beyond the tolerance costs about one near miss.
KNOWN_DI_LEVEL_TOL_DB = 0.25
KNOWN_DI_LEVEL_SLOPE = 0.08


class LevelGuardKit:
    """Adds a loss penalty when a candidate's loudness is far from a reference level.

    The three loss terms are level-invariant by design, so nothing stops the search from turning the
    output down 50 dB when that happens to lower the numbers (it did, in the first full run: a
    channel volume of 0.004). `ref_db` is the median start-render level over all candidate chains
    (a typical patch level for this DI); `tol_db` is free, beyond it the penalty is `slope` loss units
    per dB. `ref_db` None means no penalty (before the reference is measured)."""

    def __init__(self, inner, tol_db: float | None = None, slope: float | None = None, known_di: bool = False):
        self.known_di = known_di
        self.tol_db = tol_db if tol_db is not None else (KNOWN_DI_LEVEL_TOL_DB if known_di else 6.0)
        self.slope = slope if slope is not None else (KNOWN_DI_LEVEL_SLOPE if known_di else 0.5)
        self.inner, self.ref_db = inner, None

    @staticmethod
    def level_db(x) -> float:
        return float(20 * np.log10(rms(x)))

    @staticmethod
    def active_level_db(x, frame: int = 2048, span_db: float = 40.0) -> float:
        """RMS level (dBFS) over active frames: frames within `span_db` of the loudest frame."""
        x = np.asarray(x, np.float64).ravel()
        n = len(x) // frame
        if n < 2:
            return float(20 * np.log10(max(float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0, 1e-12)))
        p = (x[: n * frame].reshape(n, frame) ** 2).mean(axis=1)
        act = p >= p.max() * 10 ** (-span_db / 10)
        return float(10 * np.log10(max(float(p[act].mean()), 1e-24)))

    def score(self, target, candidate, **kw):
        sc = self.inner.score(target, candidate, **kw)
        if self.known_di:                       # Ruling 3: the reference is the target's own level
            ref, lvl = self.active_level_db(target), self.active_level_db(candidate)
        else:
            ref, lvl = self.ref_db, None
        if ref is not None:
            d = (lvl if lvl is not None else self.level_db(candidate)) - ref
            pen = self.slope * max(0.0, abs(d) - self.tol_db)
            sc.total = float(sc.total) + pen
            sc.terms = {**sc.terms, "level_db_vs_ref": d, "level_penalty": pen}
        return sc


def is_known_di_mode(weights=None, admitted: bool = False, flag: bool = False) -> bool:
    """Known-DI mode: explicit flag, admitted weights, or the weights are exactly KNOWN_DI_WEIGHTS."""
    from tonematch.metrics import KNOWN_DI_WEIGHTS
    return bool(flag or admitted or (weights is not None and dict(weights) == dict(KNOWN_DI_WEIGHTS)))


def rebase_level_reference(engine, kit, results, section, log=print) -> tuple[float, float]:
    """The level guard's reference is the median start-render level on the clip the search used. When
    that is a shorter, louder clip than the final-check excerpt (Stomp default, T062), the reference
    is too high for the final check and a good chain is pushed toward the penalty. Re-measure it as
    the same median on the final excerpt. Returns (old, new)."""
    old = kit.ref_db
    levels = []
    for r in results:
        if r.skipped:
            continue
        load_candidate(engine, r.cand, apply_values(r.cand.preset, r.space, r.start_values))
        levels.append(LevelGuardKit.level_db(engine.render(section.dry)))
    if levels:
        kit.ref_db = float(np.median(levels))
    log(f"level reference re-measured on the final excerpt: {old:.1f} -> {kit.ref_db:.1f} dBFS rms")
    return old, kit.ref_db


def make_prior(space, weight: float, mean: float = 0.5, std: float = 0.3):
    """Weak neutral Gaussian prior (MAP) over the free parameters: mean 0.5, std 0.3 of each parameter's
    own range (so 0.5 and 0.3 for a 0-1 knob), loss + weight * mean z^2.
    NOT fit on factory or community presets (none is available locally); it only keeps the search
    off the corners of the knob range unless the loss clearly wants them."""
    from tonematch.search import Prior

    if weight <= 0:
        return None
    w = {r: float(space.hi[j] - space.lo[j]) for j, r in enumerate(space.refs)}
    return Prior({r: float(space.lo[j]) + mean * w[r] for j, r in enumerate(space.refs)},
                 {r: std * w[r] for r in space.refs}, weight)


def apply_values(preset, space, values, ndigits: int | None = None):
    p = copy.deepcopy(preset)
    for (i, k), v in zip(space.refs, values, strict=True):
        p.slots[i].params[k] = float(v) if ndigits is None else round(float(v), ndigits)
    return p


# ------------------------------------------------------------------------------------------ search


@dataclass
class ChainResult:
    cand: Cand
    space: object
    start_values: np.ndarray
    values: np.ndarray
    start_loss: float = float("nan")
    loss: float = float("inf")
    terms: dict = field(default_factory=dict)
    phases: list[dict] = field(default_factory=list)
    renders: int = 0
    wall_s: float = 0.0
    skipped: str = ""


def make_plan(spr: float, wall_s: float, cap: int):
    from tonematch.search import Plan

    return Plan("native", cap, 10, False, 1.0, 1, spr, 0.0, False, est_wall_s=cap * spr,
                notes=[f"wall-clock budget {wall_s:.0f} s; serial renders at {spr:.2f} s each (measured)"])


CC_STEPS = 128   # a CC-driven Native parameter has 128 values across its assignment's Min..Max


def make_space(cand: Cand, steps: int = CC_STEPS):
    """Free-parameter space on Native's CC grid: values k*(hi-lo)/(steps-1) across the template's
    Min..Max (default 0..1) for k = 0..steps-1. Step 0 (the exact minimum) is included: native-engine's memory fix
    re-sends every parameter, so returning to 0.0 now matches setting 0.0 (ledger 2026-10-15T19:01:31).
    The start is snapped."""
    from tonematch.search import ParamSpace

    refs = cand.free if cand.free is not None else ParamSpace.from_preset(cand.preset).refs
    rng = getattr(cand.template, "ranges", None) or {}
    bounds = {}
    for r in refs:
        lo, hi = rng.get(f"{r[0]}:{r[1]}", [0.0, 1.0])
        bounds[r] = (lo, hi)
    space = ParamSpace.from_preset(cand.preset, refs, bounds, steps)
    space.start = space.from_unit(space.to_unit(space.start))
    return space


def run_phase(engine, kit, section, budget, plan, cr: ChainResult, phase: str, cap: float,
              sigma: float, seed: int, deadline: float, log, prior_weight: float = 0.0) -> None:
    from tonematch.search import Evaluator
    from tonematch.search.loop import refine_chain

    t = time.perf_counter()
    try:
        load_candidate(engine, cr.cand, apply_values(cr.cand.preset, cr.space, cr.values))
    except Exception as e:  # noqa: BLE001 - a chain that will not load is skipped and reported
        cr.skipped = f"{phase}: {type(e).__name__}: {e}"
        log(f"  {cr.cand.name}: SKIPPED ({cr.skipped})")
        return
    t_load = time.perf_counter() - t
    ev = Evaluator(engine, kit, section, cr.space, plan, budget, make_prior(cr.space, prior_weight), deadline)
    v, f, terms, stages = refine_chain(ev, cr.values, cap, sigma, seed)
    start = stages[0]["score_in"] if stages else f
    if phase == "beam":
        cr.start_loss = start
    cr.values, cr.loss, cr.terms = v, f, terms
    cr.renders += ev.calls
    cr.wall_s += time.perf_counter() - t
    cr.phases.append({"phase": phase, "loss_in": start, "loss_out": f, "renders": ev.calls,
                      "load_s": round(t_load, 1), "sigma": sigma, "stages": stages,
                      "wall_s": round(time.perf_counter() - t, 1)})
    rss = getattr(engine, "host_rss_mb", lambda: None)()
    log(f"  {cr.cand.name} {phase}: loss {start:.4f} -> {f:.4f} in {ev.calls} renders "
        f"({time.perf_counter() - t:.0f} s, load {t_load:.1f} s"
        + (f", host {rss / 1024:.1f} GB" if rss else "") + ")")


def model_choice_advice(eng, kit, section, cats: list[str], seed: int, log=print, budget: int = 300,
                        k: int = 6, out: Path | None = None) -> dict:
    """Advisory (T050, `--model-choice`): rank every eligible Native library model of each category on
    this section with `search.model_choice.choose_models` (fingerprint screen, top k, successive halving).
    The chain search is not changed: the chains are whole 8-slot templates, so this is a hint about
    which model suits a slot, written to the log and to model_choice.json."""
    import synthetic_v1 as SV

    from tonematch.catalog import load_catalog, native_unavailable
    from tonematch.engines import native_library as NL
    from tonematch.search import Budget
    from tonematch.search.model_choice import choose_models

    catalog = load_catalog()
    idx = NL.load_index()["models"]
    bad = set(native_unavailable()) | {m for m, e in idx.items() if e.get("time_variant")}
    advice = {}
    for c in cats:
        blocks = {}
        for m in sorted(m for m, e in idx.items() if e["category"] == c and SV.eligible(m, c, bad)
                        and not NL.is_routing(m)):
            tpl = NL.template_for(m)
            fp = SV.free_params(m, tpl, catalog, c) if tpl is not None else []
            if fp:
                blocks[m] = SV.Block(m, c, fp, catalog)
        if not blocks:
            continue
        ch = choose_models(section, eng, kit, SV.model_candidates(blocks),
                           Budget(max_renders=budget, clip_s=len(section.target) / SR, seed=seed), k=k)
        advice[c] = [{"model": s.model, "screen": s.screen, "refined": s.refined} for s in ch.ranked[:5]]
        log(f"model choice ({c}, {len(blocks)} models, {ch.renders:.0f} renders): "
            + ", ".join(f"{s.model[4:]} {(s.refined if s.refined is not None else s.screen):.2f}" for s in ch.ranked[:5]))
    if out is not None:
        (out / "model_choice.json").write_text(json.dumps(advice, indent=1))
    return advice


def search(engine, cands: list[Cand], section, kit, *, wall_s: float, beam: int = 2,
           beam_frac: float = 0.35, reserve_s: float = 150.0, seed: int = 0,
           log=print, progress=None, max_scores_per_chain: float | None = None,
           prior_weight: float = 0.0, refine_section=None) -> dict:
    """Beam over chains then refine on the best. Returns the ChainResults and timing facts.
    `refine_section` (clip policy beam3-refine-full): the refine runs on this longer section."""
    from tonematch.search import Budget

    t0 = time.perf_counter()
    budget = Budget(max_renders=10 ** 6, clip_s=len(section.target) / SR, short_s=len(section.target) / SR,
                    seed=seed, top_k_final=3)
    results: list[ChainResult] = []
    for c in cands:
        space = make_space(c)
        results.append(ChainResult(c, space, space.start.copy(), space.start.copy()))
    # Measure the real cost of one score (render + loss) on the first chain before planning.
    probe = results[0]
    load_candidate(engine, probe.cand)
    engine.render(section.dry)           # warm-up: the first render also measures the chain's latency
    tp = time.perf_counter()
    y = engine.render(section.dry)
    t_render = time.perf_counter() - tp
    tp = time.perf_counter()
    kit.score(section.target, y, weights=_weights())
    t_loss = time.perf_counter() - tp
    spr = t_render + t_loss
    log(f"probe: render {t_render:.2f} s + loss {t_loss:.2f} s = {spr:.2f} s per score")
    levels = {}
    if hasattr(kit, "ref_db"):                      # level reference: median start-render level
        kit.ref_db = None
        for r in results:
            load_candidate(engine, r.cand)
            levels[r.cand.name] = LevelGuardKit.level_db(engine.render(section.dry))
        kit.ref_db = float(np.median(list(levels.values())))
        log(f"level reference {kit.ref_db:.1f} dBFS rms (tolerance +-{kit.tol_db:.0f} dB, then "
            f"{kit.slope} loss/dB); start levels " + ", ".join(f"{k} {v:.1f}" for k, v in levels.items()))
    plan = make_plan(spr, wall_s, int(wall_s / spr))
    usable = max(wall_s - reserve_s - (time.perf_counter() - t0), 0.0)
    live = results
    beam_s = usable * beam_frac
    per_chain_s = beam_s / max(len(live), 1)
    log(f"budget: wall {wall_s:.0f} s, reserve {reserve_s:.0f} s; beam {beam_s:.0f} s over {len(live)} "
        f"chains ({per_chain_s:.0f} s, about {per_chain_s / spr:.0f} scores each)")
    for i, cr in enumerate(live):
        if not cr.space.n:
            cr.skipped = "no free parameters"
            continue
        cap = max(6.0, per_chain_s / spr - 2)
        cap = min(cap, max_scores_per_chain or cap)
        run_phase(engine, kit, section, budget, plan, cr, "beam", cap, 0.3, seed + 10 * i,
                  time.perf_counter() + per_chain_s * 1.15, log, prior_weight)
        if progress:
            progress(results, {"stage": "beam"})
    ok = sorted((c for c in live if not c.skipped and np.isfinite(c.loss)), key=lambda c: c.loss)
    survivors, seen = [], set()
    for c in ok:                                    # best variant per chain structure
        key = tuple((s.category, s.model_id) for s in c.cand.preset.slots)
        if key not in seen:
            survivors.append(c)
            seen.add(key)
    survivors = survivors[:max(1, beam)]
    log("survivors: " + ", ".join(f"{c.cand.name} ({c.loss:.4f})" for c in survivors))
    rsec, rbudget, spr_r = section, budget, spr
    if refine_section is not None and survivors:
        rsec = refine_section
        rbudget = Budget(max_renders=10 ** 6, clip_s=len(rsec.target) / SR, short_s=len(rsec.target) / SR,
                         seed=seed, top_k_final=3)
        if hasattr(kit, "ref_db") and kit.ref_db is not None:
            kit.ref_db_search = kit.ref_db       # the short-clip reference, for later short-clip refines
            rebase_level_reference(engine, kit, survivors, rsec, log)
        spr_r = spr * len(rsec.dry) / len(section.dry)        # a longer clip costs about proportionally more
    end = t0 + wall_s - reserve_s
    shares = [0.6, 0.4][:len(survivors)]
    shares = [s / sum(shares) for s in shares]
    for k, (cr, sh) in enumerate(zip(survivors, shares, strict=True)):
        remain = end - time.perf_counter()
        if k == 0:
            slice_s = remain * sh
        else:
            slice_s = remain
        if slice_s < 3 * spr_r:
            log(f"  {cr.cand.name}: no time left for refine")
            continue
        cap = max(6.0, slice_s / spr_r - 2)
        cap = min(cap, 2 * max_scores_per_chain if max_scores_per_chain else cap)
        run_phase(engine, kit, rsec, rbudget, plan, cr, "refine", cap, 0.15,
                  seed + 1000 + k, time.perf_counter() + slice_s, log, prior_weight)
        if progress:
            progress(results, {"stage": "refine"})
    return {"results": results, "plan": plan, "spr": spr, "budget": budget, "survivors": survivors,
            "refine_s": sum(p["wall_s"] for r in results for p in r.phases if p["phase"] == "refine"),
            "beam_s": sum(p["wall_s"] for r in results for p in r.phases if p["phase"] == "beam"),
            "t_render": t_render, "t_loss": t_loss, "search_wall_s": time.perf_counter() - t0,
            "levels_db": levels, "level_ref_db": getattr(kit, "ref_db", None), "prior_weight": prior_weight}


def _weights() -> dict:
    from tonematch.metrics import PROVISIONAL_WEIGHTS

    return dict(PROVISIONAL_WEIGHTS)


def final_check(engine, kit, section, results: list[ChainResult], top: int = 3, ndigits: int = 6,
                other_dis: list | None = None, log=print, prior_weight: float = 0.0) -> dict:
    """Top chains on the full excerpt with stems and mask; values rounded as they will be written.
    Also renders the best with the other DIs (DI sensitivity) and its unmatched start."""
    from tonematch.search import Budget, Evaluator

    budget = Budget(clip_s=len(section.target) / SR, short_s=len(section.target) / SR)
    plan = make_plan(1.0, 0.0, 10)
    ranked = sorted((r for r in results if not r.skipped and np.isfinite(r.loss)), key=lambda r: r.loss)[:top]
    finals = []
    for r in ranked:
        vals = np.round(r.values, ndigits)
        pre = apply_values(r.cand.preset, r.space, vals)
        load_candidate(engine, r.cand, pre)
        prior = make_prior(r.space, prior_weight)
        ev = Evaluator(engine, kit, section, r.space, plan, budget, prior)
        f, terms = ev.score(vals, final=True)
        if prior is not None:
            terms = {**terms, "prior_penalty": prior.penalty(r.space.as_changes(vals))}
        y = engine.render(section.dry)
        finals.append({"result": r, "values": vals, "preset": pre, "loss": f, "terms": terms, "y": y,
                       "level_db": LevelGuardKit.level_db(y)})
        log(f"final {r.cand.name}: {f:.4f} (search {r.loss:.4f})")
    finals.sort(key=lambda d: d["loss"])
    out = {"finals": finals, "di_sensitivity": [], "start_y": None, "start_loss": None, "dry_loss": None}
    if not finals:
        return out
    w = _weights()
    raw = getattr(kit, "inner", kit)                 # no level guard for the raw-DI baseline
    out["dry_loss"] = float(raw.score(section.target, section.dry, stems=section.stems,
                                      mask=section.mask, weights=w).total)
    best = finals[0]["result"]
    load_candidate(engine, best.cand, apply_values(best.cand.preset, best.space, best.start_values))
    ev = Evaluator(engine, kit, section, best.space, plan, budget, make_prior(best.space, prior_weight))
    out["start_loss"], _ = ev.score(best.start_values, final=True)
    out["start_y"] = engine.render(section.dry)
    if other_dis:
        load_candidate(engine, best.cand, finals[0]["preset"])
        for label, dry in other_dis:
            y = engine.render(dry[:len(section.target)])
            sc = kit.score(section.target, y, stems=section.stems, mask=section.mask, weights=w)
            out["di_sensitivity"].append({"di": label, "loss": float(sc.total)})
    return out


def refine_other_snapshot(engine, kit, section0, best: ChainResult, renders: int, seed: int, log,
                          prior_weight: float = 0.0):
    """Short CMA-ES from snapshot 1's values against snapshot 0's target. Returns (values, loss0_before, loss_after)."""
    from tonematch.search import Budget, Evaluator
    from tonematch.search.loop import refine_chain

    budget = Budget(clip_s=len(section0.target) / SR, short_s=len(section0.target) / SR)
    plan = make_plan(1.0, 0.0, renders)
    load_candidate(engine, best.cand, apply_values(best.cand.preset, best.space, best.values))
    ev = Evaluator(engine, kit, section0, best.space, plan, budget, make_prior(best.space, prior_weight))
    v, f, _terms, stages = refine_chain(ev, best.values, float(renders), 0.15, seed)
    log(f"snapshot 0: loss {stages[0]['score_in']:.4f} -> {f:.4f} ({ev.calls} renders)")
    return v, stages[0]["score_in"], f, ev.calls


# ------------------------------------------------------------------------------------ outputs


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.asarray(x, np.float64) ** 2) + 1e-20))


def level_match(sigs: dict[str, np.ndarray], ref: str, peak_dbfs: float = -1.0) -> tuple[dict, dict]:
    """Scale every signal to the reference's RMS, then one common gain so the loudest peak is at
    `peak_dbfs`. Returns (signals, gains in dB)."""
    target = rms(sigs[ref])
    scaled = {k: v * (target / rms(v)) for k, v in sigs.items()}
    peak = max(float(np.max(np.abs(v))) for v in scaled.values())
    common = min(1.0, 10 ** (peak_dbfs / 20) / max(peak, 1e-9))
    out = {k: (v * common).astype(np.float32) for k, v in scaled.items()}
    gains = {k: 20 * np.log10(target / rms(sigs[k]) * common) for k in sigs}
    return out, gains


def write_pack(listen: Path, target, match_y, start_y, di, extras: dict | None = None) -> dict:
    listen.mkdir(parents=True, exist_ok=True)
    sigs = {"target_stem": target, "match": match_y, "start_unmatched": start_y, "di_dry": di,
            **(extras or {})}
    n = min(len(v) for v in sigs.values())
    sigs = {k: np.asarray(v[:n], np.float32) for k, v in sigs.items()}
    lm, gains = level_match(sigs, "target_stem")
    for k, v in lm.items():
        sf.write(str(listen / f"{k}.wav"), v, SR, subtype="PCM_24")
    sf.write(str(listen / "ab_L-target_R-match.wav"), np.stack([lm["target_stem"], lm["match"]], axis=1),
             SR, subtype="PCM_24")
    # True-level DI exactly as the search fed it: the only file that may go to hardware.
    sf.write(str(listen / "di_search.wav"), np.asarray(di, np.float32), SR, subtype="FLOAT")
    info = {"samples": n, "gains_db": {k: round(float(g), 2) for k, g in gains.items()},
            "note": "listening files are level-matched and must NEVER feed hardware; use di_search.wav"}
    (listen / "gains.json").write_text(json.dumps(info, indent=1))
    return info


def write_best_hlx(cand: Cand, space, values, path: Path, name: str, ndigits: int = 6) -> Path:
    """The candidate's .hlx (fw 3.50, HX Stomp) with the matched values. Snapshot entries that carry
    a parameter (controller-assigned ones) are set to the same value, and a controller range is
    widened to include it, so the value is what the device uses in every snapshot."""
    from tonematch.preset import write_hlx

    p = apply_values(cand.preset, space, values, ndigits)
    p.name = name
    by_key = {s.key: s for s in p.slots if s.key}
    for sn in p.snapshots:
        for bk, params in sn.params.items():
            slot = by_key.get(bk)
            for pn in params:
                if slot is not None and pn in slot.params:
                    params[pn] = slot.params[pn]
    ctl = p.raw.get("data", {}).get("tone", {}).get("controller", {}).get("dsp0", {}) if p.raw else {}
    for bk, params in ctl.items():
        slot = by_key.get(bk)
        for pn, c in params.items():
            if slot is not None and pn in slot.params and "@min" in c and "@max" in c:
                v = slot.params[pn]
                c["@min"], c["@max"] = min(c["@min"], v), max(c["@max"], v)
    return write_hlx(p, path)


def write_top_hlx(finals: list[dict], out: Path, name: str) -> list[tuple[Path, int]]:
    """rank<k>_<chain>.hlx for every final-check preset, each validated. Returns [(path, exit code)]."""
    res = []
    for k, d in enumerate(finals, 1):
        r = d["result"]
        path = out / f"rank{k}_{r.cand.name}.hlx"
        write_best_hlx(r.cand, r.space, d["values"], path, f"{name} r{k} {r.cand.name}"[:32])
        res.append((path, validate_hlx(path)[0]))
    return res


def hlx_from_result(out: Path, root: Path = ROOT, only: list[str] | None = None) -> list[tuple[Path, int]]:
    """Rewrite the top-3 .hlx files of an existing run from its result.json (chains rebuilt from
    the template sets, Stomp-frozen parameters applied, values from the run)."""
    from tonematch.catalog import load_catalog

    res = json.loads((out / "result.json").read_text())
    cands, _ = build_candidates(root, load_catalog(), only, STOMP_FROZEN)
    by = {c.name: c for c in cands}
    finals = []
    for f in res["finals"]:
        c = by[f["chain"]]
        sp = make_space(c)
        vals = np.array([f["values"][f"{i}:{k}"] for i, k in sp.refs])
        finals.append({"result": SimpleNamespace(cand=c, space=sp), "values": vals})
    return write_top_hlx(finals, out, "DS-Sample")


def validate_hlx(path: Path) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "hlx_validate.py"), str(path)],
                       check=False, capture_output=True, text=True, cwd=ROOT)
    return r.returncode, (r.stdout + r.stderr).strip()


def param_table(cand: Cand, space, start, values) -> list[str]:
    rows = ["| slot | param | start | matched |", "|---|---|---|---|"]
    for (i, k), a, b in zip(space.refs, start, values, strict=True):
        rows.append(f"| {i} {cand.preset.slots[i].model_id} | {k} | {a:.3f} | {b:.3f} |")
    return rows


def write_report(path: Path, ctx: dict) -> None:
    w, di, s = ctx["window"], ctx["di"], ctx["search"]
    res: list[ChainResult] = s["results"]
    fin = ctx["final"]
    L = [f"# {ctx['song']}: end-to-end match (T041), LOCAL ONLY", "",
         (f"All losses below are the **{LABEL}**: `PROVISIONAL_WEIGHTS` (equal weights on `ltas_tilt`, "
         "`crest_flatness_hnr`, `envelope`) that `loss-trust-check` has not admitted. Lower is better; "
         "the numbers rank candidates against each other and are not an absolute fidelity score."), "",
         "## Target section", "",
         (f"- stems: `{ctx['stems_dir']}` (htdemucs_6s; the human reports the stem sounds like the guitar "
         "but much fidelity is missing)"),
         (f"- snapshot {w['snapshot']} (bars span {w['span_s'][0]:.1f}-{w['span_s'][1]:.1f} s), window "
         f"**{w['start_s']:.2f}-{w['end_s']:.2f} s** ({w['dur_s']:.1f} s)"),
         (f"- why: highest guitar-power-weighted mean separator confidence among all {w['dur_s']:.0f} s windows "
         f"of the snapshot with mostly-active guitar (mean confidence {w['mean_confidence']:.3f}, "
         f"{w['active_fraction'] * 100:.0f}% active frames); {w['exposed_overlap_s']:.1f} s of it lies in "
         "the separation stage's exposed-guitar windows (the band is silent there, others about -91 dB). "
         "Ties within 0.01 confidence go to the window most covered by the exposed-guitar windows, then to a bar-line start."),
         (f"- the mask is a per-sample confidence derived from the time-frequency mask; on this window it "
         f"averages {ctx['mask_mean']:.3f} (min {ctx['mask_min']:.2f}), so it excludes almost nothing here."),
         (f"- target descriptors (`dry.analyse`): pitch {di['desc']['pitch_lo']:.1f}-"
         f"{di['desc']['pitch_hi']:.1f} MIDI (median {di['desc']['pitch_med']:.1f}), style "
         f"{di['desc']['style']}, {di['desc']['density']:.2f} onsets/s, clarity {di['desc']['clarity']:.2f}"),
         (f"- snapshot 0 (bars 1-6, guitar enters at about 8 s, quieter and mixed with the band): "
         f"{ctx['snap0_text']}"), "",
         "## DI", ""]
    for i, d in enumerate(di["picks"]):
        c = d["clip"]
        L.append(f"- {'**used**' if i == 0 else 'sensitivity check'}: `{c['path']}` from {c['start_s']:.1f} s "
                 f"({c['style']}, pitch {c['pitch_lo']:.0f}-{c['pitch_hi']:.0f}, {c['density']:.2f} onsets/s, "
                 f"{c['source']}, {c['licence']}); " + (
                     "the user's own take cut to the target window by take_sample0_song_s, true level "
                     "(not normalised), timing loose and NOT sample-aligned"
                     if c["source"] == "user take" else
                     f"10 s read contiguously from the clip start, peak normalised to {DI_PEAK_DBFS} dBFS"))
    L += [("- why: `dry.pick_di` ranks indexed Guitar-TECHS clips by distance to the target's register, "
          "style and onset density; distinct source files are preferred so the picks differ in playing."), "",
          "## Engine and budget", "",
          (f"- engine: Helix Native {ctx['native_version']} via NativeEngine, preroll 144000, serial renders; "
          f"{ctx['renders']} renders ({ctx['restarts']} host restarts, {ctx['proactive']} of them for memory) in {ctx['wall_s']:.0f} s wall clock "
          f"(requested {ctx['wall_min']:.0f} min incl. final checks and the listening pack)"),
          f"- cost of one score on this machine: render {s['t_render']:.2f} s + loss {s['t_loss']:.2f} s",
          ("- search: beam over the chains below (CMA-ES, sigma 0.3, a few generations each), then CMA-ES "
          "(sigma 0.15) on the best two distinct chain structures (60/40 of the remaining time), then a "
          "final check of the top 3 on the full excerpt with stems and mask. No Bayesian optimisation, "
          "no predictor or retrieval seed."),
          (f"- objective = provisional loss + **level guard** (reference {s['level_ref_db']:.1f} dBFS RMS, the "
           f"median start-render level of the {len(s['levels_db'])} chains on this DI; free within "
           f"+-{ctx['args']['level_tol_db']:.0f} dB, then {ctx['args']['level_slope']} loss units per dB) + "
           f"**weak neutral prior** (every knob ~ N(0.5, 0.3), weight {ctx['args']['prior_weight']}; not fit "
           "on factory or community presets, none is available locally)."),
          ("- the Stomp was not used." if not ctx.get("stomp_speed") else
           f"- Stomp engine, speed options {ctx['stomp_speed']}: search renders use the clip policy "
           "named in clip_policy (default full excerpt; short clips are opt-in, they overfit the loudest part), the final check the full excerpt; readback_every is how often "
           "the engine reads the whole chain back; tail_mode measured sizes each wait to the chain's "
           "own decay (legacy: a full readback every load and fixed upper-bound waits). The level "
           "guard reference is measured on the clip the beam used."),
          (f"- frozen for Stomp parity (default): {ctx['frozen']}" if ctx["frozen"] else
           "- **NATIVE-ONLY run: Stomp will differ** (parity-frozen parameters such as cab EarlyReflections were free)."),
          ("- `listen/di_search.wav` is the true-level DI the search used (the only file for hardware); "
          "`listen/gains.json` records the listening gains."), "", "## Chains tried", "",
          "| chain | amp | cab | free dims | start loss | after beam | after refine | final check | renders |",
          "|---|---|---|---|---|---|---|---|---|"]
    fl = {id(d["result"]): d["loss"] for d in fin["finals"]}
    for r in sorted(res, key=lambda r: r.loss):
        pr = r.cand.preset
        amp = next((x.model_id for x in pr.slots if x.category == "amp"), "")
        cab = next((x.model_id for x in pr.slots if x.category == "cab"), "")
        ph = {p["phase"]: p["loss_out"] for p in r.phases}
        if r.skipped:
            L.append(f"| {r.cand.name} | {amp} | {cab} | {r.space.n} | skipped: {r.skipped} | | | | |")
            continue
        f = lambda v: f"{v:.4f}" if v is not None and np.isfinite(v) else ""
        L.append(f"| {r.cand.name} | {amp} | {cab} | {r.space.n} | {f(r.start_loss)} | {f(ph.get('beam'))} | "
                 f"{f(ph.get('refine'))} | {f(fl.get(id(r)))} | {r.renders} |")
    L += ["", *[f"- not searched: {n}" for n in ctx["missing"]], ""]
    L += [(f"Baselines on the same target: the raw DI with no amp scores {fin['dry_loss']:.4f}; the winning "
          f"chain at its unmatched start values scores {fin['start_loss']:.4f}."), "",
          "## Loss per stage (winning chain)", ""]
    if fin["finals"]:
        best = fin["finals"][0]["result"]
        L += ["| stage | loss | renders | wall |", "|---|---|---|---|",
              f"| start (unmatched) | {fin['start_loss']:.4f} | 1 | |"]
        for p in best.phases:
            L.append(f"| {p['phase']} | {p['loss_in']:.4f} -> {p['loss_out']:.4f} | {p['renders']} | "
                     f"{p['wall_s']:.0f} s |")
        L += [f"| final check (rounded values, stems+mask) | {fin['finals'][0]['loss']:.4f} | 1 | |", ""]
        L += ["Stage detail (CMA-ES stages of the last phase):", ""]
        for g in best.phases[-1]["stages"]:
            L.append(f"- stage {g['stage']}: {[f'{a}:{b}' for a, b in g['params']]}, {g['generations']} "
                     f"generations, {g['renders']} renders, {g['score_in']:.4f} -> {g['score_out']:.4f}")
        L.append("")
    L += ["## Top 3 presets (final check)", ""]
    for k, d in enumerate(fin["finals"], 1):
        r = d["result"]
        L += [f"### {k}. {r.cand.name}: loss {d['loss']:.4f}", "",
              f"- chain: {' > '.join(str(m) for _, m in [(s_.category, s_.model_id) for s_ in r.cand.preset.slots])}"
              + ("; delay enabled in the template" if any(x.category == 'delay' and x.enabled
                                                          for x in r.cand.preset.slots) else "; delay bypassed"),
              "- terms: " + ", ".join(f"{a}={b:.3f}" for a, b in d["terms"].items()),
              f"- source: {r.cand.note}; reached: "
              + ("beam + CMA-ES refine" if any(q["phase"] == "refine" for q in r.phases) else "beam only")
              + f" ({r.renders} renders)", "", *param_table(r.cand, r.space, r.start_values, d["values"]), ""]
        fixed = [f"{i}:{n}={v}" for i, x in enumerate(r.cand.preset.slots) if x.model_id
                 for n, v in x.params.items() if (i, n) not in r.space.refs]
        L += ["Not searched (held at the template's values): " + ", ".join(fixed), ""]
    if fin["di_sensitivity"]:
        L += ["## DI sensitivity (winning preset)", "",
              f"Primary DI: {fin['finals'][0]['loss']:.4f}. " + "; ".join(
                  f"{d['di']}: {d['loss']:.4f}" for d in fin["di_sensitivity"]) +
              ". The spread is the part of the loss that is the DI, not the preset.", ""]
    L += ["## Files", "", f"- preset: `{ctx['hlx']}` (fw 3.50, HX Stomp, validate: {ctx['validate_text']})"]
    L += ["- top-3 presets: " + ", ".join(f"`{Path(p).name}` (validate exit {c})" for p, c in ctx["top_hlx"])]
    if ctx.get("note"):
        L += [f"- NOTE: {ctx['note']}"]
    if ctx.get("hlx0"):
        L.append(f"- snapshot 0 variant: `{ctx['hlx0']}`")
    L += [(f"- listening pack: `{ctx['listen']}/` ({ctx['pack']['samples'] / SR:.1f} s each, RMS-matched to the "
          f"target stem, common gain for peak -1 dBFS; gains dB {ctx['pack']['gains_db']})"),
          ("  - `target_stem.wav`, `match.wav`, `start_unmatched.wav`, `di_dry.wav`, `match_rank2.wav`, "
          "`match_rank3.wav`, `ab_L-target_R-match.wav` (L target, R match)"),
          "", "## Caveats", "",
          (f"- **{LABEL}**: weights are provisional and equal; the loss was never admitted by `loss-trust-check`. "
          "It cannot tell a better tone from a better-looking number; the ear decides."),
          ("- **The comparison is not a null.** The DI is a different performance (different notes, player, "
          "guitar) than the song's guitar, so A/B differences include playing. The loss terms are built to "
          "ignore the notes, but the listening pack is not time-aligned and cannot be nulled."),
          ("- **The target is a separated stem** (htdemucs_6s) and the human reports missing fidelity; the "
          "search is matching the stem's artifacts as well as the guitar."),
          ("- **Few chains and few dimensions.** Only chains with an existing Native template are tried (not a "
          "model search); 8 to 11 free parameters per chain (amp controls, cab early reflections, delay mix and feedback when the delay is on). Cab Distance, cab Level, delay Time and "
          "other non-[0,1] controls cannot be driven through the CC map and stay at the template's values; "
          "a bypassed delay or reverb cannot be switched on, and an enabled delay runs at the host tempo "
          "(Native's, not the song's 67.1 bpm)."),
          ("- **The loss is blind to level, so the search needs a guard.** In the first full run (no guard, "
           "kept in `cache/matches/DS-Sample_run2_level-collapse/`) the winners drove channel volume to "
           "0.004 and the render ended 52 dB below the target stem while the loss improved from 14.0 to "
           "7.9; that preset would be silent on hardware. The guard and prior above are the response; they "
           "are my additions (search), not admitted terms. Compare `level_db_vs_ref` in each preset's terms."),
          ("- **Exact-minimum commands:** the CC route used to ignore a command of exactly 0.0 (measured "
           "2026-10-14); native-engine's memory fix re-sends every parameter and returning to 0.0 now matches "
           "setting 0.0 (ledger 2026-10-15T19:01:31), so the grid includes step 0. Earlier runs in "
           "`cache/matches/` predate that and used a 0.004 floor."),
          ("- **Short beam.** Each chain got a handful of generations, so a chain's rank after the beam "
          "partly reflects its start values."),
          ("- **Native only.** No Stomp rendering was done; Native and the Stomp differ by a measured parity "
          "residual (T040). The lead runs the hardware check."),
          "- Everything here is local; nothing in `cache/matches/` is to be committed or shared.", ""]
    path.write_text("\n".join(x for x in L if x is not None) + "\n")


# ---------------------------------------------------------------------------------------- main


def make_native_engine(preroll: int):
    from tonematch.engines.native import NativeEngine

    hv = ROOT / "cache" / "native" / "hostvenv" / "bin" / "python"
    if "TONEMATCH_NATIVE_PYTHON" not in os.environ and hv.exists():
        os.environ["TONEMATCH_NATIVE_PYTHON"] = str(hv)
    return NativeEngine(preroll=preroll)


def make_stomp_engine():
    """The bridge inside does the USB device check and holds the shared pedal lock
    (tonematch.pedal_lock) for the engine's whole life; DeviceMissing / LockHeld exit with their codes."""
    from tonematch.engines.stomp import StompEngine

    return StompEngine(purpose="match_song (Stomp)")


def jsonable(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


def run(args, engine=None, cands=None, log=None, max_scores_per_chain=None) -> int:
    max_scores_per_chain = max_scores_per_chain or getattr(args, "max_scores", None)
    from tonematch.metrics import LossKit
    from tonematch.preset import read_hlx  # noqa: F401  (import check for the preset module)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log = log or Log(out / "run.log")
    t_start = time.perf_counter()
    song = load_song(args.stems_dir)
    total_s = len(song.guitar) / SR
    log(f"loaded stems: {total_s:.1f} s, mask {song.mask_tf.shape}")
    fc, fp = frame_confidence(song.mask_tf, song.guitar)
    if args.target:
        win = manual_window(fc, fp, song.sections, *parse_target(args.target), total_s)
    else:
        win = pick_window(fc, fp, song.sections, args.snapshot, args.dur, total_s)
    log(f"target window snapshot {win['snapshot']}: {win['start_s']:.2f}-{win['end_s']:.2f} s, "
        f"confidence {win['mean_confidence']:.3f}, exposed overlap {win['exposed_overlap_s']:.1f} s")
    a, b = int(win["start_s"] * SR), int(win["end_s"] * SR)
    if args.user_di:
        c0, d0 = user_di_clip(args.user_di, args.user_di_json, win)
        picks = [(c0, d0)]
        for extra in args.user_di_extra or []:
            ej = str(extra).replace("_dry.wav", ".json")
            picks.append(user_di_clip(extra, ej if Path(ej).is_file() else None, win))
        desc = {k: getattr(c0, k) for k in ("pitch_lo", "pitch_hi", "pitch_med", "density", "style",
                                            "clarity")}
    else:
        picks, desc = choose_di(song.guitar[a:b], win["dur_s"], n=3, library=args.di_library)
    for i, (c, _) in enumerate(picks):
        log(f"DI {i}: {c.path} @ {c.start_s:.1f}s style {c.style} pitch {c.pitch_lo:.0f}-{c.pitch_hi:.0f}")
    dry = picks[0][1]
    section = build_section(song, win, fc, f"snapshot{args.snapshot}", dry, args.song)
    kit = LevelGuardKit(MaskedKit(LossKit(), section.mask), args.level_tol_db, args.level_slope)
    try:
        from tonematch.catalog import load_catalog
        catalog = load_catalog()
    except Exception:  # noqa: BLE001
        catalog = None
    missing: list[str] = []
    if cands is None:
        only = args.chains.split(",") if args.chains else None
        cands, missing = build_candidates(ROOT, catalog, only, NATIVE_FROZEN if args.native_only else STOMP_FROZEN,
                                          allow_time_variant=getattr(args, "allow_time_variant", False))
    for n in missing:
        log(f"missing: {n}")
    log("candidates: " + ", ".join(f"{c.name}({len(c.free or [])} dims)" for c in cands))
    for c in cands:
        if "time-variant" in c.note:
            log(f"WARNING {c.name}: contains a time-variant block; the null-based loss is unreliable")
    if args.dry_run:
        log("dry run: stopping before any render")
        return 0
    own = engine is None
    if own:
        engine = make_stomp_engine() if args.engine == "stomp" else make_native_engine(args.preroll)
    speed = None
    policy = args.clip_policy or (STOMP_DEFAULT_POLICY if args.engine == "stomp" else "full")
    if args.engine == "stomp" and args.stomp_legacy and not args.clip_policy:
        policy = "full"
    s_search, s_refine = clip_policy_views(section, policy)
    if args.engine == "stomp":
        speed = apply_stomp_speed(engine, args.stomp_legacy)
        speed["search_clip_s"] = len(s_search.dry) / SR
        speed["final_clip_s"] = len(section.dry) / SR
        speed["legacy"] = bool(args.stomp_legacy)
        speed["clip_policy"] = policy
        log(f"stomp speed options: {speed}")
    else:
        log(f"clip policy {policy}: search clip {len(s_search.dry) / SR:.1f} s, final {len(section.dry) / SR:.1f} s")
    clip_info = {"policy": policy, "search_clip_s": len(s_search.dry) / SR,
                 "refine_clip_s": len((s_refine or s_search).dry) / SR, "final_clip_s": len(section.dry) / SR}
    eng = TimedEngine(engine, log, rss_limit_mb=args.rss_limit_mb)
    try:
        if getattr(args, "model_choice", False):
            model_choice_advice(eng, kit, s_search, args.model_choice_cats.split(","), args.seed, log, out=out)
        sr_ = search(eng, cands, s_search, kit, wall_s=args.wall_min * 60, beam=args.beam,
                     beam_frac=args.beam_frac, reserve_s=args.reserve_s, seed=args.seed, log=log,
                     max_scores_per_chain=max_scores_per_chain, prior_weight=args.prior_weight,
                     refine_section=s_refine, progress=lambda res, info: (out / "progress.json").write_text(json.dumps(
                         {"info": info, "chains": [{"name": r.cand.name, "start": r.start_loss,
                                                    "loss": r.loss, "phases": r.phases} for r in res]},
                         default=jsonable, indent=1)))
        ref_short = getattr(kit, "ref_db_search", kit.ref_db)   # before any rebase to a longer clip
        if s_search is not section and s_refine is None and ref_short is not None:
            rebase_level_reference(eng, kit, sr_["results"], section, log)
        fin = final_check(eng, kit, section, sr_["results"], 3, other_dis=[
            (f"{c.path}@{c.start_s:.0f}s", d) for c, d in picks[1:]], log=log,
            prior_weight=args.prior_weight)
        if not fin["finals"]:
            log("no chain survived; nothing to write")
            return 1
        best = fin["finals"][0]
        hlx = out / "best_snapshot1.hlx"
        write_best_hlx(best["result"].cand, best["result"].space, best["values"], hlx, args.preset_name)
        top_hlx = write_top_hlx(fin["finals"], out, args.preset_name)
        code, vtext = validate_hlx(hlx)
        log(f"hlx_validate exit {code}: {vtext.splitlines()[-1] if vtext else ''}")
        extras = {}
        for k, d in enumerate(fin["finals"][1:3], 2):
            extras[f"match_rank{k}"] = d["y"]
        pack = write_pack(out / "listen", section.target, best["y"], fin["start_y"], section.dry, extras)
        # Snapshot 0 only if cheap: a short refine of the winning chain against snapshot 0's own target.
        snap0_text, hlx0 = "not run", None
        if args.snap0_renders > 0 and args.snapshot != 0:
            try:
                w0 = pick_window(fc, fp, song.sections, 0, args.dur, total_s)
                s0 = build_section(song, w0, fc, "snapshot0", dry, args.song)
                if policy != "full":
                    s0 = clip_policy_views(s0, "loudest3" if policy == "beam3-refine-full" else policy)[0]
                k0 = LevelGuardKit(MaskedKit(LossKit(), s0.mask), args.level_tol_db, args.level_slope)
                k0.ref_db = ref_short
                v0, before, after, n0 = refine_other_snapshot(
                    eng, k0, s0, best["result"], args.snap0_renders, args.seed + 7, log,
                    args.prior_weight)
                hlx0 = out / "best_snapshot0.hlx"
                write_best_hlx(best["result"].cand, best["result"].space, np.round(v0, 6), hlx0,
                               args.preset_name[:13] + " s0")
                snap0_text = (f"window {w0['start_s']:.1f}-{w0['end_s']:.1f} s (confidence "
                              f"{w0['mean_confidence']:.2f}); snapshot 1's preset scores {before:.4f} there, "
                              f"a {n0}-render CMA-ES refine from it reaches {after:.4f} "
                              f"(`best_snapshot0.hlx`; a short run, not a snapshot-aware preset)")
            except Exception as e:  # noqa: BLE001 - optional extra, never fatal
                snap0_text = f"failed: {type(e).__name__}: {e}"
                log(snap0_text)
        wall = time.perf_counter() - t_start
        ctx = {"song": args.song, "stems_dir": args.stems_dir, "window": win,
               "di": {"desc": desc, "picks": [{"clip": dataclasses.asdict(c)} for c, _ in picks]},
               "search": sr_, "final": fin, "missing": missing, "wall_s": wall, "wall_min": args.wall_min,
               "frozen": {f"{c}.{n}": v for (c, n), v in (NATIVE_FROZEN if args.native_only else STOMP_FROZEN).items()},
               "stomp_speed": speed, "clip": clip_info, "renders": eng.renders, "restarts": eng.restarts, "proactive": eng.proactive,
               "args": vars(args),
               "hlx": str(hlx.relative_to(ROOT)) if hlx.is_relative_to(ROOT) else str(hlx),
               "hlx0": str(hlx0) if hlx0 else None, "top_hlx": top_hlx, "note": args.note, "validate_text": f"exit {code}, see {hlx.stem}.validate.md",
               "listen": str(out / "listen"), "pack": pack, "snap0_text": snap0_text,
               "mask_mean": float(np.mean(section.mask)), "mask_min": float(np.min(section.mask)),
               "native_version": getattr(getattr(engine, "capabilities", lambda: None)(), "firmware_version", "")}
        write_report(out / "report.md", ctx)
        (out / "result.json").write_text(json.dumps({
            "label": LABEL, "window": win, "stomp_speed": speed, "clip": clip_info, "render_s": eng.render_s,
            "search_wall_s": sr_["search_wall_s"], "beam_s": sr_["beam_s"], "refine_s": sr_["refine_s"],
            "t_render_probe_s": sr_["t_render"], "t_loss_probe_s": sr_["t_loss"], "renders": eng.renders, "host_restarts": eng.restarts, "proactive_restarts": eng.proactive,
            "level_ref_db": sr_["level_ref_db"], "level_ref_final_db": kit.ref_db, "start_levels_db": sr_["levels_db"], "wall_s": wall,
            "finals": [{"chain": d["result"].cand.name, "loss": d["loss"], "terms": d["terms"], "level_db": d["level_db"],
                        "values": {f"{i}:{k}": float(v) for (i, k), v in zip(d["result"].space.refs,
                                                                             d["values"], strict=True)}}
                       for d in fin["finals"]],
            "start_loss": fin["start_loss"], "dry_loss": fin["dry_loss"],
            "di_sensitivity": fin["di_sensitivity"]}, default=jsonable, indent=1))
        log(f"done: {eng.renders} renders, {wall:.0f} s; report {out / 'report.md'}")
        return 0
    finally:
        if own:
            if args.engine == "stomp":
                engine.close(verify_bytes=True)
            else:
                engine.close()


def parse(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stems-dir", default=str(ROOT / "cache/stems/6bb5c147cd009a8e/htdemucs_6s"))
    ap.add_argument("--song", default="DS-Sample")
    ap.add_argument("--out", default=str(ROOT / "cache/matches/DS-Sample"))
    ap.add_argument("--snapshot", type=int, default=1)
    ap.add_argument("--dur", type=float, default=10.0, help="target excerpt length in seconds")
    ap.add_argument("--wall-min", type=float, default=25.0, help="wall-clock budget for the search stages")
    ap.add_argument("--reserve-s", type=float, default=150.0, help="seconds kept for the final check")
    ap.add_argument("--beam", type=int, default=2, help="chains refined after the beam")
    ap.add_argument("--beam-frac", type=float, default=0.35)
    ap.add_argument("--model-choice", action="store_true",
                    help="advisory model choice per slot (T050) before the chain search; changes no chain")
    ap.add_argument("--model-choice-cats", default="drive,amp", help="categories --model-choice ranks")
    ap.add_argument("--snap0-renders", type=int, default=30, help="0 skips the snapshot 0 refine")
    ap.add_argument("--preroll", type=int, default=144_000)
    ap.add_argument("--chains", help="comma list from " + ",".join(TEMPLATE_SETS))
    ap.add_argument("--di-library", default=None)
    ap.add_argument("--preset-name", default="DS-Sample s1")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--level-tol-db", type=float, default=6.0,
                    help="output level may differ this much from the median start-chain level for free")
    ap.add_argument("--level-slope", type=float, default=0.5, help="loss units per dB beyond the tolerance")
    ap.add_argument("--prior-weight", type=float, default=0.3,
                    help="weak neutral Gaussian prior on the knobs (0 = off)")
    ap.add_argument("--rss-limit-mb", type=float, default=9000.0,
                    help="restart the Native host when it grows past this (it leaks per render)")
    ap.add_argument("--target", help="START:END in song seconds; overrides the automatic section pick")
    ap.add_argument("--user-di", help="dry wav used instead of pick_di (true level, not normalised)")
    ap.add_argument("--user-di-json", help="take json; alignment.take_sample0_song_s places the take in song time")
    ap.add_argument("--user-di-extra", action="append",
                    help="more user dry wavs (same song window) for the DI-sensitivity check; repeatable")
    ap.add_argument("--note", help="free text appended to report.md (e.g. which rank the human preferred)")
    ap.add_argument("--rewrite-hlx", metavar="RUN_DIR",
                    help="only rewrite the top-3 .hlx files of an existing run from its result.json")
    ap.add_argument("--allow-time-variant", action="store_true",
                    help="keep chains with time-variant blocks (HD2_ReverbOcto, VIC_DelayGlitch, "
                         "Victoria_ShufflingDelay) in the pool; their null-based loss is unreliable")
    ap.add_argument("--native-only", action="store_true",
                    help="leave Stomp-parity-frozen parameters free; report.md says 'Stomp will differ'")
    ap.add_argument("--engine", choices=("native", "stomp"), default="native",
                    help="engine that renders the search (stomp: the pedal, one session, restore byte-checked)")
    ap.add_argument("--stomp-legacy", action="store_true",
                    help="Stomp: old behaviour (whole clip every render, readback every load, fixed tail waits)")
    ap.add_argument("--clip-policy", choices=CLIP_POLICIES, default=None,
                    help="what the search renders (the final check always uses the full excerpt): full | "
                         "loudest3 | loudest5 | beam3-refine-full | spread. Default: "
                         f"{STOMP_DEFAULT_POLICY} for --engine stomp, full for native")
    ap.add_argument("--max-scores", type=float, default=None,
                    help="cap on scores per chain in the beam (the refine gets twice that): a trial budget "
                         "that does not depend on how fast the engine is")
    ap.add_argument("--dry-run", action="store_true", help="pick target, DI and chains; render nothing")
    return ap.parse_args(argv)


if __name__ == "__main__":
    _a = parse()
    if _a.rewrite_hlx:
        for _p, _c in hlx_from_result(Path(_a.rewrite_hlx)):
            print(f"{_p} validate exit {_c}")
        sys.exit(0)
    sys.exit(run(_a))
