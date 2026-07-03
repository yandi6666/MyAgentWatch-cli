param(
    [string]$CliDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

$ErrorActionPreference = "Stop"

Set-Location $CliDir
& (Join-Path $CliDir "myaw.cmd") daemon start | Out-Null
