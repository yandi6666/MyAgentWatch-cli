# myagentwatch-cli

`myagentwatch-cli` is the agent-side command line client for MyAgentWatch. It sends local agent status, syncs chat and inbox data, exposes task queue commands, and runs the daemon-side task claim loop.

Copyright (C) 2026 Tianyu.

This project is designed and maintained by Tianyu. Parts of the implementation were generated with AI assistance and reviewed, edited, and integrated into this project.

## Install

```powershell
python -m pip install -e .
```

## Connect

```powershell
myaw connect http://127.0.0.1:10000/
myaw status
```

## Useful Commands

```powershell
myaw conversations
myaw chat --conv 1
myaw inbox unread
myaw tasks list
myaw tasks show 1
myaw tasks approve 1
myaw tasks reject 1 --reason "not safe"
myaw tasks retry 1
myaw tasks events 1
myaw runner status
myaw runner test --task 1
```

## Daemon Safety

The daemon can claim only tasks allowed by both server state and local policy.

- Pending or rejected tasks are not claimable.
- Approved shell tasks still require a local `shell_allowlist`.
- `daemon_policy.json` controls allowed agents, task types, command templates, concurrency, and shell command allowlists.

## License

`myagentwatch-cli` is licensed under `AGPL-3.0-only`. See [LICENSE](LICENSE).

