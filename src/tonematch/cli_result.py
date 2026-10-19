"""Result manifest for command-line tools (contract: contracts/cli-result.md, schema ``cli-result/v0``).

A tool that gets ``--result-json PATH`` writes one small JSON file when it finishes: what it was
asked to do (args), which files it read (inputs), which files it wrote (outputs), the headline
numbers (metrics), the ledger rows it appended, and whether it worked. An agent reads that file
instead of scraping stdout.

Usage in a tool::

    ap = argparse.ArgumentParser(...)
    cli_result.add_result_arg(ap)
    a = ap.parse_args(argv)
    with cli_result.scope(a, tool="separate", inputs={"song": a.song}) as res:
        ...                                   # the work
        res.outputs["stems_dir"] = d
        res.metrics["wall_clock_s"] = 12.3
        return 0                              # the return value is NOT seen; set res.exit_code

``scope`` writes ``status="ok"`` on a clean exit with ``exit_code`` 0 and ``status="error"`` otherwise;
an exception is written (type and text) and re-raised. Without ``--result-json`` it does nothing.

The write is atomic (temp file in the same folder, then rename). Paths inside the repo are written
repo-relative with forward slashes; paths outside it stay absolute.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

SCHEMA = "cli-result/v0"
ROOT = Path(__file__).resolve().parents[2]
_SKIP_ARGS = {"result_json"}


def add_result_arg(parser) -> None:
    """Add ``--result-json PATH`` (default off) to an argparse parser."""
    parser.add_argument("--result-json", metavar="PATH", default=None,
                        help="also write a machine-readable result manifest (contracts/cli-result.md) here")


def rel(p: Any, root: Path = ROOT) -> str:
    """Repo-relative POSIX path when ``p`` is inside ``root``, else the absolute path."""
    s = str(p)
    try:
        path = Path(s)
        if not path.is_absolute():
            path = (Path.cwd() / path)
        resolved = path.resolve() if path.exists() else Path(os.path.abspath(path))
        return resolved.relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return s.replace("\\", "/") if os.sep == "\\" else s


def _rel_in_repo(v: Any, root: Path) -> Any:
    """Argument values that are absolute paths inside the repo are written repo-relative (no home folder leaks)."""
    if isinstance(v, Path):
        v = str(v)
    if isinstance(v, str) and v.startswith(str(root) + os.sep):
        return rel(v, root)
    if isinstance(v, (list, tuple)):
        return [_rel_in_repo(x, root) for x in v]
    return v


def _paths(m: dict | None, root: Path) -> dict:
    out: dict = {}
    for k, v in (m or {}).items():
        if v is None:
            continue
        if isinstance(v, (list, tuple, set)):
            out[str(k)] = [rel(x, root) for x in v]
        else:
            out[str(k)] = rel(v, root)
    return out


def _jsonable(v: Any) -> Any:
    if isinstance(v, Path):
        return str(v)
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    if v is None or isinstance(v, (str, bool, int, float)):
        if isinstance(v, float) and math.isnan(v):
            return None                      # NaN is not JSON
        return v
    item = getattr(v, "item", None)          # numpy scalars
    if callable(item):
        try:
            return _jsonable(item())
        except (TypeError, ValueError):
            pass
    return str(v)


def _iso(t: Any) -> str | None:
    if t is None:
        return None
    if isinstance(t, (int, float)):
        t = _dt.datetime.fromtimestamp(t, _dt.UTC)
    if isinstance(t, _dt.datetime):
        if t.tzinfo is None:
            t = t.replace(tzinfo=_dt.UTC)
        return t.astimezone(_dt.UTC).isoformat(timespec="seconds")
    return str(t)


def write_result(path, *, tool: str, version: str | None = None, args: Any = None,
                 status: str = "ok", inputs: dict | None = None, outputs: dict | None = None,
                 metrics: dict | None = None, ledger_rows: list | None = None, notes: str = "",
                 started: Any = None, finished: Any = None, exit_code: int | None = None,
                 error: str | None = None, root: Path = ROOT) -> Path | None:
    """Write the manifest atomically to ``path`` (no-op and None when ``path`` is falsy). Returns the path."""
    if not path:
        return None
    if status not in ("ok", "error"):
        raise ValueError(f"status must be 'ok' or 'error', got {status!r}")
    if version is None:
        try:
            from tonematch import __version__ as version
        except ImportError:
            version = "unknown"
    argd = vars(args) if args is not None and not isinstance(args, dict) else dict(args or {})
    argd = {k: _rel_in_repo(v, root) for k, v in argd.items() if k not in _SKIP_ARGS}
    started_dt = _iso(started)
    finished_dt = _iso(finished) or _iso(_dt.datetime.now(_dt.UTC))
    duration = None
    if started is not None:
        try:
            s = started if isinstance(started, _dt.datetime) else None
            f = finished if isinstance(finished, _dt.datetime) else _dt.datetime.now(_dt.UTC)
            if s is not None:
                s = s if s.tzinfo else s.replace(tzinfo=_dt.UTC)
                f = f if f.tzinfo else f.replace(tzinfo=_dt.UTC)
                duration = round((f - s).total_seconds(), 3)
        except (TypeError, ValueError):
            duration = None
    doc = {
        "schema": SCHEMA,
        "tool": tool,
        "version": version,
        "status": status,
        "exit_code": exit_code if exit_code is not None else (0 if status == "ok" else 1),
        "error": error,
        "started": started_dt,
        "finished": finished_dt,
        "duration_s": duration,
        "args": _jsonable(argd),
        "inputs": _paths(inputs, root),
        "outputs": _paths(outputs, root),
        "metrics": _jsonable(metrics or {}),
        "ledger_rows": _jsonable(ledger_rows or []),
        "notes": notes or "",
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2, sort_keys=False, allow_nan=False)
            f.write("\n")
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def ledger_row_from_args(argv: list[str]) -> dict:
    """Turn the ``--kind K --dataset D ...`` argument list a tool passes to ledger_append.py into a dict."""
    row: dict = {}
    it = iter(argv)
    for tok in it:
        if tok.startswith("--"):
            try:
                val = next(it)
            except StopIteration:
                val = ""
            key = tok[2:].replace("-", "_")
            if key == "value":
                try:
                    val = float(val)
                except ValueError:
                    pass
            row[key] = val
    return row


class Result:
    """Mutable record a tool fills in inside ``scope``."""

    def __init__(self, path, tool: str, args: Any, inputs: dict | None, version: str | None) -> None:
        self.path = path
        self.tool = tool
        self.args = args
        self.version = version
        self.inputs: dict = dict(inputs or {})
        self.outputs: dict = {}
        self.metrics: dict = {}
        self.ledger_rows: list = []
        self.notes = ""
        self.exit_code = 0
        self.started = _dt.datetime.now(_dt.UTC)

    def add_ledger_row(self, row: dict | list[str]) -> None:
        self.ledger_rows.append(ledger_row_from_args(row) if isinstance(row, list) else dict(row))

    def finish(self, status: str, error: str | None = None) -> Path | None:
        return write_result(self.path, tool=self.tool, version=self.version, args=self.args, status=status,
                            inputs=self.inputs, outputs=self.outputs, metrics=self.metrics,
                            ledger_rows=self.ledger_rows, notes=self.notes, started=self.started,
                            finished=_dt.datetime.now(_dt.UTC), exit_code=self.exit_code, error=error)


class scope:  # used like a function: ``with cli_result.scope(...)``
    """Context manager: write the manifest on exit; on an exception write status "error", then re-raise."""

    def __init__(self, args: Any, *, tool: str, inputs: dict | None = None, version: str | None = None) -> None:
        path = getattr(args, "result_json", None) if args is not None and not isinstance(args, dict) \
            else (args or {}).get("result_json")
        self.res = Result(path, tool, args, inputs, version)

    def __enter__(self) -> Result:
        return self.res

    def __exit__(self, exc_type, exc, tb) -> bool:
        r = self.res
        if exc_type is None:
            r.finish("ok" if r.exit_code == 0 else "error",
                     None if r.exit_code == 0 else f"exit code {r.exit_code}")
            return False
        if issubclass(exc_type, SystemExit):
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            r.exit_code = code
            r.finish("ok" if code == 0 else "error",
                     None if code == 0 else (str(exc.code) if exc.code is not None else f"exit code {code}"))
            return False
        r.exit_code = 1
        try:
            r.finish("error", f"{exc_type.__name__}: {exc}")
        except Exception as werr:  # noqa: BLE001  never mask the tool's own exception
            print(f"warning: could not write result manifest: {werr}", file=sys.stderr)
        return False


def run(args: Any, *, tool: str, body, inputs: dict | None = None, version: str | None = None) -> int:
    """``body(res) -> exit code`` inside ``scope``; returns the exit code. The usual way to wire a tool."""
    with scope(args, tool=tool, inputs=inputs, version=version) as res:
        rc = body(res)
        res.exit_code = int(rc or 0)
    return res.exit_code
