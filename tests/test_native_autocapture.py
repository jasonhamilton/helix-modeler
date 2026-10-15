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

    def __init__(self, plugin, plugin_name=None, block_size=512, *, gui=True, dialogs=0, change=True):
        self.plugin, self.gui, self.dialogs, self.change = plugin, gui, dialogs, change
        self.state = b"START"
        self.calls: list[tuple] = []
        self.dropped = None
        self.params: dict[str, float] = {}
        self.ignored = {"cc_5"}
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
        if cmd == "editor_drop":
            self.dropped = json.loads(Path(kw["path"]).read_text())
            if self.change and not self.dialogs:
                self.state = b"DROPPED:" + kw["path"].encode()
            return {"drop": {"perform": True}, "dialogs": self.dialogs}, b""
        if cmd == "set_params":
            self.params.update(kw["values"])
            return {"read_back": kw["values"]}, b""
        if cmd == "render":  # params the fake "honours" scale the output; cc_5 is ignored by it
            x = np.frombuffer(payload, dtype="<f4")
            g = 1.0 + sum(v for k, v in self.params.items() if k not in self.ignored)
            return {}, (x * g).astype("<f4").tobytes()
        raise AssertionError(f"unexpected host command {cmd}")

    def close(self):
        pass


def _factory(**kw):
    return lambda plugin, plugin_name=None, block_size=512: FakeHost(plugin, plugin_name, block_size, **kw)


def test_capture_chain_writes_cc_map_and_saves_template(tmp_path):
    p = _preset()
    tpl = nat.capture_chain(p, "fake.vst3", start_state=b"STOMP", host_factory=_factory())
    host = FakeHost.instances[-1]
    cmds = [c for c, _ in host.calls]
    assert cmds[:5] == ["gui_available", "reload_with_state", "get_state", "editor_drop", "get_state"]
    assert "render" in cmds[5:]  # CC verification renders
    ctl = host.dropped["data"]["tone"]["controller"]["dsp0"]
    assert ctl["block3"]["Drive"]["@controller"] == 8 and ctl["block3"]["Drive"]["@cc"] == 4
    assert tpl.map["0:Drive"] == "cc_4" and tpl.map["2:Mix"].startswith("cc_")
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
    assert e.host.params["cc_4"] == pytest.approx(0.45)  # Drive through its CC
    n_capture_hosts = len(FakeHost.instances)
    p.slots[0].params["Drive"] = 0.9
    e.load(p)  # same chain: no new capture, just the CC
    assert len(FakeHost.instances) == n_capture_hosts
    assert e.host.params["cc_4"] == pytest.approx(0.9)
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
    cap = [h for h in FakeHost.instances if any(c == "editor_drop" for c, _ in h.calls)][-1]
    assert ("reload_with_state", {"path": "fake.vst3", "plugin_name": None}) in cap.calls


def test_template_records_verified_ccs_and_baked_values():
    tpl = nat.capture_chain(_preset(), "fake.vst3", host_factory=_factory())
    assert set(tpl.verified) == set(tpl.map) - {"0:Bass"} and tpl.baked_hash  # cc_5 ignored
    assert tpl.baked["0:Bass"] == pytest.approx(0.5)  # unverified CC: baked
    assert tpl.baked["1:LowCut"] == pytest.approx(19.9) and tpl.baked["2:@enabled"] is True
    assert "0:Drive" not in tpl.baked


def _count_captures():
    return sum(1 for h in FakeHost.instances if any(c == "editor_drop" for c, _ in h.calls))


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


def test_bypass_and_unverified_cc_are_baked(monkeypatch):
    e = _engine(monkeypatch, autocapture=True)
    p = _preset()
    e.load(p)
    n = _count_captures()
    p.slots[2].enabled = False  # bypass differs
    e.load(p)
    assert _count_captures() == n + 1
    tpl = e.current_template
    unverified = [k for k, v in tpl.map.items() if v in FakeHost.instances[-1].ignored]
    assert unverified and all(k in tpl.baked for k in unverified)


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
