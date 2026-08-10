from __future__ import annotations

from pathlib import Path

import pytest

from xero_ai_review_gateway.errors import GatewayError
from xero_ai_review_gateway.util import load_json_exact, load_json_object, path_within


@pytest.mark.parametrize("payload", ["[]", '"text"', "3"])
def test_json_that_is_not_an_object_is_blocked_not_a_traceback(tmp_path: Path, payload: str) -> None:
    # load_json_object reads an artefact whose key set this gateway does not
    # fix, so the object check is the only shape check it can make.
    bad = tmp_path / "bad.json"
    bad.write_text(payload, encoding="utf-8")

    for loader in (lambda: load_json_object(bad, label="test artefact"), lambda: load_json_exact(bad, set(), label="test artefact")):
        with pytest.raises(GatewayError, match="must be a JSON object"):
            loader()


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


def test_a_name_the_operating_system_refuses_is_blocked_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Windows rejects a reserved character such as ':' in a path with
    # OSError [WinError 123], which is not FileNotFoundError. Only the target
    # is made to fail, so resolving the parent still behaves normally.
    target = tmp_path / "de:mo" / "decision.json"
    real_resolve = Path.resolve

    def refuse(self: Path, strict: bool = False) -> Path:
        if self == target:
            raise OSError(22, "Invalid argument")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", refuse)

    with pytest.raises(GatewayError, match="not a usable path"):
        path_within(target, tmp_path, label="test artefact")
