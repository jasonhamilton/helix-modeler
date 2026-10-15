#!/usr/bin/env python3
"""One-time human step (T018n): answer Native's "Change HC mode?" question for good.

Opens Helix Native's editor (VST3, in a host child process), drops fixtures/smoke/known.hlx
(an HX Stomp preset) on it, and leaves the window open. Native asks whether to change the
Hardware Compatibility mode. The human ticks "Don't ask me again", keeps "Change the current
HC mode to HX Stomp and load the preset", clicks OK, then closes the plugin window.

After the window closes, the script saves the instance's state as the clean HX Stomp start
state (cache/native/start_stomp.state, local only; no byte of it is edited by us) and reports
Native's ``ChangeHWModeForFile`` preference before and after.

  cd ~/Documents/code/helix_pedal && .venv/bin/python scripts/native_hc_once.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

HOSTVENV = ROOT / "cache" / "native" / "hostvenv" / "bin" / "python"
if "TONEMATCH_NATIVE_PYTHON" not in os.environ and HOSTVENV.exists():
    os.environ["TONEMATCH_NATIVE_PYTHON"] = str(HOSTVENV)

from tonematch.engines.native import HostProcess, _hc_mode, cache_dir, locate_plugin

PREFS = Path.home() / "Library/Application Support/Line 6/Helix Native/Helix Native.prefs"
HC = {0x210100: "Helix", 0x210006: "HX Stomp"}


def pref() -> object:
    try:
        return json.loads(PREFS.read_text()).get("ChangeHWModeForFile")
    except (OSError, ValueError):
        return None


def main() -> int:
    plugin = locate_plugin()
    if plugin is None or plugin.suffix != ".vst3":
        print("Helix Native VST3 not found.", file=sys.stderr)
        return 2
    src = ROOT / "fixtures" / "smoke" / "known.hlx"
    before = pref()
    print(f"Native preference ChangeHWModeForFile before: {before}")
    print("Opening Helix Native... the window appears in ~10 s (it may be behind Terminal).")
    print("When the 'Hardware Compatibility (HC) mode' question appears:")
    print("  1. leave 'Change the current HC mode to HX Stomp and load the preset' ticked")
    print("  2. tick 'Don't ask me again' (bottom left)")
    print("  3. click OK")
    print("  4. close the plugin window (red button, top left). Do not save anything.")
    host = HostProcess(str(plugin))
    try:
        t0 = time.time()
        res, _ = host.call("editor_drop", path=str(src), open_wait_s=8.0, load_wait_s=2.0,
                           auto_close=False)
        _, state = host.call("get_state")
    finally:
        host.close()
    after = pref()
    mode = _hc_mode(state)
    out = cache_dir() / "start_stomp.state"
    ok = mode == 0x210006
    if ok:
        out.write_bytes(state)
        (out.with_suffix(".json")).write_text(json.dumps({
            "made_by": "scripts/native_hc_once.py (human answered Native's HC dialog; no byte edited)",
            "source_file": str(src.relative_to(ROOT)), "hc_mode": hex(mode),
            "pref_before": before, "pref_after": after, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, indent=1))
    print(json.dumps({"window_open_s": round(time.time() - t0, 1), "drop": res.get("drop"),
                      "dialogs_seen_after_drop": res.get("dialogs"),
                      "hc_mode_now": HC.get(mode or 0, hex(mode or 0)),
                      "pref_ChangeHWModeForFile": {"before": before, "after": after},
                      "start_state_saved": str(out) if ok else None}, indent=1))
    if not ok:
        print("The instance did not end in HX Stomp mode: please run the command again and click OK "
              "with 'Change the current HC mode' ticked.", file=sys.stderr)
        return 1
    print("Done. Thank you: Native will no longer ask, and the clean start state is saved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
