"""T018n autocapture: load() captures an unknown chain by in-process drop. Fake hosts only:
no Native, no pedalboard, no GUI."""
from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import numpy as np
import pytest

from tonematch.engines import native as nat
from tonematch.engines.base import UnsupportedModelChange
from tonematch.preset import from_dict

REAL_CATALOG_RANGES = nat.catalog_ranges

RAW = {"version": 6, "schema": "L6Preset", "meta": {},
       "data": {"meta": {"name": "Auto"}, "device": 2162694, "device_version": 58720256,
                "tone": {"dsp0": {
                    "block3": {"@model": "HD2_AmpEssexA30", "@position": 1, "@enabled": True,
                               "@cab": "cab0", "Drive": 0.45, "Bass": 0.5},
                    "cab0": {"@model": "HD2_Cab1x12BlueBell", "@enabled": True, "LowCut": 19.9},
                    "block5": {"@model": "HD2_DL4PingPong", "@position": 2, "@enabled": True,
                               "Mix": 0.23}}}}}


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setenv("TONEMATCH_CACHE", str(tmp_path / "cache"))
    monkeypatch.delenv("TONEMATCH_NATIVE_AUTOCAPTURE", raising=False)
    monkeypatch.setattr(nat, "catalog_ranges", lambda preset, catalog=None: None)  # [0, 1] fallback


def _preset():
    return from_dict(json.loads(json.dumps(RAW)))


class FakeHost:
    """Speaks the host protocol subset used by NativeEngine and capture_chain."""

    instances: ClassVar[list] = []
    file_vals: ClassVar[dict] = {}  # dropped state -> {cc: file value} (the state "carries" it)

    def __init__(self, plugin, plugin_name=None, block_size=512, *, gui=True, dialogs=0, change=True):
        self.plugin, self.gui, self.dialogs, self.change = plugin, gui, dialogs, change
        self.state = b"START"
        self.calls: list[tuple] = []
        self.dropped = None
        self.params: dict[str, float] = {}
        self.ignored = {"cc_7"}  # Bass (cc_4, cc_5 are the bypass CCs)
        self.offset = 0.0  # a host that renders differently (replay-check tests)
        self.info = {"has_state": True, "channels": 1, "version": "fake",
                     "params": [{"name": f"cc_{i}"} for i in range(128)]}
        FakeHost.instances.append(self)

    def call(self, cmd, payload=b"", **kw):
        self.calls.append((cmd, kw))
        if cmd == "gui_available":
            return {"gui": self.gui, "reason": "" if self.gui else "no screen"}, b""
        if cmd in ("set_state", "reload_with_state"):
            self.state = payload
            return {}, b""
        if cmd == "get_state":
            return {}, self.state
        if cmd == "editor_drop_many":
            results, blobs = [], []
            for path in kw["paths"]:
                self.dropped = json.loads(Path(path).read_text())
                before = self.state
                if self.change and not self.dialogs:
                    self.state = b"DROPPED:" + path.encode()
                    self.file_vals[self.state] = _file_cc_values(self.dropped)
                r = {"path": path, "drop": {"perform": True}, "dialogs": self.dialogs}
                if self.dialogs or self.state == before:
                    r["error"] = "dialog shown" if self.dialogs else "state did not change"
                    r["state_len"] = 0
                    results.append(r)
                    break
                r["state_len"] = len(self.state)
                results.append(r)
                blobs.append(self.state)
            return {"results": results}, b"".join(blobs)
        if cmd == "set_params":
            self.params.update(kw["values"])
            return {"read_back": kw["values"]}, b""
        if cmd == "render":  # params the fake "honours" scale the output; cc_7 is ignored by it
            x = np.frombuffer(payload, dtype="<f4")
            g = 1.0 + self.offset + sum(v for k, v in self.params.items() if k not in self.ignored)
            # an ignored CC leaves the param at the value the dropped file gave it
            g += sum(v for k, v in self.file_vals.get(self.state, {}).items() if k in self.ignored)
            return {}, (x * g).astype("<f4").tobytes()
        raise AssertionError(f"unexpected host command {cmd}")

    def close(self):
        pass


def _file_cc_values(doc: dict) -> dict[str, float]:
    """``cc_<n>`` -> the value the file gives the param that CC is assigned to."""
    tone = doc["data"]["tone"]
    out = {}
    for blk, prms in tone.get("controller", {}).get("dsp0", {}).items():
        for name, c in prms.items():
            v = tone["dsp0"].get(blk, {}).get(name)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[f"cc_{c['@cc']}"] = float(v)
    return out


def _factory(**kw):
    return lambda plugin, plugin_name=None, block_size=512: FakeHost(plugin, plugin_name, block_size, **kw)


def test_capture_chain_writes_cc_map_and_saves_template(tmp_path):
    p = _preset()
    tpl = nat.capture_chain(p, "fake.vst3", start_state=b"STOMP", host_factory=_factory(), replay_every=0)
    host = FakeHost.instances[-1]
    cmds = [c for c, _ in host.calls]
    assert cmds[:4] == ["gui_available", "reload_with_state", "editor_drop_many", "set_state"]
    assert "render" in cmds[4:]  # CC verification renders, on the same host
    ctl = host.dropped["data"]["tone"]["controller"]["dsp0"]
    assert ctl["block3"]["Drive"]["@controller"] == 8 and ctl["block3"]["Drive"]["@cc"] == 6
    assert tpl.map["0:Drive"] == "cc_6" and tpl.map["2:Mix"].startswith("cc_")
    assert "1:LowCut" not in tpl.map  # not in [0, 1] and no catalog range given
    assert tpl.state.startswith(b"DROPPED:")
    saved = nat.ChainTemplate.load_all()
    assert nat.chain_key(p) in saved and saved[nat.chain_key(p)].map == tpl.map


@pytest.mark.parametrize("kw, msg", [({"gui": False}, "unavailable"), ({"dialogs": 1}, "asked"),
                                     ({"change": False}, "did not change")])
def test_capture_chain_refuses_with_reason(kw, msg):
    with pytest.raises(UnsupportedModelChange, match=msg):
        nat.capture_chain(_preset(), "fake.vst3", host_factory=_factory(**kw))
    assert nat.ChainTemplate.load_all() == {}


def _engine(monkeypatch, autocapture):
    monkeypatch.setattr(nat, "HostProcess", FakeHost)
    monkeypatch.setattr(nat, "_load_codec", lambda: None)
    e = nat.NativeEngine("fake.vst3", autocapture=autocapture)
    e._capture_host_factory = _factory()
    return e


def test_load_autocaptures_unknown_chain_and_drives_ccs(monkeypatch):
    e = _engine(monkeypatch, autocapture=True)
    p = _preset()
    e.load(p)
    assert e.last_capture_s is not None and nat.chain_key(p) in e.templates
    assert e.host.state.startswith(b"DROPPED:")  # the captured state was loaded into the engine
    assert e.host.params["cc_6"] == pytest.approx(0.45)  # Drive through its CC
    n_capture_hosts = len(FakeHost.instances)
    p.slots[0].params["Drive"] = 0.9
    e.load(p)  # same chain: no new capture, just the CC
    assert len(FakeHost.instances) == n_capture_hosts
    assert e.host.params["cc_6"] == pytest.approx(0.9)
    y = e._raw_render(np.ones(8, np.float32))
    assert np.all(y > 1.0)  # the fake host's render follows the CC values


def test_load_without_autocapture_still_raises(monkeypatch):
    e = _engine(monkeypatch, autocapture=False)
    with pytest.raises(UnsupportedModelChange, match="TONEMATCH_NATIVE_AUTOCAPTURE"):
        e.load(_preset())


def test_autocapture_env_flag(monkeypatch):
    assert nat.autocapture_enabled() is False
    monkeypatch.setenv("TONEMATCH_NATIVE_AUTOCAPTURE", "1")
    assert nat.autocapture_enabled() is True
    e = _engine(monkeypatch, autocapture=None)
    assert e.autocapture is True


def test_autocapture_uses_human_start_state(monkeypatch):
    e = _engine(monkeypatch, autocapture=True)
    nat.start_state_path().parent.mkdir(parents=True, exist_ok=True)
    nat.start_state_path().write_bytes(b"HUMAN-STOMP")
    e.load(_preset())
    cap = [h for h in FakeHost.instances if any(c == "editor_drop_many" for c, _ in h.calls)][-1]
    assert ("reload_with_state", {"path": "fake.vst3", "plugin_name": None}) in cap.calls


def test_template_records_verified_ccs_and_baked_values():
    tpl = nat.capture_chain(_preset(), "fake.vst3", host_factory=_factory())
    assert set(tpl.verified) == set(tpl.map) - {"0:Bass"} and tpl.baked_hash  # cc_7 ignored
    assert tpl.baked["0:Bass"] == pytest.approx(0.5)  # unverified CC: baked
    assert tpl.baked["1:LowCut"] == pytest.approx(19.9) and "2:@enabled" not in tpl.baked
    assert "0:Drive" not in tpl.baked


def _count_captures():
    return sum(1 for h in FakeHost.instances if any(c == "editor_drop_many" for c, _ in h.calls))


def test_unmapped_value_change_does_not_reuse_template(monkeypatch):
    e = _engine(monkeypatch, autocapture=True)
    p = _preset()
    e.load(p)
    n = _count_captures()
    p.slots[1].params["LowCut"] = 80.0  # unmapped (outside [0, 1], no catalog range): baked
    e.load(p)
    assert _count_captures() == n + 1
    assert len(e._variants[nat.chain_key(p)]) == 2
    p.slots[1].params["LowCut"] = 19.9  # back to the first variant: reused, no capture
    e.load(p)
    assert _count_captures() == n + 1
    assert e.current_template.baked["1:LowCut"] == pytest.approx(19.9)


def test_bypass_rides_its_cc_and_unverified_cc_is_baked(monkeypatch):
    e = _engine(monkeypatch, autocapture=True)
    p = _preset()
    e.load(p)
    n = _count_captures()
    tpl = e.current_template
    assert tpl.map["0:@enabled"] == "cc_4" and tpl.map["2:@enabled"] == "cc_5"
    assert "1:@enabled" not in tpl.map  # a cab linked to an Amp+Cab block has no bypass
    p.slots[2].enabled = False  # bypass differs: same template, delay switched off by its CC
    e.load(p)
    assert _count_captures() == n and e.current_template is tpl
    assert e.host.params["cc_5"] == 0.0 and e.host.params["cc_4"] == 1.0
    unverified = [k for k, v in tpl.map.items() if v in FakeHost.instances[-1].ignored]
    assert unverified and all(k in tpl.baked for k in unverified)


def test_an_assigned_bypass_cc_is_live_even_if_it_did_not_move_the_render():
    vals, _ = nat.baked_fingerprint(_preset(), {"0:@enabled": "cc_4", "0:Drive": "cc_6"}, [])
    assert "0:@enabled" not in vals and "0:Drive" in vals and "2:@enabled" in vals


def test_bypass_is_baked_without_bypass_ccs():
    tpl = nat.capture_chain(_preset(), "fake.vst3", host_factory=_factory(), bypass_ccs=False)
    assert "0:@enabled" not in tpl.map and tpl.baked["2:@enabled"] is True


def test_params_of_a_bypassed_block_are_verified_with_the_block_on():
    p = _preset()
    p.slots[2].enabled = False
    host_holder = []

    def factory(plugin, plugin_name=None):
        h = FakeHost(plugin, plugin_name)
        host_holder.append(h)
        return h

    tpl = nat.capture_chain(p, "fake.vst3", host_factory=factory, replay_every=0)
    assert "2:Mix" in tpl.verified and "2:@enabled" in tpl.verified
    first = next(kw["values"] for c, kw in host_holder[-1].calls if c == "set_params")
    assert first["cc_5"] == 1.0  # base render for verification has every block on


def test_without_autocapture_a_baked_mismatch_raises_with_the_difference(monkeypatch):
    e = _engine(monkeypatch, autocapture=True)
    p = _preset()
    e.load(p)
    e2 = _engine(monkeypatch, autocapture=False)
    q = _preset()
    q.slots[1].params["LowCut"] = 120.0
    with pytest.raises(UnsupportedModelChange, match="1:LowCut"):
        e2.load(q)
    e2.load(_preset())  # identical baked values: reused from disk


def test_legacy_template_without_fingerprint_is_not_reused(monkeypatch):
    p = _preset()
    nat.ChainTemplate(nat.chain_key(p), b"OLD", {"0:Drive": "cc_4"}, name="legacy").save()
    e = _engine(monkeypatch, autocapture=False)
    with pytest.raises(UnsupportedModelChange, match="no baked-value fingerprint"):
        e.load(p)


def test_catalog_ranges_map_cab_hz_params():
    cat = {"models": {"HD2_Cab1x12BlueBell": {"params": {
        "LowCut": {"kind": "continuous", "min": 19.9, "max": 500.0},
        "@mic": {"kind": "stepped", "min": 0, "max": 15}}},
        "HD2_AmpEssexA30": {"params": {"Drive": {"kind": "continuous", "min": 0.0, "max": 1.0}}}}}
    assert REAL_CATALOG_RANGES(_preset(), cat) == {"0:Drive": (0.0, 1.0), "1:LowCut": (19.9, 500.0)}


def test_capture_chains_uses_one_host_and_one_editor_session():
    p, q = _preset(), _preset()
    q.slots[0].params["Drive"] = 0.9
    q.name = "Other"
    n0 = len(FakeHost.instances)
    out = nat.capture_chains([p, q], "fake.vst3", host_factory=_factory(), replay_every=0)
    hosts = FakeHost.instances[n0:]
    assert len(hosts) == 1 and [c for c, _ in hosts[0].calls].count("editor_drop_many") == 1
    assert all(isinstance(t, nat.ChainTemplate) for t in out) and out[0].state != out[1].state


def test_capture_chains_reports_a_failed_drop_per_preset():
    q = _preset()
    q.slots[1].params["LowCut"] = 30.0  # a different file
    st: dict = {}
    out = nat.capture_chains([_preset(), q], "fake.vst3", host_factory=_factory(dialogs=1), stats=st)
    assert all(isinstance(o, UnsupportedModelChange) for o in out)
    assert "asked a question" in str(out[0]) and "rejected" in str(out[1])
    assert st["sessions"] == 2 and len(st["rejected"]) == 2  # the 2nd file retried in a new host


def test_a_rejected_file_does_not_stop_the_files_after_it():
    class Picky(FakeHost):
        def call(self, cmd, payload=b"", **kw):
            if cmd == "editor_drop_many" and "BADFILE" in Path(kw["paths"][0]).read_text():
                self.calls.append((cmd, kw))
                return {"results": [{"path": kw["paths"][0], "error": "dialog shown", "dialogs": 1,
                                     "windows": [{"cls": "L6DialogWindow", "title": ""}],
                                     "state_len": 0}]}, b""
            return super().call(cmd, payload, **kw)

    bad = _preset()
    bad.slots[1].params["LowCut"] = 30.0
    bad.name = "BADFILE"
    good = _preset()
    st: dict = {}
    out = nat.capture_chains([bad, good], "fake.vst3", stats=st, replay_every=0,
                             host_factory=lambda plugin, plugin_name=None: Picky(plugin, plugin_name))
    assert isinstance(out[0], UnsupportedModelChange) and isinstance(out[1], nat.ChainTemplate)
    assert st["rejected"][0]["windows"][0]["cls"] == "L6DialogWindow" and st["sessions"] == 2


def test_known_live_pairs_skip_their_verification_renders():
    nat.capture_chain(_preset(), "fake.vst3", host_factory=_factory(), replay_every=0)
    first = FakeHost.instances[-1].calls.count(("render", {"reset": True}))
    assert nat._load_live()["HD2_AmpEssexA30|Drive"] is True
    assert "HD2_AmpEssexA30|Bass" not in nat._load_live()  # dead results are not recorded
    nat.capture_chain(_preset(), "fake.vst3", host_factory=_factory(), replay_every=0)
    second = FakeHost.instances[-1].calls.count(("render", {"reset": True}))
    assert second < first and second == 2  # base + the one CC not known live (Bass)


def test_engine_capture_missing_batches_unknown_chains_then_load_reuses(monkeypatch):
    e = _engine(monkeypatch, autocapture=False)
    p, q = _preset(), _preset()
    q.slots[1].params["LowCut"] = 80.0  # baked difference: needs its own template
    n0 = len(FakeHost.instances)
    assert e.capture_missing([p, q, _preset()], sessions=1) == {}
    caps = [h for h in FakeHost.instances[n0:] if any(c == "editor_drop_many" for c, _ in h.calls)]
    assert len(caps) == 1 and len(caps[0].calls[[c for c, _ in caps[0].calls].index("editor_drop_many")][1]["paths"]) == 2
    e.load(p)
    e.load(q)  # both usable without autocapture
    assert e.capture_missing([p, q]) == {}


def test_cc_value_is_linear_on_the_127_step_grid():
    assert nat.cc_value(19.9, 500.0, 95) == pytest.approx(379.0299, abs=1e-3)
    assert nat.cc_value(-60.0, 6.0, 127) == pytest.approx(6.0)
    assert nat.cc_value(0.0, 1.0, 0) == 0.0


def test_moved_preset_moves_every_mapped_param_on_the_grid_with_every_block_on():
    p = _preset()
    p.slots[2].enabled = False
    amap = {"0:Drive": "cc_6", "0:Bass": "cc_7", "2:Mix": "cc_8", "2:@enabled": "cc_5",
            "3:Level": "cc_9"}
    p.slots[2].params["Level"] = 0.0  # a dB level in a [-60, 6] range
    amap.pop("3:Level")
    amap["2:Level"] = "cc_9"
    rng = {"2:Level": [-60.0, 6.0]}
    q, vals = nat.moved_preset(p, amap, rng)
    assert q.slots[2].enabled and vals["cc_5"] == 1.0
    for ref, prm in amap.items():
        if ref.endswith("@enabled"):
            continue
        i, name = ref.split(":")
        c = round(vals[prm] * 127)
        assert 0 < abs(c - round(nat._norm(p, rng, ref) * 127)) <= nat.MOVE_STEPS
        lo, hi = rng.get(ref, [0.0, 1.0])
        assert q.slots[int(i)].params[name] == pytest.approx(nat.cc_value(lo, hi, c))
    assert p.slots[0].params["Drive"] == 0.45  # the original is untouched


def _truth_host(ignored):
    def f(plugin, plugin_name=None):
        h = FakeHost(plugin, plugin_name)
        h.ignored = set(ignored)
        return h
    return f


def test_truth_mode_drops_a_moved_copy_and_verifies_all_with_two_renders():
    tpl = nat.capture_chain(_preset(), "fake.vst3", host_factory=_truth_host(()), verify_mode="truth",
                            replay_every=0)
    h = FakeHost.instances[-1]
    drop = next(kw for c, kw in h.calls if c == "editor_drop_many")
    assert len(drop["paths"]) == 2  # the chain and its moved copy
    assert [c for c, _ in h.calls].count("render") == 2
    assert set(tpl.verified) == set(tpl.map)


def test_truth_mode_falls_back_to_per_cc_when_a_cc_is_ignored():
    st: dict = {}
    out = nat.capture_chains([_preset()], "fake.vst3", host_factory=_truth_host({"cc_7"}),
                             verify_mode="truth", replay_every=0, stats=st)
    tpl = out[0]
    assert st["chains"][0]["mode"] == "truth->per-cc" and st["chains"][0]["truth_null_db"] > -50
    assert "0:Bass" not in tpl.verified and "0:Drive" in tpl.verified
    assert tpl.baked["0:Bass"] == pytest.approx(0.5)


def test_auto_mode_picks_per_cc_for_few_params():
    st: dict = {}
    nat.capture_chains([_preset()], "fake.vst3", host_factory=_factory(), replay_every=0, stats=st)
    assert st["chains"][0]["mode"] == "per-cc"  # 3 params < truth_min_ccs


def test_bypass_ccs_are_not_rendered_in_per_cc_verification():
    nat.capture_chain(_preset(), "fake.vst3", host_factory=_factory(), verify_mode="per-cc",
                      replay_every=0)
    h = FakeHost.instances[-1]
    assert [c for c, _ in h.calls].count("render") == 1 + 3  # base + Drive, Bass, Mix


def test_replay_check_discards_a_template_a_fresh_host_renders_differently():
    hosts = []

    def factory(plugin, plugin_name=None):
        h = FakeHost(plugin, plugin_name)
        h.offset = 0.5 * len(hosts)  # the second (replay) host renders differently
        hosts.append(h)
        return h

    st: dict = {}
    out = nat.capture_chains([_preset()], "fake.vst3", host_factory=factory, replay_every=1, stats=st)
    assert isinstance(out[0], UnsupportedModelChange) and "fresh host" in str(out[0])
    assert nat.ChainTemplate.load_variants() == {}
    assert st["replay_null_db"]


def test_replay_check_passes_for_identical_hosts():
    st: dict = {}
    out = nat.capture_chains([_preset()], "fake.vst3", host_factory=_factory(), replay_every=1, stats=st)
    assert isinstance(out[0], nat.ChainTemplate) and next(iter(st["replay_null_db"].values())) < -200


def test_replay_refs_keep_the_in_session_render_of_passing_templates_only():
    refs: dict = {}
    out = nat.capture_chains([_preset()], "fake.vst3", host_factory=_factory(), replay_every=1,
                             replay_refs=refs)
    stem = nat.template_stem(out[0])
    assert list(refs) == [stem] and len(refs[stem][0]) == 48000 and isinstance(refs[stem][1], dict)
    hosts = []

    def factory(plugin, plugin_name=None):
        h = FakeHost(plugin, plugin_name)
        h.offset = 0.5 * len(hosts)
        hosts.append(h)
        return h

    refs2: dict = {}
    nat.capture_chains([_preset()], "fake.vst3", host_factory=factory, replay_every=1, replay_refs=refs2)
    assert refs2 == {}


def test_parallel_capture_keeps_order_and_uses_one_host_per_session(monkeypatch):
    monkeypatch.setattr(nat, "max_hosts_by_memory", lambda *a, **k: 2)
    ps = []
    for i in range(4):
        p = _preset()
        p.slots[1].params["LowCut"] = 20.0 + i  # baked: four different templates
        p.name = f"P{i}"
        ps.append(p)
    n0 = len(FakeHost.instances)
    st: dict = {}
    out = nat.capture_chains_parallel(ps, "fake.vst3", sessions=3, host_factory=_factory(),
                                      replay_every=0, stats=st)
    assert [t.name for t in out] == ["P0", "P1", "P2", "P3"]
    assert len(FakeHost.instances) - n0 == 2 and len(st["sessions"]) == 2  # capped at 2


def test_live_db_saves_merge():
    nat._save_live({"A|x": True})
    nat._save_live({"B|y": True, "C|z": False})
    assert nat._load_live() == {"A|x": True, "B|y": True}


def test_capture_missing_dedupes_presets_differing_only_in_cc_mapped_params(monkeypatch):
    e = _engine(monkeypatch, autocapture=False)
    p, q = _preset(), _preset()
    q.slots[0].params["Drive"] = 0.9  # CC-mapped: the same capture serves both
    n0 = len(FakeHost.instances)
    assert e.capture_missing([p, q], sessions=1) == {}
    caps = [h for h in FakeHost.instances[n0:] if any(c == "editor_drop_many" for c, _ in h.calls)]
    assert len(caps) == 1
    e.load(q)  # served by p's template through its Drive CC


def test_capture_missing_runs_parallel_sessions_and_restores_the_engine_host(monkeypatch):
    monkeypatch.setattr(nat, "max_hosts_by_memory", lambda *a, **k: 5)
    e = _engine(monkeypatch, autocapture=False)
    ps = []
    for i in range(4):
        p = _preset()
        p.slots[1].params["LowCut"] = 20.0 + i
        ps.append(p)
    e.capture_missing(ps[:1], sessions=1)
    e.load(ps[0])
    state = e.host.state
    n0 = len(FakeHost.instances)
    assert e.capture_missing(ps[1:], sessions=2) == {}
    caps = [h for h in FakeHost.instances[n0:] if any(c == "editor_drop_many" for c, _ in h.calls)]
    assert len(caps) == 2
    assert e.host.state == state  # the engine's own host is back on its loaded chain


def test_a_model_native_refused_is_not_dropped_again():
    nat.rejected_path().parent.mkdir(parents=True, exist_ok=True)
    nat.rejected_path().write_text(json.dumps({"x.hlx": {
        "models": ["HD2_DL4PingPong"], "message": [("Failed to load preset from file.\n\nHelixCore: "
                                                    "Invalid model. [code -1000302]")]}}))
    assert nat.invalid_models() == {"HD2_DL4PingPong"}
    n0 = len(FakeHost.instances)
    out = nat.capture_chains([_preset()], "fake.vst3", host_factory=_factory(), replay_every=0)
    assert isinstance(out[0], UnsupportedModelChange) and "Invalid model" in str(out[0])
    assert len(FakeHost.instances) == n0  # no host, no drop, no alert on screen


def test_latency_is_measured_once_through_a_bypassed_pass_through(monkeypatch):
    e = _engine(monkeypatch, autocapture=True)
    e.preroll = 0
    p = _preset()
    e.load(p)
    sent = []
    orig = e.host.call

    def spy(cmd, payload=b"", **kw):
        if cmd == "set_params":
            sent.append(dict(kw["values"]))
        return orig(cmd, payload, **kw)

    e.host.call = spy
    assert e.latency() == 0
    byp = {v for k, v in e.current_template.map.items() if k.endswith(":@enabled")}
    assert sent[0] == dict.fromkeys(byp, 0.0)  # every block bypassed for the impulse
    assert sent[1] == dict.fromkeys(byp, 1.0)  # then back to the loaded states
    q = _preset()
    q.slots[0].params["Drive"] = 0.9
    e.load(q)
    n = len(sent)
    e.latency()
    assert len(sent) == n  # not measured again for new settings
