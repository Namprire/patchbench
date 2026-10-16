"""Base agent interface and data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class AgentOutput:
    """
    Output from an agent - what the agent produces during solving.

    This is the SOLVING output, not the evaluation result.
    The evaluator is responsible for applying this patch and running tests.
    """

    patch_path: Path | None  # Path to the patch the agent produced (None for empty/no patch)
    error: str | None  # Error message if agent failed to produce a patch
    timed_out: bool = False
    details: dict[str, Any] = field(default_factory=dict)


class FailureMode:
    """Failure mode constants for evaluation results."""

    NO_CODE_CHANGE = "no_code_change"
    PATCH_FAILED = "patch_failed"
    TESTS_FAILED = "tests_failed"
    TESTS_PASS = "tests_pass"
    TIMEOUT = "timeout"
    OTHER = "other"


class BaseAgent(Protocol):
    """Interface for all agent implementations."""

    name: str
    supports_host_execution: bool
    supports_container_execution: bool
    model: str | None
    model_source: str | None

    def run(
        self,
        case_dir: Path,
        solving_worktree: Path,
        base_commit: str,
        prompt_mode: str,
        prompt: str | None = None,
    ) -> AgentOutput:
        """
        Run the agent on a case.

        The agent operates on a CLEAN worktree at base_commit.
        It should NOT apply test.patch - that is the evaluator's responsibility.

        Args:
            case_dir: Path to the case directory
            solving_worktree: Path to the solving worktree (clean at base_commit, no test.patch)
            base_commit: The base commit to start from
            prompt_mode: The prompt mode for the agent (e.g., "issue_only")
            prompt: Preconstructed, benchmark-safe prompt text when provided

        Returns:
            AgentOutput with what the agent produced (or error)
        """
        ...
