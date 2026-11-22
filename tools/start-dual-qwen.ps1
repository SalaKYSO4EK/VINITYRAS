param(
    [string] $LlamaServer,
    [ValidateRange(1, 32)] [int] $Threads = 6,
    [ValidateRange(1, 65535)] [int] $Port = 8086,
    [switch] $Background
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$model = 'model_bundle/models/criteria/Qwen3-4B-Q4_K_M.gguf'
if (-not $LlamaServer) {
    $local = Join-Path $root 'model_bundle/models/criteria/llama-b8157/llama-server.exe'
    $backups = @(Get-ChildItem -LiteralPath (Split-Path $root -Parent) -Directory -Filter '*-backup-*' |
        Sort-Object Name -Descending | ForEach-Object { Join-Path $_.FullName 'models/criteria/llama-b8157/llama-server.exe' })
    $LlamaServer = (@($local) + $backups) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
}
if (-not $LlamaServer) { throw 'Укажите путь к llama-server.exe через -LlamaServer.' }
$LlamaServer = (Resolve-Path -LiteralPath $LlamaServer).Path
Push-Location -LiteralPath $root
try {
    $digest = (Get-FileHash -LiteralPath $model -Algorithm SHA256).Hash
    if ($digest -ne '7485FE6F11AF29433BC51CAB58009521F205840F5B4AE3A32FA7F92E8534FDF5') {
        throw 'Контрольная сумма Qwen3-4B-Q4_K_M.gguf не совпала.'
    }
    $config = Join-Path $root 'model_bundle/config/dual-qwen.json'
    if (-not (Test-Path -LiteralPath $config)) {
        $settings = @{local_api = "http://127.0.0.1:$Port/v1"; remote_worker = "auto"}
        [IO.File]::WriteAllText($config, ($settings | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    }
    $arguments = @('-m',$model,'--host','127.0.0.1','--port',"$Port",'--alias','nexus-qwen3-4b',
        '-t',"$Threads",'-ngl','0','-c','8192','--parallel','1','--jinja','--cache-ram','512')
    if (-not $Background) {
        & $LlamaServer @arguments
        exit $LASTEXITCODE
    }
    $logs = Join-Path $root 'tmp/dual-qwen'
    New-Item -ItemType Directory -Path $logs -Force | Out-Null
    $process = Start-Process -WindowStyle Hidden -PassThru -FilePath $LlamaServer -WorkingDirectory $root -ArgumentList $arguments `
        -RedirectStandardOutput (Join-Path $logs 'local-qwen.stdout.log') -RedirectStandardError (Join-Path $logs 'local-qwen.stderr.log')
    $deadline = (Get-Date).AddSeconds(45)
    do {
        Start-Sleep -Milliseconds 400
        if ($process.HasExited) { throw "Qwen stopped with exit code $($process.ExitCode). See $logs." }
        try {
            $models = Invoke-RestMethod "http://127.0.0.1:$Port/v1/models" -TimeoutSec 2
            if ($models.data.id -contains 'nexus-qwen3-4b') { return }
        } catch {}
    } while ((Get-Date) -lt $deadline)
    throw "Qwen did not start within 45 seconds. See $logs."
} finally {
    Pop-Location
}
