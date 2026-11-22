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

function Get-RuntimeAssets {
    $manifest = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'runtime-assets.json') | ConvertFrom-Json
    $remoteCriteria = (Test-Path -LiteralPath (Join-Path $bundle 'config/qwen-api.json')) -or
        -not [string]::IsNullOrWhiteSpace($env:NEXUS_QWEN_API_BASE) -or
        -not [string]::IsNullOrWhiteSpace($env:NEXUS_QWEN_API_MODEL) -or
        -not [string]::IsNullOrWhiteSpace($env:NEXUS_QWEN_API_TIMEOUT)
    $dualQwen = (Test-Path -LiteralPath (Join-Path $bundle 'config/dual-qwen.json')) -and
        $env:NEXUS_LEGACY_INFERENCE -ne '1'
    foreach ($asset in $manifest.files) {
        if ($dualQwen -and $asset.path -like 'models/NuExtract-1.5-tiny/*') { continue }
        if (($remoteCriteria -or $dualQwen) -and $asset.component -eq 'local-qwen') { continue }
        $destination = Join-Path $bundle $asset.path
        $current = Get-Item -LiteralPath $destination -ErrorAction SilentlyContinue
        if ($current -and $current.Length -eq $asset.size) { continue }

        $partial = "$destination.download"
        $previous = Get-Item -LiteralPath $partial -ErrorAction SilentlyContinue
        if ($previous -and $previous.Length -gt $asset.size) {
            Remove-Item -LiteralPath $partial
            $previous = $null
        }
        [IO.Directory]::CreateDirectory((Split-Path $destination -Parent)) | Out-Null
        if (-not $previous -or $previous.Length -lt $asset.size) {
            $downloadUrl = [Uri]($manifest.base_url + $asset.path)
            $curlArgs = @('--fail', '--location', '--retry', '2', '--connect-timeout', '30',
                '--progress-bar', '--continue-at', '-', '--output', $partial)
            if (-not ($env:HTTPS_PROXY -or $env:ALL_PROXY -or $env:NO_PROXY)) {
                $proxy = [Net.WebRequest]::DefaultWebProxy.GetProxy($downloadUrl)
                if ($proxy -and $proxy -ne $downloadUrl) { $curlArgs += @('--proxy', $proxy.AbsoluteUri) }
            }
            Write-Host "Downloading $($asset.path) ($([Math]::Round($asset.size / 1MB)) MB)..."
            & curl.exe @curlArgs $downloadUrl.AbsoluteUri
            if ($LASTEXITCODE -ne 0) {
                throw "Download interrupted. Check your connection to GitHub and double-click start.cmd to resume."
            }
        }
        $digest = [Security.Cryptography.SHA256]::Create()
        $stream = [IO.File]::OpenRead($partial)
        try {
            $valid = $stream.Length -eq $asset.size -and
                [BitConverter]::ToString($digest.ComputeHash($stream)).Replace('-', '') -eq $asset.sha256
        } finally {
            $stream.Dispose()
            $digest.Dispose()
        }
        if (-not $valid) {
            Remove-Item -LiteralPath $partial
            throw "Download verification failed for $($asset.path). Run start.cmd again."
        }
        Move-Item -LiteralPath $partial -Destination $destination -Force
    }
}

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
    Get-RuntimeAssets
    $dualConfig = Join-Path $bundle 'config/dual-qwen.json'
    if ((Test-Path -LiteralPath $dualConfig) -and $env:NEXUS_LEGACY_INFERENCE -ne '1') {
        $dualSettings = Get-Content -LiteralPath $dualConfig -Raw | ConvertFrom-Json
        $qwenAddress = [uri]$dualSettings.local_api
        if ($qwenAddress.IsLoopback -and $qwenAddress.Scheme -eq 'http') {
            $qwenUp = $false
            try {
                $qwenModels = Invoke-RestMethod ($qwenAddress.AbsoluteUri.TrimEnd('/') + '/models') -TimeoutSec 2
                $qwenUp = $qwenModels.data.id -contains 'nexus-qwen3-4b'
            } catch {}
            if (-not $qwenUp) {
                & (Join-Path $PSScriptRoot 'start-dual-qwen.ps1') -Port $qwenAddress.Port -Background
            }
        }
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
