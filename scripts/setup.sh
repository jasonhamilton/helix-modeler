#!/usr/bin/env bash
# HX Stomp Tone Matcher: one-command setup (T218, platform).
#
#   scripts/setup.sh --check            print what is present and what is missing; changes nothing
#   scripts/setup.sh                    run every step; ask before each download and each manual step
#   scripts/setup.sh --only venv,bp     run only the named steps (names below)
#   scripts/setup.sh --skip-downloads   build the environments but download nothing
#
# Steps, in order:
#   venv       .venv with `pip install -e .[dev,separation,embed,stomp,dry]` plus huggingface_hub
#   hostvenv   cache/native/hostvenv: pedalboard (GPLv3) in its own venv, the process boundary
#   bp         cache/venvs/basic-pitch: Spotify Basic Pitch, the --no-deps recipe (docs/notes/basic-pitch-t203.md)
#   guitartechs  Guitar-TECHS P1/P2 (Zenodo, CC BY 4.0) into data/di/Guitar-TECHS   (download, asks first)
#   afxrep     AFx-Rep checkpoint and config, 1.16 GB (Hugging Face, pinned revision) (download, asks first)
#   demucs     htdemucs_6s weights, 52 MB                                            (download, asks first)
#   catalog    catalog_import.py: reads the model catalog from YOUR HX Edit install
#   hc         native_hc_once.py: one manual click in Helix Native's dialog
#   templates  Native template capture (known.hlx, then the single-block library)   (GUI session needed)
#   drivemaps  drive-families-v1.json and knob_sensitivity.json (optional; long Native job)
#   smokehlx   make_smoke_hlx.py from YOUR OWN HX Stomp export in fixtures/golden/
#   smoke      engine_smoke.py, licence_check.py, proc_check.py
#
# Rules this script keeps: it never downloads, copies or writes a Line 6 file (the catalog, templates and
# presets are produced on your machine from YOUR installs and stay under gitignored cache/). It never
# downloads without printing the size and source and getting a typed "yes". Re-running is safe: a step
# whose result is already present is skipped. macOS first. Windows: run it from Git Bash or WSL (notes in
# docs/SETUP.md). Linux: Native is untested (Wine + yabridge, best effort); the rest should work.
#
# Non-interactive consent (for the HUMAN to type, never for an agent to add): --consent guitartechs,afxrep,demucs

set -u -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

# ----- pinned facts (checked 2026-10-23) --------------------------------------------------------------
ZENODO_API="https://zenodo.org/api/records/14963133/files"
ZENODO_REC="https://zenodo.org/records/14963133"
# name size_bytes md5 (Zenodo API). Core = single notes and scales (rebuild_dry bank, guitar profile tone fit).
GT_CORE="P1_singlenotes:108626613:ca0c4674dde3805574685a313f7c39eb
P2_singlenotes:116133457:40fbf03d8b04bb2cf42df20f36dc2254
P1_scales:453349723:9c0b98e8fb42a522df727ea8bf545e4f
P2_scales:471254783:96664853872f51e5f8aa4447313b7cf5"
GT_MORE="P1_techniques:326280863:18634a41a6db5a8de10d07eb3122a872
P2_techniques:395839610:f4189251ce50be25f06a173b2c2bba00
P1_chords:981741162:be9ef8bbdceb1912d565254e607a6d94
P2_chords:1150819056:eb6f74dd19162237189281688ad7ad2e"
AFX_REPO="csteinmetz1/afx-rep"
AFX_REV="6582cb5414094146759319819a4242967ecc7e39"
AFX_SHA="3587c4f3a1a8ecbc53b8023c480a0e6ff80719bcc26ce6ee6d08b8daf41d75d4"
AFX_DIR="cache/models/afx-rep"

# ----- arguments ---------------------------------------------------------------------------------------
CHECK=0; SKIP_DL=0; ONLY=""; CONSENT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=1 ;;
    --skip-downloads) SKIP_DL=1 ;;
    --only) ONLY="${2:-}"; shift ;;
    --consent) CONSENT="${2:-}"; shift ;;
    -h|--help) sed -n 2,34p "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: $1 (try --help)"; exit 2 ;;
  esac
  shift
done

OS="$(uname -s)"
case "$OS" in
  Darwin) PLAT=macos ;;
  Linux) PLAT=linux ;;
  MINGW*|MSYS*|CYGWIN*) PLAT=windows ;;
  *) PLAT=unknown ;;
esac

vpy() { # python of a venv dir: bin/python (macOS, Linux) or Scripts/python.exe (Windows)
  if [ -x "$1/bin/python" ]; then echo "$1/bin/python"; elif [ -x "$1/Scripts/python.exe" ]; then echo "$1/Scripts/python.exe"; else echo ""; fi
}
VENV_PY="$(vpy .venv)"
HOST_PY="$(vpy cache/native/hostvenv)"
BP_PY="$(vpy cache/venvs/basic-pitch)"

PASS=0; MISS=0; FAIL=0
ok()      { echo "  [ok]      $*"; PASS=$((PASS+1)); }
missing() { echo "  [missing] $*"; MISS=$((MISS+1)); }
warn()    { echo "  [note]    $*"; }
say()     { echo; echo "== $*"; }
fail()    { echo "  [FAILED]  $*"; FAIL=$((FAIL+1)); }

want() { # step selected by --only (or no --only)?
  [ -z "$ONLY" ] && return 0
  case ",$ONLY," in *",$1,"*) return 0 ;; *) return 1 ;; esac
}
consented() { case ",$CONSENT," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }

confirm() { # confirm KEY "question"   -> 0 only on a typed yes (or --consent KEY)
  consented "$1" && { echo "  consent given on the command line for: $1"; return 0; }
  if [ ! -t 0 ]; then echo "  (not interactive: skipped. Re-run in a terminal, or pass --consent $1 yourself.)"; return 1; fi
  printf "  %s [type yes to continue] " "$2"
  read -r ans
  [ "$ans" = "yes" ]
}

find_python() { # a SYSTEM python >= 3.11 (3.12 preferred: the Basic Pitch recipe and the host venv were proven on 3.12)
  local clean="" d IFS=:                       # drop any activated venv from PATH so new venvs are not built from a venv
  for d in $PATH; do case "$d" in "${VIRTUAL_ENV:-/nonexistent}/"*|"$ROOT/.venv/"*|"$ROOT/cache/"*) ;; *) clean="$clean:$d" ;; esac; done
  local PATH="${clean#:}"
  for c in python3.12 python3.11 python3.13 python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
      if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then command -v "$c"; return 0; fi
    fi
  done
  return 1
}
PYTHON="$(find_python || true)"

run_script() { # run_script <script> [args...]  with the venv python and the host-venv env
  TONEMATCH_NATIVE_PYTHON="${HOST_PY:+$ROOT/$HOST_PY}" "$VENV_PY" "$@"
}

have_hf_model() { ls cache/models/hf/models--adefossez--HTDemucs-6s/snapshots/*/*.safetensors >/dev/null 2>&1; }
gt_present() { [ -f "data/di/Guitar-TECHS/$1/audio/directinput/directinput_all${1#P?_}.wav" ] || ls data/di/Guitar-TECHS/"$1"/audio/directinput/*.wav >/dev/null 2>&1; }

# ----- checks (always run; --check stops after them) -------------------------------------------------
do_check() {
  say "platform"
  echo "  OS: $OS ($PLAT); repo: $ROOT"
  case "$PLAT" in
    linux) warn "Linux: Helix Native is untested here (Wine + yabridge, best effort; see docs/DEV-ENV.md)." ;;
    windows) warn "Windows: run this script from Git Bash or WSL. The Native host, stompd and the Basic Pitch path (scripts/transcribe_stems.py expects bin/python) are untested on Windows." ;;
  esac
  [ -n "$PYTHON" ] && ok "python >= 3.11: $PYTHON ($("$PYTHON" -V 2>&1))" || missing "python >= 3.11 (macOS: brew install python@3.12)"

  say "environments"
  if [ -n "$VENV_PY" ]; then
    ok ".venv ($("$VENV_PY" -V 2>&1))"
    for mod in tonematch torch demucs librosa sklearn sounddevice torchlibrosa yaml; do
      "$VENV_PY" -c "import $mod" 2>/dev/null && ok "  .venv imports $mod" || missing "  .venv cannot import $mod"
    done
    "$VENV_PY" -c "from tonematch.gpu import detect_backend as d; print('  torch backend:', d())" 2>/dev/null || true
  else missing ".venv (run: scripts/setup.sh --only venv)"; fi
  if [ -n "$HOST_PY" ]; then
    "$HOST_PY" -c "import pedalboard" 2>/dev/null && ok "cache/native/hostvenv has pedalboard" || missing "cache/native/hostvenv has no pedalboard"
  else missing "cache/native/hostvenv"; fi
  if [ -n "$BP_PY" ]; then
    "$BP_PY" -c "
from pathlib import Path
from basic_pitch import ICASSP_2022_MODEL_PATH
import onnxruntime
raise SystemExit(0 if Path(str(ICASSP_2022_MODEL_PATH)).with_suffix('.onnx').is_file() else 1)" 2>/dev/null \
      && ok "cache/venvs/basic-pitch (Basic Pitch + onnxruntime + nmp.onnx)" || missing "cache/venvs/basic-pitch is incomplete"
  else missing "cache/venvs/basic-pitch"; fi

  say "downloads (each asks before it runs)"
  n=0; t=0
  for e in $(echo "$GT_CORE" | tr '\n' ' '); do t=$((t+1)); gt_present "${e%%:*}" && n=$((n+1)); done
  [ "$n" -eq "$t" ] && ok "Guitar-TECHS core (P1/P2 single notes and scales): $n/$t" || missing "Guitar-TECHS core: $n/$t folders (about 1.15 GB)"
  n=0; t=0
  for e in $(echo "$GT_MORE" | tr '\n' ' '); do t=$((t+1)); gt_present "${e%%:*}" && n=$((n+1)); done
  [ "$n" -eq "$t" ] && ok "Guitar-TECHS techniques + chords (optional): $n/$t" || warn "Guitar-TECHS techniques + chords (optional, 2.85 GB): $n/$t"
  [ -f data/di/LICENCE-Guitar-TECHS.txt ] && ok "data/di/LICENCE-Guitar-TECHS.txt (CC BY 4.0 licence and attribution)" || missing "data/di/LICENCE-Guitar-TECHS.txt"
  [ -f data/di/manifest.json ] && ok "data/di/manifest.json (DI index)" || missing "data/di/manifest.json (DI index)"
  [ -f "$AFX_DIR/afx-rep.ckpt" ] || [ -f "$AFX_DIR/encoder.pt" ] && [ -f "$AFX_DIR/config.yaml" ] \
    && ok "AFx-Rep checkpoint and config in $AFX_DIR" || missing "AFx-Rep (1.16 GB; needed by --shortlist afxrep and the encoder-based tools)"
  have_hf_model && ok "htdemucs_6s weights in cache/models/hf" || missing "htdemucs_6s weights (52 MB, downloaded on first separation)"

  say "your own Line 6 installs and exports (nothing here is ever copied into the repo)"
  if [ -n "$VENV_PY" ]; then
    "$VENV_PY" - <<'PY' 2>/dev/null || true
from tonematch.engines.native import locate_plugin
p = locate_plugin()
print("  [ok]      Helix Native plugin: %s" % p if p else "  [missing] Helix Native plugin (Native is optional if you have the Stomp)")
try:
    from tonematch.catalog import locate
    r = locate()
    print("  [ok]      HX Edit install found: %s (version %s)" % (r.get("root"), r.get("version")))
except Exception as e:
    print("  [missing] HX Edit install (%s)" % str(e).splitlines()[0][:90])
PY
  fi
  [ -f cache/catalog/current.json ] && ok "cache/catalog/current.json (catalog imported)" || missing "catalog not imported (step: catalog)"
  [ -f cache/native/start_stomp.state ] && ok "cache/native/start_stomp.state (HC mode answered, step: hc)" || missing "cache/native/start_stomp.state (step: hc)"
  ls fixtures/golden/*.hlx >/dev/null 2>&1 && ok "fixtures/golden/*.hlx (your own export)" || missing "fixtures/golden/HXStomp.hlx (export one preset from HX Edit; step smokehlx needs it)"
  [ -f fixtures/smoke/known.hlx ] && ok "fixtures/smoke/known.hlx" || missing "fixtures/smoke/known.hlx (step: smokehlx)"
  [ -f cache/native/library/block-library-v1.2/index.json ] && ok "Native single-block library v1.2" || missing "Native single-block library (step: templates; needed by match_block)"
  [ -f cache/drive-maps/drive-families-v1.json ] && ok "cache/drive-maps/drive-families-v1.json" || warn "cache/drive-maps/drive-families-v1.json missing (optional; step: drivemaps)"
  [ -f cache/drive-maps/knob_sensitivity.json ] && ok "cache/drive-maps/knob_sensitivity.json" || warn "cache/drive-maps/knob_sensitivity.json missing (optional; step: drivemaps)"

  say "HX Stomp control helper (optional; not part of the repo, see docs/SETUP.md)"
  if [ -n "${TONEMATCH_STOMPD:-}" ] && [ -x "${TONEMATCH_STOMPD}" ]; then ok "stompd: $TONEMATCH_STOMPD"
  elif [ -x cache/stomp/bin/stompd ]; then ok "stompd: cache/stomp/bin/stompd (set TONEMATCH_STOMPD to use it)"
  elif command -v stompd >/dev/null 2>&1; then ok "stompd on PATH"
  else warn "no stompd: everything works except loading presets onto the Stomp from code and the Stomp engine"; fi

  say "summary"
  echo "  present: $PASS   missing: $MISS"
}

do_check
[ "$CHECK" -eq 1 ] && exit 0

# ----- steps -------------------------------------------------------------------------------------------
if want venv; then
  say "venv: .venv"
  if [ -z "$PYTHON" ]; then fail "no python >= 3.11 found"; else
    [ -n "$VENV_PY" ] || { "$PYTHON" -m venv .venv && VENV_PY="$(vpy .venv)"; }
    if [ -n "$VENV_PY" ]; then
      "$VENV_PY" -m pip install --upgrade pip >/dev/null 2>&1
      # torch backend: the default wheel has MPS on Apple Silicon. NVIDIA, AMD and DirectML need a different
      # index: see docs/SETUP.md "Torch backend per machine"; install that torch BEFORE re-running this step.
      "$VENV_PY" -m pip install -e ".[dev,separation,embed,stomp,dry]" \
        && "$VENV_PY" -m pip install huggingface_hub \
        && ok ".venv ready" || fail "pip install failed (see the output above)"
    fi
  fi
fi

if want hostvenv; then
  say "hostvenv: cache/native/hostvenv (pedalboard is GPLv3; it lives only here, behind the process boundary)"
  if [ -n "$HOST_PY" ] && "$HOST_PY" -c "import pedalboard" 2>/dev/null; then ok "already present"
  elif [ -z "$PYTHON" ]; then fail "no python"
  else
    [ -n "$HOST_PY" ] || { mkdir -p cache/native && "$PYTHON" -m venv cache/native/hostvenv && HOST_PY="$(vpy cache/native/hostvenv)"; }
    pk="pedalboard>=0.9.26 numpy"
    [ "$PLAT" = macos ] && pk="$pk pyobjc-framework-Cocoa>=10"   # the 'native-gui' extra: hands-off template capture
    # shellcheck disable=SC2086
    "$HOST_PY" -m pip install $pk && ok "hostvenv ready" || fail "hostvenv install failed"
  fi
fi

if want bp; then
  say "bp: cache/venvs/basic-pitch (Apache-2.0 external tool, ONNX runtime, no TensorFlow)"
  if [ -n "$BP_PY" ] && "$BP_PY" -c "from basic_pitch import ICASSP_2022_MODEL_PATH; import onnxruntime" 2>/dev/null; then ok "already present"
  elif [ -z "$PYTHON" ]; then fail "no python"
  else
    [ -n "$BP_PY" ] || { mkdir -p cache/venvs && "$PYTHON" -m venv cache/venvs/basic-pitch && BP_PY="$(vpy cache/venvs/basic-pitch)"; }
    # Plain `pip install basic-pitch` fails (resolution-too-deep; on macOS its pin wants tensorflow-macos).
    # pip then warns about tensorflow-macos and resampy 0.4.3: harmless for the ONNX path.
    "$BP_PY" -m pip install --no-deps basic-pitch \
      && "$BP_PY" -m pip install onnxruntime numpy scipy librosa resampy pretty_midi mir_eval scikit-learn typing_extensions \
      && ok "Basic Pitch ready" || fail "Basic Pitch install failed"
  fi
fi

gt_fetch() { # gt_fetch "<name:size:md5> ..."  download, md5-check, extract only audio/directinput + midi, delete the zip
  for e in $1; do
    n="${e%%:*}"; md5="${e##*:}"
    gt_present "$n" && continue
    echo "  downloading $n.zip"
    mkdir -p data/di/_zips
    curl -L -C - --retry 5 -o "data/di/_zips/$n.zip" "$ZENODO_API/$n.zip/content" || { fail "download of $n"; continue; }
    got="$( (md5sum "data/di/_zips/$n.zip" 2>/dev/null || md5 -r "data/di/_zips/$n.zip") | awk '{print $1}')"
    if [ "$got" != "$md5" ]; then fail "$n.zip md5 $got, expected $md5 (file kept; delete it and retry)"; continue; fi
    "$VENV_PY" - "$n" <<'PY' || { fail "extract $n"; continue; }
import sys, zipfile
from pathlib import Path
name = sys.argv[1]
out = Path("data/di/Guitar-TECHS") / name
z = zipfile.ZipFile(f"data/di/_zips/{name}.zip")
n = 0
for m in z.infolist():
    if m.is_dir():
        continue
    parts = Path(m.filename).parts
    # keep audio/directinput/* and midi/*, whatever the top-level folder inside the zip is called
    for i, p in enumerate(parts):
        if (p == "audio" and len(parts) > i + 2 and parts[i + 1] == "directinput") or (p == "midi" and len(parts) > i + 1):
            dest = out.joinpath(*parts[i:])
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(m) as src, open(dest, "wb") as dst:
                dst.write(src.read())
            n += 1
            break
print(f"  extracted {n} files into {out}")
raise SystemExit(0 if n else 1)
PY
    rm -f "data/di/_zips/$n.zip"; ok "$n"
  done
}

gt_todo() { # gt_todo "<list>" -> the entries whose folder is missing, on one line
  local out="" e n
  for e in $1; do n="${e%%:*}"; gt_present "$n" || out="$out $e"; done
  echo "$out"
}
gt_sum() { # gt_sum "<entries>" -> total download bytes
  local t=0 e
  for e in $1; do t=$((t + $(echo "$e" | cut -d: -f2))); done
  echo "$t"
}

if want guitartechs; then
  say "guitartechs: Guitar-TECHS reference DI (Zenodo $ZENODO_REC)"
  if [ "$SKIP_DL" -eq 1 ]; then echo "  --skip-downloads: skipped"; else
    core="$(echo "$GT_CORE" | tr '\n' ' ')"; more="$(echo "$GT_MORE" | tr '\n' ' ')"
    todo="$(gt_todo "$core")"; todo_bytes="$(gt_sum "$todo")"
    if [ -z "$todo" ]; then ok "core folders already present"; else
      echo "  Source: Zenodo record 14963133, P1/P2 single notes and scales (4 zips)."
      echo "  Size: about $((todo_bytes/1000000)) MB to download, then a similar amount on disk after extraction"
      echo "        (only audio/directinput and midi are kept; each zip is deleted once extracted)."
      echo "  Licence: CC BY 4.0. The licence text and the attribution line are written to data/di/LICENCE-Guitar-TECHS.txt."
      echo "        Local use only; if you ever share a clip, keep that attribution."
      confirm guitartechs "Download the core set now?" && gt_fetch "$todo"
    fi
    todo="$(gt_todo "$more")"
    if [ -n "$todo" ]; then
      echo "  Optional extra: P1/P2 techniques + chords, about $(( $(gt_sum "$todo") / 1000000 )) MB (richer DI picking; not needed to rebuild a track)."
      confirm guitartechs-more "Download techniques + chords too?" && gt_fetch "$todo"
    fi
    # licence + attribution file (kept next to the audio, per docs/notes/dry-signal.md)
    if [ -d data/di/Guitar-TECHS ] && [ ! -f data/di/LICENCE-Guitar-TECHS.txt ]; then
      cat > data/di/LICENCE-Guitar-TECHS.txt <<EOF
Guitar-TECHS: An Electric Guitar Dataset Covering Techniques, Musical Excerpts, Chords and Scales Using a Diverse Array of Hardware
Authors: Hegel Pedroza (Villalobos), Termeh Taheri, Wallace Abreu, Ryan M. Corey, Iran R. Roman
Source: $ZENODO_REC (DOI of paper 10.1109/ICASSP49660.2025.10887996); project page https://guitar-techs.github.io/ ; paper https://arxiv.org/abs/2501.03720
Retrieved: $(date +%F). Zenodo record licence field: cc-by-4.0.

Licence text as stated on the project page (https://guitar-techs.github.io/):
"All data is licensed under a Creative Commons Attribution 4.0 License (CC BY 4.0)."
Full terms: https://creativecommons.org/licenses/by/4.0/legalcode

Attribution line (use wherever a clip or derivative is shared):
Guitar-TECHS dataset, Pedroza et al., ICASSP 2025, $ZENODO_REC, CC BY 4.0. Changes: excerpt cut, converted, resampled/mono-mixed.

Zenodo asks users to cite: Pedroza, Hegel, et al. "Guitar-TECHS: An Electric Guitar Dataset Covering Techniques, Musical Excerpts, Chords and Scales Using a Diverse Array of Hardware." ICASSP 2025.

Local copy: only audio/directinput and midi folders were extracted from the Zenodo zips (md5 verified by scripts/setup.sh).
EOF
      ok "wrote data/di/LICENCE-Guitar-TECHS.txt"
    fi
    if [ -d data/di/Guitar-TECHS ] && [ ! -f data/di/manifest.json ] && [ -n "$VENV_PY" ]; then
      echo "  building the DI index (about a minute)"
      "$VENV_PY" -c "from tonematch.dry import index_library; index_library('data/di', source='Guitar-TECHS', licence='CC BY 4.0')" \
        && ok "data/di/manifest.json" || fail "DI index"
    fi
  fi
fi

if want afxrep; then
  say "afxrep: AFx-Rep encoder (Hugging Face $AFX_REPO @ ${AFX_REV:0:10})"
  if [ -f "$AFX_DIR/config.yaml" ] && { [ -f "$AFX_DIR/afx-rep.ckpt" ] || [ -f "$AFX_DIR/encoder.pt" ]; }; then ok "already present"
  elif [ "$SKIP_DL" -eq 1 ]; then echo "  --skip-downloads: skipped"
  else
    echo "  Size: 1.16 GB (afx-rep.ckpt) + 4 KB (config.yaml). Source: https://huggingface.co/$AFX_REPO, pinned revision, sha256-checked."
    echo "  CAVEAT, read before you say yes. The only licence text on the weights is the uploader's tag \"apache-2.0\"."
    echo "  The encoder was trained on datasets several of which carry NON-COMMERCIAL or research-only terms"
    echo "  (ENST-Drums, MTG-Jamendo, parts of FSD50K, possibly MUSDB18). Whether those terms reach the weights is unsettled."
    echo "  This project downloads it to YOUR machine for local use only, never redistributes it, and never publishes the"
    echo "  file. Do not put it in a commercial product or share it. Details: docs/notes/afxrep-licence.md."
    if confirm afxrep "Download AFx-Rep (1.16 GB) for local, non-commercial use?"; then
      mkdir -p "$AFX_DIR"
      base="https://huggingface.co/$AFX_REPO/resolve/$AFX_REV"
      curl -L -C - --retry 5 -o "$AFX_DIR/config.yaml" "$base/config.yaml" \
        && curl -L -C - --retry 5 -o "$AFX_DIR/afx-rep.ckpt" "$base/afx-rep.ckpt" || fail "AFx-Rep download"
      if [ -f "$AFX_DIR/afx-rep.ckpt" ]; then
        got="$( (sha256sum "$AFX_DIR/afx-rep.ckpt" 2>/dev/null || shasum -a 256 "$AFX_DIR/afx-rep.ckpt") | awk '{print $1}')"
        [ "$got" = "$AFX_SHA" ] && ok "sha256 matches the pinned checkpoint" || fail "sha256 $got does not match $AFX_SHA; delete the file and retry"
      fi
    fi
  fi
fi

if want demucs; then
  say "demucs: htdemucs_6s weights (guitar stem separation)"
  if have_hf_model; then ok "already present"
  elif [ "$SKIP_DL" -eq 1 ]; then echo "  --skip-downloads: skipped"
  else
    echo "  Size: about 52 MB. Source: Hugging Face hub (adefossez/HTDemucs-6s), into cache/models/hf. Nothing is uploaded."
    echo "  Licence: the demucs README states code and pretrained models are MIT; the Hugging Face page carries no licence tag"
    echo "  of its own (docs/notes/separation-models.md)."
    if confirm demucs "Download the htdemucs_6s weights?"; then
      "$VENV_PY" -c "from tonematch.separation.models import Htdemucs6s; Htdemucs6s()._load(); print('loaded')" \
        && ok "htdemucs_6s ready" || fail "demucs weights"
    fi
  fi
fi

# ----- local first-run steps (need YOUR Line 6 installs) ----------------------------------------------
mkdir -p cache/setup
{
  echo "# source this: . cache/setup/env.sh   (written by scripts/setup.sh; local only)"
  [ -n "$HOST_PY" ] && echo "export TONEMATCH_NATIVE_PYTHON=\"$ROOT/$HOST_PY\""
  [ -x cache/stomp/bin/stompd ] && echo "export TONEMATCH_STOMPD=\"$ROOT/cache/stomp/bin/stompd\""
  [ -x cache/stomp/bin/stompd-compile ] && echo "export TONEMATCH_STOMPD_COMPILE=\"$ROOT/cache/stomp/bin/stompd-compile\""
} > cache/setup/env.sh

if want catalog; then
  say "catalog: read the model catalog from YOUR HX Edit install (stays in gitignored cache/catalog/)"
  if [ -f cache/catalog/current.json ]; then ok "already imported"
  else run_script scripts/catalog_import.py && ok "catalog imported" \
    || fail "catalog_import.py (exit 2 = HX Edit not found: pass --hxedit-path, or set HXEDIT_PATH)"; fi
fi

if want smokehlx; then
  say "smokehlx: fixtures/smoke/known.hlx from YOUR OWN HX Stomp export"
  if [ -f fixtures/smoke/known.hlx ]; then ok "already built"
  elif [ ! -f fixtures/golden/HXStomp.hlx ]; then
    missing "fixtures/golden/HXStomp.hlx. In HX Edit: pick any preset, File > Export Preset, save it there. It is gitignored; never commit it."
  else run_script scripts/make_smoke_hlx.py && ok "built" || fail "make_smoke_hlx.py"; fi
fi

if want hc; then
  say "hc: Helix Native's \"Change HC mode?\" question (ONE manual click, once per machine)"
  if [ -f cache/native/start_stomp.state ]; then ok "already answered"
  elif [ "$PLAT" != macos ]; then warn "native_hc_once.py is macOS-only today; on Windows/Linux this step is not scripted"
  elif [ ! -f fixtures/smoke/known.hlx ]; then missing "needs fixtures/smoke/known.hlx first (step smokehlx)"
  else
    echo "  A Helix Native window will open. Tick \"Don't ask me again\", keep \"Change the current HC mode to HX Stomp"
    echo "  and load the preset\", click OK, then close the plugin window. Needs a logged-in desktop session."
    if confirm hc "Open the Native window now?"; then run_script scripts/native_hc_once.py && ok "HC mode answered" || fail "native_hc_once.py"; fi
  fi
fi

if want templates; then
  say "templates: Native template capture (a GUI window opens during drops; do not use the keyboard meanwhile)"
  if [ "$PLAT" != macos ]; then warn "hands-off template capture is macOS-only today"
  elif [ ! -f cache/native/start_stomp.state ]; then missing "run step hc first"
  else
    if [ ! -f cache/native/library/block-library-v1.2/index.json ]; then
      echo "  Captures the single-block library (605 models; about 6 min of Native time at 3 sessions in the original run,"
      echo "  plus 190 MB under cache/native/library/). match_block.py needs it. Not yet verified on a clean machine."
      if confirm templates "Run the library capture now?"; then
        run_script scripts/native_block_library.py capture --sessions 3 --max-hosts 6 && ok "library captured" || fail "native_block_library.py capture"
      fi
    else ok "single-block library present"; fi
    # the known.hlx chain template (what engine_smoke loads) is captured on first use: step smoke sets AUTOCAPTURE=1
  fi
fi

if want drivemaps; then
  say "drivemaps: cache/drive-maps/{drive-families-v1,knob_sensitivity}.json (OPTIONAL; improves match_block --shortlist afxrep)"
  if [ -f cache/drive-maps/drive-families-v1.json ] && [ -f cache/drive-maps/knob_sensitivity.json ]; then ok "already present"
  elif [ ! -f cache/native/library/block-library-v1.2/index.json ]; then missing "needs the single-block library first (step templates)"
  else
    echo "  Chain: distortion_step3.py run (fingerprints all 60 drive models, Native, long) -> drive_tone_families.py run, tone, families."
    echo "  drive_tone_families.py (tone, families) writes cache/drive-maps/*.json directly; no copy is needed."
    if confirm drivemaps "Run the long Native fingerprint job now?"; then
      nice -n 10 "$VENV_PY" scripts/distortion_step3.py run --hosts 4 --workers 3 \
        && nice -n 10 "$VENV_PY" scripts/drive_tone_families.py run --hosts 4 --workers 3 \
        && "$VENV_PY" scripts/drive_tone_families.py tone && "$VENV_PY" scripts/drive_tone_families.py families \
        && [ -f cache/drive-maps/drive-families-v1.json ] && [ -f cache/drive-maps/knob_sensitivity.json ] \
        && ok "drive maps built" || fail "drive maps"
    fi
  fi
fi

if want smoke; then
  say "smoke: engine_smoke, licence_check, proc_check"
  if [ -f fixtures/smoke/known.hlx ] && [ -n "$VENV_PY" ]; then
    TONEMATCH_AGENT="${TONEMATCH_AGENT:-human}" TONEMATCH_NATIVE_AUTOCAPTURE=1 run_script scripts/engine_smoke.py \
      && ok "engine_smoke" || fail "engine_smoke (exit 2 = no engine found)"
  else missing "engine_smoke needs fixtures/smoke/known.hlx and .venv"; fi
  [ -n "$VENV_PY" ] && { "$VENV_PY" scripts/licence_check.py && ok "licence_check PASS" || fail "licence_check"; }
  [ -n "$VENV_PY" ] && { "$VENV_PY" scripts/proc_check.py && ok "proc_check clean" || warn "proc_check flagged processes (see above)"; }
fi

say "done"
echo "  Next: source cache/setup/env.sh, then re-run: scripts/setup.sh --check"
echo "  Then read docs/SETUP.md, section \"Your first song\"."
[ "$FAIL" -eq 0 ] || { echo "  $FAIL step(s) failed."; exit 1; }
