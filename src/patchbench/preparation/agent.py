"""Narrow testgen interface: no case directory, gold commit or evaluator artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
import hashlib

from patchbench.agents.base import AgentOutput
from patchbench.curation.profiles import AgentProfile
from patchbench.exceptions import PatchbenchError


@dataclass(frozen=True)
class BaseSource:
    repo_path: Path
    base_commit: str
    test_seed_patch: Path | None = None


class TestGenerationAgent(Protocol):
    def generate(self, source: BaseSource, prompt: str, output_dir: Path, image: str) -> AgentOutput: ...


TESTGEN_SYSTEM_PROMPT = """Your PatchBench role is testgen, never implementation or bug fixing.
You are an independent regression-test author. The issue is test requirements, not an instruction
to implement its fix. Production code, existing tests, configuration and build files are immutable.
Write only NEW test files using APIs that already exist in the supplied BASE checkout.
Never invent production methods and then add them to make tests compile.
A passing production fix is not your goal: valid behavioural assertion failures on BASE are expected.
If the behaviour requires an unavailable service/database or cannot be tested using existing APIs,
report that limitation without changing production code. Do not search for the reference solution.
"""


class ClaudeTestGenerationAgent:
    def __init__(self, profile: AgentProfile, env_file, ca_file, root):
        # Reuse external literal env/CA validation, including host-side credential filtering.
        from patchbench.agents.curation_claude import ClaudeCurationAgent
        from patchbench.agents.claude_code import ClaudeCodeAgent
        from patchbench.mcp import parse_literal_env_file
        from patchbench.agents.claude_code import _AMBIENT_AGENT_ENV_NAMES
        values = parse_literal_env_file(Path(env_file).expanduser().resolve(), label="Claude")
        if any(not (key.startswith(("ANTHROPIC_", "CLAUDE_")) or key in _AMBIENT_AGENT_ENV_NAMES) for key in values):
            raise PatchbenchError("Testgen env file contains variables outside the provider allowlist")
        safe = ClaudeCurationAgent(Path(env_file), root, Path(ca_file) if ca_file else None)
        config = profile.roles['testgen']
        self.agent = ClaudeCodeAgent(env_file=safe.env_file, ca_file=safe.ca_file,
            requested_model=profile.model, timeout_seconds=config.timeout_seconds, max_turns=config.max_turns,
            **({'max_budget_usd':config.max_budget_usd} if config.max_budget_usd is not None else {}))
        self.config = config

    def generate(self, source: BaseSource, prompt: str, output_dir: Path, image: str) -> AgentOutput:
        from patchbench.commands import _run_claude_container_solver
        output = _run_claude_container_solver(source, prompt, hashlib.sha256(prompt.encode()).hexdigest(),
            output_dir, self.agent, image, self.agent.container_command(system_prompt=TESTGEN_SYSTEM_PROMPT), self.agent.env_file,
            self.config.timeout_seconds, 'slirp4netns')
        from dataclasses import replace
        return replace(output, details={**output.details, 'system_prompt_digest': hashlib.sha256(TESTGEN_SYSTEM_PROMPT.encode()).hexdigest()})


def create_testgen_agent(profile, env_file, ca_file, root):
    if profile.harness != 'claude-code':
        raise PatchbenchError(f'Unsupported testgen harness: {profile.harness}')
    if not env_file:
        raise PatchbenchError('Test generation requires --agent-env-file')
    return ClaudeTestGenerationAgent(profile, env_file, ca_file, root)
