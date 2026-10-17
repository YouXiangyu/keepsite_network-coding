param(
    [string]$Server = '52.196.70.26',
    [string]$Domain = 'keepsite.space',
    [string]$KeyPath = '',
    [switch]$Execute
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if ($Server -notmatch '^(?:\d{1,3}\.){3}\d{1,3}$') { throw 'Use the verified IPv4 address.' }
if ($Domain -notmatch '^[a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z]{2,}$') { throw 'Invalid hostname.' }
if (-not $KeyPath) { $KeyPath = Join-Path $ProjectRoot '.cache/lightsail/ssh/id_rsa' }
$KeyPath = [IO.Path]::GetFullPath($KeyPath)
$Commit = (& git -C $ProjectRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $Commit -notmatch '^[a-f0-9]{40}$') { throw 'Cannot resolve release commit.' }
$Release = '/opt/memoverse/releases/' + $Commit
Write-Output "Plan: build this commit for same-origin API, upload source and static assets to $Server, prepare $Release, restart one service and verify health."
Write-Output 'Existing database/media/models, TLS credentials and DNS are preserved. AWS resources are not created by this script.'
if (-not $Execute) { Write-Output 'No changes made. Add -Execute to deploy.'; return }
if (-not (Test-Path -LiteralPath $KeyPath)) { throw 'SSH key missing.' }
if (& git -C $ProjectRoot status --porcelain --untracked-files=normal) { throw 'Commit changes before deploying a reproducible release.' }
$TransferDir = Join-Path $ProjectRoot '.cache/lightsail'
New-Item -ItemType Directory -Path $TransferDir -Force | Out-Null
$Archive = Join-Path $TransferDir ("release-$Commit.tar")
$KnownHosts = Join-Path $TransferDir 'ssh/known_hosts'
$SshOptions = @('-i', $KeyPath, '-o', 'BatchMode=yes', '-o', "UserKnownHostsFile=$KnownHosts", '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=15')
$OriginalApiBase = $env:VITE_API_BASE_URL
Push-Location (Join-Path $ProjectRoot 'demo')
try {
    $env:VITE_API_BASE_URL = '/'
    & npm run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
} finally {
    $env:VITE_API_BASE_URL = $OriginalApiBase
    Pop-Location
}
& git -C $ProjectRoot archive --format=tar --output=$Archive $Commit
if ($LASTEXITCODE -ne 0) { throw 'Source archive failed.' }
& tar -rf $Archive -C $ProjectRoot demo/dist/client
if ($LASTEXITCODE -ne 0) { throw 'Static asset packaging failed.' }
$RemoteArchive = "/tmp/memoverse-$Commit.tar"
& scp @SshOptions $Archive "ubuntu@${Server}:$RemoteArchive"
if ($LASTEXITCODE -ne 0) { throw 'Release upload failed.' }
$Prepare = "set -eu; test ! -e '$Release'; sudo install -d -m 0755 '$Release'; sudo tar -xf '$RemoteArchive' -C '$Release'; sudo bash '$Release/deploy/lightsail/install.sh' --release '$Release' --domain '$Domain' --execute"
& ssh @SshOptions "ubuntu@$Server" $Prepare
if ($LASTEXITCODE -ne 0) { throw 'Release preparation failed. Existing active release was not changed.' }
& ssh @SshOptions "ubuntu@$Server" "sudo bash '$Release/deploy/lightsail/activate.sh' '$Release'"
if ($LASTEXITCODE -ne 0) { throw 'Activation failed; inspect service journal and rollback result.' }
Write-Output "Deployed $Commit. Data stays in /var/lib/memoverse; origin https://$Domain."
