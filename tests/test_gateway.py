from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from xero_ai_review_gateway.errors import GatewayError
from xero_ai_review_gateway.gateway import ALLOWED_DECISIONS, MODEL_PROJECTION, BalanceRow, _assert_model_is_redacted, _iso_timestamp, _load_tb, _variance_findings, evaluate, validate_review, write_evaluation
from xero_ai_review_gateway.util import canonical_json, package_root, sha256_bytes

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


DRIFT_NAMES = ("c", "a", "b", "e", "d")
DRIFT_MESSAGE = (
    "Accounts changed section between periods "
    "('acct-a', 'acct-b', 'acct-c', 'acct-d', 'acct-e'); "
    "review the source mapping before comparison."
)
# Run in its own interpreter so PYTHONHASHSEED actually takes effect: set
# iteration order is fixed once a process has started, which is why a loop
# inside one test could never observe the non-determinism it was named for.
DRIFT_PROBE = """
import sys
from datetime import date
from decimal import Decimal

from xero_ai_review_gateway.errors import GatewayError
from xero_ai_review_gateway.gateway import BalanceRow, _variance_findings


def row(account_id, section):
    return BalanceRow(
        report_date=date(2026, 6, 30), tenant="T", section=section, account_id=account_id,
        account_name="N", account_code="9999", debit=Decimal("0.00"), credit=Decimal("0.00"),
        ytd_debit=Decimal("0.00"), ytd_credit=Decimal("18000.00"),
    )


names = %r
operation = {
    "allowed_sections": ["Revenue"], "minimum_absolute_delta": "1000.00",
    "minimum_percent_delta": "15.00", "max_results": 25,
}
try:
    _variance_findings(
        tuple(row("acct-" + name, "Assets") for name in names),
        tuple(row("acct-" + name, "Revenue") for name in names),
        entity_ref="entity-1", section="Revenue", operation=operation,
    )
except GatewayError as exc:
    sys.stdout.write(str(exc))
    sys.exit(0)
sys.exit(1)
""" % (DRIFT_NAMES,)


def test_section_drift_message_names_every_account_in_sorted_order() -> None:
    current = tuple(_row(f"acct-{n}", section="Assets", ytd_credit="18000.00") for n in DRIFT_NAMES)
    prior = tuple(_row(f"acct-{n}", section="Revenue", ytd_credit="9000.00") for n in DRIFT_NAMES)

    with pytest.raises(GatewayError) as caught:
        _variance_findings(current, prior, entity_ref="entity-1", section="Revenue", operation=OPERATION)

    assert str(caught.value) == DRIFT_MESSAGE


def test_section_drift_message_is_identical_under_different_string_hash_seeds() -> None:
    """The same inputs must not name the drifted accounts in a different order run to run."""
    messages = set()
    for seed in ("0", "1", "2", "3", "4", "5", "6", "7"):
        completed = subprocess.run(
            [sys.executable, "-c", DRIFT_PROBE],
            env={**os.environ, "PYTHONHASHSEED": seed},
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        messages.add(completed.stdout.strip())

    assert messages == {DRIFT_MESSAGE}


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


@pytest.mark.parametrize("value", ["2026-08-09", "2026-08-09T00:00:00", "2026-08-09 00:00:00", "2026-08-09T00:00"])
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


def _decided_run(tmp_path: Path, state: str) -> tuple[dict[str, Path], Path]:
    """A completed run in tmp_path/build plus a decision file recording one state."""
    model, evidence, receipt = _evaluate()
    paths = write_evaluation(model, evidence, receipt, Path("build") / "run")
    decision = {
        "schema_version": "xero-human-review-decision.v1",
        "run_id": receipt["run_id"],
        "reviewer_ref": "unit-test-reviewer",
        "reviewed_at": "2026-08-09T00:00:00Z",
        "decisions": [
            {
                "finding_id": evidence["items"][0]["finding_id"],
                "decision": state,
                "rationale": "Fabricated demo variance reviewed.",
            }
        ],
    }
    decision_path = tmp_path / "build" / "run" / "decision.json"
    decision_path.write_text(json.dumps(decision, indent=2) + "\n", encoding="utf-8")
    return paths, decision_path


def test_the_decision_allowlist_is_exactly_the_three_documented_states() -> None:
    # Widening this set is a decision to be argued for in a test change, not
    # something a refactor can do quietly. The README promises these three.
    assert ALLOWED_DECISIONS == {"ACKNOWLEDGED", "NEEDS_EVIDENCE", "ESCALATED"}


@pytest.mark.parametrize("state", ["APPROVED", "RESOLVED", "POSTED", "PAID", "LODGED", "LOCKED"])
def test_a_decision_asserting_an_accounting_action_is_refused(state: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    paths, decision_path = _decided_run(tmp_path, state)

    with pytest.raises(GatewayError, match="decision state or rationale is invalid"):
        validate_review(evidence_path=paths["evidence"], receipt_path=paths["receipt"], decision_path=decision_path)


@pytest.mark.parametrize("state", ["ACKNOWLEDGED", "NEEDS_EVIDENCE", "ESCALATED"])
def test_each_documented_review_state_is_recorded(state: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    paths, decision_path = _decided_run(tmp_path, state)

    result = validate_review(evidence_path=paths["evidence"], receipt_path=paths["receipt"], decision_path=decision_path)

    assert result["status"] == "DECISION_RECORDED"


def test_a_truncated_flag_that_contradicts_the_item_counts_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    model, evidence, receipt = _evaluate()
    paths = write_evaluation(model, evidence, receipt, Path("build") / "run")
    tampered = json.loads(paths["evidence"].read_text(encoding="utf-8"))
    # One item, one total finding: nothing was omitted, so this flag lies.
    tampered["truncated"] = True
    paths["evidence"].write_text(json.dumps(tampered, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    resealed = json.loads(paths["receipt"].read_text(encoding="utf-8"))
    resealed["evidence_sha256"] = "sha256:" + sha256_bytes(canonical_json(tampered))
    paths["receipt"].write_text(json.dumps(resealed, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(GatewayError, match="truncated must exactly describe"):
        validate_review(
            evidence_path=paths["evidence"],
            receipt_path=paths["receipt"],
            decision_path=Path("samples/decisions/sample-review-decision.json"),
        )


def test_evaluate_refuses_to_emit_a_model_result_carrying_raw_source_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """The disclosure assertion has to run on the production path, not only in its own unit test."""
    from xero_ai_review_gateway import gateway

    real = gateway._variance_findings

    def leaky(*args, **kwargs):
        model_item, evidence_item = real(*args, **kwargs)[0]
        return [(dict(model_item, review_reason=evidence_item["account_name"]), evidence_item)]

    monkeypatch.setattr(gateway, "_variance_findings", leaky)

    with pytest.raises(GatewayError, match="Internal disclosure assertion failed"):
        _evaluate()


def test_model_result_states_its_currency_and_sign_convention() -> None:
    model, _, _ = _evaluate()

    assert model["currency"] == "AUD"
    assert "ytd_net = YTDDebit - YTDCredit" in model["sign_convention"]
    # Demo revenue rose from 60,000 to 71,000. Under the stated debit-positive
    # convention that is a delta of -11000.00, and the artefact now says so.
    assert model["findings"][0]["delta"] == "-11000.00"


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-09T00:00:00Z",
        "2026-08-09T00:00:00z",
        "2026-08-09T14:30:00+10:00",
        "2026-08-09T00:00:00-05:30",
        "2026-08-09T00:00:00.123+00:00",
        "2026-08-09T00:00:00.123456+00:00",
        "2026-08-09T00:00:00.1+00:00",
        # datetime.fromisoformat accepted each of the four below on 3.10, 3.12
        # and 3.13 alike, so an already stored artefact may use any of them.
        # Pinning the grammar must not refuse a form that used to work.
        "2026-08-09 00:00:00+00:00",
        "2026-08-09t00:00:00+00:00",
        "2026-08-09T00:00+00:00",
        "2026-08-09T00:00:00+10:00:30",
    ],
)
def test_accepted_timestamp_forms_do_not_depend_on_the_interpreter(value: str) -> None:
    assert _iso_timestamp(value, field="reviewed_at") == value


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-09T00:00:00+10",  # datetime.fromisoformat accepts this on 3.11+ only
        "20260809T000000+0000",
        "2026-W32-7T00:00:00+00:00",
        "2026-08-09T00:00:00.123456789+00:00",
        # A separator that is neither T/t nor a space: refused on every
        # interpreter, and named as refused in the README grammar.
        "2026-08-09X00:00:00+00:00",
        "2026-08-09T00:00:00Z extra",
    ],
)
def test_rejected_timestamp_forms_do_not_depend_on_the_interpreter(value: str) -> None:
    with pytest.raises(GatewayError, match="must be an ISO 8601 timestamp"):
        _iso_timestamp(value, field="reviewed_at")


def test_the_readme_states_the_timestamp_grammar_the_gateway_enforces() -> None:
    """The accepted grammar is a contract for artefact authors, so it is written down."""
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    grammar = next(line for line in readme.splitlines() if line.startswith("- Artefact timestamps"))

    for documented in ("`T`, `t`, or a space", "`Z`, `z`, or `+/-HH:MM` with optional `:SS`"):
        assert documented in grammar
    for refused in ("+10", "week date", "20260809T000000+0000"):
        assert refused in grammar


def test_an_interrupted_rerun_leaves_the_previous_run_intact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A half-written pack must not mix one run's model result with another run's receipt."""
    from xero_ai_review_gateway import gateway

    monkeypatch.chdir(tmp_path)
    model, evidence, receipt = _evaluate()
    output_dir = Path("build") / "run"
    paths = write_evaluation(model, evidence, receipt, output_dir)
    before = {key: path.read_text(encoding="utf-8") for key, path in paths.items()}

    real_write = gateway._write_json
    written: list[Path] = []

    def failing(path: Path, payload: dict) -> None:
        written.append(path)
        if len(written) == 2:
            raise OSError(28, "No space left on device")
        real_write(path, payload)

    monkeypatch.setattr(gateway, "_write_json", failing)
    second_run = dict(model, run_id="sha256:a-different-run")

    with pytest.raises(GatewayError, match="run output cannot be written"):
        write_evaluation(second_run, evidence, receipt, output_dir)

    assert {key: path.read_text(encoding="utf-8") for key, path in paths.items()} == before
    assert sorted(path.name for path in (tmp_path / "build" / "run").iterdir()) == [
        "model-result.json",
        "receipt.json",
        "reviewer-evidence.json",
    ]


def test_a_pack_mixed_by_a_failed_move_is_refused_by_validate_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Three moves are not one atomic step, so the mixed pack has to fail closed at validation.

    The staged writes all succeed here and the failure lands between the moves,
    which leaves the second run's model result beside the first run's evidence
    and receipt. Those two still agree with each other, so only the receipt's
    result digest can tell that the pack describes two runs.
    """
    from xero_ai_review_gateway import gateway

    monkeypatch.chdir(tmp_path)
    model, evidence, receipt = _evaluate()
    output_dir = Path("build") / "run"
    paths = write_evaluation(model, evidence, receipt, output_dir)
    real_replace = gateway._replace
    moved: list[Path] = []

    def failing(source: Path, destination: Path) -> None:
        moved.append(destination)
        if len(moved) == 2:
            raise OSError(13, "Permission denied")
        real_replace(source, destination)

    monkeypatch.setattr(gateway, "_replace", failing)
    second_run = dict(model, run_id="sha256:a-different-run")

    with pytest.raises(GatewayError, match="run output cannot be written"):
        write_evaluation(second_run, evidence, receipt, output_dir)

    assert json.loads(paths["model"].read_text(encoding="utf-8"))["run_id"] == "sha256:a-different-run"
    assert json.loads(paths["receipt"].read_text(encoding="utf-8"))["run_id"] == receipt["run_id"]
    with pytest.raises(GatewayError, match="does not match the receipt's result digest"):
        validate_review(
            evidence_path=paths["evidence"],
            receipt_path=paths["receipt"],
            decision_path=Path("samples/decisions/sample-review-decision.json"),
        )


def test_a_reviewer_holding_only_the_evidence_and_receipt_can_still_validate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The evidence/model split is the point, so the model result is checked when present, not required."""
    monkeypatch.chdir(tmp_path)
    model, evidence, receipt = _evaluate()
    paths = write_evaluation(model, evidence, receipt, Path("build") / "run")
    paths["model"].unlink()

    result = validate_review(
        evidence_path=paths["evidence"],
        receipt_path=paths["receipt"],
        decision_path=Path("samples/decisions/sample-review-decision.json"),
    )

    assert result["status"] == "DECISION_RECORDED"


def test_the_scope_note_names_exactly_the_artefacts_that_carry_the_mode_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    model, evidence, receipt = _evaluate()
    paths = write_evaluation(model, evidence, receipt, Path("build") / "run")
    validation = validate_review(
        evidence_path=paths["evidence"],
        receipt_path=paths["receipt"],
        decision_path=Path("samples/decisions/sample-review-decision.json"),
    )
    context = json.loads((PKG / "samples" / "contexts" / "sample-monthly-variance.context.json").read_text(encoding="utf-8"))
    manifest = json.loads((PKG / "samples" / "manifests" / "sample-tb-2026-06-30.manifest.json").read_text(encoding="utf-8"))

    assert {artefact["mode"] for artefact in (manifest, context, model, evidence, receipt)} == {"synthetic"}
    # The validation output is emitted too, and it carries no mode key, so the
    # README cannot claim the marker for every emitted artefact.
    assert "mode" not in validation
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert "Every source manifest, review context, model result, reviewer evidence, and receipt is marked `mode: synthetic`." in readme
    assert "The `validate-review` output carries no `mode` key either" in readme


def test_output_directory_occupied_by_a_file_is_blocked_not_a_traceback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    model, evidence, receipt = _evaluate()
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "occupied").write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(GatewayError, match="run output cannot be written"):
        write_evaluation(model, evidence, receipt, Path("build") / "occupied")


def test_v01_source_does_not_import_a_network_client() -> None:
    package_sources = "\n".join(path.read_text(encoding="utf-8") for path in PKG.glob("*.py"))
    for forbidden in ("requests", "urllib", "http.client", "socket", "mcp"):
        assert forbidden not in package_sources
