"""Harness selection boundary; triage business logic only sees StructuredAgent."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from patchbench.curation.models import StructuredAgent
from patchbench.curation.profiles import AgentProfile
from patchbench.exceptions import PatchbenchError


def _claude(env_file: Path, root: Path, ca_file: Path | None = None) -> StructuredAgent:
    from patchbench.agents.curation_claude import ClaudeCurationAgent

    return ClaudeCurationAgent(env_file, root, ca_file)


HARNESS_FACTORIES: dict[str, Callable[[Path, Path, Path | None], StructuredAgent]] = {"claude-code": _claude}


def create_curation_agent(profile: AgentProfile, env_file: str | Path | None, root: Path, ca_file: str | Path | None = None) -> StructuredAgent:
    factory = HARNESS_FACTORIES.get(profile.harness)
    if factory is None:
        raise PatchbenchError("Unsupported curation harness")
    if env_file is None:
        raise PatchbenchError("Curation requires --agent-env-file to an external provider environment file")
    return factory(Path(env_file), root, Path(ca_file) if ca_file else None)
