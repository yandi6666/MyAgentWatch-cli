# Contributing

Thank you for helping improve `myagentwatch-cli`.

## Issues

When opening an issue, include:

- Command that failed.
- Expected and actual output.
- Server URL pattern, with tokens removed.
- OS, Python version, and daemon status.

## Pull Requests

- Keep command output stable where scripts may depend on it.
- Do not commit local `config.json`, daemon data, tokens, logs, or policy files.
- Add tests or smoke checks for CLI commands that touch daemon, inbox, chat, or task behavior.
- Preserve local policy as the final execution gate.

## Local Checks

```powershell
python -m py_compile myagentwatch_cli\cli.py myagentwatch_cli\daemon.py myagentwatch_cli\client.py
python -m myagentwatch_cli.cli tasks --help
python -m myagentwatch_cli.cli runner --help
```

