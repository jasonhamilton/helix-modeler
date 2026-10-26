"""Tiny standard-MIDI reader (no dependency): notes with onset/offset in seconds, tempo map aware."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MidiNote:
    pitch: int
    onset: float
    offset: float
    velocity: int
    channel: int = 0
    track: int = 0


def _vlq(d: bytes, i: int) -> tuple[int, int]:
    v = 0
    while True:
        b = d[i]
        i += 1
        v = (v << 7) | (b & 0x7F)
        if not b & 0x80:
            return v, i


def _track_events(d: bytes, i: int, end: int, trk: int = 0):
    """Yield (abs_tick, kind, a, b, channel, track) for note events, ('tempo', usec) for tempo."""
    t, run = 0, None
    while i < end:
        dt, i = _vlq(d, i)
        t += dt
        s = d[i]
        if s == 0xFF:
            typ = d[i + 1]
            ln, j = _vlq(d, i + 2)
            if typ == 0x51 and ln == 3:
                yield t, "tempo", int.from_bytes(d[j:j + 3], "big"), 0, 0, trk
            i = j + ln
            if typ == 0x2F:
                return
            continue
        if s in (0xF0, 0xF7):
            ln, j = _vlq(d, i + 1)
            i = j + ln
            continue
        if s & 0x80:
            run, i = s, i + 1
        elif run is None:
            raise ValueError("MIDI running status without a status byte")
        st = run
        n = 1 if (st & 0xF0) in (0xC0, 0xD0) else 2
        a = d[i]
        b = d[i + 1] if n == 2 else 0
        i += n
        k = st & 0xF0
        if k == 0x90 and b > 0:
            yield t, "on", a, b, st & 0x0F, trk
        elif k == 0x80 or (k == 0x90 and b == 0):
            yield t, "off", a, b, st & 0x0F, trk


def read_midi(path: str | Path) -> list[MidiNote]:
    d = Path(path).read_bytes()
    if d[:4] != b"MThd":
        raise ValueError("not a MIDI file")
    _, ntrk, div = struct.unpack(">HHH", d[8:14])
    if div & 0x8000:
        raise ValueError("SMPTE time division not supported")
    i = 14
    ev = []
    for ti in range(ntrk):
        if d[i:i + 4] != b"MTrk":
            break
        ln = struct.unpack(">I", d[i + 4:i + 8])[0]
        ev.extend(_track_events(d, i + 8, i + 8 + ln, ti))
        i += 8 + ln
    tempos = sorted((e[0], e[2]) for e in ev if e[1] == "tempo") or [(0, 500000)]
    if tempos[0][0] != 0:
        tempos.insert(0, (0, 500000))

    def sec(tick: int) -> float:
        s, last_t, us = 0.0, 0, tempos[0][1]
        for tt, u in tempos:
            if tt >= tick:
                break
            s += (tt - last_t) * us / 1e6 / div
            last_t, us = tt, u
        return s + (tick - last_t) * us / 1e6 / div

    open_: dict[tuple[int, int, int], list[tuple[int, int]]] = {}
    out: list[MidiNote] = []
    for t, k, a, b, ch, tr in sorted((e for e in ev if e[1] != "tempo"), key=lambda e: (e[0], e[1] == "on")):
        if k == "on":
            open_.setdefault((tr, ch, a), []).append((t, b))
        else:
            q = open_.get((tr, ch, a))
            if q:
                t0, v = q.pop(0)
                out.append(MidiNote(a, sec(t0), sec(t), v, ch, tr))
    for (tr, ch, a), q in open_.items():      # unterminated notes
        for t0, v in q:
            out.append(MidiNote(a, sec(t0), sec(t0) + 0.25, v, ch, tr))
    return sorted(out, key=lambda n: (n.onset, n.pitch))
