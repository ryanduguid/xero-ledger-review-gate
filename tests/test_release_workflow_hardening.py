"""Contracts for the manually dispatched release backfill boundary."""

import re
from pathlib import Path

import pytest


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"
TAG_PATTERN = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")


def test_release_backfill_treats_dispatch_tag_as_validated_data() -> None:
    if not WORKFLOW.is_file():
        pytest.skip("release workflow is not included in source distributions")
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "TAG: ${{ inputs.tag }}" in workflow
    assert r'if [[ ! "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then' in workflow
    assert 'gh release download "$TAG"' in workflow
    assert 'gh release download "${{ inputs.tag }}"' not in workflow
    assert '--repo "$GITHUB_REPOSITORY"' in workflow
    assert '--repo "${{ github.repository }}"' not in workflow
    assert (
        "group: release-${{ github.repository }}-"
        "${{ github.event_name == 'workflow_dispatch' && inputs.tag || github.ref_name }}"
    ) in workflow
    assert "cancel-in-progress: false" in workflow


def test_release_tag_validator_rejects_shell_shaped_and_malformed_values() -> None:
    for tag in ("1.2.3", "v1.2", "v1.2.3/extra", 'v1.2.3"; touch PWNED; #'):
        assert TAG_PATTERN.fullmatch(tag) is None

    for tag in ("v0.1.4", "v12.0.103"):
        assert TAG_PATTERN.fullmatch(tag) is not None
