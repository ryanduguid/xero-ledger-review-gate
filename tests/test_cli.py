from __future__ import annotations

from pathlib import Path

import pytest

from xero_ai_review_gateway.cli import main


def test_cli_end_to_end_from_any_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert main([
        "evaluate",
        "--context", "samples/contexts/sample-monthly-variance.context.json",
        "--request", "samples/requests/sample-revenue-variance.request.json",
        "--policy", "policy/demo-policy-v1.json",
        "--out", "build/demo",
    ]) == 0
    assert (tmp_path / "build" / "demo" / "receipt.json").is_file()
    assert main([
        "validate-review",
        "--evidence", "build/demo/reviewer-evidence.json",
        "--receipt", "build/demo/receipt.json",
        "--decision", "samples/decisions/sample-review-decision.json",
    ]) == 0


def test_cli_blocks_output_outside_cwd_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert main([
        "evaluate",
        "--context", "samples/contexts/sample-monthly-variance.context.json",
        "--request", "samples/requests/sample-revenue-variance.request.json",
        "--policy", "policy/demo-policy-v1.json",
        "--out", str(tmp_path / "elsewhere"),
    ]) == 2
