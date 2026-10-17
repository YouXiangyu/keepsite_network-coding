#!/usr/bin/env bash
# Minimal CPU inference dependencies and one verified model; no Blender installer.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON="$ROOT/backend/.venv/bin/python"
EXECUTE=0
while (($#)); do
  case "$1" in
    --execute) EXECUTE=1; shift ;;
    --python) PYTHON="${2:?Missing --python value}"; shift 2 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done
COMMIT=2e2ab3eb9b5242deadca097c929c40329dbb5012
CACHE="$ROOT/.cache"
printf 'Plan: use %s; prepare %s/anyimage at %s; install Linux CPU wheels in anyimage-deps; verify/download only the 140852051-byte MoGe2 model.\n' "$PYTHON" "$CACHE" "$COMMIT"
if (( ! EXECUTE )); then
  printf 'No changes made. Add --execute to perform this plan.\n'
  exit 0
fi
[[ -x "$PYTHON" ]] || { printf 'Python venv is missing: %s\n' "$PYTHON" >&2; exit 1; }
command -v git >/dev/null
mkdir -p "$CACHE/pip" "$CACHE/tmp" "$CACHE/models"
export PIP_CACHE_DIR="$CACHE/pip" TMPDIR="$CACHE/tmp" PYTHONDONTWRITEBYTECODE=1
if [[ ! -e "$CACHE/anyimage" ]]; then
  # Skip unrelated Git LFS assets; inference uses plain source and the model below.
  GIT_LFS_SKIP_SMUDGE=1 git clone --no-checkout https://github.com/OmooLab/AnyImage.git "$CACHE/anyimage"
  GIT_LFS_SKIP_SMUDGE=1 git -C "$CACHE/anyimage" checkout --detach "$COMMIT"
fi
[[ "$(git -C "$CACHE/anyimage" rev-parse HEAD)" == "$COMMIT" ]] || { printf 'AnyImage revision mismatch; existing files were not reset.\n' >&2; exit 1; }
[[ -z "$(git -C "$CACHE/anyimage" status --porcelain --untracked-files=no)" ]] || { printf 'AnyImage source has local modifications; inspect before continuing.\n' >&2; exit 1; }
# Keep a cache tied to the interpreter ABI and OS, never reuse Windows wheels.
ABI="$("$PYTHON" -B -c 'import sys,sysconfig; print(sys.implementation.cache_tag + ":" + sysconfig.get_platform())')"
STAMP="$CACHE/anyimage-deps/.runtime-abi"
if [[ -f "$STAMP" && "$(cat "$STAMP")" != "$ABI" ]]; then
  printf 'Cached dependency ABI differs (%s); prepare a separate compatible cache, do not overwrite it live.\n' "$ABI" >&2; exit 1
fi
if [[ ! -f "$STAMP" ]]; then
  if [[ -d "$CACHE/anyimage-deps" && -n "$(find "$CACHE/anyimage-deps" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
    printf 'Unmarked dependency cache already contains files; inspect/migrate it instead of mixing native packages.\n' >&2; exit 1
  fi
  "$PYTHON" -B -m pip install --only-binary=:all: --target "$CACHE/anyimage-deps" onnxruntime==1.24.4 trimesh==4.11.2 numpy==2.3.5
  printf '%s\n' "$ABI" > "$STAMP"
fi
"$PYTHON" -B - "$CACHE" <<'PY'
import hashlib
from pathlib import Path
import sys
cache = Path(sys.argv[1])
sys.path[:0] = [str(cache / 'anyimage-deps'), str(cache / 'anyimage/src/anyimage')]
import onnxruntime
import trimesh
import numpy
assert onnxruntime.__version__ == '1.24.4'
assert trimesh.__version__ == '4.11.2'
assert numpy.__version__ == '2.3.5'
assert 'CPUExecutionProvider' in onnxruntime.get_available_providers()
from server.model_catalog import MOGE2_MODELS
from server.model_download import download_model
spec = MOGE2_MODELS['MOGE2_VITS_NORMAL']
directory = cache / 'models' / spec.directory_name
model = directory / 'model.onnx'
expected_size, expected_hash = 140852051, '24eacb5dc7a2c54c7bc98f7de085ffbed79ad006ea5b664c2c2cdc02ff3a52f0'
assert spec.files[0] == ('model.onnx', expected_size, expected_hash)
def verified():
    if not model.is_file() or model.stat().st_size != expected_size:
        return False
    with model.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest() == expected_hash
if not verified():
    download_model(spec, directory, lambda progress, message: None, lambda: None)
assert verified(), 'Model checksum verification failed'
print('CPU native imports passed; pinned AnyImage and single model SHA256 verified. Inference/RAM acceptance is still required.')
PY
