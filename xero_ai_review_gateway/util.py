from __future__ import annotations

import hashlib
import json
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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    """Read a JSON object whose key set is not fixed by this gateway."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GatewayError(f"{label} does not exist: {path}.") from exc
    except UnicodeDecodeError as exc:
        raise GatewayError(f"{label} is not valid UTF-8 text: {path}.") from exc
    except OSError as exc:
        raise GatewayError(f"{label} cannot be read: {path}.") from exc
    except json.JSONDecodeError as exc:
        raise GatewayError(f"{label} is not valid JSON: {path}.") from exc
    if not isinstance(raw, dict):
        raise GatewayError(f"{label} must be a JSON object: {path}.")
    return raw


def load_json_exact(path: Path, required: set[str], *, label: str) -> dict[str, Any]:
    raw = load_json_object(path, label=label)
    if set(raw) != required:
        raise GatewayError(f"{label} must contain exactly: {', '.join(sorted(required))}.")
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
