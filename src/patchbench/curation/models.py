"""Narrow harness protocol and normalized advisory evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from patchbench.curation.profiles import AgentProfile


@dataclass(frozen=True)
class AgentRequest:
    role: str
    profile: AgentProfile
    prompt: str
    output_schema: dict[str, Any] | None = None


@dataclass(frozen=True)
class AgentResponse:
    text: str
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class StructuredAgent(Protocol):
    def run(self, request: AgentRequest) -> AgentResponse:
        """Return untrusted text and observed execution metadata, without case access."""
        ...


@dataclass(frozen=True)
class ValidationDiagnostic:
    """Bounded, sanitized context for a failed evidence check; never raw output."""

    code: str
    path: str
    field: str | None
    quote_preview: str | None
    supplied_preview: str | None
    quote_truncated: bool = False
    supplied_truncated: bool = False


@dataclass(frozen=True)
class TriageSummary:
    recommendation: str
    confidence: float | None
    issue_clarity: float | None
    behavioral_testability: float | None
    reason: str


@dataclass(frozen=True)
class AgentResult:
    schema_version: int
    role: str
    profile: str
    profile_schema_version: int
    profile_digest: str
    harness: str
    harness_version: str | None
    requested_model: str
    actual_model: str | None
    provider: str | None
    provider_base_url_host: str | None
    prompt_version: str
    prompt_digest: str | None
    runtime_budget: dict[str, int]
    candidate_id: str
    recommendation: str
    confidence: float | None
    scores: dict[str, float | None]
    evidence: list[dict[str, str]]
    risk_flags: list[str]
    next_action: str
    status: str
    error: str | None
    runtime_seconds: float
    input_tokens: int | None
    output_tokens: int | None
    reported_turns: int | None
    reported_cost_usd: float | None
    termination_reason: str | None
    input_digest: str | None
    timestamp: str
    output_schema_digest: str | None = None
    output_format: str | None = None
    validation_diagnostics: list[ValidationDiagnostic] = field(default_factory=list)
    summary: TriageSummary | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
