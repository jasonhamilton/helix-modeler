"""Elastic jobs for the host scaler (T138): an item-level work queue and a worker supervisor.

A job that grows and shrinks with ``host_budget``:

* ``ClaimQueue``: workers claim the next unclaimed item with an atomic ``mkdir <run_dir>/.claims/<id>``.
  The claim holds ``owner.json`` (pid, token, heartbeat). A claim whose owner is dead or whose
  heartbeat is old is *suspect*; only a second look, at least ``gap_s`` later, that still finds the same
  owner stale reclaims it (the same two-look rule as leases and the pedal lock). Reclaiming takes a
  short ``<id>.reap`` lock and re-checks, so two reapers cannot both win.
* ``atomic_write_*``: tmp file in the same directory, then ``os.replace``. An item's audio is written
  first and its JSON last, so a stopped worker never leaves a half item (no JSON means not done).
* ``Supervisor``: does no rendering. Every poll it asks the scaler (a *demand lease*, see
  host_budget) how many workers this job may run, spawns one worker when capacity appears (at most
  one per ``stagger_s``), and cooperatively stops its newest worker when the grant drops: a stop
  file is written and the worker exits after its current item, never mid-render. A worker that dies
  unexpectedly has its claim, lease and process group (its Native host) released. SIGTERM or an
  exit of the supervisor stops the workers the same way; workers also watch the supervisor's pid, so a
  SIGKILLed supervisor leaves no worker running past its current item.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- atomic writes


def _tmp_name(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")


def atomic_write_bytes(path: Path | str, data: bytes) -> None:
    path = Path(path)
    tmp = _tmp_name(path)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            with contextlib.suppress(OSError):
                os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def atomic_write_text(path: Path | str, text: str) -> None:
    atomic_write_bytes(path, text.encode())


def atomic_write_json(path: Path | str, obj, **kw) -> None:
    atomic_write_text(path, json.dumps(obj, **kw))


def atomic_write_wav(path: Path | str, data, sr: int, subtype: str = "FLOAT") -> None:
    """Write a WAV to a tmp name and rename it (soundfile needs the format spelled out)."""
    import soundfile as sf

    path = Path(path)
    tmp = _tmp_name(path)
    try:
        sf.write(str(tmp), data, sr, subtype=subtype, format="WAV")
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


# --------------------------------------------------------------------------- liveness


def pid_alive(pid: int) -> bool:
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


# --------------------------------------------------------------------------- the claim queue


class Claim:
    """A held claim. ``release()`` removes it; a heartbeat thread keeps it fresh while held."""

    def __init__(self, queue: ClaimQueue, item_id: str, token: str):
        self.queue, self.id, self.token = queue, item_id, token
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, daemon=True, name=f"claim-{item_id}")
        self._thread.start()

    def _beat(self) -> None:
        while not self._stop.wait(self.queue.beat_s):
            with contextlib.suppress(Exception):
                self.queue._write_owner(self.id, self.token)

    def release(self) -> None:
        self._stop.set()
        self.queue._drop(self.id, self.token)

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        self.release()


class ClaimQueue:
    def __init__(self, run_dir: Path | str, *, stale_s: float = 120.0, gap_s: float = 3.0,
                 beat_s: float = 15.0, now: Callable[[], float] = time.time,
                 alive: Callable[[int], bool] = pid_alive, sleep: Callable[[float], None] = time.sleep):
        self.dir = Path(run_dir) / ".claims"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.stale_s, self.gap_s, self.beat_s = stale_s, gap_s, beat_s
        self.now, self.alive, self.sleep = now, alive, sleep
        self.suspects: dict[str, tuple[str, float]] = {}  # id -> (owner token seen, time of first look)

    # -- plumbing ---------------------------------------------------------------------------

    def _path(self, item_id: str) -> Path:
        return self.dir / item_id

    def _write_owner(self, item_id: str, token: str) -> None:
        atomic_write_json(self._path(item_id) / "owner.json",
                          {"pid": os.getpid(), "token": token, "t": self.now()})

    def _owner(self, item_id: str) -> dict | None:
        try:
            return json.loads((self._path(item_id) / "owner.json").read_text())
        except (OSError, ValueError):
            return None

    def _drop(self, item_id: str, token: str) -> None:
        o = self._owner(item_id)
        if o is None or o.get("token") == token:  # never remove somebody else's claim
            shutil.rmtree(self._path(item_id), ignore_errors=True)

    def _stale(self, item_id: str) -> tuple[bool, str]:
        """(stale now?, owner token). A claim with no owner file yet is judged by its directory age."""
        o = self._owner(item_id)
        if o is None:
            try:
                age = self.now() - self._path(item_id).stat().st_mtime
            except OSError:
                return False, ""
            return age > self.stale_s, "no-owner"
        dead = not self.alive(int(o["pid"]))
        return dead or self.now() - float(o["t"]) > self.stale_s, str(o.get("token"))

    # -- claiming -----------------------------------------------------------------------------

    def try_claim(self, item_id: str) -> Claim | None:
        """Claim ``item_id``; None if somebody holds it (or it is stale and awaits its second look)."""
        p = self._path(item_id)
        try:
            os.mkdir(p)
        except FileExistsError:
            return self._maybe_reclaim(item_id)
        token = uuid.uuid4().hex
        self._write_owner(item_id, token)
        return Claim(self, item_id, token)

    def _maybe_reclaim(self, item_id: str) -> Claim | None:
        stale, token = self._stale(item_id)
        if not stale:
            self.suspects.pop(item_id, None)
            return None
        seen = self.suspects.get(item_id)
        if seen is None or seen[0] != token:
            self.suspects[item_id] = (token, self.now())  # first look
            return None
        if self.now() - seen[1] < self.gap_s:
            return None
        lock = self.dir / f"{item_id}.reap"
        try:
            os.mkdir(lock)
        except FileExistsError:
            with contextlib.suppress(OSError):
                if self.now() - lock.stat().st_mtime > 30:
                    shutil.rmtree(lock, ignore_errors=True)
            return None
        try:
            stale2, token2 = self._stale(item_id)  # re-check under the lock
            if not (stale2 and token2 == token):
                return None
            shutil.rmtree(self._path(item_id), ignore_errors=True)
            self.suspects.pop(item_id, None)
            new_token = uuid.uuid4().hex
            try:
                os.mkdir(self._path(item_id))
            except FileExistsError:
                return None
            self._write_owner(item_id, new_token)
            return Claim(self, item_id, new_token)
        finally:
            shutil.rmtree(lock, ignore_errors=True)

    def claim_next(self, item_ids: Iterable[str], max_looks: int = 2) -> Claim | None:
        """First claimable id in order. If stale claims are waiting for their second look, wait one
        ``gap_s`` and look again (at most ``max_looks`` passes)."""
        ids = list(item_ids)
        for look in range(max_looks):
            for i in ids:
                c = self.try_claim(i)
                if c is not None:
                    return c
            if not any(i in self.suspects for i in ids) or look == max_looks - 1:
                break
            self.sleep(self.gap_s + 0.05)
        return None

    # -- inspection ---------------------------------------------------------------------------

    def held(self) -> dict[str, dict]:
        out = {}
        for p in self.dir.iterdir():
            if p.is_dir() and not p.name.endswith(".reap"):
                out[p.name] = self._owner(p.name) or {}
        return out

    def release_pid(self, pid: int) -> list[str]:
        """Remove every claim owned by ``pid`` (the supervisor, after that worker died)."""
        gone = []
        for item_id, o in self.held().items():
            if o and int(o.get("pid", -1)) == int(pid):
                shutil.rmtree(self._path(item_id), ignore_errors=True)
                gone.append(item_id)
        return gone


def item_done(run_dir: Path | str, item_id: str, redo_since: float | None = None) -> bool:
    """Done = the item JSON exists (written last, atomically). With ``--redo`` an older JSON does not count."""
    f = Path(run_dir) / f"{item_id}.json"
    try:
        m = f.stat().st_mtime
    except OSError:
        return False
    return redo_since is None or m >= redo_since


# --------------------------------------------------------------------------- stop signals


def stop_file(run_dir: Path | str, pid: int) -> Path:
    return Path(run_dir) / ".workers" / f"{pid}.stop"


class StopWatch:
    """What a worker checks between items: its stop file, a SIGTERM, a dead supervisor."""

    def __init__(self, run_dir: Path | str, supervisor_pid: int | None = None, pid: int | None = None,
                 alive: Callable[[int], bool] = pid_alive):
        self.run_dir, self.supervisor_pid, self.pid, self.alive = Path(run_dir), supervisor_pid, pid or os.getpid(), alive
        self.signalled = False

    def install_signals(self, in_item: Callable[[], bool]) -> None:
        """First SIGTERM: finish the current item, then stop. Second SIGTERM, or one while no item is
        being rendered (engine opening): stop now (the caller's ``finally`` closes the engine)."""
        def handler(signum, frame):
            if self.signalled or not in_item():
                raise SystemExit(128 + signum)
            self.signalled = True
        signal.signal(signal.SIGTERM, handler)

    def should_stop(self) -> bool:
        if self.signalled or stop_file(self.run_dir, self.pid).exists():
            return True
        return bool(self.supervisor_pid and not self.alive(self.supervisor_pid))


# --------------------------------------------------------------------------- the supervisor


@dataclass
class Worker:
    proc: object            # subprocess.Popen-like: .pid, .poll(), .returncode, .send_signal
    started: float
    stopping: bool = False
    pid: int = field(init=False)

    def __post_init__(self) -> None:
        self.pid = self.proc.pid


class Supervisor:
    """Keeps as many workers as the scaler grants this job, up to the items left."""

    def __init__(self, *, run_dir: Path | str, left: Callable[[], int], total: int,
                 spawn: Callable[[], object], lease, max_workers: int, queue: ClaimQueue | None = None,
                 budget=None, priority: str = "normal", poll_s: float = 10.0, stagger_s: float = 10.0,
                 grace_s: float = 600.0, max_failures: int = 5, shrink_polls: int = 2, now: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, killpg: Callable[[int, int], None] | None = None,
                 log: Callable[[str], None] = lambda m: print(m, flush=True)):
        self.run_dir, self.left, self.total, self.spawn, self.lease = Path(run_dir), left, total, spawn, lease
        self.max_workers, self.queue, self.budget, self.priority = max_workers, queue, budget, priority
        self.poll_s, self.stagger_s, self.grace_s, self.max_failures = poll_s, stagger_s, grace_s, max_failures
        self.now, self.sleep, self.log = now, sleep, log
        self.killpg = killpg or _killpg
        self.workers: list[Worker] = []
        self.last_spawn = -1e9
        self.failures = 0
        self.shrink_polls, self.below = shrink_polls, 0
        self.terminated = False
        self.history: list[dict] = []
        (self.run_dir / ".workers").mkdir(parents=True, exist_ok=True)

    # -- helpers ------------------------------------------------------------------------------

    def active(self) -> list[Worker]:
        return [w for w in self.workers if not w.stopping]

    def _request_stop(self, w: Worker) -> None:
        w.stopping = True
        atomic_write_text(stop_file(self.run_dir, w.pid), "stop after the current item\n")
        self.log(f"supervisor: stopping worker {w.pid} after its current item ({len(self.active())} left running)")

    def _cleanup_dead(self, w: Worker, rc: int) -> None:
        """A worker that exited. Always forget its stop file; when it died unexpectedly release its
        claim and lease and reap its process group (its Native host)."""
        with contextlib.suppress(OSError):
            stop_file(self.run_dir, w.pid).unlink()
        clean = rc == 0
        if self.queue is not None:
            freed = self.queue.release_pid(w.pid)
            if freed and clean:
                self.log(f"supervisor: worker {w.pid} exited with claims {freed}; released")
        if self.budget is not None:
            self.budget.release_pid(w.pid)
        if not clean:
            self.log(f"supervisor: worker {w.pid} died (rc {rc}); claim, lease and host released")
            for sig in (signal.SIGTERM, signal.SIGKILL):
                with contextlib.suppress(OSError, ProcessLookupError):
                    self.killpg(w.pid, sig)

    # -- one poll -----------------------------------------------------------------------------

    def step(self) -> bool:
        """One supervision step. Returns True when the job is finished (nothing left, no workers)."""
        left_before = self.left()
        for w in list(self.workers):
            rc = w.proc.poll()
            if rc is not None:
                self.workers.remove(w)
                self._cleanup_dead(w, rc)
                if rc != 0 and not w.stopping:
                    self.failures += 1
                elif rc == 0:
                    self.failures = 0
        left = self.left()
        if left < left_before:
            self.failures = 0
        if self.failures >= self.max_failures:
            raise RuntimeError(f"{self.failures} workers in a row died without finishing an item; giving up")
        wanted = min(self.max_workers, left)
        info = {"workers": len(self.workers), "left": left, "done": self.total - left, "total": self.total,
                "priority": self.priority, "supervisor": True}
        granted = self.lease.refresh(wanted, info) if wanted > 0 else self.lease.granted
        active = self.active()
        self.history.append({"t": self.now(), "granted": granted, "wanted": wanted,
                             "workers": len(active), "left": left})
        if self.terminated:
            return False
        self.below = self.below + 1 if len(active) > granted else 0  # ignore a one-poll dip (no flapping)
        if len(active) > granted and self.below >= self.shrink_polls:
            for w in sorted(active, key=lambda w: -w.started)[:len(active) - granted]:  # newest first
                self._request_stop(w)
        elif len(active) <= granted and len(self.workers) < min(granted, wanted) and self.now() - self.last_spawn >= self.stagger_s:  # stopping ones still hold a host
            proc = self.spawn()
            self.workers.append(Worker(proc, self.now()))
            self.last_spawn = self.now()
            self.log(f"supervisor: started worker {proc.pid} ({len(self.workers)} running, granted {granted}, "
                     f"{left} items left)")
        return left == 0 and not self.workers

    # -- the loop -----------------------------------------------------------------------------

    def run(self) -> bool:
        """Poll until done. Returns True if the job finished, False if it was terminated."""
        try:
            while True:
                if self.step():
                    return True
                if self.terminated:
                    return False
                self.sleep(self.poll_s)
        finally:
            self.shutdown()

    def terminate(self) -> None:
        self.terminated = True

    def shutdown(self) -> None:
        """Stop every worker cooperatively; escalate after ``grace_s``. Leaves no worker behind."""
        for w in self.workers:
            if not w.stopping:
                self._request_stop(w)
        deadline = self.now() + self.grace_s
        while self.workers and self.now() < deadline:
            for w in list(self.workers):
                rc = w.proc.poll()
                if rc is not None:
                    self.workers.remove(w)
                    self._cleanup_dead(w, rc)
            if self.workers:
                self.sleep(min(2.0, self.poll_s))
        for w in list(self.workers):  # grace over: SIGTERM (the worker aborts the item), then kill
            self.log(f"supervisor: worker {w.pid} still running after {self.grace_s:.0f} s; terminating it")
            with contextlib.suppress(OSError, ProcessLookupError):
                w.proc.send_signal(signal.SIGTERM)
            for _ in range(10):
                if w.proc.poll() is not None:
                    break
                self.sleep(0.5)
            if w.proc.poll() is None:
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    with contextlib.suppress(OSError, ProcessLookupError):
                        self.killpg(w.pid, sig)
                self.sleep(0.2)
            self.workers.remove(w)
            self._cleanup_dead(w, w.proc.poll() if w.proc.poll() is not None else -9)
        with contextlib.suppress(Exception):
            self.lease.release()


def _killpg(pgid: int, sig: int) -> None:
    os.killpg(pgid, sig)
