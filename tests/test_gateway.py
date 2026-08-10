from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from xero_ai_review_gateway.errors import GatewayError
from xero_ai_review_gateway.gateway import MODEL_PROJECTION, BalanceRow, _assert_model_is_redacted, _iso_timestamp, _load_tb, _variance_findings, evaluate, validate_review, write_evaluation
from xero_ai_review_gateway.util import package_root

PKG = Path(__file__).resolve().parents[1] / "xero_ai_review_gateway"

OPERATION = {
    "allowed_sections": ["Revenue"],
    "minimum_absolute_delta": "1000.00",
    "minimum_percent_delta": "15.00",
    "max_results": 25,
}


def _row(account_id: str, *, section: str = "Revenue", ytd_credit: str = "0.00") -> BalanceRow:
    return BalanceRow(
        report_date=date(2026, 6, 30),
        tenant="Unit Test Tenant",
        section=section,
        account_id=account_id,
        account_name=f"Name {account_id}",
        account_code="9999",
        debit=Decimal("0.00"),
        credit=Decimal("0.00"),
        ytd_debit=Decimal("0.00"),
        ytd_credit=Decimal(ytd_credit),
    )


def _evaluate():
    return evaluate(
        context_path=Path("samples/contexts/sample-monthly-variance.context.json"),
        request_path=Path("samples/requests/sample-revenue-variance.request.json"),
        policy_path=Path("policy/demo-policy-v1.json"),
    )


def test_bundled_data_resolves_from_the_package() -> None:
    root = package_root()
    assert (root / "policy" / "demo-policy-v1.json").is_file()
    assert (root / "samples" / "inputs" / "sample-tb-2026-06-30.csv").is_file()


def test_policy_bound_evaluation_returns_one_redacted_revenue_finding() -> None:
    model, evidence, receipt = _evaluate()

    assert model["status"] == "REVIEW_READY"
    assert len(model["findings"]) == 1
    assert model["findings"][0]["section"] == "Revenue"
    # 11000 / 60000, expressed in percent and quantized to four decimal places.
    assert model["findings"][0]["percent_change"] == "18.3333"
    assert model["total_findings"] == 1
    assert model["truncated"] is False
    model_text = json.dumps(model)
    assert "Demo Entity Pty Ltd" not in model_text
    assert "Demo Sales" not in model_text
    assert "acct-300" not in model_text
    assert evidence["items"][0]["account_name"] == "Demo Sales"
    assert receipt["run_id"] == model["run_id"]


def test_evaluation_writes_only_below_cwd_build_and_decision_can_be_validated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    model, evidence, receipt = _evaluate()
    paths = write_evaluation(model, evidence, receipt, Path("build") / "test-output")

    assert all(path.exists() for path in paths.values())
    assert all(path.is_relative_to(tmp_path / "build") for path in paths.values())
    result = validate_review(
        evidence_path=paths["evidence"],
        receipt_path=paths["receipt"],
        decision_path=Path("samples/decisions/sample-review-decision.json"),
    )
    assert result["status"] == "DECISION_RECORDED"
    assert result["undecided_count"] == 0


def test_tampered_evidence_fails_the_receipt_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    model, evidence, receipt = _evaluate()
    paths = write_evaluation(model, evidence, receipt, Path("build") / "run")
    tampered = json.loads(paths["evidence"].read_text(encoding="utf-8"))
    tampered["items"][0]["account_name"] = "Someone Else Entirely"
    paths["evidence"].write_text(json.dumps(tampered, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(GatewayError, match="evidence digest"):
        validate_review(
            evidence_path=paths["evidence"],
            receipt_path=paths["receipt"],
            decision_path=Path("samples/decisions/sample-review-decision.json"),
        )


def test_partially_decided_findings_are_counted_as_undecided(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from xero_ai_review_gateway import gateway

    real = gateway._variance_findings

    def doubled(*args, **kwargs):
        model_item, evidence_item = real(*args, **kwargs)[0]
        return [
            (dict(model_item, finding_id=f"finding:clone-{index}"), dict(evidence_item, finding_id=f"finding:clone-{index}"))
            for index in range(2)
        ]

    monkeypatch.setattr(gateway, "_variance_findings", doubled)
    monkeypatch.chdir(tmp_path)
    model, evidence, receipt = _evaluate()
    paths = write_evaluation(model, evidence, receipt, Path("build") / "run")
    decision = {
        "schema_version": "xero-human-review-decision.v1",
        "run_id": receipt["run_id"],
        "reviewer_ref": "unit-test-reviewer",
        "reviewed_at": "2026-08-09T00:00:00Z",
        "decisions": [
            {"finding_id": "finding:clone-0", "decision": "ACKNOWLEDGED", "rationale": "First finding reviewed."}
        ],
    }
    decision_path = tmp_path / "build" / "run" / "decision.json"
    decision_path.write_text(json.dumps(decision, indent=2) + "\n", encoding="utf-8")

    result = validate_review(evidence_path=paths["evidence"], receipt_path=paths["receipt"], decision_path=decision_path)
    assert result["status"] == "PARTIAL_DECISION_RECORDED"
    assert result["decision_count"] == 1
    assert result["undecided_count"] == 1
    # Nothing was capped here, so a second decision would complete the review.
    assert result["truncated"] is False
    assert result["completable"] is True


def test_a_truncated_run_reports_that_it_cannot_be_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decision can only name a visible finding.

    Findings dropped by max_results can never be decided, so the run stays
    PARTIAL_DECISION_RECORDED however many decisions are recorded. The result
    has to say so rather than looking like a review still in progress.
    """
    from xero_ai_review_gateway import gateway

    real = gateway._variance_findings

    def over_the_cap(*args, **kwargs):
        model_item, evidence_item = real(*args, **kwargs)[0]
        # The bundled policy caps results at 25.
        return [
            (dict(model_item, finding_id=f"finding:clone-{index}"), dict(evidence_item, finding_id=f"finding:clone-{index}"))
            for index in range(30)
        ]

    monkeypatch.setattr(gateway, "_variance_findings", over_the_cap)
    monkeypatch.chdir(tmp_path)
    model, evidence, receipt = _evaluate()
    paths = write_evaluation(model, evidence, receipt, Path("build") / "run")
    assert evidence["truncated"] is True

    decision = {
        "schema_version": "xero-human-review-decision.v1",
        "run_id": receipt["run_id"],
        "reviewer_ref": "unit-test-reviewer",
        "reviewed_at": "2026-08-09T00:00:00Z",
        "decisions": [
            {
                "finding_id": evidence["items"][0]["finding_id"],
                "decision": "ACKNOWLEDGED",
                "rationale": "Only visible finding reviewed.",
            }
        ],
    }
    decision_path = tmp_path / "build" / "run" / "decision.json"
    decision_path.write_text(json.dumps(decision, indent=2) + "\n", encoding="utf-8")

    result = validate_review(evidence_path=paths["evidence"], receipt_path=paths["receipt"], decision_path=decision_path)

    assert result["status"] == "PARTIAL_DECISION_RECORDED"
    assert result["visible_findings"] == 25
    assert result["undecided_count"] == 29
    assert result["truncated"] is True
    assert result["completable"] is False


def test_decision_file_is_accepted_from_cwd_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    model, evidence, receipt = _evaluate()
    paths = write_evaluation(model, evidence, receipt, Path("build") / "run")
    local_decision = tmp_path / "build" / "run" / "decision.json"
    local_decision.write_text((PKG / "samples" / "decisions" / "sample-review-decision.json").read_text(encoding="utf-8"), encoding="utf-8")

    result = validate_review(evidence_path=paths["evidence"], receipt_path=paths["receipt"], decision_path=local_decision)
    assert result["status"] == "DECISION_RECORDED"


def test_decision_file_outside_samples_and_build_is_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    model, evidence, receipt = _evaluate()
    paths = write_evaluation(model, evidence, receipt, Path("build") / "run")
    stray = tmp_path / "decision.json"
    stray.write_text((PKG / "samples" / "decisions" / "sample-review-decision.json").read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(GatewayError, match="human decision must exist under"):
        validate_review(evidence_path=paths["evidence"], receipt_path=paths["receipt"], decision_path=stray)


def test_evaluation_output_outside_cwd_build_is_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    model, evidence, receipt = _evaluate()

    with pytest.raises(GatewayError, match="output directory must stay within"):
        write_evaluation(model, evidence, receipt, tmp_path / "elsewhere")


def test_model_findings_carry_exactly_the_declared_projection() -> None:
    model, _, _ = _evaluate()

    for finding in model["findings"]:
        assert tuple(finding) == MODEL_PROJECTION


def test_account_present_only_in_the_current_period_is_reported() -> None:
    current = (_row("acct-1", ytd_credit="18500.00"), _row("acct-new", ytd_credit="5000.00"))
    prior = (_row("acct-1", ytd_credit="18000.00"),)

    findings = _variance_findings(current, prior, entity_ref="entity-1", section="Revenue", operation=OPERATION)

    assert len(findings) == 1
    model_item, evidence_item = findings[0]
    assert model_item["prior_ytd_net"] == "0"
    assert model_item["delta"] == "-5000.00"
    assert model_item["percent_change"] is None
    assert "new in the current period" in model_item["review_reason"]
    assert evidence_item["account_id"] == "acct-new"
    assert evidence_item["prior_values"] is None
    assert evidence_item["current_values"] is not None
    assert evidence_item["source_refs"] == ["source:current"]


def test_account_present_only_in_the_prior_period_is_reported() -> None:
    current = (_row("acct-1", ytd_credit="18000.00"),)
    prior = (_row("acct-1", ytd_credit="18000.00"), _row("acct-gone", ytd_credit="9000.00"))

    findings = _variance_findings(current, prior, entity_ref="entity-1", section="Revenue", operation=OPERATION)

    assert len(findings) == 1
    model_item, evidence_item = findings[0]
    assert model_item["current_ytd_net"] == "0"
    assert model_item["prior_ytd_net"] == "-9000.00"
    assert model_item["delta"] == "9000.00"
    assert model_item["percent_change"] == "100.0000"
    assert "only in the prior period" in model_item["review_reason"]
    assert evidence_item["account_id"] == "acct-gone"
    assert evidence_item["current_values"] is None
    assert evidence_item["prior_values"] is not None
    assert evidence_item["source_refs"] == ["source:prior"]


def test_findings_beyond_max_results_are_disclosed_not_silently_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    from xero_ai_review_gateway import gateway

    real = gateway._variance_findings

    def inflated(*args, **kwargs):
        model_item, evidence_item = real(*args, **kwargs)[0]
        return [
            (dict(model_item, finding_id=f"finding:clone-{index:02d}"), dict(evidence_item, finding_id=f"finding:clone-{index:02d}"))
            for index in range(27)
        ]

    monkeypatch.setattr(gateway, "_variance_findings", inflated)
    model, evidence, _ = _evaluate()

    assert model["total_findings"] == 27
    assert model["truncated"] is True
    assert len(model["findings"]) == 25
    assert evidence["total_findings"] == 27
    assert evidence["truncated"] is True
    assert len(evidence["items"]) == 25


def test_one_sided_account_outside_the_requested_section_is_not_reported() -> None:
    current = (_row("acct-1", ytd_credit="18000.00"), _row("acct-asset", section="Assets", ytd_credit="7000.00"))
    prior = (_row("acct-1", ytd_credit="18000.00"),)

    findings = _variance_findings(current, prior, entity_ref="entity-1", section="Revenue", operation=OPERATION)

    assert findings == []


def test_account_section_drift_fails_closed() -> None:
    current = (_row("acct-1", section="Assets", ytd_credit="18000.00"),)
    prior = (_row("acct-1", section="Revenue", ytd_credit="9000.00"),)

    with pytest.raises(GatewayError, match="changed section"):
        _variance_findings(current, prior, entity_ref="entity-1", section="Revenue", operation=OPERATION)


def test_section_drift_message_is_deterministic_and_names_every_account() -> None:
    """Iterating the raw set intersection named an arbitrary one of several."""
    current = tuple(
        _row(f"acct-{n}", section="Assets", ytd_credit="18000.00") for n in ("c", "a", "b")
    )
    prior = tuple(
        _row(f"acct-{n}", section="Revenue", ytd_credit="9000.00") for n in ("c", "a", "b")
    )

    messages = set()
    for _ in range(8):
        with pytest.raises(GatewayError) as caught:
            _variance_findings(
                current, prior, entity_ref="entity-1", section="Revenue", operation=OPERATION
            )
        messages.add(str(caught.value))

    assert len(messages) == 1
    message = messages.pop()
    assert "'acct-a', 'acct-b', 'acct-c'" in message


def test_section_drift_outside_requested_section_does_not_block_review() -> None:
    current = (
        _row("acct-revenue", section="Revenue", ytd_credit="18000.00"),
        _row("acct-unrelated", section="Liabilities", ytd_credit="7000.00"),
    )
    prior = (
        _row("acct-revenue", section="Revenue", ytd_credit="18000.00"),
        _row("acct-unrelated", section="Assets", ytd_credit="7000.00"),
    )

    findings = _variance_findings(
        current,
        prior,
        entity_ref="entity-1",
        section="Revenue",
        operation=OPERATION,
    )

    assert findings == []


@pytest.mark.parametrize("value", ["2026-08-09", "2026-08-09T00:00:00"])
def test_review_timestamps_require_an_explicit_timezone(value: str) -> None:
    with pytest.raises(GatewayError, match="explicit UTC offset"):
        _iso_timestamp(value, field="reviewed_at")


def test_unbalanced_source_fails_closed(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    source = (PKG / "samples" / "inputs" / "sample-tb-2026-06-30.csv").read_text(encoding="utf-8")
    bad.write_text(source.replace(",10000.00,0.00,50000.00", ",10000.01,0.00,50000.00"), encoding="utf-8")

    with pytest.raises(GatewayError, match="movement debit and credit totals"):
        _load_tb(bad)


def test_empty_report_date_is_reported_as_empty_not_bad_iso(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    source = (PKG / "samples" / "inputs" / "sample-tb-2026-06-30.csv").read_text(encoding="utf-8")
    bad.write_text(source.replace("2026-06-30,Demo Entity Pty Ltd,Assets,acct-100", ",Demo Entity Pty Ltd,Assets,acct-100", 1), encoding="utf-8")

    with pytest.raises(GatewayError, match="ReportDate must be a non-empty string"):
        _load_tb(bad)


def test_malformed_evidence_items_are_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from xero_ai_review_gateway.util import canonical_json, sha256_bytes

    monkeypatch.chdir(tmp_path)
    build = tmp_path / "build"
    build.mkdir()
    evidence = {
        "schema_version": "xero-reviewer-evidence.v1",
        "run_id": "sha256:unit-test",
        "mode": "synthetic",
        "items": ["not-a-dict"],
        "total_findings": 1,
        "truncated": False,
    }
    receipt = {
        "schema_version": "xero-review-receipt.v1",
        "run_id": "sha256:unit-test",
        "mode": "synthetic",
        "policy_sha256": "sha256:0",
        "request_sha256": "sha256:0",
        "source_digests": {},
        "result_sha256": "sha256:0",
        "evidence_sha256": "sha256:" + sha256_bytes(canonical_json(evidence)),
        "code_version": "0.1.0",
    }
    decision = {
        "schema_version": "xero-human-review-decision.v1",
        "run_id": "sha256:unit-test",
        "reviewer_ref": "unit-test-reviewer",
        "reviewed_at": "2026-08-09T00:00:00Z",
        "decisions": [{"finding_id": "x", "decision": "ACKNOWLEDGED", "rationale": "r"}],
    }
    for name, payload in (("evidence", evidence), ("receipt", receipt), ("decision", decision)):
        (build / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(GatewayError, match="evidence items"):
        validate_review(evidence_path=build / "evidence.json", receipt_path=build / "receipt.json", decision_path=build / "decision.json")


def test_unknown_section_is_denied_by_policy(tmp_path: Path) -> None:
    model, _, _ = _evaluate()
    request = json.loads((PKG / "samples" / "requests" / "sample-revenue-variance.request.json").read_text(encoding="utf-8"))
    request["section"] = "Assets"
    bad = tmp_path / "request.json"
    bad.write_text(json.dumps(request), encoding="utf-8")

    from xero_ai_review_gateway.gateway import _load_policy, _load_request

    policy = _load_policy(PKG / "policy" / "demo-policy-v1.json")
    with pytest.raises(GatewayError, match="not allowlisted"):
        _load_request(bad, policy)
    assert model["mode"] == "synthetic"


def test_numeric_account_id_inside_an_amount_is_not_a_disclosure() -> None:
    # AccountID "100" occurs as a substring of the amount below; only an
    # exact leaf match is a disclosure.
    rows = (_row("100"),)
    model = {"findings": [{"delta": "-11000.00"}]}

    _assert_model_is_redacted(model, rows)


def test_exact_account_id_leaf_still_trips_the_disclosure_check() -> None:
    rows = (_row("100"),)
    model = {"findings": [{"delta": "100"}]}

    with pytest.raises(GatewayError, match="raw source display data"):
        _assert_model_is_redacted(model, rows)


def test_v01_source_does_not_import_a_network_client() -> None:
    package_sources = "\n".join(path.read_text(encoding="utf-8") for path in PKG.glob("*.py"))
    for forbidden in ("requests", "urllib", "http.client", "socket", "mcp"):
        assert forbidden not in package_sources
