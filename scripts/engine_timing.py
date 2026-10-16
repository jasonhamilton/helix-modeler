"""Engine timing for the status report: how long each activity really takes, per engine.

Everything here is derived from the project's own records every time the report is built, so new
ledger rows show up on the next run without anyone editing a table:

  - measured time per test: the NEWEST ledger row for each activity (ledger/runs.csv), with its
    timestamp and metric so the report can link to the row (a score time is only taken from the
    ledger; local search-run reports are never used for a measured time);
  - Native pool figures are tests per second (a test is a render plus its score) at the host count
    the human approved on 2026-10-16 (DEFAULT_HOSTS = 6); Stomp speed figures are the T061 rows
    (dataset stomp-speed) measured on firmware 3.80;
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
FW_380 = "fw 3.80"
# The human approved a cap of 6 Native hosts on 2026-10-16 (docs/DECISIONS.md), so the pool figures
# used everywhere are the 6-host ones; larger pools in the ledger are shown nowhere as the default.
DEFAULT_HOSTS = 6
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
         "line": r["_line"], "date": r["timestamp"][:10], "source": "ledger", "note": "",
         "metric": r.get("metric", "")}
    if eng == "stomp":
        if upgrade_ts is None:
            c["fw"] = FW_NOTE_NO_UPGRADE
        elif r["timestamp"] < upgrade_ts:
            c["fw"] = FW_NOTE
        else:
            c["fw"] = FW_380
    c.update(kw)
    return c


def missing(why: str = "") -> dict:
    return {"state": "not-measured", "seconds": None, "what": why, "ts": None, "date": None}


def not_needed(why: str) -> dict:
    return {"state": "n/a", "seconds": None, "what": why, "ts": None, "date": None}


# --------------------------------------------------------------------------- measurements

CAPTURE_METRIC = re.compile(r"capture", re.IGNORECASE)
CAPTURE_EXCLUDE = re.compile(r"(null|_db|_mb|bytes|count|ok)", re.IGNORECASE)


POOL_METRICS = ("renders_per_s", "pool_renders_per_s")
RATE_IN_NOTES = re.compile(r"(\d+) s (?:DI )?clip(?: \+ \d+ s pre-?roll)? ([\d.]+) tests/s")


def pool_points(rows: list[dict]) -> dict[tuple[int, int], dict]:
    """Newest tests-per-second figure for each (hosts, clip seconds) in the Native pool sweeps.
    Two row layouts exist: one row per clip length (value = that clip's rate) and one row per host
    count with both rates written in the notes. A 'test' is a render plus its score when the notes
    say so (score=True)."""
    pts: dict[tuple[int, int], dict] = {}

    def keep(key: tuple[int, int], rate: float, r: dict) -> None:
        old = pts.get(key)
        if old is not None and (old["row"]["timestamp"], old["row"]["_line"]) > (r["timestamp"], r["_line"]):
            return
        mem = re.search(r"(?:total footprint (\d+) MB|footprint (\d+) MB total)", r.get("notes", ""))
        pts[key] = {"rate": rate, "row": r, "mem_mb": int(mem[1] or mem[2]) if mem else None,
                    "score": "score" in r.get("notes", "")}

    for r in rows:
        if r.get("engine") != "native" or r.get("metric") not in POOL_METRICS:
            continue
        notes = r.get("notes", "")
        h = re.search(r"(\d+) hosts?", notes)
        v = _f(r.get("value"))
        if not h or not v:
            continue
        pairs = RATE_IN_NOTES.findall(notes)
        if pairs:
            for clip, rate in pairs:
                keep((int(h[1]), int(clip)), float(rate), r)
        else:
            c = re.search(r"(\d+) s DI clip", notes)
            if c:
                keep((int(h[1]), int(c[1])), v, r)
    return pts


def pool_group(rows: list[dict], hosts: int = DEFAULT_HOSTS) -> dict | None:
    """The Native render pool at the approved host count (default 6, or the biggest pool at or below
    it): per clip length the tests per second at one host and at that pool size, the speed-up and the
    memory. The headline fields describe the 10 s clip when there is one (older callers), and
    `clips` holds every clip length."""
    pts = pool_points(rows)
    clips: dict[int, dict] = {}
    for clip in sorted({c for _, c in pts}):
        have = {h: p for (h, c), p in pts.items() if c == clip}
        cands = [h for h in have if 1 < h <= hosts]
        if 1 not in have or not cands:
            continue
        top = max(cands)
        one, tp = have[1], have[top]
        clips[clip] = {"hosts": top, "one_rate": one["rate"], "top_rate": tp["rate"],
                       "speedup": tp["rate"] / one["rate"], "one_row": one["row"], "top_row": tp["row"],
                       "mem_gb": (tp["mem_mb"] / 1000) if tp["mem_mb"] else None, "score": tp["score"]}
    if not clips:
        return None
    main = 10 if 10 in clips else max(clips)
    m = clips[main]
    return dict(m, clip_s=main, clips=clips)


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

    def rc(engine: str, *metrics: str):
        return newest(rows, lambda r: r.get("kind") == "render-cost" and r.get("engine") == engine
                      and r.get("metric") in metrics and (_f(r.get("value")) or 0) > 0)

    # one render of a 2 s clip (the newest row of either the smoke metric or the clip-length metric)
    for eng in ("stomp", "native"):
        r = rc(eng, "seconds_per_render", "seconds_per_render_clip2s")
        put("render_2s", eng, cell(r, float(r["value"]), "render, 2 s clip", up) if r
            else missing("no seconds_per_render row"))

    # one render of a 10 s clip
    pool = pool_group(rows)
    p10 = (pool or {}).get("clips", {}).get(10)
    cands = []
    r10 = rc("native", "seconds_per_10s_render")
    if r10:
        cands.append((r10["timestamp"], cell(r10, float(r10["value"]), "render, 10 s clip", up)))
    if p10:
        o = p10["one_row"]
        cands.append((o["timestamp"], cell(o, 1.0 / p10["one_rate"],
                                          "one test, 10 s clip + 3 s pre-roll, one host (1 / tests per s)"
                                          + (", render and score" if p10["score"] else ""), up,
                                          has_score=p10["score"],
                                          note=("render and score together, 3 s pre-roll included"
                                                if p10["score"] else "3 s pre-roll included"))))
    put("render_10s", "native", max(cands, key=lambda t: t[0])[1] if cands
        else missing("no 10 s render row"))
    r10s = rc("stomp", "seconds_per_10s_render", "seconds_per_render_clip10s")
    put("render_10s", "stomp", cell(r10s, float(r10s["value"]), "render, 10 s clip", up) if r10s
        else missing("no Stomp 10 s render row"))

    # the search test: Native = one render-pool test (render + score) at the approved host count,
    # Stomp = one search trial with the T061 speed options (render side only)
    for clip, key in ((2, "search_test"), (10, "search_test_10s")):
        pc = (pool or {}).get("clips", {}).get(clip)
        if pc:
            one_s = 1.0 / pc["one_rate"]
            put(key, "native", cell(
                pc["top_row"], 1.0 / pc["top_rate"],
                f"one search test through {pc['hosts']} hosts, {clip} s clip + 3 s pre-roll"
                + (", render and score" if pc["score"] else ", render only"), up,
                hosts=pc["hosts"], rate=pc["top_rate"], one_seconds=one_s, has_score=pc["score"],
                note=f"{pc['top_rate']:.1f} tests per second; one host {fmt_dur(one_s)} "
                     f"({pc['one_rate']:.2f} per second, ledger {pc['one_row']['timestamp']})"))
        else:
            put(key, "native", missing("no render-pool row for this clip length"))

    def trial(which: str, chain: str = "K"):
        return newest(rows, lambda r: r.get("engine") == "stomp" and r.get("kind") == "render-cost"
                      and r.get("metric") == f"search_batch_per_trial_s_{which}"
                      and (_f(r.get("value")) or 0) > 0 and re.search(rf"trials on {chain};", r.get("notes", "")))
    after, before, fixed = trial("after"), trial("before"), trial("after", "Cdly0")
    if after:
        note = "render side only (scoring not included); amp-parameter trials, readback every 10 loads"
        if before:
            note += f"; before the T061 speed options {fmt_dur(float(before['value']))} (10 s clip, ledger {before['timestamp']})"
        if fixed:
            note += f"; with a fixed delay in the chain {fmt_dur(float(fixed['value']))} (ledger {fixed['timestamp']})"
        put("search_test", "stomp", cell(after, float(after["value"]),
                                         "one search trial on the pedal, 3 s clip, T061 speed options", up, note=note))
    else:
        put("search_test", "stomp", missing("no stomp-speed search-trial row"))
    if before:
        put("search_test_10s", "stomp", cell(before, float(before["value"]),
                                             "one search trial on the pedal, 10 s clip, before the T061 speed options",
                                             up, note="render side only (scoring not included)"))
    else:
        put("search_test_10s", "stomp", missing("no stomp-speed search-trial row"))

    # the Stomp session overhead (once per batch since T061) and a verification finalist
    so = rc("stomp", "session_overhead_per_test_new_s")
    so_old = rc("stomp", "session_overhead_per_test_old_s")
    put("session", "stomp", cell(so, float(so["value"]), "engine open, restore and byte-check, once per batch", up,
                                 note=(f"before: {fmt_dur(float(so_old['value']))} per test "
                                       f"(ledger {so_old['timestamp']})" if so_old else ""))
        if so else missing("no session-overhead row"))
    put("session", "native", not_needed("not applicable: no pedal session"))
    vf = rc("stomp", "verify_batch_per_finalist_s_after")
    vf_old = rc("stomp", "verify_batch_per_finalist_s_before")
    put("verify", "stomp", cell(vf, float(vf["value"]), "verify one finalist: whole write + two 10 s renders", up,
                                note=(f"before: {fmt_dur(float(vf_old['value']))} (ledger {vf_old['timestamp']})"
                                      if vf_old else ""))
        if vf else missing("no verification row"))
    put("verify", "native", not_needed("not applicable: verification runs on the pedal"))

    # one score of a candidate (loss), independent of the engine
    sc = newest(rows, lambda r: r.get("metric") in ("seconds_per_score", "score_s", "loss_s_per_candidate")
                and (_f(r.get("value")) or 0) > 0)
    if sc:
        put("score", "any", cell(sc, float(sc["value"]), "score one candidate against a target", up))
    else:
        # no ledger row, no number: a local search-run report is not evidence
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
    r = rc("stomp", "whole_preset_load_s", "whole_preset_write_s")
    wl = re.search(r"whole load\(\) median ([\d.]+) s", r.get("notes", "")) if r else None
    if r and r["metric"] == "whole_preset_write_s" and wl:
        # the newest row is a write timing whose notes carry the whole-load median (fw 3.80)
        put("load", "stomp", cell(r, float(wl[1]), "load a whole generated preset to the pedal", up,
                                  note=f"the write alone takes {fmt_dur(float(r['value']))}"))
    else:
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
    caps = [x for x in rows if x.get("kind") == "render-cost" and x.get("engine") == "native"
            and CAPTURE_METRIC.search(x.get("metric", "")) and not CAPTURE_EXCLUDE.search(x.get("metric", ""))
            and (x.get("metric", "").endswith("_s") or "_s_" in x.get("metric", ""))
            and (_f(x.get("value")) or 0) > 0]
    cap = max(caps, key=lambda r: (r["timestamp"], r["_line"]), default=None)
    if cap:
        notes = cap.get("notes", "")
        sess = re.search(r"(\d+) parallel", notes)
        verify_off = "--verify none" in notes
        bits = []
        if sess:
            bits.append(f"{sess[1]} editor sessions in parallel")
        if verify_off:
            bits.append("parameter checks switched off (refusal scan)")
        # the newest figure of every other capture dataset, so a cheap unchecked run is not the only number
        others = {}
        for x in caps:
            if x.get("dataset") != cap.get("dataset") and "per_chain" in x.get("metric", "") and (
                    x["dataset"] not in others or (x["timestamp"], x["_line"]) > (
                        others[x["dataset"]]["timestamp"], others[x["dataset"]]["_line"])):
                others[x["dataset"]] = x
        for ds, x in sorted(others.items(), key=lambda kv: kv[1]["timestamp"], reverse=True)[:2]:
            bits.append(f"{fmt_dur(float(x['value']))} per chain in {ds} (ledger {x['timestamp']})")
        put("capture", "native", cell(cap, float(cap["value"]), f"capture a new chain once ({cap['metric']})", up,
                                      sessions=int(sess[1]) if sess else None, verify_off=verify_off,
                                      note="; ".join(bits)))
    else:
        put("capture", "native", missing("no capture-time row"))

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
                                   hosts=pool["hosts"], mem_gb=pool["mem_gb"], clip_s=pool["clip_s"],
                                   clips=pool["clips"]))
    else:
        put("pool", "native", missing("no render-pool row"))
    T["_upgrade_ts"] = {"any": {"ts": up}}
    return T


# --------------------------------------------------------------------------- measured table

PLACEHOLDER_KEYS = ("render_2s", "render_10s", "score", "parity", "smoke", "search_run", "load",
                    "capture", "probe_pass", "probe_analysis", "loss_trust", "search_test",
                    "search_test_10s", "session", "verify")
ALL_ACTIVITY_KEYS = ("render_2s", "render_10s", "search_test", "search_test_10s", "session", "verify",
                     "score", "parity", "smoke", "search_run", "load",
                     "capture", "probe_pass", "probe_analysis", "trial", "combined_testing",
                     "combined_comparison", "combined_modelling")
JOB_IDS = ("match_section", "match_budget", "match_song", "verify_top3", "parity_one", "synth_ci",
           "synth_nightly", "synth_stomp_truth", "synth_stomp_sub", "synth_stomp_full", "loss_trust",
           "library", "library_packed", "probe_one", "probe_library", "probe_stomp")

ACTIVITIES = (
    # key, columns it applies to
    ("render_2s", ("stomp", "native")),
    ("render_10s", ("stomp", "native")),
    ("search_test", ("stomp", "native")),
    ("search_test_10s", ("stomp", "native")),
    ("session", ("stomp",)),
    ("verify", ("stomp",)),
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
        parts[0] += f", ledger {c['ts']}" + (f" {c['metric']}" if c.get("metric") else "")
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
    "pool10": "Native: {hosts} engine hosts at once (a render pool; the human approved a cap of 6 hosts on "
              "2026-10-16), speed-up measured with 10 s clips as tests per second",
    "pool2": "Native: {hosts} engine hosts at once (a render pool; the human approved a cap of 6 hosts on "
             "2026-10-16), speed-up measured with 2 s clips as tests per second",
    "pool2assumed": "Native: the render-pool speed-up (measured with 10 s clips) is assumed to hold for 2 s clips",
    "pooltest": "Native: a search test is a render plus its score; the pool figures count both together "
                "(tests per second, 3 s pre-roll included), so no separate scoring time is added",
    "poolscore": "Native: only the renders run in parallel; scoring stays one after another",
    "dual": "Stomp: two chains per pass (Path A + B) double throughput. The gain of 2.0 was measured on fw 3.50; "
            "the dual-path-only comparison rule was lifted on 2026-10-16 after the T063 layout fix (ledger "
            "2026-10-16T01:06:38+00:00), but the gain is not re-measured on fw 3.80",
    "session": "Stomp: one engine session serves the whole batch, so the session overhead is paid once, "
               "not once per test (T061)",
    "serial": "One after another: no parallel speed-up is measured for this activity (the benchmark "
              "steps run in sequence and a parity check needs the single pedal)",
    "capserial": "Native captures are counted one after another at the per-chain time in the newest ledger row",
    "capparallel": "Native captures: the per-chain time is the wall-clock of {sessions} editor sessions running "
                   "in parallel divided by the chains captured{checks}",
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
    pool_clips = (pool or {}).get("clips", {}) if speed else {}
    hosts = (pool or {}).get("hosts") if speed else None
    dual = _secs(T.get("dual_gain", {}).get("stomp"))
    score = T.get("score", {}).get("any")
    sc = _secs(score)
    sess = _secs(T.get("session", {}).get("stomp"))
    rows: list[dict] = []

    def speed_for(clip: int) -> tuple[float | None, str]:
        """Pool speed-up for a clip length, and the assumption that explains it."""
        if clip in pool_clips:
            return pool_clips[clip]["speedup"], f"pool{clip}"
        return speed, ("pool10" if clip == 10 else "pool2assumed")

    def date_of(*cs) -> list[str]:
        return [c["date"] for c in cs if c and c.get("date")]

    def simple(key: str, eng: str, par: float | None = None, assume=None) -> dict:
        c = T.get(key, {}).get(eng)
        s = _secs(c)
        if s is None:
            return est(None)
        return est(n * s, par, assume, date_of(c))

    # 2 s and 10 s renders
    for key, clip in (("render_2s", 2), ("render_10s", 10)):
        sv = _secs(T[key].get("stomp"))
        nv = _secs(T[key].get("native"))
        sp, why = speed_for(clip)
        rows.append({"key": key, "kind": "activity",
                     "stomp": est(n * sv, n * sv / dual, ["dual"], date_of(T[key]["stomp"], T["dual_gain"].get("stomp")))
                     if sv is not None and dual else simple(key, "stomp"),
                     "native": est(n * nv, n * nv / sp, [why],
                                   date_of(T[key]["native"], pool)) if nv is not None and sp else simple(key, "native"),
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
        if eng == "native":
            st = T.get("search_test", {}).get("native")
            if st and st["state"] == "measured" and st.get("has_score"):
                return est(n * st["one_seconds"], n * st["seconds"], ["pooltest", "pool2"], date_of(st))
        if eng == "stomp":
            st = T.get("search_test", {}).get("stomp")
            t = _secs(st)
            if t is not None:
                lower = sc is None
                ser = n * (t + (sc or 0.0)) + (sess or 0.0)
                par = (n * (t / dual + (sc or 0.0)) + (sess or 0.0)) if dual else None
                ass = (["lowerbound"] if lower else []) + (["session"] if sess else []) + (["dual"] if dual else [])
                return est(ser, par, ass, date_of(st, T.get("session", {}).get("stomp"),
                                                  score if sc is not None else None), lower)
        r = _secs(T["render_2s"].get(eng))
        if r is None:
            return est(None)
        lower = sc is None
        ser = n * (r + (sc or 0.0))
        par = None
        ass = ["lowerbound"] if lower else []
        if eng == "native" and speed:
            par = n * (r / speed + (sc or 0.0))
            ass += [speed_for(2)[1]] + ([] if lower else ["poolscore"])
        if eng == "stomp" and dual:
            par = n * (r / dual + (sc or 0.0))
            ass += ["dual"]
        return est(ser, par, ass, date_of(T["render_2s"][eng], score if sc is not None else None), lower)
    rows.append({"key": "trial", "kind": "activity", "stomp": trial("stomp"), "native": trial("native"),
                 "any": est(None)})
    vf = _secs(T.get("verify", {}).get("stomp"))
    rows.append({"key": "verify", "kind": "activity",
                 "stomp": est(n * vf + (sess or 0.0), None, ["session"] if sess else [],
                              date_of(T["verify"]["stomp"], T.get("session", {}).get("stomp")))
                 if vf is not None else est(None), "native": est(None), "any": est(None)})
    rows.append({"key": "search_run", "kind": "activity", "stomp": simple("search_run", "stomp"),
                 "native": simple("search_run", "native"), "any": est(None)})
    rows.append({"key": "load", "kind": "activity", "stomp": simple("load", "stomp"),
                 "native": simple("load", "native"), "any": est(None)})
    capc = T.get("capture", {}).get("native") or {}
    cap_assume = "capparallel" if capc.get("sessions") else "capserial"
    rows.append({"key": "capture", "kind": "activity", "stomp": est(None),
                 "native": simple("capture", "native", None, [cap_assume]), "any": est(None)})
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
                       n * (rn / speed_for(2)[0] + cp + (sc or 0.0)) if speed else None,
                       (["lowerbound"] if lower else []) + ([speed_for(2)[1], cap_assume] if speed else []),
                       date_of(T["render_2s"]["native"], T["capture"]["native"], score if sc is not None else None), lower)
    rows.append({"key": "combined_modelling", "kind": "combined", "stomp": stomp_m, "native": native_m,
                 "any": est(None)})

    order: list[str] = []
    for r in rows:
        for col in ("stomp", "native", "any"):
            for a in r[col]["assume"]:
                if a not in order:
                    order.append(a)
    checks = ", with the per-parameter checks switched off" if capc.get("verify_off") else ""
    fmt = {"hosts": hosts or DEFAULT_HOSTS, "sessions": capc.get("sessions") or 1, "checks": checks}
    assumptions = [{"id": a, "n": i + 1, "text": ASSUMPTIONS[a].format(**fmt)} for i, a in enumerate(order)]
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
    sc_ledger = _secs(T["score"].get("any"))
    r10 = _secs(T["render_10s"].get("native"))
    r10_scored = bool(T["render_10s"].get("native", {}).get("has_score"))
    sc = sc_ledger
    sc10 = 0.0 if r10_scored else sc_ledger   # a pool test already contains its score
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
    par_txt = ((f"{pool_tag}; render and score together" if r10_scored else f"{pool_tag} for the renders; scoring one after another") if speed else "")
    unit10 = (f"one test (render + score) of a 10 s excerpt, one host, {fmt_dur(r10)}" if r10_scored
              else f"render of a 10 s excerpt {fmt_dur(r10)} + score {fmt_dur(sc10)}")
    dates10 = [d for d in (T["render_10s"]["native"].get("date"), T["score"]["any"].get("date")) if d]
    if n and r10 is not None:
        ser = n * (r10 + (sc10 or 0))
        pr = n * (r10 / speed + (sc10 or 0)) if speed else None
        add("match_section", "Native", size, src, unit10, ser, pr, {"render": n * r10, "score": n * (sc10 or 0)},
            par_txt, old,
            lower=sc10 is None, dates=dates10)
    else:
        add("match_section", "Native", size, src, unit10, None, None, {})
    if n and r10 is not None and budget_min:
        b = budget_min * 60.0
        per = r10 + (sc10 or 0)
        per_p = (r10 / speed + (sc10 or 0)) if speed else None
        add("match_budget", "Native", f"{budget_min} min budget buys {fmt_count(b / per)} renders"
            + (f" ({fmt_count(b / per_p)} with {pool_tag})" if per_p else ""),
            "the 25 min request in cache/matches/*/report.md (local)", unit10, b, b if per_p else None,
            {"render": b * r10 / per, "score": b * (sc10 or 0) / per}, "", old, lower=sc10 is None, dates=dates10)
    else:
        add("match_budget", "Native", None, "no local match reports found", unit10, None, None, {})
    # whole song, up to 3 snapshot sections
    if n and r10 is not None:
        ser = 3 * n * (r10 + (sc10 or 0))
        pr = 3 * n * (r10 / speed + (sc10 or 0)) if speed else None
        add("match_song", "Native", f"3 sections x {fmt_count(n)} renders = {fmt_count(3 * n)}",
            "docs/PLAN.md (up to 3 tones) times the section size above", unit10, ser, pr,
            {"render": 3 * n * r10, "score": 3 * n * (sc10 or 0)},
            par_txt,
            "upper bound: the one real run searched one section in full and refined another briefly",
            lower=sc10 is None, dates=dates10)
    else:
        add("match_song", "Native", None, "no local match reports found", unit10, None, None, {})
    # verify the top 3 on the pedal
    vf = _secs(T.get("verify", {}).get("stomp"))
    sess = _secs(T.get("session", {}).get("stomp"))
    if vf is not None:
        add("verify_top3", "Stomp", "3 finalists, each whole-written and rendered twice on 10 s clips",
            "scripts/match_song.py (final check of the top 3); T061 verification batch",
            f"{fmt_dur(vf)} per finalist + one session {fmt_dur(sess)}",
            3 * vf + (sess or 0.0), None, {"pedal writes and renders": 3 * vf}, "",
            "the Native render of each finalist is not included; not yet timed through match_song (T062)",
            dates=[d for d in (T["verify"]["stomp"].get("date"),) if d])
    else:
        add("verify_top3", "Stomp (and Native)", "3 candidates, one parity-style check each",
            "scripts/match_song.py (final check of the top 3)",
            f"one preset on both engines + analysis {fmt_dur(par)}",
            3 * par if par is not None else None, None, {"engines + analysis": 3 * par if par else None},
            "", "includes the whole write to the pedal and a Native render", dates=[T["parity"]["any"].get("date")]
            if par is not None else None)
    # one parity check
    add("parity_one", "Stomp + Native", "1 preset", "benchmark suite parity step", f"{fmt_dur(par)} per preset",
        par, None, {"engines + analysis": par}, "", "", dates=[T["parity"]["any"].get("date")] if par is not None else None)

    # ---- synthetic benchmark
    def synth(jid: str, eng: str, prefix: str, scale: int = 1, scale_why: str = "", trial: bool = False) -> None:
        got = _size_lookup(sizes, prefix)
        r = r2n if eng == "Native" else r2s
        st_cell = T.get("search_test", {}).get("native" if eng == "Native" else "stomp")
        scored_pool = (eng == "Native" and st_cell and st_cell["state"] == "measured" and st_cell.get("has_score"))
        if got is None or (r is None and not scored_pool and not (eng == "Stomp" and trial and st_cell
                                                                  and st_cell["state"] == "measured")):
            add(jid, eng, None, "docs/notes/synthetic-benchmark-design.md (cost table)", "", None, None, {})
            return
        count, label = got
        count *= scale
        paren = re.search(r"\(([^)]*)\)", label)
        what = paren[1] if paren and paren[1][:1].isdigit() else label.split(" (")[0]
        size = f"{fmt_count(count)} renders ({what}{scale_why})"
        src = "docs/notes/synthetic-benchmark-design.md (cost table)"
        # Native: the measured pool test (render + score) when there is one
        if eng == "Native" and st_cell and st_cell["state"] == "measured" and st_cell.get("has_score"):
            add(jid, eng, size, src,
                f"one test (render + score), 2 s clip: {fmt_dur(st_cell['one_seconds'])} on one host, "
                f"{fmt_dur(st_cell['seconds'])} with {st_cell['hosts']} hosts",
                count * st_cell["one_seconds"], count * st_cell["seconds"],
                {"render": count * st_cell["one_seconds"]},
                f"{pool_tag} for render and score together", "", dates=[st_cell["date"]])
            return
        # Stomp recovery: the search trial with the speed options (render side), one session per batch
        if eng == "Stomp" and trial and st_cell and st_cell["state"] == "measured":
            ser = count * (st_cell["seconds"] + (sc or 0.0)) + (sess or 0.0)
            add(jid, eng, size, src,
                f"search trial {fmt_dur(st_cell['seconds'])}" + (f" + score {fmt_dur(sc)}" if sc else "")
                + (f" + one session {fmt_dur(sess)}" if sess else ""),
                ser, None, {"render": count * st_cell["seconds"], "score": count * (sc or 0.0)}, "",
                "amp-parameter trials only; a trial that changes the model also pays one whole write",
                lower=sc is None, dates=[st_cell["date"]])
            return
        extra_w = (wr or 0.0) if eng == "Stomp" else 0.0
        per = r + extra_w + (sc or 0.0)
        unit = (f"render {fmt_dur(r)}" + (f" + whole write {fmt_dur(extra_w)}" if eng == "Stomp" else "")
                + f" + score {fmt_dur(sc)}")
        ser = count * per
        pr = count * (r / speed + (sc or 0.0)) if (eng == "Native" and speed) else None
        d = [x for x in (T["render_2s"]["native" if eng == "Native" else "stomp"].get("date"),) if x]
        add(jid, eng, size, src, unit, ser, pr,
            {"render": count * r, "write": count * extra_w, "score": count * (sc or 0.0)},
            f"{pool_tag} for the renders; scoring one after another" if pr else "",
            ("assumes every Stomp trial includes one whole write" if eng == "Stomp"
             else "scoring time is from the 10 s excerpt, so likely high for short clips"),
            lower=sc is None, dates=d)

    synth("synth_ci", "Native", "Native CI run")
    synth("synth_nightly", "Native", "Native full recovery")
    synth("synth_stomp_truth", "Stomp", "Stomp truth")
    synth("synth_stomp_sub", "Stomp", "Stomp recovery, 10-item subset", trial=True)
    synth("synth_stomp_full", "Stomp", "Stomp recovery, 10-item subset", 6, "; x6 for 60 items", trial=True)

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
            {"capture": count * cap}, "", T["capture"]["native"].get("note", ""), dates=[T["capture"]["native"]["date"]])
        packed = math.ceil(count / 8)
        add("library_packed", "Native", f"{fmt_count(packed)} captures if 8 blocks share a capture",
            "docs/DECISIONS.md (block bypass over MIDI CC: up to 8 blocks per capture)",
            f"{fmt_dur(cap)} per capture", packed * cap, None, {"capture": packed * cap},
            "", "proposed packing, not yet used for the library",
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
             "capture": "capturing chains", "pedal writes and renders": "writing to the pedal and rendering on it",
             "engines + analysis": "the two engines and the analysis",
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
    if cap_ratio and cap_ratio[1] >= 1:
        s += (f" For Native, capturing one new chain ({fmt_dur(cap_ratio[0])}) now costs as much as about "
              f"{cap_ratio[1]:.0f} renders of a 10 s excerpt, so captures, not renders, set the pace of "
              "anything that needs new chains.")
    elif cap_ratio:
        s += (f" For Native, capturing is no longer the slow step when chains are captured in a batch: "
              f"{fmt_dur(cap_ratio[0])} per chain in the newest row, against {fmt_dur(cap_ratio[0] / cap_ratio[1])} "
              "for a 10 s test; a single capture that includes engine start-up costs far more (see the measured table).")
    if untimed:
        k = len(untimed)
        s += f" {k} job{'s have' if k != 1 else ' has'} no measurement yet and {'show' if k != 1 else 'shows'} \"{NOT_MEASURED}\"."
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
