"""Advisory triage over existing drafts, with isolated failures and atomic evidence."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from patchbench.catalog import safe_component
from patchbench.curation.models import AgentRequest, AgentResponse, AgentResult, StructuredAgent, TriageSummary, ValidationDiagnostic
from patchbench.curation.profiles import AgentProfile, load_agent_profile
from patchbench.curation.runtime import create_curation_agent
from patchbench.curation.snapshot import build_candidate_snapshot, input_digest, private_metadata_dir, sanitize_text
from patchbench.curation.triage import SCORE_NAMES, TriageDecision, TriageValidationError, parse_triage_output, triage_prompt, triage_output_schema
from patchbench.exceptions import PatchbenchError
from patchbench.io import write_json
from patchbench.repositories import load_repository


@dataclass(frozen=True)
class CurationReport:
    schema_version: int
    repository: str
    profile: str
    profile_digest: str
    dry_run: bool
    results: tuple[AgentResult, ...]
    previews: tuple[dict[str, Any], ...]
    failures: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _observed_text(details: dict[str, Any], name: str) -> str | None:
    value = details.get(name)
    return sanitize_text(value) if isinstance(value, str) and value else None


def _observed_number(details: dict[str, Any], name: str, *, integer: bool = False) -> Any:
    value = details.get(name)
    if type(value) not in ((int,) if integer else (int, float)):
        return None
    try:
        return value if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def _result(
    profile: AgentProfile, candidate_id: str, snapshot: dict[str, Any] | None,
    prompt: str | None, response: AgentResponse | None, elapsed: float,
    decision: TriageDecision | None, status: str, error: str | None,
    validation_diagnostics: list[ValidationDiagnostic],
) -> AgentResult:
    config = profile.roles["triage"]
    details = response.details if response and isinstance(response.details, dict) else {}
    decision = decision or TriageDecision("abstain", None, {key: None for key in SCORE_NAMES}, [], [status], "manual_review")
    reason = error or (
        " ".join(dict.fromkeys(entry["reason"] for entry in decision.evidence))
        or "The agent abstained without supplying supporting evidence."
    )
    return AgentResult(
        schema_version=1, role="triage", profile=profile.name,
        profile_schema_version=profile.schema_version, profile_digest=profile.digest,
        harness=profile.harness, harness_version=_observed_text(details, "agent_harness_version"),
        requested_model=profile.model, actual_model=_observed_text(details, "actual_model"),
        provider=_observed_text(details, "provider"), provider_base_url_host=_observed_text(details, "provider_base_url_host"),
        prompt_version=config.prompt_version,
        prompt_digest=hashlib.sha256(prompt.encode()).hexdigest() if prompt else None,
        runtime_budget={"timeout_seconds": config.timeout_seconds, "max_turns": config.max_turns},
        candidate_id=candidate_id, recommendation=decision.recommendation, confidence=decision.confidence,
        scores=decision.scores, evidence=decision.evidence, risk_flags=decision.risk_flags,
        next_action=decision.next_action, status=status, error=error, runtime_seconds=elapsed,
        input_tokens=_observed_number(details, "input_tokens", integer=True),
        output_tokens=_observed_number(details, "output_tokens", integer=True),
        reported_turns=_observed_number(details, "agent_num_turns", integer=True),
        reported_cost_usd=_observed_number(details, "reported_cost_usd"),
        termination_reason=_observed_text(details, "agent_termination_reason"),
        input_digest=input_digest(snapshot) if snapshot else None,
        timestamp=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        output_schema_digest=input_digest(triage_output_schema(snapshot)) if snapshot else None,
        output_format=_observed_text(details, "output_format"),
        validation_diagnostics=validation_diagnostics,
        summary=TriageSummary(
            recommendation=decision.recommendation, confidence=decision.confidence,
            issue_clarity=decision.scores["issue_clarity"],
            behavioral_testability=decision.scores["behavioral_testability"],
            reason=reason[:2000],
        ),
    )


def _discovery_features(project: Path, repository: str) -> dict[int, dict[str, Any]]:
    path = project / "artifacts" / "imports" / repository / "candidates.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return {}
    return {item["merge_request_iid"]: item for item in raw
            if isinstance(item, dict) and type(item.get("merge_request_iid")) is int} if isinstance(raw, list) else {}


def curate_drafts(
    repository: str, *, root: str | Path, agent_profile: str | Path,
    agent_env_file: str | Path | None = None, dry_run: bool = False,
    agent_ca_file: str | Path | None = None,
    agent: StructuredAgent | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> CurationReport:
    """Only write private triage records; never invoke promotion or validation."""
    project = Path(root).expanduser().resolve()
    manifest = load_repository(repository, project / "cases")
    if manifest.config["evaluator"] != "patch_tests":
        raise PatchbenchError("Triage version 1 supports coding repositories")
    profile = load_agent_profile(agent_profile)
    if "triage" not in profile.roles:
        raise PatchbenchError("Agent profile requires the triage role")
    draft_root = project / "drafts" / repository
    if (project / "drafts").is_symlink() or draft_root.is_symlink():
        raise PatchbenchError("Draft catalog must not be a symlink")
    drafts = sorted(path for path in draft_root.glob("*") if path.is_dir())
    runtime = agent
    if not dry_run and drafts and runtime is None:
        runtime = create_curation_agent(profile, agent_env_file, project, agent_ca_file)
    results: list[AgentResult] = []
    previews: list[dict[str, Any]] = []
    failures: dict[str, str] = {}
    features = _discovery_features(project, repository)
    for index, draft in enumerate(drafts, start=1):
        if not safe_component(draft.name) or draft.is_symlink():
            failures[draft.name] = "Draft must be a local directory with a safe name"
            continue
        if progress is not None:
            progress(draft.name, index, len(drafts))
        started = time.monotonic()
        snapshot = None
        prompt = None
        response = None
        decision = None
        status = "input_error"
        error = None
        validation_diagnostics: list[ValidationDiagnostic] = []
        candidate_id = draft.name
        try:
            # Old drafts can reuse deterministic discovery features without GitLab/Git.
            snapshot = build_candidate_snapshot(draft, repository, features)
            candidate_id = snapshot["candidate_id"]
            prompt = triage_prompt(snapshot, profile.roles["triage"].prompt_version)
            output_schema = triage_output_schema(snapshot)
            if dry_run:
                previews.append({"candidate_id": candidate_id, "snapshot": snapshot,
                                 "input_digest": input_digest(snapshot), "prompt": prompt,
                                 "profile": asdict(profile), "output_schema": output_schema,
                                 "output_schema_digest": input_digest(output_schema)})
                continue
            assert runtime is not None
            status = "agent_error"
            response = runtime.run(AgentRequest("triage", profile, prompt, output_schema))
            if not isinstance(response, AgentResponse):
                response = None
                raise PatchbenchError("Structured agent returned an invalid response")
            if response.error:
                error = sanitize_text(response.error)
            else:
                status = "invalid_output"
                decision = parse_triage_output(response.text, snapshot)
                status = "completed"
        except Exception as exc:
            if isinstance(exc, TriageValidationError) and status == "invalid_output":
                validation_diagnostics.append(exc.diagnostic)
            # Validation errors carry safe context. Never persist raw output or arbitrary
            # exception messages that could contain credentials or private file contents.
            error = str(exc) if isinstance(exc, PatchbenchError) and status == "invalid_output" else f"Curation {status} ({type(exc).__name__})"
        if error is not None:
            failures[candidate_id] = error
        if dry_run:
            continue
        result = _result(profile, candidate_id, snapshot, prompt, response,
                         time.monotonic() - started, decision, status, error, validation_diagnostics)
        results.append(result)
        try:
            private = private_metadata_dir(draft)
            private.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_json(private / "triage.json", result.to_dict())
        except (OSError, PatchbenchError):
            failures[candidate_id] = "Could not persist local triage evidence"
    return CurationReport(1, repository, profile.name, profile.digest, dry_run,
                          tuple(results), tuple(previews), failures)
