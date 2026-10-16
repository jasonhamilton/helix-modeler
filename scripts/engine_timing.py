"""Engine timing for the status report: how long each activity really takes, per engine.

Everything here is derived from the project's own records every time the report is built, so new
ledger rows show up on the next run without anyone editing a table:

  - measured time per test: the NEWEST ledger row for each activity (ledger/runs.csv), with its
    timestamp so the report can link to the row;
  - estimates: N tests x the measured time (N = 100), serial and, where parallel speed-up has been
    measured, with it; an estimate is never shown without saying what it assumes;
  - real-world jobs: typical sizes read from the repository (the synthetic-benchmark design note,
    the block-probe note, board/TASKS.md, the ledger and, if present, the local match reports in
    cache/matches) multiplied by the measured per-unit times;
  - 'not measured' wherever no record exists. Nothing is guessed.

Used by scripts/status_report.py (build_model, both renderers). Standard library only.
"""

from __future__ import annotations

import math
import re
import statistics
from datetime import UTC, datetime
from pathlib import Path

N_TESTS = 100
FW_NOTE = "verified on fw 3.50, recheck on 3.80 pending"
FW_NOTE_NO_UPGRADE = "fw 3.50 (pedal not yet upgraded)"
NOT_MEASURED = "not measured"

# --------------------------------------------------------------------------- formatting


def fmt_dur(s: float | None) -> str:
    """A duration in human units, always with its unit: 0.17 s, 2.68 s, 34.2 s, 2.4 min, 6.3 h."""
    if s is None or isinstance(s, bool) or not math.isfinite(s):
        return NOT_MEASURED
    if s < 1:
        return f"{s:.2f} s"
    if s < 10:
        return f"{s:.2f} s"
    if s < 100:
        return f"{s:.1f} s"
    if s < 3600:
        m = s / 60
        return f"{m:.1f} min" if m < 10 else f"{m:.0f} min"
    h = s / 3600
    return f"{h:.1f} h" if h < 10 else f"{h:.0f} h"


def fmt_count(n: float) -> str:
    return f"{n:,.0f}"


def _f(x: object) -> float | None:
    try:
        v = float(str(x))
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


# --------------------------------------------------------------------------- ledger access


def index_rows(rows: list[dict]) -> list[dict]:
    """Ledger rows with their file line (header is line 1)."""
    return [dict(r, _line=i + 2) for i, r in enumerate(rows)]


def newest(rows: list[dict], pred) -> dict | None:
    c = [r for r in rows if pred(r)]
    return max(c, key=lambda r: (r.get("timestamp", ""), r["_line"]), default=None)


def stomp_upgrade_ts(rows: list[dict]) -> str | None:
    """Timestamp of the first Stomp row saying the pedal was upgraded to firmware 3.80."""
    ts = [r["timestamp"] for r in rows if r.get("engine") == "stomp"
          and re.search(r"upgraded[^.;]*firmware 3\.80", r.get("notes", ""))]
    return min(ts) if ts else None


def cell(r: dict, seconds: float, what: str, upgrade_ts: str | None = None, **kw) -> dict:
    """A measured number, with the ledger row it came from."""
    eng = kw.pop("engine", r.get("engine"))
    c = {"state": "measured", "seconds": seconds, "what": what, "ts": r["timestamp"],
         "line": r["_line"], "date": r["timestamp"][:10], "source": "ledger", "note": ""}
    if eng == "stomp":
        if upgrade_ts is None:
            c["fw"] = FW_NOTE_NO_UPGRADE
        elif r["timestamp"] < upgrade_ts:
            c["fw"] = FW_NOTE
    c.update(kw)
    return c


def missing(why: str = "") -> dict:
    return {"state": "not-measured", "seconds": None, "what": why, "ts": None, "date": None}


def not_needed(why: str) -> dict:
    return {"state": "n/a", "seconds": None, "what": why, "ts": None, "date": None}


# --------------------------------------------------------------------------- measurements

CAPTURE_METRIC = re.compile(r"capture", re.IGNORECASE)
CAPTURE_EXCLUDE = re.compile(r"(null|_db|_mb|bytes|count|ok)", re.IGNORECASE)


def pool_group(rows: list[dict]) -> dict | None:
    """The newest Native render-pool scan (rows with metric pool_renders_per_s at one time):
    renders per second by host count, the speed-up of the biggest pool over one host, memory."""
    pool = [r for r in rows if r.get("engine") == "native" and r.get("metric") == "pool_renders_per_s"]
    if not pool:
        return None
    ts = max(r["timestamp"] for r in pool)
    grp = [r for r in pool if r["timestamp"] == ts]
    by_hosts: dict[int, tuple[float, dict]] = {}
    for r in grp:
        m = re.search(r"(\d+) hosts?", r.get("notes", ""))
        v = _f(r.get("value"))
        if m and v:
            by_hosts[int(m[1])] = (v, r)
    if 1 not in by_hosts or len(by_hosts) < 2:
        return None
    top = max(by_hosts)
    one_rate, one_row = by_hosts[1]
    top_rate, top_row = by_hosts[top]
    mem = re.search(r"total footprint (\d+) MB", top_row.get("notes", ""))
    clip = re.search(r"(\d+) s DI clip", one_row.get("notes", ""))
    return {"hosts": top, "speedup": top_rate / one_rate, "one_rate": one_rate, "top_rate": top_rate,
            "one_row": one_row, "top_row": top_row, "mem_gb": (int(mem[1]) / 1000) if mem else None,
            "clip_s": int(clip[1]) if clip else None}


def suite_info(rows: list[dict]) -> dict | None:
    """Newest benchmark-suite row: seconds per step and how many presets the parity step covered."""
    r = newest(rows, lambda x: x.get("kind") == "benchmark" and x.get("metric") == "suite_wall_clock_s")
    if r is None:
        return None
    steps = {m[1]: (m[2], float(m[3])) for m in re.finditer(
        r"(\w+) (pass|fail|skipped|not available) (\d+)s", r.get("notes", ""))}
    wall = _f(r.get("value")) or 0.0
    end = datetime.fromisoformat(r["timestamp"])
    presets = set()
    for p in rows:
        if p.get("kind") == "parity" and p.get("metric") == "null_residual_db" and p["timestamp"] <= r["timestamp"]:
            t = datetime.fromisoformat(p["timestamp"])
            if 0 <= (end - t).total_seconds() <= wall + 30:
                m = re.search(r"preset=([^;]+)", p.get("notes", ""))
                if m:
                    presets.add(m[1])
    return {"row": r, "steps": steps, "parity_presets": len(presets)}


def scan_match_reports(root: Path) -> list[dict]:
    """Local search-run reports (cache/matches/*/report.md, gitignored): renders, wall-clock and the
    cost of one score. Runs that were killed, aborted or failed are not typical and are skipped."""
    out = []
    base = root / "cache" / "matches"
    for p in sorted(base.glob("*/report.md")) if base.is_dir() else []:
        if re.search(r"failed|aborted|killed", p.parent.name, re.IGNORECASE):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        a = re.search(r"(\d+) renders[^\n]*? in (\d+) s wall clock", text)
        b = re.search(r"cost of one score on this machine: render ([\d.]+) s \+ loss ([\d.]+) s", text)
        req = re.search(r"requested (\d+) min", text)
        if not (a and b):
            continue
        out.append({"name": p.parent.name, "renders": int(a[1]), "wall_s": float(a[2]),
                    "render_s": float(b[1]), "loss_s": float(b[2]),
                    "budget_min": int(req[1]) if req else None,
                    "mtime": datetime.fromtimestamp(p.stat().st_mtime, UTC).strftime("%Y-%m-%d")})
    return out


def measure(rows: list[dict], root: Path | None = None) -> dict:
    """Every measured per-test time, by key then engine ('stomp', 'native', 'any')."""
    up = stomp_upgrade_ts(rows)
    T: dict[str, dict[str, dict]] = {}

    def put(key: str, eng: str, c: dict) -> None:
        T.setdefault(key, {})[eng] = c

    def rc(engine: str, metric: str):
        return newest(rows, lambda r: r.get("kind") == "render-cost" and r.get("engine") == engine
                      and r.get("metric") == metric and (_f(r.get("value")) or 0) > 0)

    # one render of a 2 s clip
    for eng in ("stomp", "native"):
        r = rc(eng, "seconds_per_render")
        put("render_2s", eng, cell(r, float(r["value"]), "render, 2 s clip", up) if r
            else missing("no seconds_per_render row"))

    # one render of a 10 s clip
    pool = pool_group(rows)
    cands = []
    r10 = rc("native", "seconds_per_10s_render")
    if r10:
        cands.append((r10["timestamp"], cell(r10, float(r10["value"]), "render, 10 s clip", up)))
    if pool and pool["clip_s"] == 10:
        o = pool["one_row"]
        cands.append((o["timestamp"], cell(o, 1.0 / pool["one_rate"],
                                          "render, 10 s clip + 3 s pre-roll, one host (1 / renders per s)", up)))
    put("render_10s", "native", max(cands, key=lambda t: t[0])[1] if cands
        else missing("no 10 s render row"))
    r10s = rc("stomp", "seconds_per_10s_render")
    put("render_10s", "stomp", cell(r10s, float(r10s["value"]), "render, 10 s clip", up) if r10s
        else missing("no Stomp 10 s render row"))

    # one score of a candidate (loss), independent of the engine
    sc = newest(rows, lambda r: r.get("metric") in ("seconds_per_score", "score_s", "loss_s_per_candidate")
                and (_f(r.get("value")) or 0) > 0)
    if sc:
        put("score", "any", cell(sc, float(sc["value"]), "score one candidate against a target", up))
    else:
        rep = scan_match_reports(root) if root else []
        if rep:
            last = max(rep, key=lambda x: (x["mtime"], x["name"]))
            put("score", "any", {"state": "measured", "seconds": last["loss_s"], "ts": None,
                                 "date": last["mtime"], "source": f"local match report {last['name']}",
                                 "what": "score one candidate against a target (not a ledger row)",
                                 "note": "from a local search-run report, not the ledger"})
        else:
            put("score", "any", missing("no scoring-time record"))

    # smoke and parity, from the newest benchmark-suite row
    suite = suite_info(rows)
    for eng, step in (("stomp", "smoke_stomp"), ("native", "smoke_native")):
        if suite and step in suite["steps"] and suite["steps"][step][0] == "pass":
            put("smoke", eng, cell(suite["row"], suite["steps"][step][1], f"engine smoke ({step})", up,
                                   engine=eng if eng == "stomp" else None,
                                   note="whole step of the benchmark suite"))
        else:
            put("smoke", eng, missing("no suite smoke step"))
    if suite and "parity" in suite["steps"] and suite["steps"]["parity"][0] == "pass" and suite["parity_presets"]:
        n = suite["parity_presets"]
        put("parity", "any", cell(suite["row"], suite["steps"]["parity"][1] / n,
                                  f"one preset on both engines + analysis (suite parity step / {n} presets)", up,
                                  engine="stomp", presets=n, suite_s=suite["steps"]["parity"][1]))
    else:
        put("parity", "any", missing("no suite parity step"))

    # a search run's wall-clock per render call
    for eng in ("stomp", "native"):
        r = newest(rows, lambda x, eng=eng: x.get("kind") == "render-cost" and x.get("engine") == eng
                   and x.get("metric") == "wall_per_render_call_s" and (_f(x.get("value")) or 0) > 0)
        if r:
            newer = [x for x in rows if x.get("kind") == "render-cost" and x.get("engine") == eng
                     and x.get("metric") == "seconds_per_render" and x["timestamp"] > r["timestamp"]]
            c = cell(r, float(r["value"]), "search run, wall-clock per render call (host start included)", up,
                     renders=r.get("renders"))
            if newer:
                c["note"] = (f"older than {len(newer)} newer render-cost rows, so probably overstated")
            put("search_run", eng, c)
        else:
            put("search_run", eng, missing("no search-run row"))

    # preset load or model change
    r = rc("stomp", "whole_preset_load_s")
    put("load", "stomp", cell(r, float(r["value"]), "write a whole generated preset to the pedal", up) if r
        else missing("no whole-preset load row"))
    r = rc("native", "template_load_s")
    if r:
        put("load", "native", cell(r, float(r["value"]), "load a stored template into the engine", up))
    else:
        r = rc("native", "hc_mode_switch_load_s")
        if r:
            m = re.search(r"same-mode load ([\d.]+) s", r.get("notes", ""))
            if m:
                put("load", "native", cell(r, float(m[1]), "load a stored template, same mode", up,
                                           alt_seconds=float(r["value"]),
                                           note=f"a mode change reloads the engine: {fmt_dur(float(r['value']))}"))
            else:
                put("load", "native", cell(r, float(r["value"]), "load a stored template (mode change)", up))
        else:
            put("load", "native", missing("no template-load row"))

    # first-time capture of a new chain (Native only; the pedal takes the model directly)
    put("capture", "stomp", not_needed("not needed: the pedal takes the model directly"))
    cap = newest(rows, lambda x: x.get("kind") == "render-cost" and x.get("engine") == "native"
                 and CAPTURE_METRIC.search(x.get("metric", "")) and not CAPTURE_EXCLUDE.search(x.get("metric", ""))
                 and (x.get("metric", "").endswith("_s") or "_s_" in x.get("metric", ""))
                 and (_f(x.get("value")) or 0) > 0)
    put("capture", "native", cell(cap, float(cap["value"]), f"capture a new chain once ({cap['metric']})", up)
        if cap else missing("no capture-time row"))

    # probe pass
    pr = newest(rows, lambda x: x.get("metric") == "probe_checks_pass_rate" and (_f(x.get("wall_clock_s")) or 0) > 0)
    put("probe_analysis", "any", cell(pr, float(pr["wall_clock_s"]),
                                      "analyse the probe set against known blocks (offline, no engine)", up)
        if pr else missing("no probe analysis row"))
    for eng in ("stomp", "native"):
        r = rc(eng, "probe_pass_s")
        put("probe_pass", eng, cell(r, float(r["value"]), "one probe pass through the engine", up) if r
            else missing("no engine probe-pass row"))

    # loss-trust check (proxy signals, no engine)
    lt = newest(rows, lambda x: x.get("kind") == "loss-trust" and (_f(x.get("wall_clock_s")) or 0) > 0)
    if lt:
        grp = [x for x in rows if x.get("kind") == "loss-trust" and x.get("wall_clock_s") == lt["wall_clock_s"]
               and x.get("dataset") == lt.get("dataset") and abs(
                   (datetime.fromisoformat(x["timestamp"]) - datetime.fromisoformat(lt["timestamp"])).total_seconds()) < 60]
        comps = 0
        for x in grp:
            it = re.search(r"items=(\d+)", x.get("notes", ""))
            di = re.search(r"distractors=(\d+)", x.get("notes", ""))
            if it and di:
                comps += int(it[1]) * (int(di[1]) + 1)
        put("loss_trust", "any", cell(lt, float(lt["wall_clock_s"]), "one loss-trust check run (stand-in signals)", up,
                                      comparisons=comps, rows=len(grp)))
    else:
        put("loss_trust", "any", missing("no loss-trust row"))

    # dual path throughput on the pedal
    d = rc("stomp", "dual_path_throughput_gain")
    put("dual_gain", "stomp", cell(d, float(d["value"]), "two chains per pass on the pedal (Path A + B)", up) if d
        else missing("no dual-path row"))
    if pool:
        put("pool", "native", cell(pool["top_row"], pool["speedup"], f"{pool['hosts']}-host render pool speed-up", up,
                                   hosts=pool["hosts"], mem_gb=pool["mem_gb"], clip_s=pool["clip_s"]))
    else:
        put("pool", "native", missing("no render-pool row"))
    T["_upgrade_ts"] = {"any": {"ts": up}}
    return T


# --------------------------------------------------------------------------- measured table

PLACEHOLDER_KEYS = ("render_2s", "render_10s", "score", "parity", "smoke", "search_run", "load",
                    "capture", "probe_pass", "probe_analysis", "loss_trust")
ALL_ACTIVITY_KEYS = ("render_2s", "render_10s", "score", "parity", "smoke", "search_run", "load",
                     "capture", "probe_pass", "probe_analysis", "trial", "combined_testing",
                     "combined_comparison", "combined_modelling")
JOB_IDS = ("match_section", "match_budget", "match_song", "verify_top3", "parity_one", "synth_ci",
           "synth_nightly", "synth_stomp_truth", "synth_stomp_sub", "synth_stomp_full", "loss_trust",
           "library", "library_packed", "probe_one", "probe_library", "probe_stomp")

ACTIVITIES = (
    # key, columns it applies to
    ("render_2s", ("stomp", "native")),
    ("render_10s", ("stomp", "native")),
    ("score", ("any",)),
    ("parity", ("any",)),
    ("smoke", ("stomp", "native")),
    ("search_run", ("stomp", "native")),
    ("load", ("stomp", "native")),
    ("capture", ("stomp", "native")),
    ("probe_pass", ("stomp", "native")),
    ("probe_analysis", ("any",)),
)


def cell_text(c: dict | None) -> str:
    """One measured cell as plain text. The ledger timestamp is left bare so the renderers link it."""
    if not c or c["state"] == "not-measured":
        return NOT_MEASURED
    if c["state"] == "n/a":
        return c["what"]
    parts = [f"{fmt_dur(c['seconds'])} (measured {c['date']}"]
    if c.get("ts"):
        parts[0] += f", ledger {c['ts']}"
    elif c.get("source") and c["source"] != "ledger":
        parts[0] += f", {c['source']}"
    parts[0] += ")"
    if c.get("fw"):
        parts.append(c["fw"])
    if c.get("note"):
        parts.append(c["note"])
    return "; ".join(parts)


def measured_rows(T: dict, labels: dict) -> list[dict]:
    out = []
    for key, _ in ACTIVITIES:
        lab = labels.get(key, {"label": key, "means": ""})
        out.append({"key": key, "label": lab["label"], "means": lab.get("means", ""),
                    "stomp": T.get(key, {}).get("stomp") or missing(),
                    "native": T.get(key, {}).get("native") or missing(),
                    "any": T.get(key, {}).get("any") or missing()})
    return out


# --------------------------------------------------------------------------- estimates

ASSUMPTIONS = {
    "pool10": "Native: several engine hosts at once (a render pool), speed-up measured with 10 s clips",
    "pool2": "Native: the same render-pool speed-up (measured with 10 s clips) is assumed to hold for 2 s clips",
    "poolscore": "Native: only the renders run in parallel; scoring stays one after another",
    "dual": "Stomp: two chains per pass (Path A + B) double throughput, but only when both sides of "
            "a comparison are two-chain renders; two-chain renders differ from single-chain ones",
    "serial": "One after another: no parallel speed-up is measured for this activity (the benchmark "
              "steps run in sequence and a parity check needs the single pedal)",
    "capserial": "Native captures stay one at a time: capturing several chains in parallel is not measured yet",
    "lowerbound": "Scoring time is not measured, so a total containing it is a lower bound",
}


def est(serial: float | None, parallel: float | None = None, assume: list[str] | None = None,
        dates: list[str] | None = None, lower: bool = False, comps: list[str] | None = None) -> dict:
    if serial is None:
        return {"state": "not-measured", "serial_s": None, "parallel_s": None, "assume": [], "date": None,
                "lower": False}
    return {"state": "estimated", "serial_s": serial, "parallel_s": parallel, "assume": assume or [],
            "date": min(dates) if dates else None, "lower": lower}


def _secs(c: dict | None) -> float | None:
    return c["seconds"] if c and c["state"] == "measured" else None


def est_text(c: dict, which: str) -> str:
    """'serial' or 'parallel' estimate cell text."""
    if c["state"] != "estimated":
        return NOT_MEASURED
    v = c["serial_s"] if which == "serial" else c["parallel_s"]
    if v is None:
        return NOT_MEASURED
    pre = "at least " if c.get("lower") else ""
    return f"{pre}{fmt_dur(v)} (est., from {c['date']})"


def estimate_rows(T: dict, n: int = N_TESTS) -> tuple[list[dict], list[dict]]:
    """Rows of 'n tests' estimates (stomp / native / any cells, each serial and parallel) and the
    numbered list of assumptions they use."""
    pool = T.get("pool", {}).get("native")
    speed = pool["seconds"] if pool and pool["state"] == "measured" else None
    dual = _secs(T.get("dual_gain", {}).get("stomp"))
    score = T.get("score", {}).get("any")
    sc = _secs(score)
    rows: list[dict] = []

    def date_of(*cs) -> list[str]:
        return [c["date"] for c in cs if c and c.get("date")]

    def simple(key: str, eng: str, par: float | None = None, assume=None) -> dict:
        c = T.get(key, {}).get(eng)
        s = _secs(c)
        if s is None:
            return est(None)
        return est(n * s, par, assume, date_of(c))

    # 2 s and 10 s renders
    for key in ("render_2s", "render_10s"):
        sv = _secs(T[key].get("stomp"))
        nv = _secs(T[key].get("native"))
        rows.append({"key": key, "kind": "activity",
                     "stomp": est(n * sv, n * sv / dual, ["dual"], date_of(T[key]["stomp"], T["dual_gain"].get("stomp")))
                     if sv is not None and dual else simple(key, "stomp"),
                     "native": est(n * nv, n * nv / speed, ["pool10" if key == "render_10s" else "pool2"],
                                   date_of(T[key]["native"], pool)) if nv is not None and speed else simple(key, "native"),
                     "any": est(None)})
    # one score
    rows.append({"key": "score", "kind": "activity", "stomp": est(None), "native": est(None),
                 "any": simple("score", "any", None, ["serial"] if sc is not None else None)})
    # one parity comparison
    rows.append({"key": "parity", "kind": "activity", "stomp": est(None), "native": est(None),
                 "any": simple("parity", "any", None, ["serial"])})
    rows.append({"key": "smoke", "kind": "activity", "stomp": simple("smoke", "stomp", None, ["serial"]),
                 "native": simple("smoke", "native", None, ["serial"]), "any": est(None)})

    # one search trial = render + score
    def trial(eng: str) -> dict:
        r = _secs(T["render_2s"].get(eng))
        if r is None:
            return est(None)
        lower = sc is None
        ser = n * (r + (sc or 0.0))
        par = None
        ass = ["lowerbound"] if lower else []
        if eng == "native" and speed:
            par = n * (r / speed + (sc or 0.0))
            ass += ["pool2"] + ([] if lower else ["poolscore"])
        if eng == "stomp" and dual:
            par = n * (r / dual + (sc or 0.0))
            ass += ["dual"]
        return est(ser, par, ass, date_of(T["render_2s"][eng], score if sc is not None else None), lower)
    rows.append({"key": "trial", "kind": "activity", "stomp": trial("stomp"), "native": trial("native"),
                 "any": est(None)})
    rows.append({"key": "search_run", "kind": "activity", "stomp": simple("search_run", "stomp"),
                 "native": simple("search_run", "native"), "any": est(None)})
    rows.append({"key": "load", "kind": "activity", "stomp": simple("load", "stomp"),
                 "native": simple("load", "native"), "any": est(None)})
    rows.append({"key": "capture", "kind": "activity", "stomp": est(None),
                 "native": simple("capture", "native", None, ["capserial"]), "any": est(None)})
    rows.append({"key": "probe_analysis", "kind": "activity", "stomp": est(None), "native": est(None),
                 "any": simple("probe_analysis", "any", None, ["serial"])})

    # combined lines the human asked for
    sm_s, sm_n, par = (_secs(T["smoke"].get("stomp")), _secs(T["smoke"].get("native")),
                       _secs(T["parity"].get("any")))
    tot = None if None in (sm_s, sm_n, par) else sm_s + sm_n + par
    rows.append({"key": "combined_testing", "kind": "combined",
                 "stomp": est(n * sm_s, None, ["serial"], date_of(T["smoke"]["stomp"])) if sm_s is not None else est(None),
                 "native": est(n * sm_n, None, ["serial"], date_of(T["smoke"]["native"])) if sm_n is not None else est(None),
                 "any": est(n * tot, None, ["serial"], date_of(T["smoke"]["stomp"], T["smoke"]["native"], T["parity"]["any"]))
                 if tot is not None else est(None)})
    rows.append({"key": "combined_comparison", "kind": "combined", "stomp": est(None), "native": est(None),
                 "any": simple("score", "any", None, ["serial"])})
    wr = _secs(T["load"].get("stomp"))
    cp = _secs(T["capture"].get("native"))
    rs, rn = _secs(T["render_2s"].get("stomp")), _secs(T["render_2s"].get("native"))
    lower = sc is None

    stomp_m = est(None)
    if rs is not None and wr is not None:
        stomp_m = est(n * (rs + wr + (sc or 0.0)),
                      n * (rs / dual + wr + (sc or 0.0)) if dual else None,
                      (["lowerbound"] if lower else []) + (["dual"] if dual else []),
                      date_of(T["render_2s"]["stomp"], T["load"]["stomp"], score if sc is not None else None), lower)
    native_m = est(None)
    if rn is not None and cp is not None:
        native_m = est(n * (rn + cp + (sc or 0.0)),
                       n * (rn / speed + cp + (sc or 0.0)) if speed else None,
                       (["lowerbound"] if lower else []) + (["pool2", "capserial"] if speed else []),
                       date_of(T["render_2s"]["native"], T["capture"]["native"], score if sc is not None else None), lower)
    rows.append({"key": "combined_modelling", "kind": "combined", "stomp": stomp_m, "native": native_m,
                 "any": est(None)})

    order: list[str] = []
    for r in rows:
        for col in ("stomp", "native", "any"):
            for a in r[col]["assume"]:
                if a not in order:
                    order.append(a)
    assumptions = [{"id": a, "n": i + 1, "text": ASSUMPTIONS[a]} for i, a in enumerate(order)]
    num = {a["id"]: a["n"] for a in assumptions}
    for r in rows:
        for col in ("stomp", "native", "any"):
            r[col]["assume_n"] = [num[a] for a in r[col]["assume"]]
    return rows, assumptions


# --------------------------------------------------------------------------- real-world jobs


def design_sizes(root: Path) -> dict[str, int]:
    """Render counts from the synthetic-benchmark design note's cost table, keyed by the row label."""
    p = root / "docs" / "notes" / "synthetic-benchmark-design.md"
    out: dict[str, int] = {}
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\|\s*([^|]+?)\s*\|\s*([\d,]+)\s*\|", line)
            if m and not m[1].startswith(("Step", "---")):
                out[m[1]] = int(m[2].replace(",", ""))
    return out


def library_size(root: Path) -> tuple[int, int] | None:
    p = root / "board" / "TASKS.md"
    if p.is_file():
        m = re.search(r'id="t048"></a>T048.*?\(~(\d[\d,]*), ~(\d+) s each\)', p.read_text(encoding="utf-8"))
        if m:
            return int(m[1].replace(",", "")), int(m[2])
    return None


def probe_audio_s(root: Path) -> float | None:
    p = root / "docs" / "notes" / "block-probe.md"
    if p.is_file():
        m = re.search(r"Duration: ([\d.]+) s with the DI excerpts", p.read_text(encoding="utf-8"))
        if m:
            return float(m[1])
    return None


def _size_lookup(sizes: dict[str, int], prefix: str) -> tuple[int, str] | None:
    for k, v in sizes.items():
        if k.startswith(prefix):
            return v, k
    return None


def job_rows(T: dict, root: Path, cfg: list[dict], rows: list[dict]) -> tuple[list[dict], str]:
    """The real-world jobs table and its one-paragraph takeaway."""
    sizes = design_sizes(root)
    lib = library_size(root)
    probe_s = probe_audio_s(root)
    reports = scan_match_reports(root)
    pool = T.get("pool", {}).get("native")
    speed = pool["seconds"] if pool and pool["state"] == "measured" else None
    sc = _secs(T["score"].get("any"))
    r10 = _secs(T["render_10s"].get("native"))
    r2n, r2s = _secs(T["render_2s"].get("native")), _secs(T["render_2s"].get("stomp"))
    wr = _secs(T["load"].get("stomp"))
    cap = _secs(T["capture"].get("native"))
    par = _secs(T["parity"].get("any"))
    pool_tag = f"{pool['hosts']} hosts" if pool and pool.get("hosts") else "a pool"
    up = {j["id"]: j for j in cfg}
    out: list[dict] = []

    def add(jid: str, engine: str, size: str | None, source: str, unit: str, serial: float | None,
            parallel: float | None, comps: dict[str, float | None], par_note: str = "", extra: str = "",
            lower: bool = False, dates: list[str] | None = None) -> None:
        j = up.get(jid, {"name": jid, "purpose": ""})
        dom = None
        known = {k: v for k, v in comps.items() if v}
        if known:
            dom = max(known, key=known.get)
        state = "estimated" if serial is not None else "not-measured"
        out.append({"id": jid, "name": j["name"], "purpose": j.get("purpose", ""), "engine": engine,
                    "size": size or NOT_MEASURED, "source": source, "unit": unit, "serial_s": serial,
                    "parallel_s": parallel, "par_note": par_note, "extra": extra, "lower": lower,
                    "dominant": dom, "dominant_s": known.get(dom) if dom else None,
                    "state": state, "date": min(dates) if dates else None})

    # ---- match one song section
    if reports:
        ren = [x["renders"] for x in reports]
        n = statistics.median(ren)
        budget_min = next((x["budget_min"] for x in reversed(reports) if x["budget_min"]), None)
        size = f"{fmt_count(n)} renders (median of {len(reports)} local runs, range {min(ren)} to {max(ren)})"
        src = "cache/matches/*/report.md (local, not in the repo)"
        w_old = statistics.median([x["wall_s"] for x in reports])
        r_old = statistics.median([x["render_s"] for x in reports])
        l_old = statistics.median([x["loss_s"] for x in reports])
        old = (f"earlier runs: {fmt_dur(r_old)} per render + {fmt_dur(l_old)} per score, "
               f"median wall-clock {fmt_dur(w_old)}")
    else:
        n, budget_min, size, src, old, w_old = None, None, None, "no local match reports found", "", None
    unit10 = f"render of a 10 s excerpt {fmt_dur(r10)} + score {fmt_dur(sc)}"
    dates10 = [d for d in (T["render_10s"]["native"].get("date"), T["score"]["any"].get("date")) if d]
    if n and r10 is not None:
        ser = n * (r10 + (sc or 0))
        pr = n * (r10 / speed + (sc or 0)) if speed else None
        add("match_section", "Native", size, src, unit10, ser, pr, {"render": n * r10, "score": n * (sc or 0)},
            f"{pool_tag} for the renders; scoring one after another" if speed else "", old,
            lower=sc is None, dates=dates10)
    else:
        add("match_section", "Native", size, src, unit10, None, None, {})
    if n and r10 is not None and budget_min:
        b = budget_min * 60.0
        per = r10 + (sc or 0)
        per_p = (r10 / speed + (sc or 0)) if speed else None
        add("match_budget", "Native", f"{budget_min} min budget buys {fmt_count(b / per)} renders"
            + (f" ({fmt_count(b / per_p)} with {pool_tag})" if per_p else ""),
            "the 25 min request in cache/matches/*/report.md (local)", unit10, b, b if per_p else None,
            {"render": b * r10 / per, "score": b * (sc or 0) / per}, "", old, lower=sc is None, dates=dates10)
    else:
        add("match_budget", "Native", None, "no local match reports found", unit10, None, None, {})
    # whole song, up to 3 snapshot sections
    if n and r10 is not None:
        ser = 3 * n * (r10 + (sc or 0))
        pr = 3 * n * (r10 / speed + (sc or 0)) if speed else None
        add("match_song", "Native", f"3 sections x {fmt_count(n)} renders = {fmt_count(3 * n)}",
            "docs/PLAN.md (up to 3 tones) times the section size above", unit10, ser, pr,
            {"render": 3 * n * r10, "score": 3 * n * (sc or 0)},
            f"{pool_tag} for the renders; scoring one after another" if speed else "",
            "upper bound: the one real run searched one section in full and refined another briefly",
            lower=sc is None, dates=dates10)
    else:
        add("match_song", "Native", None, "no local match reports found", unit10, None, None, {})
    # verify the top 3 on the pedal
    add("verify_top3", "Stomp (and Native)", "3 candidates, one parity-style check each",
        "scripts/match_song.py (final check of the top 3)", f"one preset on both engines + analysis {fmt_dur(par)}",
        3 * par if par is not None else None, None, {"engines + analysis": 3 * par if par else None},
        "", "includes the whole write to the pedal and a Native render", dates=[T["parity"]["any"].get("date")]
        if par is not None else None)
    # one parity check
    add("parity_one", "Stomp + Native", "1 preset", "benchmark suite parity step", f"{fmt_dur(par)} per preset",
        par, None, {"engines + analysis": par}, "", "", dates=[T["parity"]["any"].get("date")] if par is not None else None)

    # ---- synthetic benchmark
    def synth(jid: str, eng: str, prefix: str, scale: int = 1, scale_why: str = "") -> None:
        got = _size_lookup(sizes, prefix)
        r = r2n if eng == "Native" else r2s
        if got is None or r is None:
            add(jid, eng, None, "docs/notes/synthetic-benchmark-design.md (cost table)", "", None, None, {})
            return
        count, label = got
        count *= scale
        extra_w = (wr or 0.0) if eng == "Stomp" else 0.0
        per = r + extra_w + (sc or 0.0)
        unit = (f"render {fmt_dur(r)}" + (f" + whole write {fmt_dur(extra_w)}" if eng == "Stomp" else "")
                + f" + score {fmt_dur(sc)}")
        ser = count * per
        pr = count * (r / speed + (sc or 0.0)) if (eng == "Native" and speed) else None
        d = [x for x in (T["render_2s"]["native" if eng == "Native" else "stomp"].get("date"),) if x]
        paren = re.search(r"\(([^)]*)\)", label)
        what = paren[1] if paren and paren[1][:1].isdigit() else label.split(" (")[0]
        add(jid, eng, f"{fmt_count(count)} renders ({what}{scale_why})",
            "docs/notes/synthetic-benchmark-design.md (cost table)", unit, ser, pr,
            {"render": count * r, "write": count * extra_w, "score": count * (sc or 0.0)},
            f"{pool_tag} for the renders; scoring one after another" if pr else "",
            ("assumes every Stomp trial includes one whole write" if eng == "Stomp"
             else "scoring time is from the 10 s excerpt, so likely high for short clips"),
            lower=sc is None, dates=d)

    synth("synth_ci", "Native", "Native CI run")
    synth("synth_nightly", "Native", "Native full recovery")
    synth("synth_stomp_truth", "Stomp", "Stomp truth")
    synth("synth_stomp_sub", "Stomp", "Stomp recovery, 10-item subset")
    synth("synth_stomp_full", "Stomp", "Stomp recovery, 10-item subset", 6, "; x6 for 60 items")

    # ---- loss trust
    lt = T["loss_trust"]["any"]
    if lt["state"] == "measured":
        comps = lt.get("comparisons") or 0
        add("loss_trust", "none (stand-in signals)",
            f"up to {fmt_count(comps)} comparisons ({lt.get('rows', 0)} term-by-subset rows x items x candidates)" if comps
            else "size not recorded", "ledger loss-trust notes",
            f"{fmt_dur(lt['seconds'])} measured for the whole run", lt["seconds"], None, {"scoring": lt["seconds"]},
            "", (f"about {fmt_dur(lt['seconds'] / comps)} per comparison, an upper bound" if comps else ""),
            dates=[lt["date"]])
    else:
        add("loss_trust", "none (stand-in signals)", None, "ledger loss-trust notes", "", None, None, {})

    # ---- block library
    if lib and cap is not None:
        count, est_s = lib
        add("library", "Native", f"{fmt_count(count)} models, one capture each",
            "board/TASKS.md T048", f"{fmt_dur(cap)} per capture (T048 planned {est_s} s)", count * cap, None,
            {"capture": count * cap}, "parallel capture: not measured", "", dates=[T["capture"]["native"]["date"]])
        packed = math.ceil(count / 8)
        add("library_packed", "Native", f"{fmt_count(packed)} captures if 8 blocks share a capture",
            "docs/DECISIONS.md (block bypass over MIDI CC: up to 8 blocks per capture)",
            f"{fmt_dur(cap)} per capture", packed * cap, None, {"capture": packed * cap},
            "parallel capture: not measured", "proposed packing, not yet used for the library",
            dates=[T["capture"]["native"]["date"]])
    else:
        add("library", "Native", None, "board/TASKS.md T048", "", None, None, {})
        add("library_packed", "Native", None, "board/TASKS.md T048", "", None, None, {})

    # ---- probing blocks
    if probe_s and r10 is not None:
        per_audio = r10 / ((pool.get("clip_s") or 10) + 3) if pool else r10 / 13.0
        one = (probe_s + 3) * per_audio
        add("probe_one", "Native", f"1 pass of {probe_s:g} s of audio per settings point, number of settings "
            "points not yet defined", "docs/notes/block-probe.md (duration)",
            f"estimated {fmt_dur(one)} per pass: the measured 10 s render scaled by audio length",
            one, one / speed if speed else None, {"render": one},
            f"{pool_tag}" if speed else "", "per settings point; a probe pass through an engine is not measured",
            dates=[T["render_10s"]["native"]["date"]] if T["render_10s"]["native"].get("date") else None)
        if lib:
            count = lib[0]
            add("probe_library", "Native", f"{fmt_count(count)} blocks x 1 settings point (illustration: the real "
                "grid is not defined)", "board/TASKS.md T048 and T047", f"{fmt_dur(one)} per pass", count * one,
                count * one / speed if speed else None, {"render": count * one},
                f"{pool_tag}" if speed else "", "multiply by the number of settings points",
                dates=[T["render_10s"]["native"]["date"]] if T["render_10s"]["native"].get("date") else None)
        else:
            add("probe_library", "Native", None, "board/TASKS.md T048", "", None, None, {})
    else:
        add("probe_one", "Native", None, "docs/notes/block-probe.md", "", None, None, {})
        add("probe_library", "Native", None, "board/TASKS.md T048", "", None, None, {})
    add("probe_stomp", "Stomp", "1 pass per settings point", "docs/notes/block-probe.md",
        "no Stomp render of a long clip is measured", None, None, {})

    ratio = (cap, cap / r10) if cap and r10 else None
    return out, takeaway(out, ratio)


DOM_LABEL = {"render": "rendering", "score": "scoring", "write": "writing presets to the pedal",
             "capture": "capturing chains", "engines + analysis": "the two engines and the analysis",
             "scoring": "scoring"}


def takeaway(jobs: list[dict], cap_ratio: tuple[float, float] | None = None) -> str:
    timed = [j for j in jobs if j["serial_s"] is not None and j["id"] not in ("match_budget", "parity_one")]
    untimed = [j for j in jobs if j["serial_s"] is None]
    if not timed:
        return "No job could be timed yet: the ledger does not hold the measurements."
    timed.sort(key=lambda j: -j["serial_s"])
    a = timed[0]
    s = (f"The longest job today, one after another, is \"{a['name']}\" at {fmt_dur(a['serial_s'])}"
         + (f", most of it {DOM_LABEL.get(a['dominant'], a['dominant'])} ({fmt_dur(a['dominant_s'])})"
            if a.get("dominant") else ""))
    if len(timed) > 1:
        b = timed[1]
        s += f"; next is \"{b['name']}\" at {fmt_dur(b['serial_s'])}"
    s += "."
    par = [j for j in timed if j["parallel_s"] is not None]
    if par:
        p = max(par, key=lambda j: j["serial_s"] - j["parallel_s"])
        s += (f" Parallel Native hosts cut \"{p['name']}\" from {fmt_dur(p['serial_s'])} to "
              f"{fmt_dur(p['parallel_s'])}; the Stomp has no such speed-up beyond two chains per pass.")
    if cap_ratio:
        s += (f" For Native, capturing one new chain ({fmt_dur(cap_ratio[0])}) now costs as much as about "
              f"{cap_ratio[1]:.0f} renders of a 10 s excerpt, so captures, not renders, set the pace of "
              "anything that needs new chains.")
    if untimed:
        k = len(untimed)
        s += f" {k} job{'s have' if k != 1 else ' has'} no measurement yet and show \"{NOT_MEASURED}\"."
    if any(j["lower"] for j in timed):
        s += " Totals that include scoring are lower bounds while scoring time has no ledger record."
    return s


# --------------------------------------------------------------------------- the engines section


def fill_placeholders(text: str, T: dict) -> str:
    """{t:render_2s:stomp} -> the current measured time, so verdicts never carry stale numbers."""
    def sub(m: re.Match) -> str:
        c = T.get(m[1], {}).get(m[2])
        return fmt_dur(c["seconds"]) if c and c["state"] == "measured" else NOT_MEASURED
    return re.sub(r"\{t:(\w+):(\w+)\}", sub, text)


def build(cfg: dict, ledger: list[dict], root: Path | None) -> dict:
    rows = index_rows(ledger)
    T = measure(rows, root)
    est_rows, assumptions = estimate_rows(T)
    labels = cfg.get("activities", {})
    caps = []
    for c in cfg.get("capabilities", []):
        item = {"capability": c["capability"], "both": None, "stomp": None, "native": None}
        if c.get("both"):
            item["both"] = {"verdict": fill_placeholders(c["both"]["verdict"], T),
                            "fw350": bool(c["both"].get("fw350"))}
        else:
            for eng in ("stomp", "native"):
                item[eng] = {"verdict": fill_placeholders(c[eng]["verdict"], T),
                             "fw350": bool(c[eng].get("fw350"))}
        caps.append(item)
    jobs, take = job_rows(T, root or Path("."), cfg.get("jobs", []), rows)
    up = T["_upgrade_ts"]["any"]["ts"]
    return {
        "n": N_TESTS, "fw_note": FW_NOTE, "upgrade_ts": up,
        "capabilities": caps,
        "measured": measured_rows(T, labels),
        "estimates": est_rows, "assumptions": assumptions,
        "labels": {k: labels.get(k, {"label": k, "means": ""}) for k in
                   [r["key"] for r in est_rows]},
        "jobs": jobs, "takeaway": take,
        "units": cfg.get("units", ""),
        "pool": ({"hosts": T["pool"]["native"].get("hosts"), "speedup": T["pool"]["native"]["seconds"],
                  "mem_gb": T["pool"]["native"].get("mem_gb"), "ts": T["pool"]["native"]["ts"]}
                 if T.get("pool", {}).get("native", {}).get("state") == "measured" else None),
    }
