$ErrorActionPreference = "Continue"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = "C:\Users\天宇\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$OutLog = Join-Path $Root "heartbeat-daemon.log"
$ErrLog = Join-Path $Root "heartbeat-daemon.err.log"

if (-not (Test-Path -LiteralPath $Python)) {
    $Python = "python"
}

Set-Location -LiteralPath $Root

Start-Process `
    -FilePath $Python `
    -ArgumentList @("-B", "-m", "myagentwatch_cli.cli", "heartbeat", "--status", "active", "--daemon") `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog
