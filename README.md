# Xero AI Review Gateway

A **fixed-policy ledger-review boundary for AI**, not an AI that can operate Xero.

The first version consumes fabricated, validated Xero-shaped trial-balance CSVs and produces a bounded, redacted variance-review result plus separate local reviewer evidence. It deliberately has no Xero OAuth, no HTTP/MCP/LLM client, no free-form prompts, and no accounting write operation.

```text
Fabricated validated TB exports
          |
          v
Context + source-integrity gate
          |
          v
One allowlisted variance-review operation
          |
          +--> redacted model-facing result
          |
          +--> local reviewer evidence + receipt
          |
          v
Human acknowledgement or escalation
```

## Demo

```bash
python -m pip install -e ".[dev]"

xero-ai-review-gateway evaluate \
  --context samples/contexts/sample-monthly-variance.context.json \
  --request samples/requests/sample-revenue-variance.request.json \
  --policy policy/demo-policy-v1.json \
  --out build/demo
```

The command works from any directory: the relative `samples/` and `policy/` paths resolve against the data bundled inside the installed package, and outputs land below `build/` in the directory you run it from. It writes three deterministic files below `build/demo`:

- `model-result.json`: the only artefact a future AI adapter may receive. It has redacted account references and bounded values only.
- `reviewer-evidence.json`: fabricated display evidence for a human reviewer. In a real system this would need its own access controls.
- `receipt.json`: source, policy, request, result, and evidence hashes that make the run reproducible and let `validate-review` detect a tampered evidence file.

The sole supported request is `trial_balance_variance` for an explicitly allowlisted section. It cannot select columns, change thresholds, use a natural-language prompt, choose a file path, or invoke an arbitrary Xero/MCP tool.

```bash
xero-ai-review-gateway validate-review \
  --evidence build/demo/reviewer-evidence.json \
  --receipt build/demo/receipt.json \
  --decision samples/decisions/sample-review-decision.json
```

`--decision` accepts a file from the bundled `samples/` data or from `build/` under the working directory, so a real decision can sit next to the run outputs it refers to. The decision validator accepts only `ACKNOWLEDGED`, `NEEDS_EVIDENCE`, or `ESCALATED`, requires timezone-qualified review timestamps, and reports `PARTIAL_DECISION_RECORDED` until every finding is accounted for. A decision can only name a finding the evidence carries, so a run whose findings were capped by `max_results` stays `PARTIAL_DECISION_RECORDED` however many decisions are recorded. Silence about an omitted finding is not a decision about it. The validation output says which case you are in: `truncated` and `completable` distinguish a review still in progress from one that cannot complete until the run is repeated with a higher `max_results` or a narrower request. It records no accounting action and rejects `APPROVED`, `RESOLVED`, `POSTED`, `PAID`, `LODGED`, and `LOCKED`.

## Control boundary

- The canonical source contract has exactly ten columns: `ReportDate,Tenant,Section,AccountID,AccountName,AccountCode,Debit,Credit,YTDDebit,YTDCredit`.
- CSV schema, duplicate account IDs, reporting dates, balance pairs, source hashes, entity, basis, currency, tracking filters, and draft setting are all checked before review.
- Monetary values use `Decimal`, never binary floating point.
- `percent_change` in the model result is expressed in percent and quantized to four decimal places (`"18.3333"` means 18.3333%). It is `null` when there is no prior balance to compare against.
- The model result states its own `currency` and `sign_convention`. Amounts are debit-positive (`ytd_net = YTDDebit - YTDCredit`), so a revenue, liability, or equity balance is negative and a revenue increase shows as a negative `delta`.
- Current and prior reports must sit in the same Australian financial year, or be the same day and month in different years. YTD columns reset on 1 July, so a comparison across the reset would report a whole prior-year balance as a movement.
- Current/prior trial balances are joined by stable `AccountID`, not account display name or code.
- An account changing section between periods fails closed instead of disappearing from, or being silently reclassified within, a section-scoped comparison.
- The model result never contains a tenant name, account name, account code, source file path, token, raw error, or free text copied from source data.
- Artefact timestamps (`export.generated_at`, `reviewed_at`) are accepted as `YYYY-MM-DD`, then `T`, `t`, or a space, then `HH:MM` with optional `:SS` and optional `.` plus one to six fractional digits, then `Z`, `z`, or `+/-HH:MM` with optional `:SS`. The gateway fixes that grammar itself rather than inheriting `datetime.fromisoformat`, whose accepted forms widened in Python 3.11: a bare `+10` offset, a week date, a basic-format `20260809T000000+0000`, and a fraction longer than six digits are refused on every interpreter, as is a separator character other than `T`, `t`, or a space.
- The three run artefacts are staged beside their destinations and moved into place only once all three are written, receipt last. The moves are not one atomic step, so an interrupted run can still leave one new file beside two old ones; `validate-review` refuses that pack, because the receipt seals both the reviewer evidence and the model result sitting beside it.
- The package contains no network imports or mutation adapter. A future live connection must remain an authorised, read-only export handoff rather than an AI-controlled broad Xero tool set.

## Scope and limitation

Every source manifest, review context, model result, reviewer evidence, and receipt is marked `mode: synthetic`. The policy, request, and human-decision files carry no `mode` key: each is validated against an exact key set, so adding one is rejected. The `validate-review` output carries no `mode` key either; it reports the decision status for a run whose artefacts were already checked. It is a local design demonstration, not a client-data processor, production security system, accounting service, or professional opinion. The reviewer evidence/model-result file split demonstrates disclosure minimisation only; it is not an access-control mechanism by itself.

## Development

```bash
pytest
python -m build
```

MIT licensed.

