"""Agent interface and implementations."""

from patchbench.agents.base import AgentOutput, BaseAgent, FailureMode
from patchbench.agents.claude_code import ClaudeCodeAgent
from patchbench.agents.container_command import ContainerCommandAgent
from patchbench.agents.dummy import DummyAgent
from patchbench.agents.gold import GoldPatchAgent
from patchbench.models import RunResult

__all__ = [
    "AgentOutput",
    "BaseAgent",
    "ClaudeCodeAgent",
    "ContainerCommandAgent",
    "DummyAgent",
    "FailureMode",
    "GoldPatchAgent",
    "RunResult",
]

# Agent registry for CLI and run_agent
AGENT_REGISTRY = {
    "dummy": DummyAgent,
    "gold": GoldPatchAgent,
    "claude-code": ClaudeCodeAgent,
    "container-command": ContainerCommandAgent,
}
