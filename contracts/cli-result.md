# CLI result manifest, v0

Owner: `platform` (proposed; the lead ratifies). Implemented by `src/tonematch/cli_result.py`. Additive: no existing contract, script output or ledger column changes. Read by any agent that is handed a finished run.

A command-line tool that accepts `--result-json PATH` writes one small JSON file when it finishes. The file says what the tool was asked to do, which files it read and wrote, the headline numbers, which ledger rows it appended, and whether it worked. An agent reads this file instead of scraping stdout.

Without the option a tool behaves exactly as before and writes nothing extra.

## Schema `cli-result/v0`

```json
{
  "schema": "cli-result/v0",
  "tool": "separate",
  "version": "0.0.1",
  "status": "ok",
  "exit_code": 0,
  "error": null,
  "started": "2026-10-18T20:20:41+00:00",
  "finished": "2026-10-18T20:22:09+00:00",
  "duration_s": 88.1,
  "args": {"song": "data/songs/Mother.mp3", "model": "htdemucs_6s", "no_ledger": false},
  "inputs": {"song": "data/songs/Mother.mp3"},
  "outputs": {"stems_dir": "cache/stems/2ec11f9795be822e/htdemucs_6s"},
  "metrics": {"wall_clock_s": 85.2, "confidence_mean": 0.61},
  "ledger_rows": [{"kind": "separate", "engine": "-", "dataset": "Mother-2ec11f97", "metric": "wall_clock_s", "value": 85.2}],
  "notes": ""
}
```

| Field | Type | Meaning |
|---|---|---|
| `schema` | string | Always `cli-result/v0`. A breaking change gets a new string. |
| `tool` | string | The script name without `.py`; a sub-command is appended (`synthetic_v1 run`). |
| `version` | string | `tonematch.__version__` at the time of the run. |
| `status` | `"ok"` or `"error"` | `ok` only when the tool finished and its exit code is 0. `error` when it raised, or exited non-zero (including a check that ran and failed, such as a failing `hlx_validate`). |
| `exit_code` | int | The process exit code the tool returns. |
| `error` | string or null | `ExceptionType: text` for an exception, `exit code N` for a plain non-zero exit, else null. A reason for a handled failure is in `notes`. |
| `started`, `finished` | ISO 8601 UTC | Wall-clock start and end. |
| `duration_s` | number | `finished - started`. |
| `args` | object | The parsed command line (`vars(args)`), JSON-safe, without `result_json`. |
| `inputs` | object name to path, or to a list of paths | Files and folders the tool read. Only what it knows; a tool that reads a whole cache folder lists the folder. |
| `outputs` | object name to path, or to a list of paths | Files and folders the tool wrote. |
| `metrics` | object | Headline numbers and short facts. Names are the tool's own and are listed per tool in `docs/TOOLS.md`. |
| `ledger_rows` | list of objects | Each row the tool appended (`kind`, `engine`, `dataset`, `metric`, `value`, and `renders`, `wall_clock_s`, `notes` when set). With `--no-ledger` a tool lists the rows it would have written and marks them `"written": false`. |
| `notes` | string | Free text: dry-run remarks, why a tool stopped. |

## Rules

- **Paths** inside the repository are written repo-relative with forward slashes. A path outside the repository (a temp folder, a user folder) stays absolute. Nothing is resolved through symlinks that leave the repository.
- **Atomic write**: the file is written to a temporary name in the same folder and renamed over the target, so a reader never sees half a file and a failed write leaves the old file intact. Parent folders are created.
- **Error path**: when the tool raises, the manifest is written with `status="error"`, `exit_code` 1 and the exception text, then the exception propagates unchanged. `SystemExit` is recorded with its code and propagates.
- **Local data**: a manifest names local files (stems, takes, presets). It is written where the caller asks; it is not a ledger row and is not committed. Do not point `--result-json` at a tracked folder for a run that touched song audio.
- **No audio, no preset bodies** go in a manifest, only paths and numbers. Model ids may appear in `metrics` of tools whose outputs already name them, so treat a manifest from a Native or Stomp run as local, like the cache folders it points into.
- The manifest does not replace the ledger. The ledger is still the record of a claim; `ledger_rows` only echoes what the run appended.

## Helper API (`tonematch.cli_result`)

- `add_result_arg(parser)` adds `--result-json PATH` (default off).
- `write_result(path, *, tool, version=None, args=None, status="ok", inputs=None, outputs=None, metrics=None, ledger_rows=None, notes="", started=None, finished=None, exit_code=None, error=None)` writes the file; a falsy `path` is a no-op.
- `scope(args, tool=, inputs=)` is a context manager that yields a `Result` (`.inputs`, `.outputs`, `.metrics`, `.ledger_rows`, `.notes`, `.exit_code`, `.add_ledger_row(...)`). It writes on exit and writes `status="error"` then re-raises on an exception.
- `run(args, tool=, body=, inputs=)` calls `body(result) -> exit code` inside `scope` and returns the code.
- `ledger_row_from_args(argv)` turns the `--kind ... --metric ...` list a tool hands to `ledger_append.py` into a row object.

Tests: `tests/test_cli_result.py` (schema, atomic write, error path, repo-relative paths) and `tests/test_cli_result_tools.py` (one cheap run per wired tool).
