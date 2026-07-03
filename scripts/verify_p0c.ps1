param(
    [int]$TimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Myaw = Join-Path $Root "myaw.cmd"
$ConfigPath = Join-Path $Root "config.json"
$DataDir = Join-Path $Root "data"
$PidPath = Join-Path $DataDir "daemon.pid"
$BackupPath = Join-Path $DataDir "verify_p0c.config.backup.json"
$BadServer = "http://127.0.0.1:19999"
$DbPath = Join-Path (Split-Path -Parent $Root) "myagentwatch\myagentwatch\data\myagentwatch.db"

function Fail($Message) {
    throw "[P0-C VERIFY FAILED] $Message"
}

function Info($Message) {
    Write-Host "[P0-C] $Message"
}

function Invoke-Myaw {
    param([string[]]$MyawArgs)
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $Myaw @MyawArgs 2>&1
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldPreference
    }
    if ($output) {
        $output | ForEach-Object { Write-Host $_ }
    }
    if ($code -ne 0) {
        Fail "myaw $($MyawArgs -join ' ') exited with $code"
    }
    return $output
}

function Invoke-MyawJson {
    param([string[]]$MyawArgs)
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $Myaw @MyawArgs 2>&1
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldPreference
    }
    if ($code -ne 0) {
        if ($output) {
            $output | ForEach-Object { Write-Host $_ }
        }
        Fail "myaw $($MyawArgs -join ' ') exited with $code"
    }
    return ($output -join "`n") | ConvertFrom-Json
}

function Wait-Until {
    param(
        [scriptblock]$Condition,
        [string]$Description,
        [int]$Seconds = $TimeoutSeconds
    )
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        $result = & $Condition
        if ($result) {
            return $result
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    Fail "timed out waiting for $Description"
}

function Set-ConfigServer {
    param([string]$Server)
    $cfg = Get-Content -Raw -LiteralPath $ConfigPath | ConvertFrom-Json
    $cfg.server = $Server
    $json = $cfg | ConvertTo-Json -Depth 20
    [System.IO.File]::WriteAllText($ConfigPath, $json, [System.Text.UTF8Encoding]::new($false))
}

function Restore-Config {
    if (Test-Path -LiteralPath $BackupPath) {
        Copy-Item -LiteralPath $BackupPath -Destination $ConfigPath -Force
    }
}

function Remove-ConfigBackup {
    if (Test-Path -LiteralPath $BackupPath) {
        Remove-Item -LiteralPath $BackupPath -Force
    }
}

function Get-PythonExe {
    if ($env:MYAW_PYTHON -and (Test-Path -LiteralPath $env:MYAW_PYTHON)) {
        return $env:MYAW_PYTHON
    }
    $codexPy = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    if (Test-Path -LiteralPath $codexPy) {
        return $codexPy
    }
    return "python"
}

function Get-DbCount {
    param([string]$Table)
    if (-not (Test-Path -LiteralPath $DbPath)) {
        return $null
    }
    $py = Get-PythonExe
    $code = "import sqlite3,sys; conn=sqlite3.connect(sys.argv[1]); print(conn.execute('select count(*) from ' + sys.argv[2]).fetchone()[0])"
    $value = & $py -c $code $DbPath $Table
    if ($LASTEXITCODE -ne 0) {
        return $null
    }
    return [int]($value | Select-Object -First 1)
}

function Assert-Running {
    $status = Invoke-MyawJson -MyawArgs @("daemon", "status", "--json")
    if (-not $status.running) {
        Fail "daemon is not running"
    }
    return $status
}

function Assert-Queue {
    param([int]$Pending)
    $queue = Invoke-MyawJson -MyawArgs @("daemon", "queue", "--json")
    if ([int]$queue.stats.pending -ne $Pending) {
        Fail "expected pending queue $Pending, got $($queue.stats.pending)"
    }
    return $queue
}

New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
Copy-Item -LiteralPath $ConfigPath -Destination $BackupPath -Force

$originalServer = (Get-Content -Raw -LiteralPath $ConfigPath | ConvertFrom-Json).server
$resourceBefore = Get-DbCount "agent_resources"
$processBefore = Get-DbCount "agent_processes"

try {
    Info "checking start/status/restart/logs"
    Invoke-Myaw -MyawArgs @("daemon", "start") | Out-Null
    $first = Assert-Running
    Invoke-Myaw -MyawArgs @("daemon", "start") | Out-Null
    $second = Assert-Running
    if ($first.pid -ne $second.pid) {
        Fail "duplicate start changed daemon pid"
    }
    Invoke-Myaw -MyawArgs @("daemon", "restart") | Out-Null
    Assert-Running | Out-Null
    Invoke-Myaw -MyawArgs @("daemon", "logs", "--lines", "5") | Out-Null
    Invoke-Myaw -MyawArgs @("daemon", "queue") | Out-Null
    Invoke-Myaw -MyawArgs @("daemon", "cleanup-dead") | Out-Null

    Info "checking stale pid cleanup"
    Invoke-Myaw -MyawArgs @("daemon", "stop") | Out-Null
    "99999" | Set-Content -LiteralPath $PidPath -Encoding ASCII
    Invoke-Myaw -MyawArgs @("daemon", "start") | Out-Null
    Assert-Running | Out-Null
    Invoke-Myaw -MyawArgs @("daemon", "stop") | Out-Null
    "99999" | Set-Content -LiteralPath $PidPath -Encoding ASCII
    Invoke-Myaw -MyawArgs @("daemon", "stop") | Out-Null

    Info "checking retry queue under simulated disconnect"
    Set-ConfigServer $BadServer
    Invoke-Myaw -MyawArgs @("daemon", "start") | Out-Null
    Wait-Until {
        $q = Invoke-MyawJson -MyawArgs @("daemon", "queue", "--json")
        if ([int]$q.stats.pending -gt 0) { return $q }
        return $null
    } "pending retry queue after bad server" | Out-Null
    Invoke-Myaw -MyawArgs @("daemon", "stop") | Out-Null

    Info "checking queue recovery after restoring config"
    Restore-Config
    Invoke-Myaw -MyawArgs @("daemon", "start") | Out-Null
    Wait-Until {
        $q = Invoke-MyawJson -MyawArgs @("daemon", "queue", "--json")
        if ([int]$q.stats.pending -eq 0) { return $q }
        return $null
    } "empty retry queue after restore" | Out-Null
    $finalStatus = Assert-Running

    if ($resourceBefore -ne $null) {
        Wait-Until {
            $now = Get-DbCount "agent_resources"
            if ($now -gt $resourceBefore) { return $now }
            return $null
        } "agent_resources database growth" | Out-Null
    }
    if ($processBefore -ne $null) {
        Wait-Until {
            $now = Get-DbCount "agent_processes"
            if ($now -gt $processBefore) { return $now }
            return $null
        } "agent_processes database growth" | Out-Null
    }

    $queue = Invoke-MyawJson -MyawArgs @("daemon", "queue", "--json")
    if ([int]$queue.stats.dead -ne 0) {
        Fail "expected 0 dead queue items, got $($queue.stats.dead)"
    }

    Info "OK: daemon running pid=$($finalStatus.pid), queue pending=0 dead=0, server=$originalServer"
}
finally {
    Restore-Config
    $status = Invoke-MyawJson -MyawArgs @("daemon", "status", "--json")
    if (-not $status.running) {
        Invoke-Myaw -MyawArgs @("daemon", "start") | Out-Null
    }
    Remove-ConfigBackup
}
