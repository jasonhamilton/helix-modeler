"""T097: pedal restores are judged by decoded equality, bytes kept as a secondary signal.

Offline: fake decoders and a fake bridge. The optional fixture test reads the two edit-buffer
backups of the 20:27Z push session from cache/ (never committed) and skips when they or an
offline stompd decoder are missing."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from test_stomp import FakeBridge, make_engine, make_preset

from tonematch.engines import stomp
from tonematch.engines.base import EngineError

ROOT = Path(__file__).resolve().parents[1]
PUSH = ROOT / "cache" / "stomp" / "hw" / "20261016T202715Z-push"

DOC = {"name": "04C", "slots": [{"pos": 1, "model": "AmpA", "params": [{"i": 0, "value": 0.5}]}],
       "snapshot_controllers": [], "assignments": ["a", "b"]}


def _write(tmp_path: Path, a: bytes, b: bytes) -> tuple[Path, Path]:
    pa, pb = tmp_path / "backup.bin", tmp_path / "after.bin"
    pa.write_bytes(a)
    pb.write_bytes(b)
    return pa, pb


def _reordered(d: dict) -> dict:
    return {k: d[k] for k in reversed(list(d))}


# --------------------------------------------------------------------------- compare_backups


def test_identical_bytes_need_no_decoder(tmp_path):
    pa, pb = _write(tmp_path, b"doc", b"doc")
    r = stomp.compare_backups(pa, pb, decoder=lambda p: pytest.fail("decoded identical bytes"))
    assert r == {"bytes_equal_backup": True, "decoded_equal_backup": True, "restore_compare": "bytes-identical"}


def test_reencoded_buffer_is_equal_when_decoded(tmp_path, caplog):
    pa, pb = _write(tmp_path, b"doc-old", b"doc-reencoded")
    dec = {pa: DOC, pb: _reordered(DOC)}   # same document, keys in another order
    with caplog.at_level(logging.INFO, logger="tonematch.engines.stomp"):
        r = stomp.compare_backups(pa, pb, decoder=lambda p: dec[p])
    assert r["bytes_equal_backup"] is False and r["decoded_equal_backup"] is True
    assert r["restore_compare"] == "decoded" and "decoded_difference" not in r
    infos = [m for m in caplog.records if m.levelno == logging.INFO]
    assert len(infos) == 1 and "decoded identical" in infos[0].getMessage()
    assert not [m for m in caplog.records if m.levelno >= logging.WARNING]


def test_real_difference_is_reported(tmp_path):
    pa, pb = _write(tmp_path, b"doc-a", b"doc-b")
    other = {**DOC, "slots": [{"pos": 1, "model": "AmpA", "params": [{"i": 0, "value": 0.7}]}]}
    dec = {pa: DOC, pb: other}
    r = stomp.compare_backups(pa, pb, decoder=lambda p: dec[p])
    assert r["bytes_equal_backup"] is False and r["decoded_equal_backup"] is False
    assert r["restore_compare"] == "decoded"
    assert r["decoded_difference"] == ".slots[0].params[0].value: 0.5 vs 0.7"


def test_missing_key_and_list_length_are_differences(tmp_path):
    pa, pb = _write(tmp_path, b"a", b"b")
    dec = {pa: DOC, pb: {k: v for k, v in DOC.items() if k != "name"}}
    assert stomp.compare_backups(pa, pb, decoder=lambda p: dec[p])["decoded_difference"] == ".name: only in backup"
    dec = {pa: DOC, pb: {**DOC, "assignments": ["a"]}}
    assert stomp.compare_backups(pa, pb, decoder=lambda p: dec[p])["decoded_difference"] == ".assignments: 2 vs 1 items"


def test_no_decoder_falls_back_to_bytes(tmp_path):
    pa, pb = _write(tmp_path, b"a", b"b")
    r = stomp.compare_backups(pa, pb, decoder=None)
    assert r["bytes_equal_backup"] is False and r["decoded_equal_backup"] is None
    assert r["restore_compare"] == "bytes" and "judged by bytes" in r["decode_unavailable"]


def test_no_compiler_configured_falls_back_to_bytes(tmp_path, monkeypatch):
    """The real decode_document with no stompd compiler raises; the verdict is the bytes."""
    monkeypatch.delenv(stomp.ENV_COMPILER, raising=False)
    monkeypatch.setattr(stomp, "helper_path", lambda: None)
    pa, pb = _write(tmp_path, b"a", b"b")
    r = stomp.compare_backups(pa, pb)
    assert r["decoded_equal_backup"] is None and r["restore_compare"] == "bytes"
    assert "no stompd compiler" in r["decode_unavailable"]


# --------------------------------------------------------------------------- restore_verdict


def test_restore_verdict_prefers_restore_ok_and_reads_old_reports():
    assert stomp.restore_verdict({"restore_ok": True, "bytes_equal_backup": False}) is True
    assert stomp.restore_verdict({"restore_ok": False, "bytes_equal_backup": True}) is False
    assert stomp.restore_verdict({"match": True, "bytes_equal_backup": True}) is True     # pre-T097
    assert stomp.restore_verdict({"match": False, "bytes_equal_backup": True}) is False
    assert stomp.restore_verdict({}) is False


# --------------------------------------------------------------------------- StompEngine.close


def _engine_with_after(monkeypatch, tmp_path, after: bytes, decoder):
    eng, b = make_engine(monkeypatch, tmp_path)

    def call(cmd, timeout_s=30.0, **a):
        if cmd == "backup":
            Path(a["path"]).write_bytes(after if "_after_restore" in Path(a["path"]).name else b"backup-bytes")
        return FakeBridge.call(b, cmd, timeout_s, **a)

    b.call = call
    Path(eng.backup_path).write_bytes(b"backup-bytes")   # the backup taken at open
    eng._decoder = decoder
    eng.load(make_preset(drive=0.9))
    return eng, b


def test_close_passes_a_reencoded_buffer(monkeypatch, tmp_path):
    def dec(p):
        return dict(DOC)

    eng, b = _engine_with_after(monkeypatch, tmp_path, b"backup-bytes+3", dec)
    eng.close(verify_bytes=True)
    rr = eng.restore_report
    assert rr["match"] is True and rr["bytes_equal_backup"] is False
    assert rr["decoded_equal_backup"] is True and rr["restore_compare"] == "decoded"
    assert rr["restore_ok"] is True and stomp.restore_verdict(rr) is True
    assert b.closed


def test_close_fails_a_real_difference(monkeypatch, tmp_path):
    def dec(p):
        return DOC if "after_restore" not in Path(p).name else {**DOC, "name": "other"}

    eng, _b = _engine_with_after(monkeypatch, tmp_path, b"changed", dec)
    eng.close(verify_bytes=True)
    rr = eng.restore_report
    assert rr["decoded_equal_backup"] is False and rr["restore_ok"] is False
    assert rr["decoded_difference"].startswith(".name")


def test_close_without_decoder_judges_by_bytes(monkeypatch, tmp_path):
    eng, _b = _engine_with_after(monkeypatch, tmp_path, b"changed", None)
    eng.close(verify_bytes=True)
    rr = eng.restore_report
    assert rr["restore_compare"] == "bytes" and rr["decoded_equal_backup"] is None
    assert rr["restore_ok"] is False and "judged by bytes" in rr["decode_unavailable"]


def test_close_decoder_error_judges_by_bytes(monkeypatch, tmp_path):
    def dec(p):
        raise EngineError("no stompd compiler: set TONEMATCH_STOMPD_COMPILE")

    eng, _b = _engine_with_after(monkeypatch, tmp_path, b"backup-bytes", dec)
    eng.close(verify_bytes=True)
    rr = eng.restore_report
    assert rr["bytes_equal_backup"] is True and rr["restore_ok"] is True   # identical: decoder never needed


def test_close_without_verify_is_chain_only(monkeypatch, tmp_path):
    eng, _b = _engine_with_after(monkeypatch, tmp_path, b"x", None)
    eng.close()
    assert eng.restore_report["restore_compare"] == "chain-only"
    assert eng.restore_report["restore_ok"] is eng.restore_report["match"]


# --------------------------------------------------------------------------- real fixture (cache only)


def _offline_decoder() -> Path | None:
    for c in (os.environ.get(stomp.ENV_COMPILER), ROOT / "cache" / "stomp" / "bin" / "stompd-compile",
              ROOT / "cache" / "stomp" / "bin" / "stompd"):
        if c and Path(c).is_file() and stomp.supports(Path(c), "decode"):
            return Path(c)
    return None


def test_push_session_fixture_is_decoded_equal():
    """20:27Z push session: 2456 vs 2459 bytes, the same document once decoded (lead, S1 closed)."""
    a, b = PUSH / "20261016T202716Z_editbuffer.bin", PUSH / "20261016T202716Z_editbuffer_after_restore.bin"
    exe = _offline_decoder()
    if not (a.is_file() and b.is_file()) or exe is None:
        pytest.skip("cache fixture or offline stompd decoder not present")
    r = stomp.compare_backups(a, b, decoder=lambda p: stomp.decode_document(p, compiler=exe))
    assert r["bytes_equal_backup"] is False
    assert r["decoded_equal_backup"] is True and r["restore_compare"] == "decoded"


# --------------------------------------------------------------------------- T108: assignment order


def test_reordered_assignments_are_equal_when_decoded(tmp_path):
    """The 16:52Z verify session: the device read the same three CC 110 assignments back in
    reverse order. Assignments are a set; any other list keeps its order."""
    pa, pb = _write(tmp_path, b"doc-a", b"doc-b")
    rev = {**DOC, "assignments": list(reversed(DOC["assignments"]))}
    r = stomp.compare_backups(pa, pb, decoder=lambda p: {pa: DOC, pb: rev}[p])
    assert r["decoded_equal_backup"] is True and "decoded_difference" not in r
    other = {**DOC, "assignments": ["a", "c"]}
    r = stomp.compare_backups(pa, pb, decoder=lambda p: {pa: DOC, pb: other}[p])
    assert r["decoded_equal_backup"] is False and "assignments" in r["decoded_difference"]
    slots = {**DOC, "slots": list(reversed(DOC["slots"] + [{"pos": 2, "model": "X", "params": []}]))}
    base = {**DOC, "slots": DOC["slots"] + [{"pos": 2, "model": "X", "params": []}]}
    r = stomp.compare_backups(pa, pb, decoder=lambda p: {pa: base, pb: slots}[p])
    assert r["decoded_equal_backup"] is False          # slot order still matters


VERIFY = ROOT / "cache" / "stomp" / "hw" / "20261017T165159Z-verify"


@pytest.mark.skipif(not (VERIFY / "20261017T165200Z_editbuffer_after_restore.bin").exists()
                    or not (ROOT / "cache" / "stomp" / "bin" / "stompd-compile").exists(),
                    reason="the 16:52Z verify buffers and an offline decoder are local (cache/)")
def test_verify_session_buffers_are_decoded_equal():
    exe = ROOT / "cache" / "stomp" / "bin" / "stompd-compile"
    r = stomp.compare_backups(VERIFY / "20261017T165200Z_editbuffer.bin",
                              VERIFY / "20261017T165200Z_editbuffer_after_restore.bin",
                              decoder=lambda p: stomp.decode_document(p, exe))
    assert r["decoded_equal_backup"] is True
