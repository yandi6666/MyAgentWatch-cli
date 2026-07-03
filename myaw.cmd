@echo off
setlocal

set "CLI_DIR=%~dp0"
set "PYTHONPATH=%CLI_DIR%;%PYTHONPATH%"
set "PYTHONDONTWRITEBYTECODE=1"

if defined MYAW_PYTHON (
  "%MYAW_PYTHON%" -m myagentwatch_cli.cli %*
  exit /b %ERRORLEVEL%
)

set "CODEX_PY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%CODEX_PY%" (
  "%CODEX_PY%" -m myagentwatch_cli.cli %*
  exit /b %ERRORLEVEL%
)

python -m myagentwatch_cli.cli %*
exit /b %ERRORLEVEL%
