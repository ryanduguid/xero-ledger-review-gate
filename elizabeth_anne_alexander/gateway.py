from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .errors import GatewayError
from .util import (
    FileSnapshot,
    build_root,
    canonical_json,
    load_json_exact,
    load_json_exact_snapshot,
    load_json_object,
    package_root,
    path_within,
    sha256_bytes,
    snapshot_file,
)
from .version import __version__


CANONICAL_COLUMNS = (
    "ReportDate", "Tenant", "Section", "AccountID", "AccountName", "AccountCode", "Debit", "Credit", "YTDDebit", "YTDCredit"
)
# The model-facing keys each finding carries; emitted findings are built by
# projecting through this tuple, so the declared contract cannot drift from
# the emitted keys. percent_change is expressed in percent, quantized to four
# decimal places ("18.3333" means 18.3333%), and is null when there is no
# prior balance to compare against.
MODEL_PROJECTION = (
    "finding_id", "evidence_ref", "account_ref", "section", "current_ytd_net", "prior_ytd_net", "delta", "percent_change", "review_reason"
)
ALLOWED_DECISIONS = {"ACKNOWLEDGED", "NEEDS_EVIDENCE", "ESCALATED"}
# The model result carries a debit-positive net, so a revenue or liability
# balance is negative. Stating that on the artefact keeps a reader of the
# model result alone from reading a revenue increase as a fall.
SIGN_CONVENTION = "debit_positive: ytd_net = YTDDebit - YTDCredit, so revenue, liability, and equity balances are negative"
# datetime.fromisoformat accepts a wider grammar on 3.11+ than on 3.10, so the
# set of artefacts the gateway accepts would otherwise depend on the
# interpreter. Define the accepted grammar here instead, wide enough to cover
# every form all supported interpreters already accepted: a full date, T/t or
# a space, a time with optional seconds and optional fractional seconds, then
# Z/z or an explicit +/-HH:MM offset that may carry seconds. README "Control
# boundary" states the same grammar for artefact authors.
_TIMESTAMP = re.compile(r"(\d{4}-\d{2}-\d{2})[Tt ](\d{2}:\d{2}(?::\d{2})?)(?:\.(\d{1,6}))?(Z|z|[+-]\d{2}:\d{2}(?::\d{2})?)")
_TIMESTAMP_WITHOUT_OFFSET = re.compile(r"\d{4}-\d{2}-\d{2}(?:[Tt ]\d{2}:\d{2}(?::\d{2})?(?:\.\d{1,6})?)?")


@dataclass(frozen=True)
class BalanceRow:
    report_date: date
    tenant: str
    section: str
    account_id: str
    account_name: str
    account_code: str
    debit: Decimal
    credit: Decimal
    ytd_debit: Decimal
    ytd_credit: Decimal

    @property
    def ytd_net(self) -> Decimal:
        return self.ytd_debit - self.ytd_credit


@dataclass(frozen=True)
class Source:
    manifest_path: Path
    manifest: dict[str, Any]
    csv_path: Path
    rows: tuple[BalanceRow, ...]
    manifest_snapshot: FileSnapshot
    csv_snapshot: FileSnapshot


def _non_empty(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GatewayError(f"{field} must be a non-empty string.")
    text = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise GatewayError(f"{field} must not contain control characters.")
    return text


def _optional_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise GatewayError(f"{field} must be a string.")
    text = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise GatewayError(f"{field} must not contain control characters.")
    return text


def _iso_timestamp(value: Any, *, field: str) -> str:
    text = _non_empty(value, field=field)
    match = _TIMESTAMP.fullmatch(text)
    if match is None:
        if _TIMESTAMP_WITHOUT_OFFSET.fullmatch(text):
            raise GatewayError(f"{field} must include an explicit UTC offset or Z.")
        raise GatewayError(f"{field} must be an ISO 8601 timestamp.")
    day, clock, fraction, offset = match.groups()
    # Restate the accepted form in the one shape that parses identically on
    # every supported interpreter: seconds present, fraction padded to
    # microseconds, Z/z expanded. fromisoformat still rejects an impossible
    # date, time, or offset. The offset group above is not optional, so the
    # parsed value is always timezone-aware; the offset-less inputs are
    # refused by the _TIMESTAMP_WITHOUT_OFFSET branch instead.
    clock = clock if len(clock) == 8 else f"{clock}:00"
    normalized = f"{day}T{clock}.{(fraction or '0').ljust(6, '0')}" + ("+00:00" if offset in {"Z", "z"} else offset)
    try:
        datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise GatewayError(f"{field} must be an ISO 8601 timestamp.") from exc
    return text


def _resolve_bundled(path: Path, subdir: str, *, label: str) -> Path:
    """Resolve a bundled data path; relative paths are anchored at the package, not the CWD."""
    root = package_root()
    candidate = path if path.is_absolute() else root / path
    return path_within(candidate, root / subdir, label=label)


def _resolve_decision(path: Path) -> Path:
    """Human decisions may sit with the bundled samples or beside run outputs under CWD build/."""
    bundled_candidate = path if path.is_absolute() else package_root() / path
    attempts = (
        (bundled_candidate, package_root() / "samples"),
        (path, build_root()),
    )
    for candidate, parent in attempts:
        try:
            return path_within(candidate, parent, label="human decision")
        except GatewayError:
            continue
    raise GatewayError(f"human decision must exist under the bundled samples/ data or the working directory's build/: {path}.")


def _decimal(value: Any, *, field: str) -> Decimal:
    if not isinstance(value, str):
        raise GatewayError(f"{field} must be a decimal string.")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise GatewayError(f"{field} is not a valid decimal.") from exc
    if not result.is_finite():
        raise GatewayError(f"{field} must be finite.")
    # Finite is not enough. `1E+1000000` parses, is finite, and balances against
    # itself, but the first sum overflows the default context and decimal
    # raises out of the balance gate instead of the gate refusing the file.
    # This is the one place any amount or threshold enters, so bounding it here
    # keeps every later sum, difference and quotient inside the context.
    if result != 0 and not -18 <= result.adjusted() <= 18:
        raise GatewayError(f"{field} is outside the supported magnitude range.")
    return result


def _load_tb_snapshot(snapshot: FileSnapshot) -> tuple[BalanceRow, ...]:
    rows: list[BalanceRow] = []
    seen: set[str] = set()
    try:
        with io.StringIO(snapshot.content.decode("utf-8-sig"), newline="") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames is None or tuple(reader.fieldnames) != CANONICAL_COLUMNS:
                raise GatewayError("CSV must have exactly the canonical ten-column header in its declared order.")
            for line, raw in enumerate(reader, start=2):
                if None in raw:
                    raise GatewayError(f"CSV row {line} has more fields than its header.")
                report_date_text = _non_empty(raw["ReportDate"], field=f"CSV row {line} ReportDate")
                try:
                    report_date = date.fromisoformat(report_date_text)
                except ValueError as exc:
                    raise GatewayError(f"CSV row {line} ReportDate must be ISO YYYY-MM-DD.") from exc
                account_id = _non_empty(raw["AccountID"], field=f"CSV row {line} AccountID")
                if account_id in seen:
                    raise GatewayError(f"CSV has duplicate AccountID {account_id!r}.")
                seen.add(account_id)
                row = BalanceRow(
                    report_date=report_date,
                    tenant=_non_empty(raw["Tenant"], field=f"CSV row {line} Tenant"),
                    section=_non_empty(raw["Section"], field=f"CSV row {line} Section"),
                    account_id=account_id,
                    account_name=_non_empty(raw["AccountName"], field=f"CSV row {line} AccountName"),
                    account_code=_optional_text(raw["AccountCode"], field=f"CSV row {line} AccountCode"),
                    debit=_decimal(raw["Debit"], field=f"CSV row {line} Debit"),
                    credit=_decimal(raw["Credit"], field=f"CSV row {line} Credit"),
                    ytd_debit=_decimal(raw["YTDDebit"], field=f"CSV row {line} YTDDebit"),
                    ytd_credit=_decimal(raw["YTDCredit"], field=f"CSV row {line} YTDCredit"),
                )
                rows.append(row)
    except UnicodeDecodeError as exc:
        raise GatewayError(f"source CSV is not valid UTF-8 text: {snapshot.path}.") from exc
    except csv.Error as exc:
        raise GatewayError(f"source CSV cannot be parsed as CSV: {snapshot.path}.") from exc
    if not rows:
        raise GatewayError("CSV must contain at least one account row.")
    if len({row.tenant for row in rows}) != 1 or len({row.report_date for row in rows}) != 1:
        raise GatewayError("CSV must contain exactly one tenant and report date.")
    zero = Decimal("0")
    if sum((row.debit for row in rows), zero) != sum((row.credit for row in rows), zero):
        raise GatewayError("CSV movement debit and credit totals are not exactly balanced.")
    if sum((row.ytd_debit for row in rows), zero) != sum((row.ytd_credit for row in rows), zero):
        raise GatewayError("CSV YTD debit and credit totals are not exactly balanced.")
    return tuple(rows)


def _load_tb(path: Path) -> tuple[BalanceRow, ...]:
    # The snapshot catches filesystem failures before parsing and ensures the
    # rows and digest can only describe the same immutable byte sequence.
    return _load_tb_snapshot(snapshot_file(path, label="source CSV"))


def _load_manifest(path: Path) -> Source:
    manifest, manifest_snapshot = load_json_exact_snapshot(
        path,
        {"schema_version", "mode", "source_system", "entity_ref", "report", "export"},
        label="source manifest",
    )
    if manifest["schema_version"] != "xero-source-manifest.v1" or manifest["mode"] != "synthetic":
        raise GatewayError("Only xero-source-manifest.v1 in synthetic mode is supported.")
    if manifest["source_system"] != "xero-trial-balance-export":
        raise GatewayError("Source manifest must declare xero-trial-balance-export.")
    _non_empty(manifest["entity_ref"], field="entity_ref")
    report = manifest["report"]
    export = manifest["export"]
    if not isinstance(report, dict) or set(report) != {"name", "as_at", "basis", "currency", "tracking_filters", "include_drafts"}:
        raise GatewayError("source manifest report has an invalid shape.")
    if not isinstance(export, dict) or set(export) != {"schema", "csv", "sha256", "generated_at"}:
        raise GatewayError("source manifest export has an invalid shape.")
    if report["name"] != "Trial Balance" or report["basis"] != "accrual" or report["currency"] != "AUD":
        raise GatewayError("Only accrual AUD Trial Balance reports are supported in v0.1.")
    if not isinstance(report["tracking_filters"], list) or not isinstance(report["include_drafts"], bool):
        raise GatewayError("source manifest report tracking/draft fields are invalid.")
    as_at_text = _non_empty(report["as_at"], field="report.as_at")
    try:
        as_at = date.fromisoformat(as_at_text)
    except ValueError as exc:
        raise GatewayError("report.as_at must be an ISO date.") from exc
    if export["schema"] != "xero-tb-csv.v1" or not isinstance(export["csv"], str) or not isinstance(export["sha256"], str):
        raise GatewayError("source manifest export schema, csv, or sha256 is invalid.")
    _iso_timestamp(export["generated_at"], field="export.generated_at")
    root = package_root()
    csv_path = path_within(root / export["csv"], root / "samples", label="manifest CSV")
    csv_snapshot = snapshot_file(csv_path, label="manifest CSV")
    if csv_snapshot.sha256 != export["sha256"].lower():
        raise GatewayError("manifest CSV SHA-256 does not match the supplied source file.")
    rows = _load_tb_snapshot(csv_snapshot)
    if rows[0].report_date != as_at:
        raise GatewayError("manifest report.as_at and CSV ReportDate do not match.")
    return Source(
        manifest_path=path,
        manifest=manifest,
        csv_path=csv_path,
        rows=rows,
        manifest_snapshot=manifest_snapshot,
        csv_snapshot=csv_snapshot,
    )


def _au_financial_year(value: date) -> int:
    """The Australian financial year ending 30 June that contains value (1 Jul 2026 falls in FY2027)."""
    return value.year + 1 if value.month >= 7 else value.year


def _load_context_snapshot(path: Path) -> tuple[Source, Source, FileSnapshot]:
    context, context_snapshot = load_json_exact_snapshot(
        path,
        {"schema_version", "mode", "current_manifest", "prior_manifest"},
        label="review context",
    )
    if context["schema_version"] != "xero-review-context.v1" or context["mode"] != "synthetic":
        raise GatewayError("Only xero-review-context.v1 in synthetic mode is supported.")
    root = package_root()
    current_path = path_within(root / _non_empty(context["current_manifest"], field="current_manifest"), root / "samples", label="current manifest")
    prior_path = path_within(root / _non_empty(context["prior_manifest"], field="prior_manifest"), root / "samples", label="prior manifest")
    current, prior = _load_manifest(current_path), _load_manifest(prior_path)
    current_report, prior_report = current.manifest["report"], prior.manifest["report"]
    for field in ("basis", "currency", "tracking_filters", "include_drafts"):
        if current_report[field] != prior_report[field]:
            raise GatewayError(f"Current/prior source context mismatch for {field}.")
    if current.manifest["entity_ref"] != prior.manifest["entity_ref"]:
        raise GatewayError("Current/prior source context has different entity_ref values.")
    current_date, prior_date = current.rows[0].report_date, prior.rows[0].report_date
    if prior_date >= current_date:
        raise GatewayError("Prior report date must be earlier than the current report date.")
    # YTD columns reset on 1 July, so a comparison that straddles the reset
    # reports the whole prior-year balance as a movement. Two dates are
    # comparable within one financial year, or as the same day and month one
    # or more years apart, which is the year-on-year comparison.
    same_day_and_month = (current_date.month, current_date.day) == (prior_date.month, prior_date.day)
    if _au_financial_year(current_date) != _au_financial_year(prior_date) and not same_day_and_month:
        raise GatewayError(
            "Current and prior reports fall in different financial years; YTD balances reset on 1 July and are not comparable."
        )
    return current, prior, context_snapshot


def _load_context(path: Path) -> tuple[Source, Source]:
    current, prior, _snapshot = _load_context_snapshot(path)
    return current, prior


def _load_policy_snapshot(path: Path) -> tuple[dict[str, Any], FileSnapshot]:
    policy, policy_snapshot = load_json_exact_snapshot(
        path,
        {"schema_version", "policy_id", "operations", "model_projection"},
        label="policy",
    )
    # tuple() on a non-iterable raises TypeError, which is not the fail-closed
    # GatewayError every other malformed-policy path produces. Type-check first,
    # in the same style as allowed_sections below.
    projection = policy["model_projection"]
    if (
        policy["schema_version"] != "xero-review-policy.v1"
        or not isinstance(projection, list)
        or not all(isinstance(value, str) for value in projection)
        or tuple(projection) != MODEL_PROJECTION
    ):
        raise GatewayError("Policy schema or model projection is unsupported.")
    operations = policy["operations"]
    if not isinstance(operations, dict) or set(operations) != {"trial_balance_variance"}:
        raise GatewayError("Policy must contain only the trial_balance_variance operation.")
    operation = operations["trial_balance_variance"]
    if not isinstance(operation, dict) or set(operation) != {"allowed_sections", "minimum_absolute_delta", "minimum_percent_delta", "max_results"}:
        raise GatewayError("Policy operation has an invalid shape.")
    if not isinstance(operation["allowed_sections"], list) or not all(isinstance(value, str) for value in operation["allowed_sections"]):
        raise GatewayError("Policy allowed_sections must be a list of strings.")
    max_results = operation["max_results"]
    # bool is a subclass of int and JSON true would otherwise pass the bound
    # and cap every run at one finding, so refuse it the way validate_review
    # refuses a boolean total_findings.
    if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= 100:
        raise GatewayError("Policy max_results must be an integer from 1 to 100.")
    for field in ("minimum_absolute_delta", "minimum_percent_delta"):
        if _decimal(operation[field], field=field) < 0:
            raise GatewayError(f"Policy {field} cannot be negative.")
    return policy, policy_snapshot


def _load_policy(path: Path) -> dict[str, Any]:
    policy, _snapshot = _load_policy_snapshot(path)
    return policy


def _load_request_snapshot(path: Path, policy: dict[str, Any]) -> tuple[dict[str, Any], FileSnapshot]:
    request, request_snapshot = load_json_exact_snapshot(
        path,
        {"schema_version", "request_id", "operation", "section", "policy_id"},
        label="review request",
    )
    if request["schema_version"] != "xero-review-request.v1" or request["operation"] != "trial_balance_variance":
        raise GatewayError("Only the trial_balance_variance request is supported.")
    if request["policy_id"] != policy["policy_id"]:
        raise GatewayError("Request policy_id does not match the selected policy.")
    allowed_sections = policy["operations"]["trial_balance_variance"]["allowed_sections"]
    if request["section"] not in allowed_sections:
        raise GatewayError("Request section is not allowlisted by policy.")
    _non_empty(request["request_id"], field="request_id")
    return request, request_snapshot


def _load_request(path: Path, policy: dict[str, Any]) -> dict[str, Any]:
    request, _snapshot = _load_request_snapshot(path, policy)
    return request


def _decimal_string(value: Decimal) -> str:
    return format(value, "f")


def _percent_string(ratio: Decimal) -> str:
    """Express a change ratio as a percentage quantized to four decimal places.

    quantize raises InvalidOperation once the result needs more digits than the
    context allows, which a large enough ratio reaches. Everything else in this
    module refuses rather than raises, so this converts too.
    """
    try:
        return _decimal_string((ratio * Decimal("100")).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
    except InvalidOperation as exc:
        raise GatewayError("Percentage change is outside the supported magnitude range.") from exc


def _row_values(row: BalanceRow | None) -> dict[str, str] | None:
    if row is None:
        return None
    return {
        "debit": _decimal_string(row.debit),
        "credit": _decimal_string(row.credit),
        "ytd_debit": _decimal_string(row.ytd_debit),
        "ytd_credit": _decimal_string(row.ytd_credit),
    }


def _variance_findings(
    current_rows: tuple[BalanceRow, ...],
    prior_rows: tuple[BalanceRow, ...],
    *,
    entity_ref: str,
    section: str,
    operation: dict[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Compare every account seen in either period, so one-sided accounts cannot hide."""
    absolute = _decimal(operation["minimum_absolute_delta"], field="minimum_absolute_delta")
    minimum_percent = _decimal(operation["minimum_percent_delta"], field="minimum_percent_delta") / Decimal("100")
    current_by_id = {row.account_id: row for row in current_rows}
    prior_by_id = {row.account_id: row for row in prior_rows}
    # Sorted and reported in full: iterating the unordered set intersection
    # named an arbitrary one of several drifting accounts, so the same inputs
    # produced a different message run to run.
    drifted = sorted(
        account_id
        for account_id in set(current_by_id) & set(prior_by_id)
        if current_by_id[account_id].section != prior_by_id[account_id].section
        and section in {current_by_id[account_id].section, prior_by_id[account_id].section}
    )
    if drifted:
        listed = ", ".join(repr(account_id) for account_id in drifted)
        raise GatewayError(
            f"Accounts changed section between periods ({listed}); review the source mapping before comparison."
        )
    report_date = current_rows[0].report_date
    findings: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for account_id in sorted(set(current_by_id) | set(prior_by_id)):
        current_row = current_by_id.get(account_id)
        prior_row = prior_by_id.get(account_id)
        # account_id came from one of the two maps, so at least one row exists.
        row = current_row if current_row is not None else prior_row
        if row is None or row.section != section:
            continue
        current_net = current_row.ytd_net if current_row is not None else Decimal("0")
        prior_net = prior_row.ytd_net if prior_row is not None else Decimal("0")
        delta = current_net - prior_net
        if prior_row is None or prior_net == 0:
            percent = None
        else:
            percent = abs(delta / prior_net)
        if delta == 0 or abs(delta) < absolute or (percent is not None and percent < minimum_percent):
            continue
        if current_row is None:
            reason = "Account exists only in the prior period; its balance movement exceeds the approved variance thresholds."
        elif prior_row is None:
            reason = "Account is new in the current period; its balance movement exceeds the approved variance thresholds."
        else:
            reason = "Movement exceeds the approved variance thresholds."
        account_ref = "acct:" + sha256_bytes(f"{entity_ref}:{account_id}".encode("utf-8"))[:24]
        finding_id = "finding:" + sha256_bytes(f"{account_ref}:{delta}:{report_date}".encode("utf-8"))[:24]
        evidence_ref = "evidence:" + sha256_bytes(f"{finding_id}:reviewer-evidence".encode("utf-8"))[:24]
        projected_values = {
            "finding_id": finding_id,
            "evidence_ref": evidence_ref,
            "account_ref": account_ref,
            "section": row.section,
            "current_ytd_net": _decimal_string(current_net),
            "prior_ytd_net": _decimal_string(prior_net),
            "delta": _decimal_string(delta),
            "percent_change": None if percent is None else _percent_string(percent),
            "review_reason": reason,
        }
        model_item = {key: projected_values[key] for key in MODEL_PROJECTION}
        evidence_item = {
            "finding_id": finding_id,
            "account_id": account_id,
            "account_code": row.account_code,
            "account_name": row.account_name,
            "current_values": _row_values(current_row),
            "prior_values": _row_values(prior_row),
            "source_refs": [ref for ref, present in (("source:current", current_row), ("source:prior", prior_row)) if present is not None],
        }
        findings.append((model_item, evidence_item))
    return findings


def evaluate(*, context_path: Path, request_path: Path, policy_path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Evaluate the one fixed, read-only synthetic operation without network or ledger access."""
    context_path = _resolve_bundled(context_path, "samples", label="context")
    request_path = _resolve_bundled(request_path, "samples", label="request")
    policy_path = _resolve_bundled(policy_path, "policy", label="policy")
    policy, policy_snapshot = _load_policy_snapshot(policy_path)
    request, request_snapshot = _load_request_snapshot(request_path, policy)
    current, prior, context_snapshot = _load_context_snapshot(context_path)
    operation = policy["operations"]["trial_balance_variance"]
    all_findings = _variance_findings(
        current.rows,
        prior.rows,
        entity_ref=current.manifest["entity_ref"],
        section=request["section"],
        operation=operation,
    )
    total_findings = len(all_findings)
    findings = all_findings[: operation["max_results"]]
    truncated = total_findings > operation["max_results"]
    run_seed = {
        "context_sha256": context_snapshot.sha256,
        "request_sha256": request_snapshot.sha256,
        "policy_sha256": policy_snapshot.sha256,
        "current_csv_sha256": current.csv_snapshot.sha256,
        "prior_csv_sha256": prior.csv_snapshot.sha256,
        # The manifests carry entity_ref, include_drafts and tracking_filters,
        # which change what the figures mean. Sealing only the CSVs let two
        # runs over different entities or filters share one run_id, so the
        # receipt did not identify its own inputs and the reproducibility claim
        # in the README was not true.
        "current_manifest_sha256": current.manifest_snapshot.sha256,
        "prior_manifest_sha256": prior.manifest_snapshot.sha256,
    }
    run_id = "sha256:" + sha256_bytes(canonical_json(run_seed))
    model = {
        "schema_version": "xero-model-review-result.v1",
        "run_id": run_id,
        "mode": "synthetic",
        "status": "REVIEW_READY",
        "operation": request["operation"],
        "currency": current.manifest["report"]["currency"],
        "sign_convention": SIGN_CONVENTION,
        "findings": [item[0] for item in findings],
        "total_findings": total_findings,
        "truncated": truncated,
        "limitations": [
            "This is a synthetic-data review result.",
            "No journal, payment, filing, or period-locking action is available.",
            "A human reviewer must assess each finding against source evidence.",
        ],
    }
    evidence = {
        "schema_version": "xero-reviewer-evidence.v1",
        "run_id": run_id,
        "mode": "synthetic",
        "items": [item[1] for item in findings],
        "total_findings": total_findings,
        "truncated": truncated,
    }
    receipt = {
        "schema_version": "xero-review-receipt.v1",
        "run_id": run_id,
        "mode": "synthetic",
        "policy_sha256": "sha256:" + run_seed["policy_sha256"],
        "request_sha256": "sha256:" + run_seed["request_sha256"],
        # Manifests as well as CSVs: the receipt is the record of what was
        # reviewed, and the manifest is where entity_ref and the filters live.
        "source_digests": {
            "current": "sha256:" + run_seed["current_csv_sha256"],
            "prior": "sha256:" + run_seed["prior_csv_sha256"],
            "current_manifest": "sha256:" + run_seed["current_manifest_sha256"],
            "prior_manifest": "sha256:" + run_seed["prior_manifest_sha256"],
        },
        "result_sha256": "sha256:" + sha256_bytes(canonical_json(model)),
        "evidence_sha256": "sha256:" + sha256_bytes(canonical_json(evidence)),
        "code_version": __version__,
    }
    _assert_model_is_redacted(model, current.rows + prior.rows)
    return model, evidence, receipt


def _leaf_strings(value: Any) -> Any:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _leaf_strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _leaf_strings(child)


def _assert_model_is_redacted(model: dict[str, Any], rows: tuple[BalanceRow, ...]) -> None:
    # Compare each emitted leaf string for exact equality with a forbidden value;
    # substring matching over the serialised model false-positives when an
    # ordinary numeric AccountID happens to occur inside an amount or digest.
    # Tenant, account name and account code are the three source display values
    # the README promises the model result never carries, so all three are
    # forbidden as leaves, not only as key names. AccountID joins them because
    # it is the join key the evidence file is indexed by.
    forbidden = (
        {row.tenant for row in rows}
        | {row.account_name for row in rows}
        | {row.account_code for row in rows if row.account_code}
        | {row.account_id for row in rows}
    )
    if any(leaf in forbidden for leaf in _leaf_strings(model)):
        raise GatewayError("Internal disclosure assertion failed: model result contains raw source display data.")
    serialised = json.dumps(model, sort_keys=True)
    if '"account_code"' in serialised or '"account_name"' in serialised or '"tenant"' in serialised:
        raise GatewayError("Internal disclosure assertion failed: model result contains a prohibited source field.")


def validate_review(*, evidence_path: Path, receipt_path: Path, decision_path: Path) -> dict[str, Any]:
    evidence_path = path_within(evidence_path, build_root(), label="reviewer evidence")
    receipt_path = path_within(receipt_path, build_root(), label="receipt")
    decision_path = _resolve_decision(decision_path)
    evidence = load_json_exact(evidence_path, {"schema_version", "run_id", "mode", "items", "total_findings", "truncated"}, label="reviewer evidence")
    receipt = load_json_exact(receipt_path, {"schema_version", "run_id", "mode", "policy_sha256", "request_sha256", "source_digests", "result_sha256", "evidence_sha256", "code_version"}, label="receipt")
    decision = load_json_exact(decision_path, {"schema_version", "run_id", "reviewer_ref", "reviewed_at", "decisions"}, label="human decision")
    if evidence["schema_version"] != "xero-reviewer-evidence.v1" or receipt["schema_version"] != "xero-review-receipt.v1" or decision["schema_version"] != "xero-human-review-decision.v1":
        raise GatewayError("A supplied review artefact has an unsupported schema version.")
    if evidence["mode"] != "synthetic" or receipt["mode"] != "synthetic":
        raise GatewayError("Only synthetic review artefacts are supported.")
    if not (evidence["run_id"] == receipt["run_id"] == decision["run_id"]):
        raise GatewayError("Decision, evidence, and receipt must refer to the same run_id.")
    if "sha256:" + sha256_bytes(canonical_json(evidence)) != receipt["evidence_sha256"]:
        raise GatewayError("Reviewer evidence does not match the receipt's evidence digest.")
    # write_evaluation moves three files one at a time, so a failure between
    # the moves can leave one run's model result beside another run's receipt.
    # The receipt seals the model result too, so check it whenever the file is
    # there. It is not required: the evidence/model split exists so a reviewer
    # can hold the evidence and receipt without the model result.
    model_path = receipt_path.with_name("model-result.json")
    if model_path.is_file():
        model = load_json_object(model_path, label="model result")
        if "sha256:" + sha256_bytes(canonical_json(model)) != receipt["result_sha256"]:
            raise GatewayError("Model result beside the receipt does not match the receipt's result digest.")
    items = evidence["items"]
    evidence_fields = {"finding_id", "account_id", "account_code", "account_name", "current_values", "prior_values", "source_refs"}
    if not isinstance(items, list) or not all(isinstance(item, dict) and set(item) == evidence_fields for item in items):
        raise GatewayError("Reviewer evidence items must be a list of objects with the exact evidence shape.")
    finding_ids = [_non_empty(item["finding_id"], field="evidence finding_id") for item in items]
    if len(finding_ids) != len(set(finding_ids)):
        raise GatewayError("Reviewer evidence contains duplicate finding IDs.")
    total_findings = evidence["total_findings"]
    if isinstance(total_findings, bool) or not isinstance(total_findings, int) or total_findings < len(items):
        raise GatewayError("Reviewer evidence total_findings must be an integer at least as large as the visible item count.")
    if not isinstance(evidence["truncated"], bool) or evidence["truncated"] != (total_findings > len(items)):
        raise GatewayError("Reviewer evidence truncated must exactly describe whether findings were omitted.")
    _non_empty(decision["reviewer_ref"], field="human decision reviewer_ref")
    _iso_timestamp(decision["reviewed_at"], field="human decision reviewed_at")
    if not isinstance(decision["decisions"], list):
        raise GatewayError("Human decision decisions must be a list.")
    # A clean run carries zero findings, so its sign-off is the empty decisions
    # list: reviewer_ref, reviewed_at and run_id above still record who looked
    # at which run. Once the evidence carries findings an empty list is not a
    # partial review; it is a file that decided nothing, so it stays refused.
    if not decision["decisions"] and total_findings > 0:
        raise GatewayError("Human decision must contain at least one decision when the evidence carries findings.")
    known = set(finding_ids)
    decided: set[str] = set()
    for item in decision["decisions"]:
        if not isinstance(item, dict) or set(item) != {"finding_id", "decision", "rationale"}:
            raise GatewayError("Each human decision must contain exactly finding_id, decision, and rationale.")
        finding_id = _non_empty(item["finding_id"], field="human decision finding_id")
        if finding_id not in known or finding_id in decided:
            raise GatewayError("Human decision refers to an unknown or duplicate finding.")
        if not isinstance(item["decision"], str) or item["decision"] not in ALLOWED_DECISIONS or not isinstance(item["rationale"], str) or not item["rationale"].strip():
            raise GatewayError("Human decision state or rationale is invalid.")
        _non_empty(item["rationale"], field="human decision rationale")
        decided.add(finding_id)
    undecided_count = total_findings - len(decided)
    # A decision can only name a finding the evidence actually carries, so a
    # run whose findings were capped can never reach DECISION_RECORDED. That
    # is deliberate - silence about an omitted finding is not a decision about
    # it - but the caller has to be able to tell "still being reviewed" from
    # "cannot be completed at this max_results".
    return {
        "schema_version": "xero-review-decision-validation.v1",
        "run_id": receipt["run_id"],
        "status": "DECISION_RECORDED" if undecided_count == 0 else "PARTIAL_DECISION_RECORDED",
        "decision_count": len(decided),
        "undecided_count": undecided_count,
        "visible_findings": len(items),
        "truncated": evidence["truncated"],
        "completable": not evidence["truncated"],
        "limitation": "Validation records a structurally valid human decision; it does not approve, resolve, post, pay, lodge, or lock anything.",
    }


from .persist import write_evaluation
