"""Offline tests of the T197 restore path (scripts/ds_sample_reference_chain.py restore)."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ds_ref", ROOT / "scripts" / "ds_sample_reference_chain.py")
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


def chain(name="Smoke Known", model=101, gain=0.5, routing=1):
    return {"setlist": 0, "index": 11, "name": name, "assignments": [],
            "slots": [{"pos": 0, "kind": "Input", "enabled": True, "routing": routing},
                      {"pos": 1, "kind": "Block", "model": model, "enabled": True,
                       "params": [{"i": 0, "value": gain}]}]}


class FakeBridge:
    """The pedal: `select` of 04C reloads `flash` (its saved chain) unless `sticky` keeps the edit
    buffer; `restore` writes the backup document; `backup` writes the current document."""

    def __init__(self, flash, buffer, flash_doc, backup_doc, sticky=0):
        self.flash, self.buffer, self.sticky = flash, buffer, sticky
        self.flash_doc, self.backup_doc, self.doc = flash_doc, backup_doc, b"loaded-reference-chain"
        self.calls, self.ready, self.closed = [], {"presets": 126}, False
        self.release_report = {"ok": True}

    def call(self, op, **kw):
        self.calls.append((op, kw))
        if op == "select":
            if kw["index"] == 11:
                if self.sticky:
                    self.sticky -= 1
                else:
                    self.buffer, self.doc = json.loads(json.dumps(self.flash)), self.flash_doc
            else:
                self.buffer = chain(name="other", model=7)
            return {}
        if op == "chain":
            return json.loads(json.dumps(self.buffer))
        if op == "restore":
            self.doc = Path(kw["path"]).read_bytes()
            self.buffer = json.loads(json.dumps(self.backup_chain))
            return {}
        if op == "backup":
            Path(kw["path"]).write_bytes(self.doc)
            return {}
        raise AssertionError(op)

    def close(self):
        self.closed = True


def record(tmp_path, original_chain, doc=b"04C-document"):
    bp = tmp_path / "x_editbuffer.bin"
    bp.write_bytes(doc)
    rp = tmp_path / "session.json"
    rp.write_text(json.dumps({"original": {"setlist": 0, "index": 11, "label": "04C", "name": "Smoke Known"},
                              "original_chain": original_chain, "backup_path": str(bp)}))
    return rp


def test_restore_reselects_04c_and_verifies(tmp_path):
    orig = chain()
    b = FakeBridge(flash=orig, buffer=chain(name="DS Ref Chain", model=55, gain=0.3), flash_doc=b"04C-document",
                   backup_doc=b"04C-document")
    r = R.restore(record(tmp_path, orig), bridge_factory=lambda: b)
    assert r["restore_ok"] is True
    assert [c[0] for c in b.calls] == ["select", "chain", "backup"]
    assert r["report"]["bytes_equal_backup"] is True and b.closed


def test_restore_steps_away_when_reselect_keeps_buffer(tmp_path):
    orig = chain()
    b = FakeBridge(flash=orig, buffer=chain(name="DS Ref Chain", model=55), flash_doc=b"04C-document",
                   backup_doc=b"04C-document", sticky=1)
    r = R.restore(record(tmp_path, orig), bridge_factory=lambda: b)
    assert r["restore_ok"] is True
    assert ("select", {"setlist": 0, "index": 12, "timeout_s": 20}) in b.calls
    assert "restore" not in [c[0] for c in b.calls]


def test_restore_writes_backup_when_flash_differs(tmp_path):
    orig = chain(gain=0.6)          # 04C had unsaved edits at open
    b = FakeBridge(flash=chain(gain=0.5), buffer=chain(name="DS Ref Chain", model=55), flash_doc=b"flash-doc",
                   backup_doc=b"04C-document")
    b.backup_chain = orig
    r = R.restore(record(tmp_path, orig), bridge_factory=lambda: b)
    assert r["restore_ok"] is True
    assert "restore" in [c[0] for c in b.calls]


def test_restore_reports_failure(tmp_path, monkeypatch):
    import tonematch.engines.stomp as S

    monkeypatch.setattr(S, "decode_document", lambda p: {"bytes": Path(p).read_bytes().decode()})
    orig = chain()
    b = FakeBridge(flash=chain(gain=0.9), buffer=chain(name="DS Ref Chain", model=55), flash_doc=b"flash-doc",
                   backup_doc=b"04C-document")
    b.backup_chain = chain(gain=0.8)   # the write does not land as recorded
    r = R.restore(record(tmp_path, orig), bridge_factory=lambda: b)
    assert r["restore_ok"] is False


@pytest.mark.parametrize("hall", [True, False])
def test_build_chain_values(hall):
    pytest.importorskip("tonematch.catalog")
    from tonematch.catalog import CatalogError

    try:
        p, _ = R.build(with_hall=hall)
    except (CatalogError, FileNotFoundError):
        pytest.skip("no local catalog / base preset")
    models = [s.model_id for s in p.slots]
    assert models[:5] == ["HD2_DistScream808", "HD2_AmpBrit2204", "HD2_Cab4x12Greenback25",
                          "HD2_DelaySimpleDelay", "HD2_EQGraphic10Band"]
    assert ("HD2_ReverbHall" in models) is hall
    amp = p.slots[1]
    assert amp.extra["@cab"] == "cab0" and amp.params["Drive"] == 0.3 and amp.params["ChVol"] == 0.6
    assert p.slots[3].params["Time"] == 0.026 and p.slots[3].params["TempoSync1"] is False
