"""Case identity and artifact locations shared by CLI, service, and exporters."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from patchbench.exceptions import PatchbenchError


def read_metadata(case_dir: str | Path) -> dict[str, Any]:
    path = Path(case_dir) / "meta.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise PatchbenchError(f"Could not read case metadata: {path}") from exc
    if not isinstance(raw, dict):
        raise PatchbenchError(f"Case metadata must be a mapping: {path}")
    case_type(raw)
    return raw


def case_type(metadata: dict[str, Any]) -> str:
    # Missing type is supported for historical coding cases only.
    kind = metadata.get("case_type", "coding")
    if kind not in ("coding", "landb_answer"):
        raise PatchbenchError(f"Unsupported case_type: {kind!r}")
    return kind


def discover_cases(cases_dir: str | Path) -> dict[str, Path]:
    """Index self-contained case directories by declared identity, never prefix."""
    root = Path(cases_dir).resolve()
    cases: dict[str, Path] = {}
    for path in sorted(root.glob("*/meta.yaml")):
        metadata = read_metadata(path.parent)
        identifier = metadata.get("id")
        if not safe_component(identifier):
            raise PatchbenchError(f"Invalid case ID in {path}")
        if identifier in cases:
            raise PatchbenchError(f"Duplicate case ID: {identifier}")
        if not path.resolve().is_relative_to(root):
            raise PatchbenchError(f"Case metadata escapes cases directory: {path}")
        cases[identifier] = path.parent
    return cases


def resolve_case(root: Path, identifier: str) -> Path:
    if not safe_component(identifier):
        raise PatchbenchError("Case ID must be one safe path component")
    cases = discover_cases(root / "cases")
    if identifier not in cases:
        raise PatchbenchError(f"Unknown case: {identifier}")
    return cases[identifier]


def resolve_case_reference(value: str | Path, root: Path) -> Path:
    """CLI arguments may name a case by directory or by declared case ID."""
    path = Path(value).expanduser()
    if path.is_dir():
        return path.resolve()
    if safe_component(str(value)):
        return resolve_case(root, str(value))
    raise PatchbenchError(f"Case directory does not exist: {value}")


def safe_component(value: object) -> bool:
    return (isinstance(value, str) and bool(value) and Path(value).name == value
            and "\\" not in value and value not in {".", ".."})


def case_output_dir(case_dir: str | Path) -> Path:
    directory = Path(case_dir).resolve()
    identifier = read_metadata(directory).get("id")
    if not safe_component(identifier):
        raise PatchbenchError("Case ID must be one safe path component")
    root = directory.parent.parent if directory.parent.name == "cases" else directory.parent
    for parent in directory.parents:
        if (parent / "cases").is_dir() and (parent / "repositories").is_dir():
            root = parent
            break
    return root / "artifacts" / identifier


def validation_path(case_dir: str | Path) -> Path:
    return case_output_dir(case_dir) / "validation.json"


def case_runs_dir(case_dir: str | Path) -> Path:
    return case_output_dir(case_dir) / "runs"
