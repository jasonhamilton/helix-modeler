"""T168: the bridge's link-health handling with a fake helper. No pedal.

- every reply's `link` stats are folded into `StompBridge.link_summary`;
- a slow USB write or a slow mid-write credit marks the bridge degraded, after which a further
  whole-document write (`restore`, op 21) is refused with LinkDegraded BEFORE anything is sent,
  while reads, select and quit still work (the session can be closed cleanly);
- TONEMATCH_STOMP_TRACE writes every call, reply and helper TRACE line to a log;
- `drain` is allowed (read-only); writing commands stay outside the allowlist.
"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from tonematch.engines import stomp_bridge as sb
from tonematch.engines.stomp_bridge import ALLOWED_COMMANDS, BridgeError, LinkDegraded, StompBridge

# Fake stompd: ready, then one reply per request. FAKE_LINK_<CMD> (JSON) is that command's `link`
# stats; the default is a healthy link. With STOMPD_TRACE=1 it prints a TRACE line per request.
FAKE = """#!{python}
import json, os, sys
log = open(os.environ["FAKE_LOG"], "a")
def say(m):
    print(json.dumps(m), flush=True)
say({{"ok": True, "event": "ready", "presets": 126, "teardown": "session-close", "release": True}})
healthy = {{"writes": 3, "write_ms_max": 0.4, "slow_writes": 0, "credits": 9, "credit_ms_max": 1.8,
           "ahead_max": 1, "idle_backlog_max": 1, "in_age_ms_max": 3}}
for line in sys.stdin:
    req = json.loads(line)
    cmd = req["cmd"]
    log.write("cmd %s\\n" % cmd); log.flush()
    if os.environ.get("STOMPD_TRACE") == "1":
        print("TRACE " + json.dumps({{"k": "cmd", "cmd": cmd, "t": 1.0}}), file=sys.stderr, flush=True)
    link = json.loads(os.environ.get("FAKE_LINK_" + cmd.upper(), "null")) or healthy
    if cmd == "quit":
        say({{"ok": True, "bye": True}})
        say({{"ok": True, "event": "closed", "release": True, "release_sent": True,
             "acks": {{"events": True, "data": True, "control": True}}, "ms": 900}})
        sys.exit(0)
    if cmd == "drain":
        say({{"ok": True, "drained": 2, "queued": 0, "pump": True, "link": link}})
        continue
    say({{"ok": True, "cmd": cmd, "link": link, **({{"label": "04C", "name": "Smoke Known"}} if cmd == "info" else {{}})}})
"""


@pytest.fixture
def helper(tmp_path, monkeypatch):
    exe = tmp_path / "stompd"
    exe.write_text(FAKE.format(python=sys.executable))
    exe.chmod(0o755)
    log = tmp_path / "log.txt"
    log.write_text("")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.delenv(sb.ENV_TRACE, raising=False)
    monkeypatch.setenv(sb.ENV_USB_RESET, "off")

    def cmds() -> list[str]:
        return [ln.split()[1] for ln in log.read_text().splitlines() if ln.startswith("cmd ")]

    return SimpleNamespace(exe=exe, cmds=cmds, tmp=tmp_path)


def open_bridge(helper, **kw) -> StompBridge:
    return StompBridge(binary=helper.exe, health_check=True, audio_busy=lambda: False, **kw)


def test_healthy_link_is_summarised_and_restore_allowed(helper, tmp_path):
    b = open_bridge(helper, allow_document_write=True)
    try:
        b.call("chain")
        b.call("restore", path=str(tmp_path / "doc.bin"))
        assert b.degraded is None
        ls = b.link_summary
        assert ls["with_stats"] == 3 and ls["writes"] == 9 and ls["credits"] == 27
        assert ls["credit_ms_max"] == 1.8 and ls["write_ms_max"] == 0.4
    finally:
        b.close()
    assert helper.cmds() == ["info", "chain", "restore", "quit"]


def test_slow_write_degrades_and_refuses_the_next_restore_before_sending(helper, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_LINK_CHAIN", json.dumps({"writes": 4, "write_ms_max": 2009.4, "slow_writes": 1}))
    b = open_bridge(helper, allow_document_write=True)
    try:
        b.call("chain")
        assert b.degraded and "2009 ms" in b.degraded
        with pytest.raises(LinkDegraded):
            b.call("restore", path=str(tmp_path / "doc.bin"))
        # reads, select and the session end still work: the run can stop cleanly
        b.call("info")
        b.call("select", setlist=0, index=11)
    finally:
        b.close()
    assert "restore" not in helper.cmds(), "nothing was sent for the refused write"
    assert helper.cmds()[-1] == "quit" and b.released


def test_slow_mid_write_credit_is_a_precursor(helper, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_LINK_RESTORE", json.dumps({"writes": 12, "write_ms_max": 0.5, "credits": 9,
                                                        "credit_ms_max": 94.0}))
    b = open_bridge(helper, allow_document_write=True)
    try:
        b.call("restore", path=str(tmp_path / "a.bin"))   # this one completed
        assert b.degraded and "credit" in b.degraded
        with pytest.raises(LinkDegraded):
            b.call("restore", path=str(tmp_path / "b.bin"))
    finally:
        b.close()
    assert helper.cmds().count("restore") == 1


def test_link_degraded_is_a_bridge_error():
    assert issubclass(LinkDegraded, BridgeError)


def test_thresholds_are_well_above_healthy_traffic():
    # healthy (T168 traces): writes <= 17.8 ms (a flash select), non-final credits <= 16 ms
    assert sb.PRECURSOR_WRITE_MS >= 50 and sb.PRECURSOR_CREDIT_MS >= 16


def test_old_helper_without_link_stats_never_degrades(helper, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_LINK_CHAIN", json.dumps(False))   # falls back to healthy in the fake...
    b = open_bridge(helper)
    try:
        b._note_link("chain", None)   # ...so call the hook directly the way an old helper's reply would
        assert b.degraded is None
    finally:
        b.close()


def test_trace_log_has_calls_replies_and_helper_lines(helper, monkeypatch, tmp_path):
    trace = tmp_path / "t.log"
    monkeypatch.setenv(sb.ENV_TRACE, str(trace))
    b = open_bridge(helper)
    try:
        b.call("drain")
    finally:
        b.close()
    rows = [json.loads(ln) for ln in trace.read_text().splitlines()]
    kinds = [r.get("py") or r.get("k") for r in rows]
    assert "start" in kinds and "call" in kinds and "reply" in kinds and "closed" in kinds
    assert "cmd" in kinds, "the helper's TRACE lines are in the log"
    reply = next(r for r in rows if r.get("py") == "reply" and r["cmd"] == "drain")
    assert reply["link"]["credits"] == 9


def test_trace_path_values(monkeypatch, tmp_path):
    assert sb.trace_path("") is None and sb.trace_path("0") is None
    assert sb.trace_path(str(tmp_path / "x.log")) == tmp_path / "x.log"
    assert sb.trace_path("1").parent.name == "trace"


def test_drain_is_allowed_and_writers_are_not():
    assert "drain" in ALLOWED_COMMANDS
    assert not {"save", "rename", "write_doc", "write_slot", "assign_cc"} & ALLOWED_COMMANDS


def test_an_isolated_slow_idle_keepalive_does_not_degrade(helper, monkeypatch):
    # T168 soak: one 133 ms idle keepalive, normal traffic right after; reported, not a precursor
    monkeypatch.setenv("FAKE_LINK_CHAIN", json.dumps({"writes": 12, "write_ms_max": 0.3, "idle_writes": 9,
                                                      "idle_write_ms_max": 132.6}))
    b = open_bridge(helper)
    try:
        b.call("chain")
        assert b.degraded is None and b.link_summary["idle_write_ms_max"] == 132.6
    finally:
        b.close()


def test_an_idle_write_near_the_write_limit_degrades(helper, monkeypatch):
    monkeypatch.setenv("FAKE_LINK_CHAIN", json.dumps({"writes": 1, "write_ms_max": 0.3, "idle_writes": 3,
                                                      "idle_write_ms_max": 1500.0}))
    b = open_bridge(helper)
    try:
        b.call("chain")
        assert b.degraded and "idle keepalive" in b.degraded
    finally:
        b.close()
