param([ValidateRange(1,65535)][int]$Port = 8087, [switch]$NoBrowser, [switch]$RuntimePath)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $root
$bundle = Join-Path $root 'model_bundle'
$archive = Join-Path $bundle 'runtime-win64.zip'
$url = "http://127.0.0.1:$Port"
$sha = [Security.Cryptography.SHA256]::Create()
$instance = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($root.ToLowerInvariant()))).Replace('-', '')
$sha.Dispose()
$cache = Join-Path $env:LOCALAPPDATA ("NexusPDF\" + $instance.Substring(0,16))
$python = Join-Path $cache 'runtime\python.exe'
if ($RuntimePath) { Write-Output $python; exit 0 }
$mutex = [Threading.Mutex]::new($false, "Local\NexusPDF-$instance")
$ownsLock = $false
try {
    try { $ownsLock = $mutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $ownsLock = $true }
    if (-not $ownsLock) {
        Write-Host "NEXUS is already running or starting. Check its console for the address."
        if (-not $NoBrowser) {
            try {
                $health = Invoke-RestMethod "$url/api/health" -TimeoutSec 2
                if ($health.status -eq 'ok') { Start-Process $url }
            } catch { Write-Host "Startup is in progress." }
        }
        exit 0
    }
    $files = @($archive, (Join-Path $bundle 'models\NuExtract-1.5-tiny\model.safetensors'),
        (Join-Path $bundle 'models\criteria\qwen2.5-1.5b-instruct-q4_k_m.gguf'))
    foreach ($file in $files) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf) -or (Get-Item -LiteralPath $file).Length -lt 1024) {
            Write-Host "Downloading required files with Git LFS..."
            & git lfs pull
            if ($LASTEXITCODE -ne 0) { throw "Clone the project with Git for Windows, then run git lfs pull." }
            break
        }
    }
    foreach ($file in $files) {
        if ((Get-Item -LiteralPath $file).Length -lt 1024) { throw "Missing Git LFS file: $file" }
    }
    $stamp = Join-Path $cache 'runtime\.nexus-archive'
    $info = Get-Item -LiteralPath $archive
    $archiveKey = "$($info.Length):$($info.LastWriteTimeUtc.Ticks)"
    if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $stamp) -or
        (Get-Content -Raw -LiteralPath $stamp).Trim() -ne $archiveKey) {
        Write-Host "Preparing the bundled Python runtime. This may take a few minutes..."
        [IO.Directory]::CreateDirectory($cache) | Out-Null
        & tar.exe -xf $archive -C $cache
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $python)) { throw "Could not unpack $archive" }
        $pathConfig = Join-Path $cache 'runtime\python312._pth'
        $lines = [IO.File]::ReadAllLines($pathConfig) | ForEach-Object { if ($_ -eq '..') { $bundle } else { $_ } }
        [IO.File]::WriteAllLines($pathConfig, [string[]]$lines, [Text.UTF8Encoding]::new($false))
        Set-Content -LiteralPath $stamp -Value $archiveKey -Encoding ASCII
    }
    $env:PYTHONUTF8 = '1'
    $serverArgs = @('backend/server.py','--port',"$Port",'--frontend-dir','frontend/dist','--jobs-dir','backend/runs/local','--model-python',$python)
    if (-not $NoBrowser) { $serverArgs += '--open-browser' }
    Write-Host "NEXUS: $url   |   Stop: Ctrl+C"
    & $python -B -X utf8 @serverArgs
    exit $LASTEXITCODE
} finally {
    if ($ownsLock) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
