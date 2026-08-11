# Contributing

This repository is a design reference for a fixed-policy AI review boundary, not a production connector. Its value is what it refuses to do, so the refusals are the part to protect.

## The boundary is the feature

Contributions must not add:

- OAuth, network calls, or any live Xero connection
- model or LLM invocation
- any tool that mutates accounting data

A change that widens the model-facing payload, or that lets reviewer-only evidence reach a model-facing structure, needs an explicit argument in the pull request for why the narrower version does not work.

## Data boundary

- Synthetic data only. The `.gitignore` blocks `.csv` outside `xero_ai_review_gateway/samples/inputs/`, plus `.env`, `.env.*`, `*.token` and `*.pem`.
- Do not commit anything drawn from a real Xero organisation, including account names, tenant identifiers and balances.

## Local verification

Python 3.10 or newer, with `uv` and a committed lock file.

```bash
uv sync --locked --extra dev --python 3.12
uv run --locked --extra dev --python 3.12 pytest
uv run --locked --extra dev --python 3.12 python -m build
```

CI runs the tests on Python 3.10 through 3.13, plus a packaging job and CodeQL.

## Pull requests

State which policy or gate the change affects and show the test that holds it. A gate with no test that fails when the gate is removed is not a gate.

For a potential security vulnerability, follow [SECURITY.md](SECURITY.md) rather than opening an issue.
