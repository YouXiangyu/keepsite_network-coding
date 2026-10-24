param([switch]$SkipFrontend)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonPath = Join-Path $projectRoot 'backend/.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw '请先按 backend/README.md 创建项目虚拟环境。' }
if ($env:KEEPSITE_STORAGE -notin @('local', 's3')) { throw '请先按 backend/README.md 明确配置 KEEPSITE_STORAGE 和鉴权环境。' }
if ($env:KEEPSITE_PUBLIC_URL -notin @('http://127.0.0.1:4173', 'http://localhost:4173')) { throw '本机开发的 KEEPSITE_PUBLIC_URL 必须与 4173 前端 origin 一致；不要直接复用生产环境文件。' }
$authSettings = @('KEEPSITE_COGNITO_REGION', 'KEEPSITE_COGNITO_USER_POOL_ID', 'KEEPSITE_COGNITO_CLIENT_ID', 'KEEPSITE_COGNITO_DOMAIN', 'KEEPSITE_SESSION_KEY')
foreach ($setting in $authSettings) {
    if (-not [Environment]::GetEnvironmentVariable($setting, 'Process')) { throw "缺少鉴权配置项 $setting；未启动进程。" }
}
$logDirectory = Join-Path $projectRoot '.cache/run'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$started = @()
$backendReady = $false
try { $backendReady = (Invoke-RestMethod 'http://127.0.0.1:8000/api/health' -TimeoutSec 2).status -eq 'ok' -and (Invoke-RestMethod 'http://127.0.0.1:8000/api/auth/session' -TimeoutSec 2).configured } catch {}
if (-not $backendReady) {
    if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) { throw '8000 端口被其他程序占用，未停止已有进程。' }
    $apiProcess = Start-Process -FilePath $pythonPath -ArgumentList @('-m', 'uvicorn', 'backend.app:create_app', '--factory', '--host', '127.0.0.1', '--port', '8000') -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDirectory 'api.out.log') -RedirectStandardError (Join-Path $logDirectory 'api.err.log') -PassThru
    $started += @{ kind = 'backend'; pid = $apiProcess.Id }
}
if (-not $SkipFrontend -and -not (Get-NetTCPConnection -LocalPort 4173 -State Listen -ErrorAction SilentlyContinue)) {
    $nodePath = (Get-Command node.exe).Source
    $demoDirectory = Join-Path $projectRoot 'demo'
    Push-Location $demoDirectory
    try { & $nodePath 'scripts/check-web-runtime.mjs'; if ($LASTEXITCODE -ne 0) { throw 'Web 运行约束检查失败。' } } finally { Pop-Location }
    $webProcess = Start-Process -FilePath $nodePath -ArgumentList @('node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--port', '4173', '--strictPort') -WorkingDirectory $demoDirectory -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDirectory 'web.out.log') -RedirectStandardError (Join-Path $logDirectory 'web.err.log') -PassThru
    $started += @{ kind = 'frontend'; pid = $webProcess.Id }
}
if ($started.Count) { $started | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $logDirectory ('started-' + [DateTime]::Now.ToString('yyyyMMdd-HHmmss') + '.json')) -Encoding UTF8 }
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    try { if ((Invoke-RestMethod 'http://127.0.0.1:8000/api/health' -TimeoutSec 1).status -eq 'ok' -and (Invoke-RestMethod 'http://127.0.0.1:8000/api/auth/session' -TimeoutSec 1).configured) { $backendReady = $true; break } } catch {}
    Start-Sleep -Milliseconds 500
}
if (-not $backendReady) { throw '后端未启动成功，请查看 .cache/run/api.err.log。' }
Write-Output '前端 http://127.0.0.1:4173/；本机 API http://127.0.0.1:8000/docs'
