param([string]$Python = (Join-Path (Split-Path -Parent $PSScriptRoot) 'backend/.venv/Scripts/python.exe'))
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
$Commit = '2e2ab3eb9b5242deadca097c929c40329dbb5012'
$env:PIP_CACHE_DIR = Join-Path $ProjectRoot '.cache/pip'
$env:TEMP = Join-Path $ProjectRoot '.cache/tmp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force $env:TEMP | Out-Null
if (-not (Test-Path '.cache/anyimage/.git')) {
    git clone --no-checkout https://github.com/OmooLab/AnyImage.git .cache/anyimage
    if ($LASTEXITCODE -ne 0) { throw 'AnyImage clone failed' }
    git -C .cache/anyimage checkout --detach $Commit
    if ($LASTEXITCODE -ne 0) { throw 'Pinned AnyImage checkout failed' }
}
$ActualCommit = git -C .cache/anyimage rev-parse HEAD
if ($ActualCommit -ne $Commit) {
    throw "AnyImage must be checked out at $Commit; found $ActualCommit. Review before changing the checkout."
}
& $Python -B -m pip install --target .cache/anyimage-deps onnxruntime==1.24.4 trimesh==4.11.2 numpy==2.3.5
if ($LASTEXITCODE -ne 0) { throw 'Inference dependency installation failed' }
@'
import hashlib
from pathlib import Path
import sys
sys.path.insert(0, str(Path('.cache/anyimage/src/anyimage').resolve()))
from server.model_catalog import MOGE2_MODELS
from server.model_download import download_model
spec = MOGE2_MODELS['MOGE2_VITS_NORMAL']
directory = Path('.cache/models') / spec.directory_name
model = directory / 'model.onnx'
expected = spec.files[0][2]
if not model.exists() or hashlib.sha256(model.read_bytes()).hexdigest() != expected:
    download_model(spec, directory, lambda p, m: None, lambda: None)
print('AnyImage source pinned; single geometry model SHA256 verified.')
'@ | & $Python -B -
if ($LASTEXITCODE -ne 0) { throw 'Geometry model download or validation failed' }
