"""Tool-free structured-agent implementation using the existing Claude process runner."""

from __future__ import annotations

import tempfile
from pathlib import Path

from patchbench.agents.claude_code import ClaudeCodeAgent
from patchbench.curation.models import AgentRequest, AgentResponse
from patchbench.exceptions import PatchbenchError


class ClaudeCurationAgent:
    def __init__(self, env_file: Path, project_root: Path, ca_file: Path | None = None) -> None:
        resolved = env_file.expanduser().resolve()
        package_root = Path(__file__).resolve().parents[3]
        if resolved.is_relative_to(project_root.resolve()) or resolved.is_relative_to(package_root):
            raise PatchbenchError("Agent env file must be outside the PatchBench project")
        if not resolved.is_file():
            raise PatchbenchError(f"Agent env file does not exist: {resolved}. Supply your existing provider file with --agent-env-file.")
        self.env_file = resolved
        self.ca_file = None
        if ca_file is not None:
            from patchbench.commands import _validate_provider_ca_file

            self.ca_file = _validate_provider_ca_file(ca_file)[0]
        # Validate common configuration once before creating per-candidate evidence.
        # This never starts Claude or exposes credential values in diagnostics.
        with tempfile.TemporaryDirectory(prefix="patchbench-curation-preflight-") as directory:
            environment = ClaudeCodeAgent(env_file=resolved, ca_file=self.ca_file)._agent_environment(
                isolated_home=Path(directory),
            )
            if not (environment.get("ANTHROPIC_AUTH_TOKEN") or environment.get("ANTHROPIC_API_KEY")):
                raise PatchbenchError("Agent env file requires a nonempty provider credential")

    def run(self, request: AgentRequest) -> AgentResponse:
        config = request.profile.roles[request.role]
        agent = ClaudeCodeAgent(
            env_file=self.env_file, requested_model=request.profile.model,
            timeout_seconds=config.timeout_seconds, max_turns=config.max_turns,
            ca_file=self.ca_file,
            **({'max_budget_usd':config.max_budget_usd} if config.max_budget_usd is not None else {}),
        )
        # No draft/source paths are passed to Claude. HOME and settings are isolated;
        # built-in tools and ambient MCP are disabled by the shared prompt runner.
        with tempfile.TemporaryDirectory(prefix="patchbench-curation-") as directory:
            output = agent.run_prompt(request.prompt, Path(directory), isolated=True, output_schema=request.output_schema)
        error = output.error
        # The CLI's StructuredOutput serializer has no filesystem/network authority.
        # All other tool activity remains forbidden, including MCP and built-ins.
        serializers = 0
        if request.output_schema is not None:
            for event in output.events:
                message = event.get("message")
                content = event.get("content")
                if content is None and isinstance(message, dict):
                    content = message.get("content")
                if not isinstance(content, list):
                    content = [event] if event.get("type") in {"tool_use", "tool"} else []
                serializers += sum(1 for block in content if isinstance(block, dict)
                                   and block.get("type") in {"tool_use", "tool"}
                                   and (block.get("name") or block.get("tool")) == "StructuredOutput")
        if output.details.get("agent_tool_event_count", 0) > serializers:
            error = "Unexpected tool activity in a tool-free curation invocation"
        if output.details.get("agent_termination_reason") == "interrupted":
            error = "Curation invocation was interrupted"
        return AgentResponse(output.final_response, error, output.details)
