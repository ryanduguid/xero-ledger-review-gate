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


def test_a_movement_over_the_absolute_floor_but_under_the_percent_floor_is_not_reported() -> None:
    """Both floors bound the reported set, so clearing one of them is not enough.

    Every other variance test either clears both floors or has percent None,
    the one-sided case where the percentage clause is skipped by design, so the
    percentage half of the policy's bounded-results contract never ran.
    """
    # -61,500 against -60,000 is a 1,500 movement: over minimum_absolute_delta
    # 1000.00, and 2.50% against a minimum_percent_delta of 15.00.
    current = (_row("acct-1", ytd_credit="61500.00"),)
    prior = (_row("acct-1", ytd_credit="60000.00"),)

    assert _variance_findings(current, prior, entity_ref="entity-1", section="Revenue", operation=OPERATION) == []


def test_a_movement_exactly_at_the_percent_floor_is_reported() -> None:
    """The floor is a minimum, not a value to exceed, so 15.00% is inside the reported set."""
    current = (_row("acct-1", ytd_credit="69000.00"),)
    prior = (_row("acct-1", ytd_credit="60000.00"),)

    findings = _variance_findings(current, prior, entity_ref="entity-1", section="Revenue", operation=OPERATION)

    assert len(findings) == 1
    assert findings[0][0]["delta"] == "-9000.00"
    assert findings[0][0]["percent_change"] == "15.0000"


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


EVIDENCE_ITEM = {
    "finding_id": "x",
    "account_id": "acct-1",
    "account_code": "9999",
    "account_name": "Name acct-1",
    "current_values": None,
    "prior_values": None,
    "source_refs": ["source:current"],
}


@pytest.mark.parametrize(
    "items",
    [
        ["not-a-dict"],
        # A dict is not enough: an item carrying an extra field, or missing a
        # declared one, still has the finding_id every later line reads, so
        # only the exact key-set comparison can refuse either.
        [dict(EVIDENCE_ITEM, unexpected="extra")],
        [{key: value for key, value in EVIDENCE_ITEM.items() if key != "source_refs"}],
    ],
)
def test_malformed_evidence_items_are_blocked(items: list, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from xero_ai_review_gateway.util import canonical_json, sha256_bytes

    monkeypatch.chdir(tmp_path)
    build = tmp_path / "build"
    build.mkdir()
    evidence = {
        "schema_version": "xero-reviewer-evidence.v1",
        "run_id": "sha256:unit-test",
        "mode": "synthetic",
        "items": items,
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


@pytest.mark.parametrize("leaf", ["Unit Test Tenant", "Name acct-1", "9999"])
def test_every_source_display_value_the_readme_names_is_forbidden_as_a_leaf(leaf: str) -> None:
    """Tenant, account name and account code are the three values README "Control boundary" names.

    AccountID has its own test above. The account code was reachable only as a
    key name, so a code leaking into a model leaf under some future projection
    would have passed the last-line defence that exists to catch exactly that.
    """
    rows = (_row("acct-1"),)
    model = {"findings": [{"review_reason": leaf}]}

    with pytest.raises(GatewayError, match="raw source display data"):
        _assert_model_is_redacted(model, rows)


@pytest.mark.parametrize("field", ["tenant", "account_name", "account_code"])
def test_a_prohibited_source_field_name_trips_the_disclosure_check(field: str) -> None:
    """The key-name branch catches a source field whose value no row happens to hold."""
    rows = (_row("acct-1"),)
    model = {"findings": [{field: "a value no row carries"}]}

    with pytest.raises(GatewayError, match="prohibited source field"):
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


def _resealed_run(
    tmp_path: Path,
    *,
    evidence_edit=None,
    receipt_edit=None,
    decision_edit=None,
) -> dict[str, Path]:
    """A completed run whose artefacts are edited and then resealed against each other.

    validate_review compares the receipt's evidence digest before every shape
    gate, so an unsealed edit is refused by the digest check and proves nothing
    about the gate a test names. Resealing takes the digest out of the picture
    and leaves exactly one line able to refuse the pack, which is what makes
    deleting that line turn the test red.
    """
    model, evidence, receipt = _evaluate()
    paths = write_evaluation(model, evidence, receipt, Path("build") / "run")
    evidence = json.loads(paths["evidence"].read_text(encoding="utf-8"))
    receipt = json.loads(paths["receipt"].read_text(encoding="utf-8"))
    decision = {
        "schema_version": "xero-human-review-decision.v1",
        "run_id": receipt["run_id"],
        "reviewer_ref": "unit-test-reviewer",
        "reviewed_at": "2026-08-09T00:00:00Z",
        "decisions": [
            {
                "finding_id": evidence["items"][0]["finding_id"],
                "decision": "ACKNOWLEDGED",
                "rationale": "Fabricated demo variance reviewed.",
            }
        ],
    }
    for payload, edit in ((evidence, evidence_edit), (decision, decision_edit)):
        if edit is not None:
            edit(payload)
    receipt["evidence_sha256"] = "sha256:" + sha256_bytes(canonical_json(evidence))
    if receipt_edit is not None:
        receipt_edit(receipt)
    for key, payload in (("evidence", evidence), ("receipt", receipt)):
        paths[key].write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths["decision"] = tmp_path / "build" / "run" / "decision.json"
    paths["decision"].write_text(json.dumps(decision, indent=2) + "\n", encoding="utf-8")
    return paths


def _validate(paths: dict[str, Path]) -> dict:
    return validate_review(evidence_path=paths["evidence"], receipt_path=paths["receipt"], decision_path=paths["decision"])


def test_the_resealed_pack_helper_leaves_a_run_that_still_validates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no edit applied the pack must pass, or the refusals below prove nothing."""
    monkeypatch.chdir(tmp_path)

    assert _validate(_resealed_run(tmp_path))["status"] == "DECISION_RECORDED"


def test_a_decision_naming_a_finding_the_evidence_does_not_carry_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """This is the binding between the human sign-off and the reviewer evidence.

    Without it a fabricated finding_id counts toward the decided set, so
    undecided_count reaches zero and validate_review reports DECISION_RECORDED
    for a run whose real findings were never looked at.
    """
    monkeypatch.chdir(tmp_path)

    def fabricate(decision: dict) -> None:
        decision["decisions"][0]["finding_id"] = "finding:never-emitted"

    with pytest.raises(GatewayError, match="unknown or duplicate finding"):
        _validate(_resealed_run(tmp_path, decision_edit=fabricate))


def test_the_same_finding_decided_twice_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two contradictory decisions for one finding must not collapse into one recorded decision."""
    monkeypatch.chdir(tmp_path)

    def decide_twice(decision: dict) -> None:
        decision["decisions"].append(dict(decision["decisions"][0], decision="ESCALATED", rationale="And again."))

    with pytest.raises(GatewayError, match="unknown or duplicate finding"):
        _validate(_resealed_run(tmp_path, decision_edit=decide_twice))


def test_a_reviewed_at_without_an_offset_is_refused_through_validate_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The timestamp grammar has its own unit tests; this pins the call site that runs it."""
    monkeypatch.chdir(tmp_path)

    def strip_offset(decision: dict) -> None:
        decision["reviewed_at"] = "2026-08-09T00:00:00"

    with pytest.raises(GatewayError, match="explicit UTC offset"):
        _validate(_resealed_run(tmp_path, decision_edit=strip_offset))


@pytest.mark.parametrize("artefact", ["evidence", "receipt"])
def test_a_review_artefact_not_marked_synthetic_is_refused(artefact: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    def relabel(payload: dict) -> None:
        payload["mode"] = "live"

    with pytest.raises(GatewayError, match="Only synthetic review artefacts"):
        _validate(_resealed_run(tmp_path, **{f"{artefact}_edit": relabel}))


@pytest.mark.parametrize("artefact", ["evidence", "receipt", "decision"])
def test_a_run_id_that_disagrees_across_the_pack_is_refused(
    artefact: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This is the only line binding a human sign-off to the run it claims to be about.

    Nothing else compares the three run_id values: the receipt digests seal the
    evidence and the model result, not the decision. Without this check a
    decision written for one run validates against another run's evidence and
    receipt whenever the finding IDs coincide, and validate_review reports
    DECISION_RECORDED for a run nobody signed off.
    """
    monkeypatch.chdir(tmp_path)

    def restamp(payload: dict) -> None:
        payload["run_id"] = "sha256:some-other-run"

    with pytest.raises(GatewayError, match="must refer to the same run_id"):
        _validate(_resealed_run(tmp_path, **{f"{artefact}_edit": restamp}))


def test_reviewer_evidence_carrying_one_finding_twice_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two items sharing a finding_id make one decision look like two, or hide one of the pair."""
    monkeypatch.chdir(tmp_path)

    def duplicate(evidence: dict) -> None:
        evidence["items"].append(dict(evidence["items"][0]))
        evidence["total_findings"] = 2

    with pytest.raises(GatewayError, match="duplicate finding IDs"):
        _validate(_resealed_run(tmp_path, evidence_edit=duplicate))


@pytest.mark.parametrize("value", [True, 0])
def test_evidence_total_findings_that_is_boolean_or_below_the_item_count_is_refused(
    value: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """total_findings drives undecided_count, so JSON true would close a review on its own."""
    monkeypatch.chdir(tmp_path)

    def retotal(evidence: dict) -> None:
        evidence["total_findings"] = value

    with pytest.raises(GatewayError, match="total_findings must be an integer at least as large"):
        _validate(_resealed_run(tmp_path, evidence_edit=retotal))


@pytest.mark.parametrize("reviewer_ref", ["", "   ", "demo\nreviewer"])
def test_a_blank_or_control_character_reviewer_ref_is_refused(
    reviewer_ref: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reviewer_ref is the only record of who signed off, and a control character rewrites a log line."""
    monkeypatch.chdir(tmp_path)

    def rename(decision: dict) -> None:
        decision["reviewer_ref"] = reviewer_ref

    with pytest.raises(GatewayError, match="human decision reviewer_ref"):
        _validate(_resealed_run(tmp_path, decision_edit=rename))


@pytest.mark.parametrize("rationale", ["reviewed\nACKNOWLEDGED by the reviewer", "reviewed\x7f"])
def test_a_control_character_in_a_decision_rationale_is_refused(
    rationale: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rationale is reviewer free text written into a record, exactly like reviewer_ref.

    The line above this one already refuses a non-string or blank rationale, so
    the control-character branch is the whole of what this call still does.
    Without it a rationale can carry a newline and forge a second line in any
    log or report that prints the decision record.
    """
    monkeypatch.chdir(tmp_path)

    def rewrite(decision: dict) -> None:
        decision["decisions"][0]["rationale"] = rationale

    with pytest.raises(GatewayError, match="human decision rationale must not contain control characters"):
        _validate(_resealed_run(tmp_path, decision_edit=rewrite))


def test_a_decision_state_that_is_not_a_string_is_refused_not_a_type_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the isinstance guard the allowlist test raises TypeError: unhashable type."""
    monkeypatch.chdir(tmp_path)

    def listify(decision: dict) -> None:
        decision["decisions"][0]["decision"] = ["ACKNOWLEDGED"]

    with pytest.raises(GatewayError, match="decision state or rationale is invalid"):
        _validate(_resealed_run(tmp_path, decision_edit=listify))


@pytest.mark.parametrize("decisions", [[], {}, "ACKNOWLEDGED", None])
def test_a_decision_file_that_records_no_decision_is_refused(
    decisions: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty decisions array is not a partial review; it is a file that decided nothing.

    Without this gate an empty list reaches undecided_count == total_findings
    and is reported as PARTIAL_DECISION_RECORDED, so a decision file recording
    nothing at all becomes a valid review record instead of a blocked run.
    """
    monkeypatch.chdir(tmp_path)

    def replace(decision: dict) -> None:
        decision["decisions"] = decisions

    with pytest.raises(GatewayError, match="must contain at least one decision"):
        _validate(_resealed_run(tmp_path, decision_edit=replace))


@pytest.mark.parametrize("mangle", ["not-a-dict", "extra-field", "missing-field"])
def test_malformed_human_decision_entries_are_blocked(
    mangle: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The evidence items are checked this way; the decision entries need the same.

    A non-dict entry reaches item["finding_id"] and raises TypeError instead of
    a blocked run, and an entry carrying an extra field still has every key the
    later lines read, so only the exact key-set comparison can refuse either.
    """
    monkeypatch.chdir(tmp_path)

    def mangle_entry(decision: dict) -> None:
        entry = decision["decisions"][0]
        if mangle == "not-a-dict":
            decision["decisions"][0] = entry["finding_id"]
        elif mangle == "extra-field":
            decision["decisions"][0] = dict(entry, unexpected="extra")
        else:
            decision["decisions"][0] = {key: value for key, value in entry.items() if key != "rationale"}

    with pytest.raises(GatewayError, match="Each human decision must contain exactly"):
        _validate(_resealed_run(tmp_path, decision_edit=mangle_entry))


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


def test_the_three_artefacts_the_scope_note_exempts_reject_an_added_mode_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The scope note says adding a `mode` key to these three is rejected; this runs that rule.

    The test above asserts the README sentence is present. Only the exact
    key-set comparison in load_json_exact enforces it, and with that one line
    disabled all three of these artefacts load and the run completes, so the
    tie-out was pinning prose that nothing policed.
    """
    from xero_ai_review_gateway.gateway import _load_policy, _load_request

    policy_path = tmp_path / "policy.json"
    policy_source = json.loads((PKG / "policy" / "demo-policy-v1.json").read_text(encoding="utf-8"))
    policy_path.write_text(json.dumps(dict(policy_source, mode="live")), encoding="utf-8")
    with pytest.raises(GatewayError, match="policy must contain exactly: model_projection, operations, policy_id, schema_version"):
        _load_policy(policy_path)

    request_path = tmp_path / "request.json"
    request_source = json.loads((PKG / "samples" / "requests" / "sample-revenue-variance.request.json").read_text(encoding="utf-8"))
    request_path.write_text(json.dumps(dict(request_source, mode="live")), encoding="utf-8")
    with pytest.raises(GatewayError, match="review request must contain exactly: operation, policy_id, request_id, schema_version, section"):
        _load_request(request_path, _load_policy(PKG / "policy" / "demo-policy-v1.json"))

    monkeypatch.chdir(tmp_path)

    def mark_live(decision: dict) -> None:
        decision["mode"] = "live"

    with pytest.raises(GatewayError, match="human decision must contain exactly: decisions, reviewed_at, reviewer_ref, run_id, schema_version"):
        _validate(_resealed_run(tmp_path, decision_edit=mark_live))


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


def test_an_absurd_magnitude_is_refused_not_raised_out_of_the_balance_gate() -> None:
    """`1E+1000000` parses and is finite, so it reached the arithmetic and the
    first sum raised decimal.Overflow out of the gate. A gateway that exists to
    fail closed has to refuse the file instead."""
    from xero_ai_review_gateway.gateway import _decimal

    with pytest.raises(GatewayError, match="supported magnitude range"):
        _decimal("1E+1000000", field="YTDDebit")

    # the ordinary range is untouched
    assert _decimal("0.00", field="YTDDebit") == Decimal("0.00")
    assert _decimal("-999999999999999999", field="YTDDebit") == Decimal("-999999999999999999")


def test_an_absurd_percentage_is_refused_not_raised_out_of_the_projection() -> None:
    """quantize raises InvalidOperation once the result needs more digits than
    the context allows, and that walked out of _percent_string as a bare
    decimal error rather than the fail-closed GatewayError."""
    from xero_ai_review_gateway.gateway import _percent_string

    with pytest.raises(GatewayError, match="supported magnitude range"):
        _percent_string(Decimal("1E+30"))

    assert _percent_string(Decimal("0.183333")) == "18.3333"


def test_a_non_iterable_model_projection_is_refused_not_a_type_error(tmp_path: Path) -> None:
    """tuple() on an int raises TypeError, which is not the fail-closed error
    every other malformed-policy path produces."""
    from xero_ai_review_gateway.gateway import _load_policy

    source = json.loads((PKG / "policy" / "demo-policy-v1.json").read_text(encoding="utf-8"))
    source["model_projection"] = 9
    broken = tmp_path / "broken-policy.json"
    broken.write_text(json.dumps(source), encoding="utf-8")

    with pytest.raises(GatewayError, match="schema or model projection is unsupported"):
        _load_policy(broken)


def test_the_run_id_moves_when_only_the_source_manifest_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The manifests carry entity_ref, include_drafts and tracking_filters, all
    of which change what the figures mean. Sealing only the CSVs let two runs
    over different entities share one run_id, so a receipt did not identify its
    own inputs and the reproducibility claim did not hold."""
    monkeypatch.chdir(tmp_path)
    model, _evidence, receipt = _evaluate()
    baseline_run_id = model["run_id"]

    assert set(receipt["source_digests"]) == {
        "current", "prior", "current_manifest", "prior_manifest"}

    # Change one manifest field. Both CSVs stay byte-identical, so nothing the
    # old seed covered moves at all.
    manifests = [
        PKG / "samples" / "manifests" / "sample-tb-2026-06-30.manifest.json",
        PKG / "samples" / "manifests" / "sample-tb-2026-05-31.manifest.json",
    ]
    # bytes, so restoring cannot rewrite the shipped files' line endings
    originals = {path: path.read_bytes() for path in manifests}
    try:
        # Both of them: the gateway refuses a pair whose entity_ref disagrees,
        # so changing one only proves that check works.
        for path in manifests:
            payload = json.loads(originals[path].decode("utf-8"))
            payload["entity_ref"] = "sample-entity-002"
            path.write_bytes((json.dumps(payload, indent=2) + "\n").encode("utf-8"))
        moved, _e, _r = _evaluate()
    finally:
        for path in manifests:
            path.write_bytes(originals[path])

    assert moved["run_id"] != baseline_run_id
