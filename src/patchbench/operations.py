"""Validated execution adapters for local GUI administrative operations."""

from __future__ import annotations

import tempfile
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Callable

from patchbench.catalog import read_metadata, case_type as get_case_type, resolve_case, discover_cases
from patchbench.case import is_safe_path_component
from patchbench.commands import evaluate_patch, report, verify_agent_image
from patchbench.containers import build_claude_code_image, build_image
from patchbench.dashboard_export import export_dashboard
from patchbench.discovery import discover_gitlab_cases
from patchbench.exceptions import PatchbenchError
from patchbench.ingest import ingest_gitlab_mr
from patchbench.landb_answer_benchmark import verify_live_dev_case
from patchbench.local_landb_mcp_smoke import (
    run_patchbench_dev_smoke_test,
    run_patchbench_smoke_test,
)


OPERATION_KINDS = {
    "evaluate-patch",
    "verify-landb-dev",
    "smoke-test-landb-mcp",
    "discover-gitlab-cases",
    "ingest-gitlab-mr",
    "build-env",
    "build-agent-image",
    "verify-agent-image",
    "report",
    "export-dashboard",
}


@dataclass(frozen=True)
class OperationOutcome:
    result: Any
    refresh_dashboard: bool = False
    dashboard_manifest: dict[str, Any] | None = None


def normalize_operation(payload: dict[str, Any], root: Path) -> dict[str, Any]:
    """Validate one browser operation without touching external state."""
    kind = _text(payload, "kind")
    if kind not in OPERATION_KINDS:
        raise PatchbenchError(f"Unsupported operation: {kind}")
    normalized: dict[str, Any] = {"kind": kind}

    if kind == "evaluate-patch":
        normalized["case_id"] = _case_id(payload, root, coding_only=True)
        patch_text = _text(payload, "patch_text")
        if len(patch_text.encode("utf-8")) > 1_500_000:
            raise PatchbenchError("patch_text must not exceed 1.5 MB")
        normalized["patch_text"] = patch_text
    elif kind == "verify-landb-dev":
        normalized.update(
            case_id=_case_id(payload, root, answer_only=True),
            landb_mcp_repo=_text(payload, "landb_mcp_repo"),
            mcp_env_file=_text(payload, "mcp_env_file"),
            timeout_seconds=_positive_int(payload, "timeout_seconds", 60),
        )
    elif kind == "smoke-test-landb-mcp":
        live_dev = _boolean(payload, "live_dev", False)
        container = _boolean(payload, "container", False)
        mcp_env_file = _optional_text(payload, "mcp_env_file")
        if live_dev and not mcp_env_file:
            raise PatchbenchError("live DEV smoke requires mcp_env_file")
        if not live_dev and mcp_env_file:
            raise PatchbenchError("mcp_env_file requires live_dev")
        agent_env_file = _optional_text(payload, "agent_env_file")
        agent_image = _optional_text(payload, "agent_image")
        if container and (not agent_env_file or not agent_image):
            raise PatchbenchError(
                "container smoke requires agent_env_file and agent_image"
            )
        normalized.update(
            landb_mcp_repo=_text(payload, "landb_mcp_repo"),
            live_dev=live_dev,
            container=container,
            mcp_env_file=mcp_env_file,
            model=_optional_text(payload, "model"),
            timeout_seconds=_positive_int(payload, "timeout_seconds", 180),
            agent_env_file=agent_env_file,
            agent_ca_file=_optional_text(payload, "agent_ca_file"),
            agent_image=agent_image,
            agent_network=_optional_text(payload, "agent_network") or "pasta",
            artifacts_dir=_optional_text(payload, "artifacts_dir"),
        )
    elif kind == "discover-gitlab-cases":
        normalized.update(
            project_id=_text(payload, "project_id"),
            repo=_safe_name(payload, "repo"),
            repo_path=_text(payload, "repo_path"),
            limit=_positive_int(payload, "limit", 100, maximum=500),
            gitlab_url=_optional_text(payload, "gitlab_url"),
        )
    elif kind == "ingest-gitlab-mr":
        case_id = _new_case_id(payload, root)
        normalized.update(
            project_id=_text(payload, "project_id"),
            mr_iid=_positive_int(payload, "mr_iid"),
            case_id=case_id,
            repo_path=_text(payload, "repo_path"),
            gitlab_url=_optional_text(payload, "gitlab_url"),
        )
    elif kind == "build-env":
        normalized["repo"] = _safe_name(payload, "repo")
    elif kind == "build-agent-image":
        normalized.update(
            image=_text(payload, "image"),
            landb_mcp_repo=_optional_text(payload, "landb_mcp_repo"),
        )
    elif kind == "verify-agent-image":
        raw_case_ids = payload.get("case_ids")
        if not isinstance(raw_case_ids, list) or not raw_case_ids:
            raise PatchbenchError("case_ids must be a non-empty list")
        case_ids = []
        for raw_case_id in raw_case_ids:
            case_ids.append(_case_id({"case_id": raw_case_id}, root, coding_only=True))
        normalized.update(
            case_ids=case_ids,
            image=_text(payload, "image"),
            require_landb_mcp=_boolean(payload, "require_landb_mcp", False),
        )
    elif kind == "report":
        normalized["path"] = str(_project_path(root, _text(payload, "path")))
    elif kind == "export-dashboard":
        normalized.update(
            include_gold=_boolean(payload, "include_gold", False),
            freshness_seconds=_positive_int(payload, "freshness_seconds", 60),
        )
    return normalized


def execute_operation(
    config: dict[str, Any],
    root: Path,
    operation_id: str,
    progress: Callable[[str], None],
) -> OperationOutcome:
    """Execute one previously normalized operation."""
    kind = config["kind"]
    progress(f"Starting {kind}")
    if kind == "evaluate-patch":
        with tempfile.TemporaryDirectory(prefix="patchbench-gui-patch-") as raw:
            patch_path = Path(raw) / "candidate.patch"
            patch_path.write_text(config["patch_text"], encoding="utf-8")
            result = evaluate_patch(resolve_case(root, config["case_id"]), patch_path)
        progress("Patch evaluation completed")
        return OperationOutcome(_json_value(result))
    if kind == "verify-landb-dev":
        status = verify_live_dev_case(
            resolve_case(root, config["case_id"]),
            landb_mcp_repo=config["landb_mcp_repo"],
            mcp_env_file=config["mcp_env_file"],
            timeout_seconds=config["timeout_seconds"],
        )
        progress(f"Read-only DEV preflight returned HTTP {status}")
        return OperationOutcome({"dev_preflight": "passed", "status": status})
    if kind == "smoke-test-landb-mcp":
        common = {
            "landb_mcp_repo": config["landb_mcp_repo"],
            "model": config["model"],
            "timeout_seconds": config["timeout_seconds"],
            "container": config["container"],
            "agent_env_file": config["agent_env_file"],
            "agent_ca_file": config["agent_ca_file"],
            "agent_image": config["agent_image"],
            "agent_network": config["agent_network"],
            "artifacts_dir": config["artifacts_dir"],
            "progress": progress,
        }
        if config["live_dev"]:
            result = run_patchbench_dev_smoke_test(
                **common, mcp_env_file=config["mcp_env_file"]
            )
        else:
            result = run_patchbench_smoke_test(**common)
        return OperationOutcome(_json_value(result))
    if kind == "discover-gitlab-cases":
        with tempfile.TemporaryDirectory(prefix="patchbench-gui-discovery-") as raw:
            candidates = discover_gitlab_cases(
                project_id=config["project_id"],
                repo=config["repo"],
                repo_path=config["repo_path"],
                output_path=Path(raw) / f"{operation_id}.json",
                limit=config["limit"],
                gitlab_url=config["gitlab_url"],
            )
        progress(f"Discovered {len(candidates)} merged merge requests")
        return OperationOutcome(candidates)
    if kind == "ingest-gitlab-mr":
        output = ingest_gitlab_mr(
            project_id=config["project_id"],
            mr_iid=config["mr_iid"],
            case_id=config["case_id"],
            repo_path=config["repo_path"],
            output_path=root / "cases" / config["case_id"],
            gitlab_url=config["gitlab_url"],
        )
        progress(f"Created draft case {config['case_id']}")
        return OperationOutcome(
            {"case_id": config["case_id"], "output": output.relative_to(root).as_posix()},
            refresh_dashboard=True,
        )
    if kind == "build-env":
        image = build_image(config["repo"])
        progress(f"Built evaluation image {image}")
        return OperationOutcome({"image": image})
    if kind == "build-agent-image":
        image = build_claude_code_image(
            config["image"], landb_mcp_repo=config["landb_mcp_repo"]
        )
        progress(f"Built agent image {image}")
        return OperationOutcome({"image": image})
    if kind == "verify-agent-image":
        results = verify_agent_image(
            [resolve_case(root, case_id) for case_id in config["case_ids"]],
            image_name=config["image"],
            require_landb_mcp=config["require_landb_mcp"],
        )
        return OperationOutcome([_json_value(result) for result in results])
    if kind == "report":
        return OperationOutcome({"report": report(config["path"])})
    if kind == "export-dashboard":
        manifest = export_dashboard(
            cases_dir=root / "cases",
            runs_dir=root / "runs",
            output=root / "frontend" / "public" / "data",
            include_gold=config["include_gold"],
            freshness_seconds=config["freshness_seconds"],
        )
        return OperationOutcome(manifest, dashboard_manifest=manifest)
    raise PatchbenchError(f"Unsupported operation: {kind}")


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return value


def _text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PatchbenchError(f"{key} must be a non-empty string")
    return value.strip()


def _optional_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise PatchbenchError(f"{key} must be a string")
    return value.strip() or None


def _boolean(payload: dict[str, Any], key: str, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise PatchbenchError(f"{key} must be a boolean")
    return value


def _positive_int(
    payload: dict[str, Any], key: str, default: int | None = None, maximum: int | None = None
) -> int:
    value = payload.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PatchbenchError(f"{key} must be a positive integer")
    if maximum is not None and value > maximum:
        raise PatchbenchError(f"{key} must not exceed {maximum}")
    return value


def _case_id(
    payload: dict[str, Any],
    root: Path,
    *,
    coding_only: bool = False,
    answer_only: bool = False,
) -> str:
    case_id = _text(payload, "case_id")
    if not is_safe_path_component(case_id):
        raise PatchbenchError("case_id must be one safe path component")
    case_dir = resolve_case(root, case_id)
    metadata = read_metadata(case_dir)
    case_type = get_case_type(metadata)
    if coding_only and case_type != "coding":
        raise PatchbenchError(f"Operation requires a coding case: {case_id}")
    if answer_only and case_type != "landb_answer":
        raise PatchbenchError(f"Operation requires a landb_answer case: {case_id}")
    return case_id


def _new_case_id(payload: dict[str, Any], root: Path) -> str:
    case_id = _text(payload, "case_id")
    if not is_safe_path_component(case_id):
        raise PatchbenchError("case_id must be one safe path component")
    if (root / "cases" / case_id).exists() or case_id in discover_cases(root / "cases"):
        raise PatchbenchError(f"Case already exists: {case_id}")
    return case_id


def _safe_name(payload: dict[str, Any], key: str) -> str:
    value = _text(payload, key)
    if any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in value):
        raise PatchbenchError(f"{key} contains unsupported characters")
    return value


def _project_path(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise PatchbenchError("path must stay within the PatchBench project") from exc
    return resolved
