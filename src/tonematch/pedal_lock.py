"""The HX Stomp device check and the shared pedal lock (stomp-engine, T090).

Every pedal user takes this lock: ``StompBridge`` takes it for each control session (so
``StompEngine`` and every script that opens a bridge are covered), and batch scripts wrap their
whole run in :func:`session` so that no other agent gets the pedal between two sessions of one
batch. The lock is re-entrant inside one process and inherited by child processes through
``TONEMATCH_PEDAL_LOCK_TOKEN``.

Device check (:func:`require_device`): before a session, the HX Stomp must be on USB (macOS: the
IOUSB registry names "HX Stomp"; Linux: sysfs; other systems: a hook, :data:`PROBES`, unknown means
"not checked"). If it is missing the user is told ``HX Stomp not detected over USB: check the cable
and power``. Interactive runs (a terminal on stdin and stderr) may retry or abort; non-interactive
runs (agents) stop at once with exit code :data:`EXIT_NO_DEVICE` and never loop. stompd's own "no HX
device found" is mapped to the same error by ``StompBridge``.

Lock layout (default ``cache/stomp/pedal.lock``, override ``TONEMATCH_PEDAL_LOCK_DIR``):
- ``pedal.lock/owner.json``: pid, agent (``TONEMATCH_AGENT``), host, purpose, created_utc,
  heartbeat_utc, token. The heartbeat is rewritten about every :data:`HEARTBEAT_S` seconds while
  the lock is held.
- ``pedal.intent/<pid>.json``: written before an attempt to take the lock and removed after it,
  so an agent that is between lock states is visible to everyone else.
- ``pedal.lock.log``: one JSON line per removal (who, why, the checks).

A lock is removed without asking only when it is confidently stale, i.e. ALL of:
(a) its owner pid is not alive (same host only; another host can never be checked);
(b) no ``stompd`` process runs, checked twice :data:`STOMPD_RECHECK_S` apart, so the gap between
    two sessions of a batch is not taken for idle;
(c) its heartbeat is older than :data:`STALE_AFTER_S` (and than :data:`HEARTBEAT_LIMIT_FACTOR`
    heartbeat periods, the longest gap a live session can leave);
(d) no fresh intent of another live pid exists (someone is between lock states or about to lock).
A lock with no owner.json (the legacy mkdir-only kind, or one a moment old) fails (a) and (c) by
definition, so it always goes to the user. Otherwise the lock is never removed silently: an
interactive run shows owner.json, its age and the checks and asks; a non-interactive run stops
with exit code :data:`EXIT_LOCK_HELD` and says how to remove it (``python -m tonematch.pedal_lock``).

The errors subclass ``SystemExit`` with an int code, so an agent's script that does not catch them
exits with the distinct code (the message is printed to stderr when raised, and is ``str(e)``);
``except Exception`` blocks do not swallow them.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import platform
import random
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

ROOT = Path(__file__).resolve().parents[2]
ENV_LOCK_DIR = "TONEMATCH_PEDAL_LOCK_DIR"
ENV_TOKEN = "TONEMATCH_PEDAL_LOCK_TOKEN"
ENV_INTERACTIVE = "TONEMATCH_INTERACTIVE"      # 1/0 forces the prompt on or off
ENV_PROBE = "TONEMATCH_STOMP_DEVICE_CHECK"      # "off" skips the USB presence check

DEVICE_MISSING_MESSAGE = "HX Stomp not detected over USB: check the cable and power"
EXIT_NO_DEVICE = 69    # sysexits EX_UNAVAILABLE
EXIT_LOCK_HELD = 75    # sysexits EX_TEMPFAIL

HEARTBEAT_S = 30.0
HEARTBEAT_LIMIT_FACTOR = 4          # a live owner never goes this many heartbeats without one
STALE_AFTER_S = 120.0
STOMPD_RECHECK_S = 5.0
INTENT_FRESH_S = 60.0
POLL_S = 15.0
VENDOR_ID = 0x0E41
PRODUCT_ID_HX_STOMP = 0x4246


def lock_dir() -> Path:
    env = os.environ.get(ENV_LOCK_DIR)
    return Path(env).expanduser() if env else ROOT / "cache" / "stomp" / "pedal.lock"


def _utc(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_utc(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.UTC).timestamp()
    except ValueError:
        try:
            return dt.datetime.fromisoformat(s).timestamp()
        except ValueError:
            return None


def interactive() -> bool:
    """A human can answer: terminals on stdin and stderr (agents run without), unless forced."""
    env = os.environ.get(ENV_INTERACTIVE)
    if env is not None and env.strip() != "":
        return env.strip().lower() in ("1", "yes", "true", "on")
    try:
        return sys.stdin.isatty() and sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


# --------------------------------------------------------------------------- errors


class PedalUnavailable(SystemExit):
    """The pedal cannot be used now. ``code`` is the process exit code; ``str(e)`` the message."""

    exit_code = 1

    def __init__(self, message: str, detail: dict | None = None, announce: bool = True):
        super().__init__(self.exit_code)
        self.message = message
        self.detail = detail or {}
        if announce:
            print(message, file=sys.stderr, flush=True)

    def __str__(self) -> str:
        return self.message


class DeviceMissing(PedalUnavailable):
    exit_code = EXIT_NO_DEVICE

    def __init__(self, message: str = DEVICE_MISSING_MESSAGE, detail: dict | None = None, announce: bool = True):
        super().__init__(message, detail, announce)


class LockHeld(PedalUnavailable):
    exit_code = EXIT_LOCK_HELD


# --------------------------------------------------------------------------- device check


def _probe_macos() -> bool | None:
    try:
        out = subprocess.run(["ioreg", "-p", "IOUSB", "-l", "-w0"], check=False, capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return "HX Stomp" in out or (f'"idVendor" = {VENDOR_ID}' in out and f'"idProduct" = {PRODUCT_ID_HX_STOMP}' in out)


def _probe_linux() -> bool | None:
    base = Path("/sys/bus/usb/devices")
    if not base.exists():
        return None
    for dev in base.glob("*"):
        try:
            v, p = (dev / "idVendor").read_text().strip(), (dev / "idProduct").read_text().strip()
        except OSError:
            continue
        try:
            if int(v, 16) == VENDOR_ID and int(p, 16) == PRODUCT_ID_HX_STOMP:
                return True
        except ValueError:
            continue
        with contextlib.suppress(OSError):
            if "HX Stomp" in (dev / "product").read_text():
                return True
    return False


# The hook for other systems: platform.system() -> probe returning True (present), False (absent)
# or None (cannot tell: the session goes ahead and stompd's own error is mapped instead).
PROBES: dict[str, Callable[[], bool | None]] = {"Darwin": _probe_macos, "Linux": _probe_linux}


def device_present(probe: Callable[[], bool | None] | None = None) -> bool | None:
    if os.environ.get(ENV_PROBE, "").strip().lower() in ("off", "0", "no"):
        return None
    fn = probe or PROBES.get(platform.system())
    if fn is None:
        return None
    try:
        return fn()
    except Exception:  # noqa: BLE001 - a broken probe must not block a session; stompd still checks
        return None


def ask_retry(message: str = DEVICE_MISSING_MESSAGE, prompt: Callable[[str], str] = input) -> bool:
    """Interactive only: show the message and ask retry/abort. True = retry."""
    print(message, file=sys.stderr, flush=True)
    try:
        ans = prompt("[r]etry or [a]bort? ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("r", "retry", "y", "yes", "")


def require_device(probe: Callable[[], bool | None] | None = None, is_interactive: bool | None = None,
                   prompt: Callable[[str], str] = input) -> bool | None:
    """Return when the HX Stomp is on USB (or presence cannot be checked here: None). When it is
    missing: interactive, ask retry/abort (each retry is the user's choice); non-interactive, raise
    DeviceMissing at once."""
    inter = interactive() if is_interactive is None else is_interactive
    while True:
        present = device_present(probe)
        if present is not False:
            return present
        if not inter:
            raise DeviceMissing()
        if not ask_retry(DEVICE_MISSING_MESSAGE, prompt):
            raise DeviceMissing(announce=False)


# --------------------------------------------------------------------------- processes


class ProcessTable:
    """What the stale check asks of the operating system. Tests pass a fake."""

    host = socket.gethostname()

    def alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def stompd_pids(self) -> list[int]:
        """Every running process whose executable name starts with ``stompd`` (the USB helper;
        its offline compile build too, to err on the side of 'busy')."""
        try:
            out = subprocess.run(["ps", "-axo", "pid=,comm="], check=False, capture_output=True, text=True,
                                 timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return [-1]   # cannot tell: treat as running (never confidently idle)
        pids = []
        for line in out.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and os.path.basename(parts[1]).startswith("stompd"):
                with contextlib.suppress(ValueError):
                    pids.append(int(parts[0]))
        return pids


@dataclass
class Env:
    """Injectable clock, sleep, process table and prompt (tests)."""

    procs: ProcessTable = field(default_factory=ProcessTable)
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    prompt: Callable[[str], str] = input
    out: Callable[[str], None] = field(default=lambda s: print(s, file=sys.stderr, flush=True))


# --------------------------------------------------------------------------- files


def _read_json(p: Path) -> dict | None:
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _write_json_atomic(p: Path, d: dict) -> None:
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(d, indent=1))
    os.replace(tmp, p)


def intent_dir(ld: Path) -> Path:
    return ld.with_name(ld.name.replace(".lock", "") + ".intent")


def log_path(ld: Path) -> Path:
    return ld.with_name(ld.name + ".log")


@dataclass
class Assessment:
    exists: bool
    owner: dict | None
    age_s: float | None             # since the newest heartbeat (or the directory's mtime)
    checks: dict                    # name -> {"ok": bool, "why": str}
    stale: bool

    def text(self) -> str:
        lines = [f"owner.json: {json.dumps(self.owner) if self.owner else 'none (legacy mkdir-only lock, or one being written)'}",
                 f"age since last heartbeat: {'unknown' if self.age_s is None else f'{self.age_s:.0f} s'}"]
        for k, v in self.checks.items():
            lines.append(f"  check {k}: {'PASS' if v['ok'] else 'FAIL'} - {v['why']}")
        return "\n".join(lines)


def _fresh_intents(ld: Path, env: Env, me: int) -> list[dict]:
    out = []
    d = intent_dir(ld)
    if not d.is_dir():
        return out
    for f in d.glob("*.json"):
        info = _read_json(f) or {}
        try:
            pid = int(info.get("pid", f.stem))
        except (TypeError, ValueError):
            continue
        if pid == me:
            continue
        t = _parse_utc(info.get("updated_utc")) or f.stat().st_mtime
        same_host = info.get("host", env.procs.host) == env.procs.host
        alive = env.procs.alive(pid) if same_host else True
        if not alive and same_host:
            with contextlib.suppress(OSError):
                f.unlink()   # a dead pid's intent is litter
            continue
        if env.now() - t <= INTENT_FRESH_S:
            out.append({"pid": pid, **info})
    return out


def assess(ld: Path | None = None, env: Env | None = None, me: int | None = None,
           recheck_stompd: bool = True) -> Assessment:
    """Evaluate the stale checks (a)-(d) on the lock at ``ld``. Pure apart from the injected env."""
    ld = ld or lock_dir()
    env = env or Env()
    me = os.getpid() if me is None else me
    if not ld.exists():
        return Assessment(False, None, None, {}, False)
    owner = _read_json(ld / "owner.json")
    hb = None
    if owner:
        hb = max(x for x in (_parse_utc(owner.get("heartbeat_utc")), _parse_utc(owner.get("created_utc")), 0.0)
                 if x is not None)
        with contextlib.suppress(OSError):
            hb = max(hb, (ld / "owner.json").stat().st_mtime)
    else:
        with contextlib.suppress(OSError):
            hb = ld.stat().st_mtime
    age = None if hb is None else env.now() - hb
    checks: dict = {}
    # (a) owner pid
    if not owner or "pid" not in owner:
        checks["a_owner_dead"] = {"ok": False, "why": "no owner.json: the owner cannot be checked"}
    elif owner.get("host") not in (None, env.procs.host):
        checks["a_owner_dead"] = {"ok": False, "why": f"owned on another host ({owner.get('host')}): cannot check its pid"}
    else:
        alive = env.procs.alive(int(owner["pid"]))
        checks["a_owner_dead"] = {"ok": not alive, "why": f"pid {owner['pid']} is {'alive' if alive else 'not running'}"}
    # (c) heartbeat age
    limit = max(STALE_AFTER_S, HEARTBEAT_LIMIT_FACTOR * HEARTBEAT_S)
    if not owner:
        checks["c_heartbeat_old"] = {"ok": False, "why": "no owner.json: no heartbeat to judge"}
    else:
        checks["c_heartbeat_old"] = {"ok": age is not None and age > limit,
                                     "why": f"heartbeat {age:.0f} s ago (limit {limit:.0f} s)" if age is not None
                                     else "no heartbeat time"}
    # (d) intents
    fresh = _fresh_intents(ld, env, me)
    checks["d_no_intent"] = {"ok": not fresh, "why": "no other agent is about to lock" if not fresh else
                             "fresh intent from live pid(s) " + ", ".join(str(i["pid"]) for i in fresh)}
    # (b) stompd, twice, only when everything else already says stale (it costs the wait)
    cheap_ok = all(v["ok"] for v in checks.values())
    if cheap_ok:
        first = env.procs.stompd_pids()
        second: list[int] = []
        if not first and recheck_stompd:
            env.sleep(STOMPD_RECHECK_S)
            second = env.procs.stompd_pids()
        idle = not first and not second
        checks["b_no_stompd"] = {"ok": idle, "why": (f"no stompd process in two checks {STOMPD_RECHECK_S:.0f} s apart"
                                                     if idle else f"stompd running (pids {first or second})")}
        if idle:   # the wait may have let someone start: look at the intents and the owner again
            fresh = _fresh_intents(ld, env, me)
            if fresh:
                checks["d_no_intent"] = {"ok": False, "why": "fresh intent appeared during the stompd check: pid(s) "
                                         + ", ".join(str(i["pid"]) for i in fresh)}
            if _read_json(ld / "owner.json") != owner:
                checks["a_owner_dead"] = {"ok": False, "why": "owner.json changed during the check"}
    else:
        pids = env.procs.stompd_pids()
        checks["b_no_stompd"] = {"ok": not pids, "why": "not re-checked (another check already failed); "
                                 + (f"stompd running (pids {pids})" if pids else "no stompd seen now")}
    stale = all(v["ok"] for v in checks.values()) and len(checks) == 4
    return Assessment(True, owner, age, checks, stale)


def _log(ld: Path, entry: dict) -> None:
    with contextlib.suppress(OSError), log_path(ld).open("a") as f:
        f.write(json.dumps(entry) + "\n")


def remove_lock(ld: Path, a: Assessment, why: str, env: Env | None = None) -> bool:
    """Remove the lock that ``a`` assessed, and only that one: it is renamed aside first, and put
    back if what was renamed is not the assessed lock. Logged either way."""
    env = env or Env()
    if _read_json(ld / "owner.json") != a.owner:
        env.out(f"pedal lock {ld} changed while it was being checked; not removed")
        return False
    tomb = ld.with_name(f"{ld.name}.removed-{int(env.now())}-{os.getpid()}")
    try:
        os.rename(ld, tomb)
    except FileNotFoundError:
        return True
    except OSError as e:
        env.out(f"could not move the pedal lock aside ({e}); not removed")
        return False
    if _read_json(tomb / "owner.json") != a.owner:
        with contextlib.suppress(OSError):
            os.rename(tomb, ld)
        _log(ld, {"utc": _utc(env.now()), "event": "remove-aborted", "why": "lock replaced during removal",
                  "by_pid": os.getpid()})
        return False
    shutil.rmtree(tomb, ignore_errors=True)
    _log(ld, {"utc": _utc(env.now()), "event": "removed", "why": why, "by_pid": os.getpid(),
              "by_agent": os.environ.get("TONEMATCH_AGENT"), "owner": a.owner, "age_s": a.age_s, "checks": a.checks})
    env.out(f"removed pedal lock {ld}: {why}")
    return True


# --------------------------------------------------------------------------- the lock


class _Held:
    def __init__(self, ld: Path, owner: dict, own: bool, env: Env):
        self.ld, self.owner, self.own, self.env = ld, owner, own, env
        self.count = 1
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if own:
            self._thread = threading.Thread(target=self._beat, name="pedal-lock-heartbeat", daemon=True)
            self._thread.start()

    def _beat(self) -> None:
        while not self._stop.wait(HEARTBEAT_S):
            self.heartbeat()

    def heartbeat(self) -> None:
        if not self.own or not self.ld.is_dir():
            return
        cur = _read_json(self.ld / "owner.json")
        if cur is not None and cur.get("token") != self.owner.get("token"):
            return    # our lock was removed and someone else holds it now: never overwrite theirs
        self.owner["heartbeat_utc"] = _utc(self.env.now())
        with contextlib.suppress(OSError):
            _write_json_atomic(self.ld / "owner.json", self.owner)

    def release(self) -> None:
        self._stop.set()
        if not self.own:
            return
        cur = _read_json(self.ld / "owner.json")
        if cur and cur.get("token") == self.owner.get("token"):
            shutil.rmtree(self.ld, ignore_errors=True)
        if os.environ.get(ENV_TOKEN) == self.owner.get("token"):
            os.environ.pop(ENV_TOKEN, None)


_HELD: dict[str, _Held] = {}


def _key(ld: Path) -> str:
    return os.path.abspath(str(ld))


_HELD_LOCK = threading.Lock()


class PedalLock:
    """One hold on the pedal lock. ``acquire()`` / ``release()`` or use as a context manager."""

    def __init__(self, purpose: str, wait_s: float = 0.0, poll_s: float = POLL_S, ld: Path | None = None,
                 is_interactive: bool | None = None, env: Env | None = None):
        self.purpose = purpose
        self.wait_s = float(wait_s)
        self.poll_s = float(poll_s)
        self.ld = Path(ld) if ld else lock_dir()
        self.is_interactive = interactive() if is_interactive is None else is_interactive
        self.env = env or Env()
        self._held: _Held | None = None
        self.removed: list[str] = []     # why stale locks were removed while acquiring

    # -- intents ----------------------------------------------------------------------------
    def _intent_path(self) -> Path:
        return intent_dir(self.ld) / f"{os.getpid()}.json"

    def _write_intent(self) -> None:
        p = self._intent_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(p, {"pid": os.getpid(), "host": self.env.procs.host, "purpose": self.purpose,
                               "agent": os.environ.get("TONEMATCH_AGENT"), "updated_utc": _utc(self.env.now())})

    def _drop_intent(self) -> None:
        with contextlib.suppress(OSError):
            self._intent_path().unlink()

    # -- acquire ------------------------------------------------------------------------------
    def _try_mkdir(self) -> dict | None:
        self.ld.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.ld.mkdir()
        except FileExistsError:
            return None
        t = self.env.now()
        owner = {"pid": os.getpid(), "agent": os.environ.get("TONEMATCH_AGENT"), "host": self.env.procs.host,
                 "purpose": self.purpose, "created_utc": _utc(t), "heartbeat_utc": _utc(t),
                 "token": uuid.uuid4().hex, "argv": " ".join(sys.argv)[:300]}
        _write_json_atomic(self.ld / "owner.json", owner)
        return owner

    def _reuse(self) -> bool:
        """Re-entrant in this process; inherited by a child whose parent holds the lock."""
        key = _key(self.ld)
        with _HELD_LOCK:
            h = _HELD.get(key)
            if h is not None and self.ld.is_dir():
                h.count += 1
                self._held = h
                return True
        tok = os.environ.get(ENV_TOKEN)
        owner = _read_json(self.ld / "owner.json") if tok else None
        if tok and owner and owner.get("token") == tok and self.env.procs.alive(int(owner.get("pid", -1))):
            h = _Held(self.ld, owner, own=False, env=self.env)
            with _HELD_LOCK:
                _HELD[_key(self.ld)] = h
            self._held = h
            return True
        return False

    def acquire(self) -> Self:
        if self._held is not None:
            return self
        if self._reuse():
            return self
        deadline = self.env.now() + self.wait_s
        told = False
        while True:
            self._write_intent()
            try:
                owner = self._try_mkdir()
                if owner is not None:
                    h = _Held(self.ld, owner, own=True, env=self.env)
                    with _HELD_LOCK:
                        _HELD[_key(self.ld)] = h
                    self._held = h
                    os.environ[ENV_TOKEN] = owner["token"]
                    return self
                a = assess(self.ld, self.env)
                if not a.exists:
                    continue    # released between mkdir and the check: try again at once
                if a.stale:
                    why = "stale: " + "; ".join(v["why"] for v in a.checks.values())
                    if remove_lock(self.ld, a, why, self.env):
                        self.removed.append(why)
                        continue
                    # not removed (it changed under us): treat it as held
            finally:
                self._drop_intent()
            if self.env.now() < deadline:
                if not told:
                    who = (a.owner or {}).get("agent") or (a.owner or {}).get("pid") or "unknown owner"
                    self.env.out(f"pedal lock held by {who} ({(a.owner or {}).get('purpose', 'no owner.json')}); "
                                 f"waiting up to {self.wait_s:.0f} s")
                    told = True
                self.env.sleep(min(self.poll_s * random.uniform(0.8, 1.2), max(0.0, deadline - self.env.now()) + 0.01))
                continue
            return self._not_stale(a)

    def _not_stale(self, a: Assessment) -> Self:
        info = (f"The HX Stomp is locked by another session ({self.ld}).\n{a.text()}")
        if self.is_interactive:
            self.env.out(info)
            try:
                ans = self.env.prompt("Delete this lock and continue? Only if you are sure no pedal session is "
                                      "running. [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = ""
            if ans in ("y", "yes") and remove_lock(self.ld, a, "removed by the user at the prompt", self.env):
                self.removed.append("user")
                self.wait_s = 0.0
                return self.acquire()
            raise LockHeld("pedal lock kept; nothing was done with the pedal", {"assessment": a.checks})
        raise LockHeld(
            f"{info}\nNot removed: it is not confidently stale. If you are sure no pedal session is running, "
            f"remove it with `python -m tonematch.pedal_lock break` (asks, shows the checks) or "
            f"`rm -rf {self.ld}`, then run again. Nothing was done with the pedal.",
            {"owner": a.owner, "age_s": a.age_s, "checks": a.checks})

    def release(self) -> None:
        h, self._held = self._held, None
        if h is None:
            return
        with _HELD_LOCK:
            h.count -= 1
            if h.count > 0:
                return
            for k, v in list(_HELD.items()):
                if v is h:
                    del _HELD[k]
        h.release()

    @property
    def held(self) -> bool:
        return self._held is not None

    @property
    def owner(self) -> dict | None:
        return self._held.owner if self._held else None

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


def held_here(ld: Path | None = None) -> bool:
    """Whether this process (or its parent, by token) holds the lock."""
    with _HELD_LOCK:
        return _key(ld or lock_dir()) in _HELD


@contextlib.contextmanager
def session(purpose: str, wait_s: float = 0.0, check_device: bool = True, **kw) -> Iterator[PedalLock]:
    """Device check, then the lock, for a whole batch of pedal work."""
    if check_device:
        require_device(is_interactive=kw.get("is_interactive"))
    lk = PedalLock(purpose, wait_s=wait_s, **kw).acquire()
    try:
        yield lk
    finally:
        lk.release()


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m tonematch.pedal_lock", description="Show or remove the pedal lock.")
    ap.add_argument("cmd", choices=("status", "break", "device"))
    ap.add_argument("--yes", action="store_true", help="break: remove without asking (you checked yourself)")
    a = ap.parse_args(argv)
    ld = lock_dir()
    if a.cmd == "device":
        p = device_present()
        print({True: "HX Stomp on USB", False: DEVICE_MISSING_MESSAGE, None: "presence cannot be checked here"}[p])
        return 0 if p is not False else EXIT_NO_DEVICE
    ass = assess(ld)
    if not ass.exists:
        print(f"no pedal lock at {ld}")
        return 0
    print(f"pedal lock {ld}\n{ass.text()}\nconfidently stale: {ass.stale}")
    if a.cmd == "status":
        return 0
    if not a.yes:
        if not interactive():
            print("not removed: run in a terminal to be asked, or pass --yes", file=sys.stderr)
            return EXIT_LOCK_HELD
        if input("Delete this lock? [y/N] ").strip().lower() not in ("y", "yes"):
            return EXIT_LOCK_HELD
    return 0 if remove_lock(ld, ass, "removed by the user (pedal_lock break)" + (" --yes" if a.yes else "")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
