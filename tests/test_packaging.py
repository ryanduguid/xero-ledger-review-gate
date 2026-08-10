from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def test_build_artefacts_cannot_be_committed_by_accident() -> None:
    """The README tells the reader to run `python -m build`, which fills dist/."""
    ignore = REPO / ".gitignore"
    if not ignore.is_file():
        pytest.skip("not running from a source checkout")
    entries = {line.strip() for line in ignore.read_text(encoding="utf-8").splitlines()}

    assert {"build/", "dist/"} <= entries
