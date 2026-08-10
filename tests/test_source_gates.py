"""Negative tests for the source-integrity and context gates.

Every gate the README lists as "checked before review" is exercised here with
an artefact that must be refused. Each test is written so that disabling the
single line it names lets the artefact through and the test fails.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from xero_ai_review_gateway import gateway
from xero_ai_review_gateway.errors import GatewayError
from xero_ai_review_gateway.gateway import _load_context, _load_manifest, _load_policy, _load_request, _load_tb
from xero_ai_review_gateway.util import sha256_file

PKG = Path(__file__).resolve().parents[1] / "xero_ai_review_gateway"
CURRENT_MANIFEST = "sample-tb-2026-06-30.manifest.json"
PRIOR_MANIFEST = "sample-tb-2026-05-31.manifest.json"
CONTEXT = "sample-monthly-variance.context.json"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A writable copy of the bundled policy/ and samples/ data, used as the package root."""
    root = tmp_path / "pkg"
    shutil.copytree(PKG / "samples", root / "samples")
    shutil.copytree(PKG / "policy", root / "policy")
    monkeypatch.setattr(gateway, "package_root", lambda: root)
    return root


def _manifest_path(root: Path, name: str) -> Path:
    return root / "samples" / "manifests" / name


def _reseal(root: Path, name: str) -> None:
    """Restate a manifest's declared digest after its CSV was edited."""
    path = _manifest_path(root, name)
    manifest = _read(path)
    manifest["export"]["sha256"] = sha256_file(root / manifest["export"]["csv"])
    _write(path, manifest)


def _redate(root: Path, manifest_name: str, new_date: str) -> None:
    """Move a source's report date, in both the CSV rows and its manifest."""
    path = _manifest_path(root, manifest_name)
    manifest = _read(path)
    csv_path = root / manifest["export"]["csv"]
    text = csv_path.read_text(encoding="utf-8")
    old_date = text.splitlines()[1].split(",")[0]
    csv_path.write_text(text.replace(old_date, new_date), encoding="utf-8")
    manifest["report"]["as_at"] = new_date
    manifest["export"]["sha256"] = sha256_file(csv_path)
    _write(path, manifest)


def _tb(tmp_path: Path, *replacements: tuple[str, str]) -> Path:
    """The bundled current CSV with literal substitutions applied."""
    text = (PKG / "samples" / "inputs" / "sample-tb-2026-06-30.csv").read_text(encoding="utf-8")
    for old, new in replacements:
        assert old in text, old
        text = text.replace(old, new)
    path = tmp_path / "edited.csv"
    path.write_text(text, encoding="utf-8")
    return path


# --- CSV contract -----------------------------------------------------------


def test_reordered_header_is_refused_even_though_the_names_all_match(tmp_path: Path) -> None:
    # The column names are the canonical ten; only the order differs, so a
    # sorted or set comparison would accept this file and read every Debit
    # value as a Credit.
    path = _tb(tmp_path, ("AccountCode,Debit,Credit,", "AccountCode,Credit,Debit,"))

    with pytest.raises(GatewayError, match="canonical ten-column header"):
        _load_tb(path)


def test_duplicate_account_id_is_refused(tmp_path: Path) -> None:
    # Renaming acct-200 onto acct-100 leaves every column total unchanged, so
    # only the duplicate check can catch it.
    path = _tb(tmp_path, ("acct-200", "acct-100"))

    with pytest.raises(GatewayError, match="duplicate AccountID"):
        _load_tb(path)


def test_unbalanced_ytd_columns_fail_closed_even_when_the_movement_balances(tmp_path: Path) -> None:
    path = _tb(tmp_path, ("10000.00,0.00,50000.00", "10000.00,0.00,50001.00"))

    with pytest.raises(GatewayError, match="YTD debit and credit totals"):
        _load_tb(path)


def test_a_second_tenant_in_one_csv_is_refused(tmp_path: Path) -> None:
    path = _tb(tmp_path, ("Demo Entity Pty Ltd,Equity", "Other Entity Pty Ltd,Equity"))

    with pytest.raises(GatewayError, match="exactly one tenant and report date"):
        _load_tb(path)


def test_a_second_report_date_in_one_csv_is_refused(tmp_path: Path) -> None:
    path = _tb(tmp_path, ("2026-06-30,Demo Entity Pty Ltd,Equity", "2026-06-29,Demo Entity Pty Ltd,Equity"))

    with pytest.raises(GatewayError, match="exactly one tenant and report date"):
        _load_tb(path)


# --- manifest gates ---------------------------------------------------------


def test_csv_that_does_not_match_the_declared_digest_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _sandbox(tmp_path, monkeypatch)
    csv_path = root / "samples" / "inputs" / "sample-tb-2026-06-30.csv"
    # A rename that leaves a perfectly valid, balanced trial balance: only the
    # digest comparison stands between it and the review.
    csv_path.write_text(csv_path.read_text(encoding="utf-8").replace("Demo Bank", "Demo Bank Account"), encoding="utf-8")

    with pytest.raises(GatewayError, match="SHA-256 does not match"):
        _load_manifest(_manifest_path(root, CURRENT_MANIFEST))


def test_manifest_as_at_that_disagrees_with_the_csv_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _sandbox(tmp_path, monkeypatch)
    path = _manifest_path(root, CURRENT_MANIFEST)
    manifest = _read(path)
    manifest["report"]["as_at"] = "2026-06-29"
    _write(path, manifest)

    with pytest.raises(GatewayError, match="as_at and CSV ReportDate do not match"):
        _load_manifest(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [("mode", "live"), ("schema_version", "xero-source-manifest.v2")],
)
def test_only_synthetic_v1_manifests_are_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: str) -> None:
    root = _sandbox(tmp_path, monkeypatch)
    path = _manifest_path(root, CURRENT_MANIFEST)
    manifest = _read(path)
    manifest[field] = value
    _write(path, manifest)

    with pytest.raises(GatewayError, match="synthetic mode is supported"):
        _load_manifest(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [("name", "Profit and Loss"), ("basis", "cash"), ("currency", "NZD")],
)
def test_only_accrual_aud_trial_balances_are_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: str) -> None:
    root = _sandbox(tmp_path, monkeypatch)
    path = _manifest_path(root, CURRENT_MANIFEST)
    manifest = _read(path)
    manifest["report"][field] = value
    _write(path, manifest)

    with pytest.raises(GatewayError, match="accrual AUD Trial Balance"):
        _load_manifest(path)


# --- context gates ----------------------------------------------------------


def test_current_and_prior_from_different_entities_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _sandbox(tmp_path, monkeypatch)
    path = _manifest_path(root, PRIOR_MANIFEST)
    manifest = _read(path)
    manifest["entity_ref"] = "sample-entity-002"
    _write(path, manifest)

    with pytest.raises(GatewayError, match="different entity_ref"):
        _load_context(root / "samples" / "contexts" / CONTEXT)


@pytest.mark.parametrize(("field", "value"), [("tracking_filters", ["Sydney"]), ("include_drafts", True)])
def test_current_and_prior_report_settings_must_agree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object) -> None:
    root = _sandbox(tmp_path, monkeypatch)
    path = _manifest_path(root, PRIOR_MANIFEST)
    manifest = _read(path)
    manifest["report"][field] = value
    _write(path, manifest)

    with pytest.raises(GatewayError, match=f"mismatch for {field}"):
        _load_context(root / "samples" / "contexts" / CONTEXT)


@pytest.mark.parametrize("prior_manifest", [CURRENT_MANIFEST, PRIOR_MANIFEST])
def test_prior_report_must_predate_the_current_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prior_manifest: str) -> None:
    # Reversed (prior after current) and identical (prior equal to current)
    # must both fail; the second pins the boundary.
    root = _sandbox(tmp_path, monkeypatch)
    path = root / "samples" / "contexts" / CONTEXT
    context = _read(path)
    context["current_manifest"] = f"samples/manifests/{PRIOR_MANIFEST}"
    context["prior_manifest"] = f"samples/manifests/{prior_manifest}"
    _write(path, context)

    with pytest.raises(GatewayError, match="must be earlier than the current report date"):
        _load_context(path)


def test_a_comparison_across_the_1_july_ytd_reset_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 31 Jul 2026 is FY2027; 31 May 2026 is FY2026. The YTD columns reset in
    # between, so the delta would be the whole prior-year balance.
    root = _sandbox(tmp_path, monkeypatch)
    _redate(root, CURRENT_MANIFEST, "2026-07-31")

    with pytest.raises(GatewayError, match="different financial years"):
        _load_context(root / "samples" / "contexts" / CONTEXT)


def test_a_year_on_year_comparison_of_the_same_date_is_still_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 31 May 2027 against 31 May 2026 is two different financial years, but
    # both are the same point in their own year, so the YTD figures compare.
    root = _sandbox(tmp_path, monkeypatch)
    _redate(root, CURRENT_MANIFEST, "2027-05-31")

    current, prior = _load_context(root / "samples" / "contexts" / CONTEXT)

    assert str(current.rows[0].report_date) == "2027-05-31"
    assert str(prior.rows[0].report_date) == "2026-05-31"


# --- policy and request gates ----------------------------------------------


def test_request_bound_to_another_policy_is_refused(tmp_path: Path) -> None:
    policy = _load_policy(PKG / "policy" / "demo-policy-v1.json")
    request = _read(PKG / "samples" / "requests" / "sample-revenue-variance.request.json")
    request["policy_id"] = "some-other-policy"
    path = tmp_path / "request.json"
    _write(path, request)

    with pytest.raises(GatewayError, match="policy_id does not match"):
        _load_request(path, policy)


@pytest.mark.parametrize("mutate", ["reorder", "drop"])
def test_policy_model_projection_must_match_the_code_contract(tmp_path: Path, mutate: str) -> None:
    policy = _read(PKG / "policy" / "demo-policy-v1.json")
    projection = list(policy["model_projection"])
    policy["model_projection"] = projection[::-1] if mutate == "reorder" else projection[:-1]
    path = tmp_path / "policy.json"
    _write(path, policy)

    with pytest.raises(GatewayError, match="model projection is unsupported"):
        _load_policy(path)


@pytest.mark.parametrize("value", [0, 101, -1, "25", 2.5, True, False])
def test_policy_max_results_outside_one_to_one_hundred_is_refused(tmp_path: Path, value: object) -> None:
    # JSON true is the case an isinstance(..., int) bound lets through: bool is
    # a subclass of int and 1 <= True <= 100 holds, so the policy would load
    # and silently cap every run at one finding.
    policy = _read(PKG / "policy" / "demo-policy-v1.json")
    policy["operations"]["trial_balance_variance"]["max_results"] = value
    path = tmp_path / "policy.json"
    _write(path, policy)

    with pytest.raises(GatewayError, match="max_results must be an integer from 1 to 100"):
        _load_policy(path)


@pytest.mark.parametrize("value", [1, 100])
def test_policy_max_results_at_either_bound_is_accepted(tmp_path: Path, value: int) -> None:
    policy = _read(PKG / "policy" / "demo-policy-v1.json")
    policy["operations"]["trial_balance_variance"]["max_results"] = value
    path = tmp_path / "policy.json"
    _write(path, policy)

    assert _load_policy(path)["operations"]["trial_balance_variance"]["max_results"] == value
