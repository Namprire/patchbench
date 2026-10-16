"""Credential-free, versioned runtime profiles for curation roles."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from patchbench.catalog import safe_component
from patchbench.exceptions import PatchbenchError


@dataclass(frozen=True)
class RoleConfig:
    prompt_version: str
    timeout_seconds: int
    max_turns: int
    max_budget_usd: float | None = None


@dataclass(frozen=True)
class AgentProfile:
    schema_version: int
    name: str
    harness: str
    model: str
    roles: dict[str, RoleConfig]

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def load_agent_profile(path: str | Path) -> AgentProfile:
    try:
        raw = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise PatchbenchError("Could not read agent profile") from exc
    required = {"schema_version", "name", "harness", "model", "roles"}
    if not isinstance(raw, dict) or set(raw) != required:
        raise PatchbenchError("Agent profile requires exactly schema_version, name, harness, model, roles")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise PatchbenchError("Unsupported agent profile schema_version")
    if not all(safe_component(raw[key]) for key in ("name", "harness")):
        raise PatchbenchError("Profile name and harness must be safe identifiers")
    if not isinstance(raw["model"], str) or not raw["model"].strip() or raw["model"].startswith("-"):
        raise PatchbenchError("Profile requires a model identifier")
    roles = raw["roles"]
    if not isinstance(roles, dict) or not roles or not set(roles) <= {"triage", "testgen", "mirror"}:
        raise PatchbenchError("Profile requires supported roles: triage, testgen or mirror")
    parsed = {}
    for role, config in roles.items():
        if not isinstance(config, dict) or not {"prompt_version", "timeout_seconds", "max_turns"} <= set(config) or set(config) - {"prompt_version", "timeout_seconds", "max_turns", "max_budget_usd"}:
            raise PatchbenchError("Role requires prompt_version, timeout_seconds, max_turns")
        if config["prompt_version"] not in ({"mirror-v1", "mirror-v2"} if role == "mirror" else {"testgen-v1", "testgen-mirror-v1"} if role == "testgen" else {f"{role}-v1"}):
            raise PatchbenchError(f"Unsupported {role} prompt_version")
        for key in ("timeout_seconds", "max_turns"):
            if type(config[key]) is not int or config[key] <= 0:
                raise PatchbenchError(f"Role {key} must be a positive integer")
        ceiling = config.get('max_budget_usd')
        if ceiling is not None and (type(ceiling) not in (int,float) or not math.isfinite(ceiling) or ceiling <= 0):
            raise PatchbenchError('Role max_budget_usd must be a positive finite number')
        parsed[role] = RoleConfig(**config)
    return AgentProfile(1, raw["name"], raw["harness"], raw["model"], parsed)
