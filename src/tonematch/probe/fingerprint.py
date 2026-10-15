"""Block fingerprint format (schema `tonematch.block-fingerprint` v1) and the per-feature compare.

The schema is documented in docs/notes/block-probe.md; bump SCHEMA_VERSION on any change to the
meaning or layout of a field, and keep `compare` reading the previous version until no stored
fingerprint uses it.

`compare(a, b)` diffs two fingerprints feature by feature against tolerances (`TOLERANCES`; a
caller may override any of them) and returns rows sorted by how far past its tolerance each one is.
Typical uses: the same block on the Stomp vs Native (per-block parity), or one block at setting A
vs setting B (what a knob does).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

SCHEMA = "tonematch.block-fingerprint"
SCHEMA_VERSION = 1
SECTIONS = ("alignment", "linear", "harmonics", "static_curve", "transfer_curve", "imd", "dynamics",
            "delay", "reverb", "modulation", "di")

# (absolute or relative tolerance, relative?) per feature. dB unless named otherwise.
TOLERANCES: dict[str, tuple[float, bool]] = {
    "alignment.latency_samples": (2, False),
    "linear.mag_db": (1.0, False),            # RMS over the frequency grid, per sweep level
    "linear.mag_db.max": (3.0, False),        # worst single grid point, per sweep level
    "linear.gain_1k_db": (0.5, False),
    "harmonics.gain_db": (1.0, False),
    "harmonics.h_db": (3.0, False),           # only where either side is above HARM_FLOOR_DB
    "harmonics.thd_db": (3.0, False),
    "harmonics.odd_even_db": (6.0, False),
    "static_curve.small_signal_gain_db": (0.5, False),
    "static_curve.knee_dbfs": (3.0, False),
    "static_curve.ratio": (0.2, True),
    "static_curve.p1db_in_dbfs": (2.0, False),
    "static_curve.out_max_dbfs": (1.0, False),
    "transfer_curve.symmetry": (0.1, False),
    "transfer_curve.pos_ceiling": (0.1, True),
    "transfer_curve.neg_ceiling": (0.1, True),
    "transfer_curve.r2": (0.1, False),
    "imd.imd2_db": (3.0, False),
    "imd.imd3_db": (3.0, False),
    "imd.imd_total_db": (3.0, False),
    "dynamics.level_dependent_gain_db": (1.0, False),
    "dynamics.attack_ms": (0.25, True),
    "dynamics.release_ms": (0.25, True),
    "dynamics.gr_after_up_db": (1.0, False),
    "dynamics.gr_after_down_db": (1.0, False),
    "dynamics.burst_overshoot_db": (1.0, False),
    "dynamics.gate_threshold_dbfs": (5.0, False),
    "delay.detected": (0, False),
    "delay.time_ms": (1.0, False),
    "delay.feedback": (0.05, False),
    "delay.echo_level_db": (1.0, False),
    "reverb.rt60_s": (0.1, True),
    "reverb.edt_s": (0.15, True),
    "reverb.wet_level_db": (2.0, False),
    "modulation.kind": (0, False),
    "modulation.rate_hz": (0.05, True),
    "modulation.am_depth_db_pp": (0.5, False),
    "modulation.delay_depth_ms_pp": (0.15, True),
    "di.rms_gain_db": (1.0, False),
    "di.coherence": (0.05, False),
    "di.response_db": (1.5, False),
    "di.nonlinear_fraction_db": (3.0, False),
}
HARM_FLOOR_DB = -80.0     # harmonic / IMD levels below this on both sides count as equal
MOD_FLOOR = {"am_depth_db_pp": 0.3, "delay_depth_ms_pp": 0.02}


def validate(fp: dict) -> list[str]:
    """Problems with a fingerprint dict (empty list = valid v1)."""
    errs = []
    if fp.get("schema") != SCHEMA:
        errs.append(f"schema is {fp.get('schema')!r}, expected {SCHEMA!r}")
    if fp.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"schema_version is {fp.get('schema_version')!r}, expected {SCHEMA_VERSION}")
    for k in ("probe_version", "probe_sha256", "sample_rate", "subject", "created") + SECTIONS:
        if k not in fp:
            errs.append(f"missing {k}")
    lin = fp.get("linear", {})
    if lin and any(len(m) != len(lin.get("freqs_hz", [])) for m in lin.get("mag_db", [])):
        errs.append("linear.mag_db rows do not match freqs_hz")
    for p in fp.get("harmonics", {}).get("per_freq", []):
        if len(p.get("h_db", [])) != len(p.get("levels_dbfs", [])):
            errs.append(f"harmonics {p.get('freq_hz')} Hz: h_db rows do not match levels")
    try:
        json.dumps(fp, allow_nan=False)
    except ValueError as e:
        errs.append(f"not strict JSON: {e}")
    return errs


def save(fp: dict, path: str | Path) -> Path:
    errs = validate(fp)
    if errs:
        raise ValueError("invalid fingerprint: " + "; ".join(errs))
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(fp, indent=1, allow_nan=False) + "\n")
    return p


def load(path: str | Path) -> dict:
    fp = json.loads(Path(path).read_text())
    errs = validate(fp)
    if errs:
        raise ValueError(f"{path}: " + "; ".join(errs))
    return fp


# ------------------------------------------------------------------------------------- compare

class _Cmp:
    def __init__(self, tol: dict[str, tuple[float, bool]]):
        self.tol = tol
        self.rows: list[dict] = []

    def _add(self, feature: str, key: str, a, b, diff, where=None) -> None:
        tol, rel = self.tol[key]
        if diff is None:
            status, score = "differs", float("inf")
        else:
            score = abs(diff) / tol if tol > 0 else (0.0 if diff == 0 else float("inf"))
            status = "ok" if score <= 1.0 else "differs"
        row = {"feature": feature, "a": a, "b": b, "diff": diff, "tol": tol, "rel": rel,
               "score": score, "status": status}
        if where is not None:
            row["where"] = where
        self.rows.append(row)

    def scalar(self, feature: str, key: str, a, b) -> None:
        if a is None and b is None:
            return
        if a is None or b is None:
            self._add(feature, key, a, b, None)
            return
        if isinstance(a, (bool, str)) or isinstance(b, (bool, str)):
            self._add(feature, key, a, b, 0 if a == b else 1)
            return
        _tol, rel = self.tol[key]
        diff = (b - a) / abs(a) if rel and abs(a) > 1e-12 else b - a
        self._add(feature, key, a, b, float(diff))

    def array(self, feature: str, key: str, a, b, *, mode: str = "max", floor: float | None = None,
              labels=None) -> None:
        if a is None or b is None:
            if a is not None or b is not None:
                self._add(feature, key, "array" if a else None, "array" if b else None, None)
            return
        A = np.array([np.nan if v is None else v for v in np.ravel(a)], dtype=float)
        B = np.array([np.nan if v is None else v for v in np.ravel(b)], dtype=float)
        if A.shape != B.shape:
            self._add(feature, key, f"len {A.size}", f"len {B.size}", None)
            return
        m = np.isfinite(A) & np.isfinite(B)
        if floor is not None:
            m &= (A > floor) | (B > floor)
        if not m.any():
            return
        d = np.where(m, B - A, 0.0)
        if mode == "rms":
            diff = float(np.sqrt(np.mean(d[m] ** 2)))
        else:
            diff = float(d[np.argmax(np.abs(d))])
        if mode == "rms":
            self._add(feature, key, None, None, diff)
            return
        i = int(np.argmax(np.abs(d)))
        where = labels[i] if labels is not None and i < len(labels) else i
        self._add(feature, key, None, None, diff, where)


def compare(a: dict, b: dict, tolerances: dict[str, tuple[float, bool]] | None = None) -> dict:
    """Per-feature diff of fingerprint b against a. Rows sorted worst first."""
    tol = dict(TOLERANCES)
    tol.update(tolerances or {})
    c = _Cmp(tol)
    warnings = []
    if a.get("probe_sha256") != b.get("probe_sha256"):
        warnings.append(f"different probes: {a.get('probe_version')} vs {b.get('probe_version')}"
                        " (sections from changed segments may not be comparable)")

    c.scalar("alignment.latency_samples", "alignment.latency_samples",
             a["alignment"]["latency_samples"], b["alignment"]["latency_samples"])

    la, lb = a["linear"], b["linear"]
    if la.get("freqs_hz") == lb.get("freqs_hz"):
        for i, L in enumerate(la["levels_dbfs"]):
            if i < len(lb["mag_db"]):
                c.array(f"linear.mag_db@{L:g}dBFS", "linear.mag_db", la["mag_db"][i],
                        lb["mag_db"][i], mode="rms")
                c.array(f"linear.mag_db.max@{L:g}dBFS", "linear.mag_db.max", la["mag_db"][i],
                        lb["mag_db"][i], labels=[f"{f:g} Hz" for f in la["freqs_hz"]])
    c.array("linear.gain_1k_db", "linear.gain_1k_db", la["gain_1k_db"], lb["gain_1k_db"],
            labels=[f"{L:g} dBFS" for L in la["levels_dbfs"]])

    pb = {p["freq_hz"]: p for p in b["harmonics"]["per_freq"]}
    for pa in a["harmonics"]["per_freq"]:
        q = pb.get(pa["freq_hz"])
        if q is None:
            continue
        f = f"{pa['freq_hz']:g}Hz"
        lv = [f"{L:g} dBFS" for L in pa["levels_dbfs"]]
        c.array(f"harmonics.gain_db@{f}", "harmonics.gain_db", pa["gain_db"], q["gain_db"],
                labels=lv)
        c.array(f"harmonics.thd_db@{f}", "harmonics.thd_db", pa["thd_db"], q["thd_db"],
                floor=HARM_FLOOR_DB, labels=lv)
        hl = [f"{L:g} dBFS H{k}" for L in pa["levels_dbfs"] for k in pa["h_orders"]]
        c.array(f"harmonics.h_db@{f}", "harmonics.h_db", pa["h_db"], q["h_db"],
                floor=HARM_FLOOR_DB, labels=hl)
        loud = [i for i, (ta, tb) in enumerate(zip(pa["thd_db"], q["thd_db"]))
                if max(ta or -999, tb or -999) > -60]
        if loud:
            c.array(f"harmonics.odd_even_db@{f}", "harmonics.odd_even_db",
                    [pa["odd_even_db"][i] for i in loud], [q["odd_even_db"][i] for i in loud],
                    labels=[lv[i] for i in loud])
        for k in ("small_signal_gain_db", "knee_dbfs", "ratio", "p1db_in_dbfs", "out_max_dbfs"):
            c.scalar(f"static_curve.{k}@{f}", f"static_curve.{k}", pa["static"].get(k),
                     q["static"].get(k))

    ta, tb = a.get("transfer_curve") or {}, b.get("transfer_curve") or {}
    for k in ("symmetry", "pos_ceiling", "neg_ceiling", "r2"):
        c.scalar(f"transfer_curve.{k}", f"transfer_curve.{k}", ta.get(k), tb.get(k))

    ib = {r["name"]: r for r in b["imd"]}
    for r in a["imd"]:
        q = ib.get(r["name"])
        if q is None:
            continue
        for k in ("imd2_db", "imd3_db", "imd_total_db"):
            if r.get(k) is None and q.get(k) is None:
                continue
            if max(r.get(k) or -999, q.get(k) or -999) <= HARM_FLOOR_DB:
                continue
            c.scalar(f"imd.{k}@{r['name']}", f"imd.{k}", r.get(k), q.get(k))

    da, db_ = a["dynamics"], b["dynamics"]
    for k in ("level_dependent_gain_db", "attack_ms", "release_ms", "burst_overshoot_db",
              "gate_threshold_dbfs"):
        c.scalar(f"dynamics.{k}", f"dynamics.{k}", da.get(k), db_.get(k))
    for k in ("gr_after_up_db", "gr_after_down_db"):
        c.array(f"dynamics.{k}", f"dynamics.{k}", da.get(k), db_.get(k),
                labels=[f"{t} ms" for t in da.get("gr_times_ms", [])])

    c.scalar("delay.detected", "delay.detected", bool(a["delay"]["detected"]),
             bool(b["delay"]["detected"]))
    if a["delay"]["detected"] and b["delay"]["detected"]:
        for k in ("time_ms", "feedback", "echo_level_db"):
            c.scalar(f"delay.{k}", f"delay.{k}", a["delay"].get(k), b["delay"].get(k))
    for k in ("rt60_s", "edt_s", "wet_level_db"):
        c.scalar(f"reverb.{k}", f"reverb.{k}", a["reverb"].get(k), b["reverb"].get(k))

    ma, mb = a["modulation"], b["modulation"]
    c.scalar("modulation.kind", "modulation.kind", ma.get("kind"), mb.get("kind"))
    if ma.get("rate_hz") is not None and mb.get("rate_hz") is not None:
        c.scalar("modulation.rate_hz", "modulation.rate_hz", ma["rate_hz"], mb["rate_hz"])
    for k, fl in MOD_FLOOR.items():
        va, vb = ma.get(k), mb.get(k)
        if va is not None and vb is not None and max(va, vb) > fl:
            c.scalar(f"modulation.{k}", f"modulation.{k}", va, vb)

    dib = {d["name"]: d for d in b.get("di", [])}
    for d in a.get("di", []):
        q = dib.get(d["name"])
        if q is None:
            continue
        bands = [f"{f:g} Hz" for f in d["bands_hz"]]
        c.scalar(f"di.rms_gain_db@{d['name']}", "di.rms_gain_db", d["rms_gain_db"],
                 q["rms_gain_db"])
        c.scalar(f"di.nonlinear_fraction_db@{d['name']}", "di.nonlinear_fraction_db",
                 d["nonlinear_fraction_db"], q["nonlinear_fraction_db"])
        c.array(f"di.coherence@{d['name']}", "di.coherence", d["coherence"], q["coherence"],
                labels=bands)
        c.array(f"di.response_db@{d['name']}", "di.response_db", d["response_db"],
                q["response_db"], labels=bands)
    if a.get("di") and not b.get("di") or b.get("di") and not a.get("di"):
        warnings.append("only one fingerprint has DI sections")

    rows = sorted(c.rows, key=lambda r: -r["score"])
    return {"a": a.get("subject", {}), "b": b.get("subject", {}), "warnings": warnings,
            "n_compared": len(rows), "n_differs": sum(r["status"] == "differs" for r in rows),
            "rows": rows}


def format_compare(res: dict, *, all_rows: bool = False) -> str:
    def fmt(v):
        if v is None:
            return "-"
        if isinstance(v, float):
            return f"{v:.4g}"
        return str(v)

    lines = [f"A: {json.dumps(res['a'])}", f"B: {json.dumps(res['b'])}"]
    lines += [f"WARNING: {w}" for w in res["warnings"]]
    lines.append(f"{res['n_differs']} of {res['n_compared']} features differ beyond tolerance")
    lines.append(f"{'status':8} {'feature':44} {'diff (B-A)':>11} {'tol':>8}  a / b / where")
    for r in res["rows"]:
        if r["status"] == "ok" and not all_rows:
            continue
        tol = f"{r['tol']:g}{' rel' if r['rel'] else ''}"
        extra = f"{fmt(r['a'])} / {fmt(r['b'])}" if r["a"] is not None or r["b"] is not None else ""
        if "where" in r:
            extra += f" at {r['where']}"
        lines.append(f"{r['status']:8} {r['feature']:44} {fmt(r['diff']):>11} {tol:>8}  {extra}")
    return "\n".join(lines)
