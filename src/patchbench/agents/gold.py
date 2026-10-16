"""Gold patch agent - returns the human fix patch for evaluation."""

from __future__ import annotations

from pathlib import Path

from patchbench.agents.base import AgentOutput, BaseAgent
from patchbench.case import load_case


class GoldPatchAgent(BaseAgent):
    """An agent that returns the human fix patch for evaluation."""

    name = "gold"
    supports_host_execution = True
    supports_container_execution = False
    model = "unknown"
    model_source = "not applicable; sanity-check agent"

    def run(
        self,
        case_dir: Path,
        solving_worktree: Path,
        base_commit: str,
        prompt_mode: str,
        prompt: str | None = None,
    ) -> AgentOutput:
        """
        Run the gold patch agent.

        The gold agent is a sanity-check agent that returns the human fix patch.
        It is NOT a normal solving agent - it does not run tests or make decisions.

        Args:
            case_dir: Path to the case directory
            solving_worktree: Path to the solving worktree (unused by gold)
            base_commit: The base commit (unused by gold)
            prompt_mode: The prompt mode (unused by gold)

        Returns:
            AgentOutput with gold.patch path for the evaluator to apply
        """
        case = load_case(case_dir)

        # Return the gold.patch path for the evaluator to apply.
        # The evaluator is responsible for:
        # 1. Creating a worktree at base_commit
        # 2. Applying test.patch (hidden from the agent)
        # 3. Applying agent's patch (gold.patch)
        # 4. Running tests and determining success/failure
        return AgentOutput(
            patch_path=case.gold_patch,
            error=None,
        )
