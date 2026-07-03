# Release Checklist

Before publishing to GitHub:

- `LICENSE` exists and declares `AGPL-3.0-only`.
- `README.md`, `CONTRIBUTING.md`, and `SECURITY.md` exist.
- No secrets, tokens, private keys, `.env`, `config.json`, daemon data, local databases, or logs are committed.
- No private local paths are required for normal installation or runtime.
- CLI `py_compile` passes.
- `python -m myagentwatch_cli.cli tasks --help` shows approve/reject/retry/events.
- `python -m myagentwatch_cli.cli runner test --help` works.
- The daemon policy remains the final execution gate for autostart and shell commands.
- GitHub release notes mention the AI-assisted implementation note from `README.md`.

