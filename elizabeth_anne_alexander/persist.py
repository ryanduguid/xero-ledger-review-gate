"""Atomic write of one evaluation pack (model, evidence, receipt)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .errors import GatewayError
from .util import build_root, path_within

def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _replace(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def write_evaluation(model: dict[str, Any], evidence: dict[str, Any], receipt: dict[str, Any], output_dir: Path) -> dict[str, Path]:
    """Stage all three artefacts, then move them into place, receipt last.

    The three files describe one run. Writing them straight into the output
    directory means an interrupted second run can leave a truncated file, or a
    new model-result.json beside the previous run's evidence and receipt. Each
    file is written under a temporary name first, and nothing is moved until
    all three staged files exist.

    Three separate moves are not one atomic step, so a failure between them can
    still leave one new file beside two old ones. The receipt seals both the
    evidence and the model result and is moved last, so validate_review refuses
    every such mixed pack rather than reporting a decision against artefacts
    that came from two different runs.
    """
    output_dir = path_within(output_dir, build_root(), label="output directory", require_exists=False)
    paths = {"model": output_dir / "model-result.json", "evidence": output_dir / "reviewer-evidence.json", "receipt": output_dir / "receipt.json"}
    staged = {key: path.with_name(path.name + ".partial") for key, path in paths.items()}
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        for key, payload in (("model", model), ("evidence", evidence), ("receipt", receipt)):
            _write_json(staged[key], payload)
        for key in ("model", "evidence", "receipt"):
            _replace(staged[key], paths[key])
    except OSError as exc:
        for temporary in staged.values():
            try:
                temporary.unlink()
            except OSError:
                pass
        raise GatewayError(f"run output cannot be written to {output_dir}: {exc}.") from exc
    return paths
