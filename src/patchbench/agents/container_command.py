"""Minimal proof agent executed by the generic container solver."""

from __future__ import annotations

from pathlib import Path

from patchbench.agents.base import AgentOutput


class ContainerCommandAgent:
    """Container-only agent configured by an image and shell command."""

    name = "container-command"
    supports_host_execution = False
    supports_container_execution = True
    model = "unknown"
    model_source = "not applicable; configured command"
    image = "patchbench-rucio-historical:tests-v2"
    command = "true"

    def run(
        self,
        case_dir: Path,
        solving_worktree: Path,
        base_commit: str,
        prompt_mode: str,
        prompt: str | None = None,
    ) -> AgentOutput:
        raise RuntimeError("container-command must use the container solver executor")
