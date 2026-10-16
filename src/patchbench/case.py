from __future__ import annotations

import re
import shlex
from pathlib import Path

from patchbench.catalog import case_output_dir, case_type, read_metadata, safe_component
from patchbench.repositories import load_repository
from patchbench.exceptions import PatchbenchError
from patchbench.models import CaseMetadata, REQUIRED_META_FIELDS, SetupCommand


def load_case(case_dir: str | Path) -> CaseMetadata:
    resolved_case_dir = Path(case_dir).expanduser().resolve()
    meta_path = resolved_case_dir / "meta.yaml"
    raw = read_metadata(resolved_case_dir)
    if case_type(raw) != "coding":
        raise PatchbenchError("Operation requires a coding case")
    repository = load_repository(raw["repository"], resolved_case_dir) if "repository" in raw else None
    if repository is not None:
        if repository.config["evaluator"] != "patch_tests":
            raise PatchbenchError("Coding case requires the patch_tests evaluator")
        raw = {**raw, "repo_path": raw.get("repo_path") or str(repository.checkout())}

    oracle_mode = raw.get("oracle_mode", "injected_test_patch")
    if oracle_mode not in {"injected_test_patch", "existing_tests", "existing_tests_with_regression"}:
        raise PatchbenchError("Unsupported oracle_mode")
    required_fields = REQUIRED_META_FIELDS - ({"test_patch"} if oracle_mode in {"existing_tests", "existing_tests_with_regression"} else set())
    if oracle_mode in {"existing_tests", "existing_tests_with_regression"}:
        from patchbench.mirroring.schema import check_metadata
        check_metadata(raw, resolved_case_dir)
    missing = sorted(required_fields - raw.keys())
    if missing:
        raise PatchbenchError(f"Case metadata missing required fields: {', '.join(missing)}")

    for field in required_fields:
        value = raw[field]
        if not isinstance(value, str) or not value.strip():
            raise PatchbenchError(
                f"meta.yaml field '{field}' must be a non-empty string"
            )
    if not is_safe_path_component(raw["id"]):
        raise PatchbenchError("meta.yaml field 'id' must be a single path component")

    test_command = raw["test_command"]
    if not isinstance(test_command, str) or not test_command.strip():
        raise PatchbenchError("meta.yaml field 'test_command' must be a non-empty string")

    workdir = raw.get("workdir")
    if workdir is not None and (not isinstance(workdir, str) or not workdir.strip()):
        raise PatchbenchError("meta.yaml field 'workdir' must be a non-empty string when provided")
    if isinstance(workdir, str) and not _is_safe_relative_path(workdir):
        raise PatchbenchError("meta.yaml field 'workdir' must stay within the repository")

    env = raw.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise PatchbenchError("meta.yaml field 'env' must be a mapping of string keys to string values")
    if any(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None for key in env):
        raise PatchbenchError(
            "meta.yaml field 'env' keys must be valid environment variable names"
        )

    setup_commands = _parse_setup_commands(raw.get("setup_commands", []))
    timeout_seconds = _parse_timeout_seconds(raw.get("timeout_seconds", 300))
    relevant_files = _parse_relevant_files(raw.get("hints", {}))
    image = raw.get("image") or (repository.config.get("image") if repository else None)
    if image is not None and (not isinstance(image, str) or not image.strip()):
        raise PatchbenchError("Case image must be a non-empty string")

    repo_path = _resolve_path(resolved_case_dir, raw["repo_path"])
    problem_statement = _resolve_case_artifact(
        resolved_case_dir, raw["problem_statement"], "problem_statement"
    )
    gold_patch = _resolve_case_artifact(
        resolved_case_dir, raw["gold_patch"], "gold_patch"
    )
    test_patch = _resolve_case_artifact(
        resolved_case_dir, raw["test_patch"], "test_patch"
    ) if oracle_mode == "injected_test_patch" else None

    if not repo_path.is_dir():
        raise PatchbenchError(f"Resolved repo_path is not a directory: {repo_path}")
    for path_value, label in [
        (problem_statement, "problem_statement"),
        (gold_patch, "gold_patch"),
        (test_patch, "test_patch"),
    ]:
        if path_value is not None and not path_value.is_file():
            raise PatchbenchError(f"Resolved {label} is not a file: {path_value}")

    logs_dir = case_output_dir(resolved_case_dir) / "logs"
    runs_dir = case_output_dir(resolved_case_dir) / "runs"

    return CaseMetadata(
        id=str(raw["id"]),
        repo_path=repo_path,
        base_commit=str(raw["base_commit"]),
        fix_commit=str(raw["fix_commit"]),
        test_command=shlex.split(test_command),
        problem_statement=problem_statement,
        gold_patch=gold_patch,
        test_patch=test_patch,
        workdir=workdir.strip() if isinstance(workdir, str) else None,
        env=dict(env),
        setup_commands=setup_commands,
        timeout_seconds=timeout_seconds,
        relevant_files=relevant_files,
        case_dir=resolved_case_dir,
        logs_dir=logs_dir,
        runs_dir=runs_dir,
        meta_path=meta_path,
        image=image,
        repository_id=repository.id if repository else None,
        oracle_mode=oracle_mode,
    )


def ensure_case_output_dirs(case: CaseMetadata) -> None:
    case.logs_dir.mkdir(parents=True, exist_ok=True)
    case.runs_dir.mkdir(parents=True, exist_ok=True)


def result_path_for_patch(case: CaseMetadata, patch_path: Path) -> Path:
    patch_name = patch_path.stem.replace(" ", "_") or "patch"
    return case.runs_dir / f"{patch_name}.result.json"


def is_safe_path_component(value: object) -> bool:
    return safe_component(value)


def _resolve_path(case_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (case_dir / path).resolve()
    else:
        path = path.resolve()
    return path


def _resolve_case_artifact(case_dir: Path, value: str, label: str) -> Path:
    if not _is_safe_relative_path(value):
        raise PatchbenchError(
            f"meta.yaml field '{label}' must be a relative path within the case"
        )
    resolved = (case_dir / value).resolve()
    try:
        resolved.relative_to(case_dir)
    except ValueError as exc:
        raise PatchbenchError(
            f"Resolved {label} escapes the case directory: {resolved}"
        ) from exc
    return resolved


def _is_safe_relative_path(value: str) -> bool:
    path = Path(value)
    return not path.is_absolute() and "\\" not in value and ".." not in path.parts


def _parse_setup_commands(value: object) -> list[SetupCommand]:
    if not isinstance(value, list):
        raise PatchbenchError("meta.yaml field 'setup_commands' must be a list when provided")

    parsed: list[SetupCommand] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise PatchbenchError("each setup_commands entry must be a non-empty string")
        parsed.append(tuple(shlex.split(item)))
    return parsed


def _parse_timeout_seconds(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PatchbenchError("meta.yaml field 'timeout_seconds' must be a positive integer")
    return value


def _parse_relevant_files(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, dict):
        raise PatchbenchError("meta.yaml field 'hints' must be a mapping when provided")
    relevant_files = value.get("relevant_files", [])
    if not isinstance(relevant_files, list) or not all(
        isinstance(path, str) and path.strip() for path in relevant_files
    ):
        raise PatchbenchError(
            "meta.yaml hints.relevant_files must be a list of non-empty strings"
        )
    parsed = [path.strip() for path in relevant_files]
    if any(not _is_safe_relative_path(path) for path in parsed):
        raise PatchbenchError(
            "meta.yaml hints.relevant_files entries must stay within the repository"
        )
    return parsed
