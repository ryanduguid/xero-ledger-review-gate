from __future__ import annotations

from pathlib import Path

import pytest

from xero_ai_review_gateway.errors import GatewayError
from xero_ai_review_gateway.util import load_json_exact


def test_non_utf8_json_is_blocked_not_a_traceback(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"\xff\xfe{}")

    with pytest.raises(GatewayError, match="not valid UTF-8"):
        load_json_exact(bad, set(), label="test artefact")


def test_unreadable_path_is_blocked_not_a_traceback(tmp_path: Path) -> None:
    # A directory cannot be read as a file; the error must stay inside the
    # fail-closed GatewayError contract instead of escaping as OSError.
    with pytest.raises(GatewayError, match="cannot be read"):
        load_json_exact(tmp_path, set(), label="test artefact")
