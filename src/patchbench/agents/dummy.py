"""Dummy agent that produces no code changes."""

from __future__ import annotations

from pathlib import Path

from patchbench.agents.base import AgentOutput, BaseAgent


class DummyAgent(BaseAgent):
    """An agent that produces no code changes."""

    name = "dummy"
    supports_host_execution = True
    supports_container_execution = False
    model = "unknown"
    model_source = "not applicable"

    def run(
        self,
        case_dir: Path,
        solving_worktree: Path,
        base_commit: str,
        prompt_mode: str,
        prompt: str | None = None,
    ) -> AgentOutput:
        """
        Run the dummy agent.

        The dummy agent produces no patch - it indicates failure without
        making any code changes. The evaluator will handle the result.

        Args:
            case_dir: Path to the case directory
            solving_worktree: Path to the solving worktree (unused by dummy)
            base_commit: The base commit (unused by dummy)
            prompt_mode: The prompt mode (unused by dummy)

        Returns:
            AgentOutput with no patch (patch_path=None)
        """
        return AgentOutput(
            patch_path=None,
            error="No code changes were made by the agent",
        )
