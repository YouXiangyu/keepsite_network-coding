param(
    [Parameter(Mandatory)][string]$Server,
    [string]$Domain = 'keepsite.space',
    [string]$KeyPath = '',
    [switch]$Execute,
    [switch]$InitializeHost
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if ($Server -notmatch '^(?:\d{1,3}\.){3}\d{1,3}$') { throw 'Use the verified IPv4 address.' }
if ($Domain -notmatch '^[a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z]{2,}$') { throw 'Invalid hostname.' }
if (-not $KeyPath) { $KeyPath = Join-Path $ProjectRoot '.cache/server/ssh/id_rsa' }
$KeyPath = [IO.Path]::GetFullPath($KeyPath)
$Commit = (& git -C $ProjectRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $Commit -notmatch '^[a-f0-9]{40}$') { throw 'Cannot resolve release commit.' }
$Release = '/opt/memoverse/releases/' + $Commit
Write-Output "Plan: build this commit for same-origin API, upload source and static assets to $Server, prepare $Release, restart one service and verify health."
Write-Output 'Existing database/media/models, TLS credentials and DNS are preserved. AWS resources are not created by this script.'
Write-Output "Initial host installation requested: $InitializeHost. Verify the selected instance is running and SSH host identity is known before execution."
if (-not $Execute) { Write-Output 'No changes made. Add -Execute to deploy.'; return }
if (-not (Test-Path -LiteralPath $KeyPath)) { throw 'SSH key missing.' }
if (& git -C $ProjectRoot status --porcelain --untracked-files=normal) { throw 'Commit changes before deploying a reproducible release.' }
$TransferDir = Join-Path $ProjectRoot '.cache/server'
New-Item -ItemType Directory -Path $TransferDir -Force | Out-Null
$Archive = Join-Path $TransferDir ("release-$Commit.tar")
$HashFile = "$Archive.sha256"
$KnownHosts = Join-Path $TransferDir 'ssh/known_hosts'
if (-not (Test-Path -LiteralPath $KnownHosts)) { throw 'Verified known_hosts file missing; establish host identity before deploying.' }
$SshOptions = @('-i', $KeyPath, '-o', 'BatchMode=yes', '-o', "UserKnownHostsFile=$KnownHosts", '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=15')
if (Test-Path -LiteralPath $Archive) {
    if (-not (Test-Path -LiteralPath $HashFile)) { throw 'Cached artifact has no verified hash. Inspect it; no rebuild/overwrite attempted.' }
    $ArtifactHash = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($ArtifactHash -ne (Get-Content -LiteralPath $HashFile -Raw).Trim()) { throw 'Cached artifact hash mismatch.' }
    Write-Output 'Reusing the exact cached artifact for this commit.'
} else {
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
    if ((& git -C $ProjectRoot rev-parse HEAD).Trim() -ne $Commit -or (& git -C $ProjectRoot status --porcelain --untracked-files=normal)) { throw 'Source changed during build; no artifact published.' }
    $Building = Join-Path $TransferDir ("release-$Commit-" + [guid]::NewGuid().ToString('N') + '.building.tar')
    & git -C $ProjectRoot archive --format=tar --output=$Building $Commit
    if ($LASTEXITCODE -ne 0) { throw 'Source archive failed.' }
    & tar -rf $Building -C $ProjectRoot demo/dist/client
    if ($LASTEXITCODE -ne 0) { throw 'Static asset packaging failed.' }
    $ArtifactHash = (Get-FileHash -LiteralPath $Building -Algorithm SHA256).Hash.ToLowerInvariant()
    Move-Item -LiteralPath $Building -Destination $Archive
    [IO.File]::WriteAllText($HashFile, "$ArtifactHash`n", [Text.UTF8Encoding]::new($false))
}
$RemoteArchive = "/tmp/memoverse-$Commit.tar"
$RemotePrepare = "/tmp/memoverse-prepare-$Commit.py"
$LocalPrepare = Join-Path $TransferDir ("prepare-$Commit.py")
$PrepareSource = & tar -xOf $Archive deploy/ubuntu/prepare-release.py
if ($LASTEXITCODE -ne 0) { throw 'Verified artifact has no preparation helper.' }
[IO.File]::WriteAllText($LocalPrepare, (($PrepareSource -join "`n") + "`n"), [Text.UTF8Encoding]::new($false))
& scp @SshOptions $Archive "ubuntu@${Server}:$RemoteArchive"
if ($LASTEXITCODE -ne 0) { throw 'Release upload failed.' }
& scp @SshOptions $LocalPrepare "ubuntu@${Server}:$RemotePrepare"
if ($LASTEXITCODE -ne 0) { throw 'Preparation helper upload failed.' }
$HostFlag = if ($InitializeHost) { ' --initialize-host' } else { '' }
$Prepare = "set -eu; sudo python3 '$RemotePrepare' '$RemoteArchive' '$Commit' '$ArtifactHash' '$Domain'$HostFlag"
& ssh @SshOptions "ubuntu@$Server" $Prepare
if ($LASTEXITCODE -ne 0) { throw 'Release preparation failed. Existing active release was not changed.' }
& ssh @SshOptions "ubuntu@$Server" "sudo bash '$Release/deploy/ubuntu/activate.sh' '$Release'"
if ($LASTEXITCODE -ne 0) { throw 'Activation failed; inspect service journal and rollback result.' }
Write-Output "Deployed $Commit. Data stays in /var/lib/memoverse; origin https://$Domain."
& ssh @SshOptions "ubuntu@$Server" "rm -- '$RemoteArchive' '$RemotePrepare'"
if ($LASTEXITCODE -ne 0) { Write-Warning 'Deployment succeeded, but owned remote upload files were not cleaned up.' }
