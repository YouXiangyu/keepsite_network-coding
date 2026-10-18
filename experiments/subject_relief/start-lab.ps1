param(
    [ValidateRange(1024, 65535)][int]$Port = 4183,
    [switch]$BuildOnly
)
$ErrorActionPreference = 'Stop'
$LabProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$LabPython = Join-Path $LabProjectRoot 'backend/.venv/Scripts/python.exe'
$LabBundler = Join-Path $LabProjectRoot 'demo/node_modules/.bin/rolldown.cmd'
$LabUi = Join-Path $LabProjectRoot '.cache/subject-relief/lab-ui'
if (-not (Test-Path -LiteralPath $LabPython -PathType Leaf)) { throw 'Missing project Python environment.' }
if (-not (Test-Path -LiteralPath $LabBundler -PathType Leaf)) { throw 'Missing existing frontend dependencies.' }
Write-Host 'Plan: build the local lab page in the D-drive project cache.'
if (-not $BuildOnly) { Write-Host "Then start the local-only lab at http://127.0.0.1:$Port/ . Ctrl+C stops the service." }
Push-Location $LabProjectRoot
try {
    New-Item -ItemType Directory -Force -Path $LabUi | Out-Null
    & $LabBundler 'experiments/subject_relief/lab.js' --format esm --platform browser --file (Join-Path $LabUi 'lab.bundle.js')
    if ($LASTEXITCODE -ne 0) { throw 'Lab page build failed.' }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'lab.html') -Destination (Join-Path $LabUi 'index.html')
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'lab.css') -Destination (Join-Path $LabUi 'styles.css')
    if (-not $BuildOnly) {
        $env:PYTHONDONTWRITEBYTECODE = '1'
        & $LabPython -B -m experiments.subject_relief.lab_server --port $Port
        if ($LASTEXITCODE -ne 0) { throw 'Lab service exited with an error.' }
    }
} finally {
    Pop-Location
}
