from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from .errors import GatewayError


def package_root() -> Path:
    """The installed package directory that carries the bundled policy/ and samples/ data."""
    return Path(str(resources.files(__package__)))


def build_root() -> Path:
    """Run outputs are anchored below build/ in the invoking working directory."""
    return Path.cwd() / "build"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class FileSnapshot:
    """Immutable bytes and the digest calculated from that exact read."""

    path: Path
    content: bytes
    sha256: str


def _snapshot_file(path: Path, *, label: str, missing_is_unreadable: bool) -> FileSnapshot:
    try:
        content = path.read_bytes()
    except FileNotFoundError as exc:
        if not missing_is_unreadable:
            raise GatewayError(f"{label} does not exist: {path}.") from exc
        raise GatewayError(f"{label} cannot be read: {path}.") from exc
    except OSError as exc:
        raise GatewayError(f"{label} cannot be read: {path}.") from exc
    return FileSnapshot(path=path, content=content, sha256=sha256_bytes(content))


def snapshot_file(path: Path, *, label: str = "source file") -> FileSnapshot:
    """Read a file once and bind its immutable bytes to their SHA-256 digest."""

    return _snapshot_file(path, label=label, missing_is_unreadable=True)


def sha256_file(path: Path, *, label: str = "source file") -> str:
    """Digest a file, converting a filesystem failure the way load_json_object does.

    path_within only guarantees the resolved path exists and is contained; it
    cannot say the name is a readable file. A manifest naming a directory must
    stay inside the fail-closed contract rather than escape as a traceback that
    prints the local filesystem layout.
    """
    return snapshot_file(path, label=label).sha256


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _object_refusing_duplicate_keys(pairs: list[tuple[str, Any]], *, label: str, path: Path) -> dict[str, Any]:
    """Build a JSON object, refusing a key spelled twice instead of keeping its last value.

    Plain json.loads is last-key-wins, so a manifest carrying two sha256 keys
    parses to the second digest alone and the exact-key-set gate never sees
    that two were supplied. json.loads runs this hook for every object at
    every nesting level.
    """
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GatewayError(f"{label} has duplicate key {key!r}: {path}.")
        result[key] = value
    return result


def load_json_object_snapshot(path: Path, *, label: str) -> tuple[dict[str, Any], FileSnapshot]:
    """Parse JSON from the same immutable bytes whose digest is retained."""
    snapshot = _snapshot_file(path, label=label, missing_is_unreadable=False)
    try:
        raw = json.loads(
            snapshot.content.decode("utf-8"),
            object_pairs_hook=lambda pairs: _object_refusing_duplicate_keys(pairs, label=label, path=path),
        )
    except UnicodeDecodeError as exc:
        raise GatewayError(f"{label} is not valid UTF-8 text: {path}.") from exc
    except json.JSONDecodeError as exc:
        raise GatewayError(f"{label} is not valid JSON: {path}.") from exc
    if not isinstance(raw, dict):
        raise GatewayError(f"{label} must be a JSON object: {path}.")
    return raw, snapshot


def load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    """Read a JSON object whose key set is not fixed by this gateway."""
    raw, _snapshot = load_json_object_snapshot(path, label=label)
    return raw


def load_json_exact_snapshot(path: Path, required: set[str], *, label: str) -> tuple[dict[str, Any], FileSnapshot]:
    raw, snapshot = load_json_object_snapshot(path, label=label)
    if set(raw) != required:
        raise GatewayError(f"{label} must contain exactly: {', '.join(sorted(required))}.")
    return raw, snapshot


def load_json_exact(path: Path, required: set[str], *, label: str) -> dict[str, Any]:
    raw, _snapshot = load_json_exact_snapshot(path, required, label=label)
    return raw


def path_within(path: Path, parent: Path, *, label: str, require_exists: bool = True) -> Path:
    root = parent.resolve()
    try:
        resolved = path.resolve(strict=require_exists)
    except FileNotFoundError as exc:
        raise GatewayError(f"{label} does not exist: {path}.") from exc
    except OSError as exc:
        # A name the operating system refuses outright (a reserved character,
        # a too-long path, a file used as a directory component) must stay
        # inside the fail-closed contract rather than escape as a traceback.
        raise GatewayError(f"{label} is not a usable path: {path}.") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise GatewayError(f"{label} must stay within {root}.") from exc
    return resolved
