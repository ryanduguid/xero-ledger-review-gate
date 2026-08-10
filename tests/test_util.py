from __future__ import annotations

import json
from pathlib import Path

import pytest

from xero_ai_review_gateway.errors import GatewayError
from xero_ai_review_gateway.util import load_json_exact, load_json_object, path_within, sha256_file


@pytest.mark.parametrize("payload", ["[]", '"text"', "3"])
def test_json_that_is_not_an_object_is_blocked_not_a_traceback(tmp_path: Path, payload: str) -> None:
    # load_json_object reads an artefact whose key set this gateway does not
    # fix, so the object check is the only shape check it can make.
    bad = tmp_path / "bad.json"
    bad.write_text(payload, encoding="utf-8")

    for loader in (lambda: load_json_object(bad, label="test artefact"), lambda: load_json_exact(bad, set(), label="test artefact")):
        with pytest.raises(GatewayError, match="must be a JSON object"):
            loader()


@pytest.mark.parametrize(
    "payload",
    [
        # An extra key and a missing key. Both parse as JSON objects and both
        # carry a valid schema_version, so the exact key-set comparison is the
        # only line that can refuse either. Every other load_json_exact case in
        # this file raises at the isinstance, UTF-8 or OSError branch first and
        # never reaches the key-set comparison at all.
        {"schema_version": "v1", "run_id": "sha256:0", "mode": "live"},
        {"schema_version": "v1"},
    ],
)
def test_a_key_set_that_is_not_exactly_the_declared_one_is_refused(tmp_path: Path, payload: dict) -> None:
    artefact = tmp_path / "artefact.json"
    artefact.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(GatewayError, match="test artefact must contain exactly: run_id, schema_version"):
        load_json_exact(artefact, {"schema_version", "run_id"}, label="test artefact")


def test_the_exact_declared_key_set_is_accepted(tmp_path: Path) -> None:
    """Without this the refusals above would also pass against a loader that refused everything."""
    artefact = tmp_path / "artefact.json"
    artefact.write_text(json.dumps({"schema_version": "v1", "run_id": "sha256:0"}), encoding="utf-8")

    assert load_json_exact(artefact, {"schema_version", "run_id"}, label="test artefact") == {
        "schema_version": "v1",
        "run_id": "sha256:0",
    }


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


def test_a_file_that_cannot_be_digested_is_blocked_not_a_traceback(tmp_path: Path) -> None:
    # sha256_file reads a path path_within has already accepted, and a path
    # that exists and is contained can still be a directory.
    with pytest.raises(GatewayError, match="test artefact cannot be read"):
        sha256_file(tmp_path, label="test artefact")


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
