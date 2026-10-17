# ==== Внешний сторож AI Manager ====
# Запускается из run.bat (один экземпляр). Раз в IntervalSec проверяет GET /health;
# после Fails неудач подряд убивает процесс uvicorn этого проекта (вместе с llama-server),
# а цикл в run.bat поднимает сервис заново. Дополняет сторож внутри процесса
# (app/watchdog.py) на случай, когда завис весь интерпретатор или старт сервиса.
# Сам завершается, если сервис не запущен дольше IdleExitSec (run.bat закрыт).
param(
    [int]$Port = 8000,
    [int]$IntervalSec = 30,
    [int]$Fails = 5,
    [int]$GraceSec = 300,
    [int]$IdleExitSec = 120
)

$root = Split-Path -Parent $PSScriptRoot
$log = Join-Path $root "logs\watchdog.log"
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null

$created = $false
$mutex = New-Object System.Threading.Mutex($true, "Local\AIManagerWatchdog-$Port", [ref]$created)
if (-not $created) { exit 0 }

function Write-Log([string]$msg) {
    $line = "{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $msg
    Add-Content -Path $log -Value $line -Encoding UTF8
}

function Get-ServiceProcs {
    # uvicorn запускается с --app-dir <корень проекта> — так отличаем его от других app.main:app
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*app.main:app*" -and $_.CommandLine -like "*$root*" }
}

function Test-Health {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 10 -UseBasicParsing
        return $r.StatusCode -eq 200
    } catch { return $false }
}

Write-Log "Сторож запущен (порт $Port, проверка каждые $IntervalSec с, перезапуск после $Fails неудач)"
$failCount = 0
$idleSince = Get-Date
while ($true) {
    Start-Sleep -Seconds $IntervalSec
    $procs = @(Get-ServiceProcs)
    if ($procs.Count -eq 0) {
        $failCount = 0
        if (((Get-Date) - $idleSince).TotalSeconds -gt $IdleExitSec) {
            Write-Log "Сервис не запущен $IdleExitSec с — сторож завершается"
            break
        }
        continue
    }
    $idleSince = Get-Date
    $oldest = ($procs | Sort-Object CreationDate | Select-Object -First 1)
    if (((Get-Date) - $oldest.CreationDate).TotalSeconds -lt $GraceSec) { $failCount = 0; continue }

    if (Test-Health) { $failCount = 0; continue }
    $failCount++
    Write-Log "/health не отвечает ($failCount/$Fails), PID $($oldest.ProcessId)"
    if ($failCount -ge $Fails) {
        foreach ($p in $procs) {
            Write-Log "Перезапуск: taskkill /T /F PID $($p.ProcessId)"
            & taskkill.exe /T /F /PID $p.ProcessId 2>&1 | Out-Null
        }
        $failCount = 0
    }
}
$mutex.ReleaseMutex()
