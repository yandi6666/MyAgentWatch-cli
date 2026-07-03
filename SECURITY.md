# Security Policy

## Supported Versions

The current development branch is the supported version.

## Reporting Vulnerabilities

Please report security issues privately to the project maintainer before publishing details. If the project is hosted on GitHub, use GitHub Security Advisories when available.

Include:

- Affected version or commit.
- Reproduction steps.
- Impact assessment.
- Whether the issue can bypass daemon policy, shell allowlists, task approval, or local config boundaries.

## Security Boundaries

- The daemon must not execute tasks unless server approval and local policy both allow it.
- Shell execution must remain allowlisted.
- Do not commit `config.json`, `.env`, tokens, private keys, local databases, logs, or daemon data.

