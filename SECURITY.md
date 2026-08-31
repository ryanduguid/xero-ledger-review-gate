# Security boundary

This v0.1 repository is a synthetic-data demonstration only. It has no OAuth, API client, HTTP client, MCP server, LLM client, credential store, or accounting-system write path.

Do not place real Xero exports, client data, tokens, `.env` files, or workpapers in this repository. A future live implementation would require separate access controls, approval, retention, audit, and privacy design; the split between the model result and reviewer evidence files in this demo is **not** a multi-user security boundary.

## Reporting a vulnerability

Report a suspected vulnerability privately through [GitHub's advisory form](https://github.com/ryanduguid/xero-ledger-review-gate/security/advisories/new), or by email to ryan@duguid.com.au. Do not open a public issue for one.

Include the gate or refusal you believe is bypassed and the artefacts that reproduce it. Fabricated data only, as everywhere else in this repository.
