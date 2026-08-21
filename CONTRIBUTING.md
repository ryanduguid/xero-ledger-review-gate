# Contributing

This repository demonstrates a fixed-policy boundary for AI-assisted trial-balance review. Treat the refusals below as the part worth protecting; they are what the design exists to show.

## What contributions must not add

- OAuth, network calls, or any live Xero connection
- model or LLM invocation
- any tool that mutates accounting data

If your change widens the model-facing payload, or lets reviewer-only evidence reach a model-facing structure, argue in the pull request why the narrower version does not work.

## Data boundary

- Use synthetic data. The `.gitignore` blocks `.csv` outside `elizabeth_anne_alexander/samples/inputs/`, plus `.env`, `.env.*`, `*.token` and `*.pem`.
- Keep anything drawn from a real Xero organisation out of the repository, including account names, tenant identifiers and balances.

## Local verification

Python 3.10 or newer, with `uv` and a committed lock file.

```bash
uv sync --locked --extra dev --python 3.12
uv run --locked --extra dev --python 3.12 pytest
uv run --locked --extra dev --python 3.12 python -m build
```

CI runs the tests on Python 3.10 through 3.13, plus a packaging job and CodeQL.

## Pull requests

State which policy or gate your change affects and show the test that holds it. Delete the gate in your working copy and confirm the test fails. A test that passes either way holds nothing.

For a potential security vulnerability, follow [SECURITY.md](SECURITY.md) rather than opening an issue.
