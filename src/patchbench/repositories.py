"""Versioned repository defaults, with machine-local checkout overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from patchbench.catalog import safe_component
from patchbench.exceptions import PatchbenchError


DEFAULT_MANIFEST_ROOT = Path(__file__).resolve().parents[2] / "repositories"


@dataclass(frozen=True)
class RepositoryManifest:
    id: str
    path: Path
    config: dict[str, Any]

    def checkout(self) -> Path:
        setting = self.config["checkout"]
        value = os.environ.get(setting["env"]) or setting["default"]
        path = Path(value).expanduser()
        return (path if path.is_absolute() else self.path.parent.parent / path).resolve()

    def asset(self, key: str) -> Path:
        path = Path(self.config[key])
        return (self.path.parent.parent / path).resolve()


def load_repository(identifier: str, case_dir: Path | None = None) -> RepositoryManifest:
    if not safe_component(identifier):
        raise PatchbenchError("repository must be one safe identifier")
    manifest_root = DEFAULT_MANIFEST_ROOT
    if case_dir is not None:
        for parent in case_dir.resolve().parents:
            if (parent / "repositories").is_dir():
                manifest_root = parent / "repositories"
                break
    path = manifest_root / f"{identifier}.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise PatchbenchError(f"Could not read repository manifest: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1 or raw.get("id") != identifier:
        raise PatchbenchError(f"Invalid repository manifest: {path}")
    if raw.get("evaluator") not in {"patch_tests", "answer_assertions"}:
        raise PatchbenchError(f"Unsupported repository evaluator in {path}")
    checkout = raw.get("checkout")
    if not isinstance(checkout, dict) or not all(
        isinstance(checkout.get(key), str) and checkout[key] for key in ("env", "default")
    ):
        raise PatchbenchError(f"Repository checkout requires env and default: {path}")
    required = ("image", "containerfile") if raw["evaluator"] == "patch_tests" else ("task_manifest", "fixtures")
    for key in (*required, "solver_image"):
        if key in required or key in raw:
            if not isinstance(raw.get(key), str) or not raw[key].strip():
                raise PatchbenchError(f"Repository requires a non-empty {key}: {path}")
    if raw["evaluator"] == "answer_assertions":
        policy = raw.get("answer_policy")
        if not isinstance(policy, dict):
            raise PatchbenchError(f"Repository requires answer_policy: {path}")
        for key in ("mcp_server", "read_tool", "operation_argument"):
            if not isinstance(policy.get(key), str) or not policy[key].strip():
                raise PatchbenchError(f"Answer policy requires {key}: {path}")
        for key in ("discovery_tools", "forbidden_tool_markers", "allowed_http_methods", "forbidden_prompt_markers"):
            values = policy.get(key)
            if not isinstance(values, list) or not values or not all(isinstance(item, str) and item for item in values):
                raise PatchbenchError(f"Answer policy requires a non-empty list of {key}: {path}")
        if policy.get("call_limit_mode") != "diagnostic":
            raise PatchbenchError("Only diagnostic tool-call limits are currently supported")
        if any(method not in {"GET", "HEAD", "OPTIONS"} for method in policy["allowed_http_methods"]):
            raise PatchbenchError("Answer policy permits only read-only HTTP methods")
    source = raw.get("source")
    if source is not None:
        if not isinstance(source, dict):
            raise PatchbenchError(f"Repository source must be a mapping: {path}")
        for key in ("gitlab_url", "project_id", "git_url"):
            if key in source and (not isinstance(source[key], str) or not source[key].strip()):
                raise PatchbenchError(f"Repository source {key} must be a non-empty string: {path}")
    return RepositoryManifest(identifier, path, raw)
