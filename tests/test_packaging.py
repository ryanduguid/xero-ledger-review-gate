from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def test_active_package_identity_is_consistent() -> None:
    active_surfaces = [
        REPO / "pyproject.toml",
        REPO / "MANIFEST.in",
        REPO / ".gitignore",
        REPO / ".github" / "workflows" / "ci.yml",
        REPO / ".github" / "workflows" / "release.yml",
        REPO / "README.md",
        REPO / "CITATION.cff",
        REPO / "DATA-FLOW.md",
        REPO / "DISCLAIMER.md",
        REPO / "CONTRIBUTING.md",
        REPO / "RELEASING.md",
        REPO / "llms.txt",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in active_surfaces)
    releasing = (REPO / "RELEASING.md").read_text(encoding="utf-8")
    runtime_text = text.replace(releasing, "")

    assert "xero-ledger-review-gate" in text
    assert "xero-ai-" + "review-gateway" not in runtime_text
    assert "elizabeth-anne-alexander" in text
    assert "elizabeth_anne_alexander" in text
    assert "ElizabethAnneAlexander" not in text
    assert "xero_ai_review_gateway" not in runtime_text
    assert "xero-ai-" + "review-gateway" in releasing
    assert "xero_ai_review_gateway-0.1.1" in releasing


def test_release_workflow_keeps_the_pinned_reusable_policy_caller() -> None:
    workflow = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert (
        "uses: ryanduguid/release-policy/.github/workflows/"
        "release-python.yml@3b8a377207cab2c7c808fcc96b66578f4695beea"
        " # release-python.yml's on.workflow_call never re-exposed job outputs;"
        " caller PyPI publish silently failed until #11"
    ) in workflow
    assert "from elizabeth_anne_alexander.version import __version__" in workflow


def test_build_artefacts_cannot_be_committed_by_accident() -> None:
    """The README tells the reader to run `python -m build`, which fills dist/."""
    ignore = REPO / ".gitignore"
    if not ignore.is_file():
        pytest.skip("not running from a source checkout")
    entries = {line.strip() for line in ignore.read_text(encoding="utf-8").splitlines()}

    assert {"build/", "dist/"} <= entries


def test_the_ci_test_step_does_not_repeat_the_quiet_flag_from_addopts() -> None:
    """`-q` twice is `-qq`, which drops pytest's summary line from the job log.

    pyproject already sets addopts = "-q", so a CI step that adds its own
    leaves a reviewer with a progress bar, no test count and no timing: the
    log stops saying how many tests actually ran.
    """
    workflow = REPO / ".github" / "workflows" / "ci.yml"
    pyproject = REPO / "pyproject.toml"
    if not (workflow.is_file() and pyproject.is_file()):
        pytest.skip("not running from a source checkout")
    addopts = [line for line in pyproject.read_text(encoding="utf-8").splitlines() if line.startswith("addopts")]
    steps = [line for line in workflow.read_text(encoding="utf-8").splitlines() if " pytest" in line]

    assert steps, "the workflow no longer runs pytest"
    for step in steps:
        arguments = step.split(" pytest", 1)[1]
        if "-o addopts=" in arguments:
            continue  # the step replaces addopts rather than adding to it
        assert sum(line.count("-q") for line in addopts) + arguments.count("-q") <= 1, step
