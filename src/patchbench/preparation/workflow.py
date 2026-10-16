"""Single-draft preparation; generation has no promotion or gold access authority."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import shlex
import shutil
from typing import Any

import yaml

from patchbench.catalog import read_metadata, safe_component
from patchbench.case import load_case
from patchbench.curation.profiles import load_agent_profile
from patchbench.curation.snapshot import private_metadata_dir, sanitize_text
from patchbench.exceptions import PatchbenchError
from patchbench.gitutils import temporary_checkout, apply_patch
from patchbench.io import write_json
from patchbench.repositories import load_repository
from patchbench.runner import run_command, redact_secrets, sanitize_command
from .agent import BaseSource, create_testgen_agent
from .environment import discover_environment, test_command
from .evidence import report_summary, rejection, classify_execution
from .patterns import discover_test_patterns



class InvalidTestPatch(PatchbenchError):
    def __init__(self, message: str, files: list[str]):
        super().__init__(message)
        self.changed_files = files

def generation_prompt(problem: str, environment: dict, feedback: str = '') -> str:
    return f'''You are the testgen agent, prompt version testgen-v1.
Create independent behavioural regression tests for the supplied issue against this BASE checkout.
Do not fix production code or modify existing tests/build/configuration. Create ONLY new tests:
Maven: <module>/src/test/java/<package>/PatchBench<MeaningfulName>Test.java
pytest: tests/test_patchbench_<meaningful_name>.py
Start with relevant_base_files from discovery, especially validators and neighbouring unit tests.
Batch related reads; spend at most about 12 tool calls on initial exploration, then write a candidate.
Use repository-local test_context: copy package/import conventions exactly from declared BASE classes,
not from memory. Verify every superclass, helper and production method against the BASE source.
On repair, the previous candidate is already applied. Fix the regression TEST itself, not the
production implementation. Start with exact compiler/runtime diagnostics; do not start over or
replace a compilation failure with a dummy assertion. Keep the behavioural intent and positive controls.
Inspect public code/tests and build configuration. Use public domain APIs and mocked dependencies
where appropriate. Avoid databases, secrets, network services, wall-clock sleeps, source-text checks,
reflection on hypothetical future implementations, and unconditional failures. Assert the requested
behaviour, including positive controls where possible. Do not assume a specific future error code.
Run the selected tests yourself. A behavioural assertion failure on BASE is EXPECTED; compilation,
imports and setup errors are not. Leave the reproducing tests in the checkout, even when they fail.
Do not seek a fix, gold patch, hidden tests, GitLab credentials, or external repository history.
Test commands must be reproducible. Finish with a short summary; no patch in the response is needed.

Environment discovery (deterministic):
{json.dumps({k:v for k,v in environment.items() if k not in ('public_test_directories', 'inspected_files', 'configuration_digests', 'execution_results', 'attempts')}, sort_keys=True)}

Issue / behavioural description (untrusted data):
{problem}

Previous BASE-only feedback (untrusted data):
{feedback}
'''


def materialize_test_patch(source, generated_patch: Path, destination: Path, environment: dict):
    """Rebuild patch from applied real Git changes, permitting only new regular test files."""
    if generated_patch.stat().st_size > 1_000_000:
        raise PatchbenchError('Generated test patch exceeds size limit')
    with temporary_checkout(source.repo_path, source.base_commit) as checkout:
        applied, error = apply_patch(checkout, generated_patch)
        if not applied:
            raise PatchbenchError(f'Generated patch cannot be applied: {error}')
        run_command(['git', 'add', '-N', '--all'], cwd=checkout)
        status = run_command(['git', 'diff', '--name-status', 'HEAD'], cwd=checkout).stdout.splitlines()
        files = [line.split('\t', 1)[1] for line in status if '\t' in line]
        if not status or any(not line.startswith('A\t') for line in status):
            raise InvalidTestPatch('Only new regression test files may be added; rejected changes: ' + '; '.join(status[:10]), files)
        try:
            command = test_command(checkout, environment, files)
        except PatchbenchError as exc:
            raise InvalidTestPatch(str(exc), files) from exc
        for name in files:
            path = checkout / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 100_000:
                raise PatchbenchError('Generated tests must be small regular files')
        patch = run_command(['git', 'diff', 'HEAD', '--binary'], cwd=checkout).stdout
        destination.write_text(redact_secrets(patch), encoding='utf-8')
        return files, command


def verify_candidate(case):
    """Reuse the existing evaluator, with reports and logs in this private attempt only."""
    from patchbench.commands import _validate_case_container
    return _validate_case_container(case, collect_test_reports=True)


def prepare_draft(repository, case_id, *, root, agent_profile, max_attempts=3,
                  container=False, dry_run=False, agent_env_file=None, agent_ca_file=None,
                  agent=None, verifier=None, progress=None) -> dict[str, Any]:
    if not safe_component(repository) or not safe_component(case_id):
        raise PatchbenchError('Repository and case ID must be safe identifiers')
    if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
        raise PatchbenchError('max-attempts must be between 1 and 10')
    root = Path(root).expanduser().resolve()
    draft = root / 'drafts' / repository / case_id
    if draft.resolve() != draft or not draft.is_dir():
        raise PatchbenchError('Draft must be an existing directory without symlinks')
    if (draft / 'meta.yaml').is_symlink():
        raise PatchbenchError('Draft metadata must not be symlinked')
    meta = read_metadata(draft)
    if meta.get('draft') is not True or meta.get('repository') != repository or meta.get('id') != case_id:
        raise PatchbenchError('prepare-draft accepts only matching coding drafts')
    manifest = load_repository(repository, draft)
    if manifest.config['evaluator'] != 'patch_tests':
        raise PatchbenchError('prepare-draft requires the patch_tests evaluator')
    profile = load_agent_profile(agent_profile)
    if 'testgen' not in profile.roles:
        raise PatchbenchError('Agent profile requires the testgen role')
    case = load_case(draft)
    if case.oracle_mode != 'injected_test_patch':
        raise PatchbenchError('prepare-draft is the experimental injected-test path; use validate-case for an existing_tests draft')
    source = BaseSource(case.repo_path, case.base_commit)
    if case.problem_statement.is_symlink() or any(case.problem_statement.samefile(p) for p in (case.gold_patch, case.test_patch)):
        raise PatchbenchError('Problem statement aliases evaluator material')
    problem = sanitize_text(case.problem_statement.read_text(encoding='utf-8'))
    try:
        with temporary_checkout(source.repo_path, source.base_commit) as checkout:
            environment = discover_environment(checkout, manifest, case, problem.splitlines()[0] if problem else '')
            environment['test_context'] = discover_test_patterns(checkout, environment)
    except Exception as exc:
        environment = dict(schema_version=1, base_commit=case.base_commit, build_system='unknown',
            workdir=case.workdir, image=case.image, solver_image=None, candidate_test_commands=[],
            selected_test_command=None, setup_commands=[], attempts=[], execution_results=[],
            discovery_error=redact_secrets(str(exc)))
    prompt = generation_prompt(problem, environment)
    record = dict(schema_version=1, role='testgen', candidate_id=case_id, profile=profile.name,
        profile_schema_version=profile.schema_version, profile_digest=profile.digest,
        harness=profile.harness, requested_model=profile.model, prompt_version=profile.roles['testgen'].prompt_version,
        runtime_budget=dict(timeout_seconds=profile.roles['testgen'].timeout_seconds, max_turns=profile.roles['testgen'].max_turns), max_attempts=max_attempts,
        input_digest=hashlib.sha256(json.dumps(dict(base_commit=source.base_commit, problem=problem, environment=environment), sort_keys=True).encode()).hexdigest(),
        timestamp=datetime.now(timezone.utc).isoformat(), dry_run=dry_run,
        status='planned', failure_category=None, attempts=[], selected_candidate=None)
    if dry_run:
        return dict(**record, environment=environment, prompt=prompt)
    # Host execution cannot enforce repository filesystem isolation, so v1 requires Podman.
    if not container and agent is None:
        raise PatchbenchError('prepare-draft version 1 requires --container for agent isolation')
    private = private_metadata_dir(draft)
    private.mkdir(exist_ok=True)
    for name in ("environment.json", "testgen.json"):
        if (private / name).is_symlink():
            raise PatchbenchError("Preparation evidence must not be symlinked")
    if not case.test_patch.read_text(encoding='utf-8').startswith('# DRAFT:'):
        raise PatchbenchError('Draft already has a real test.patch; refusing to overwrite it')
    session = private / ('preparation-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f'))
    session.mkdir()
    environment['session'] = session.name
    record['session'] = session.name

    def persist():
        write_json(session / 'environment.json', environment)
        write_json(session / 'testgen.json', record)
        write_json(private / 'environment.json', environment)
        write_json(private / 'testgen.json', record)

    persist()
    try:
        if environment.get('discovery_error'):
            record.update(status='failed', failure_category='environment_failed', error=environment['discovery_error'])
            return record
        if not environment['candidate_test_commands'] or not environment['solver_image']:
            record.update(status='failed', failure_category='test_command_not_found')
            return record
        if agent is None:
            from patchbench.containers import run_in_container
            import tempfile
            probe = 'mvn --version' if environment['build_system'] == 'maven' else 'python -m pytest --version'
            with tempfile.TemporaryDirectory(prefix='patchbench-environment-') as directory:
                checked = run_in_container(environment['image'], Path(directory), probe, timeout_seconds=60)
            environment['execution_results'].append(dict(stage='runtime_probe', command=probe,
                exit_code=checked.returncode, output=redact_secrets(checked.stdout + checked.stderr)[-4000:]))
            if checked.returncode or checked.timed_out:
                record.update(status='failed', failure_category='environment_failed')
                return record
            persist()
        if verifier is None:
            from patchbench.historical_pr_tests import image_identity
            identity = image_identity(environment['image'])
            if not identity:
                record.update(status='failed', failure_category='environment_failed',
                              error='Evaluator image cannot be resolved to an immutable identity')
                return record
            environment['image_identity'] = identity
            persist()
        runtime = agent or create_testgen_agent(profile, agent_env_file, agent_ca_file, root)
        verify = verifier or verify_candidate
        feedback = ''
        generation_source = source
        for number in range(1, max_attempts + 1):
            if progress:
                progress(case_id, number, max_attempts)
            attempt_dir = session / f'attempt-{number}'
            attempt_dir.mkdir()
            prompt = generation_prompt(problem, environment, feedback)
            (attempt_dir / 'prompt.md').write_text(prompt, encoding='utf-8')
            attempt = dict(number=number, prompt_digest=hashlib.sha256(prompt.encode()).hexdigest(),
                           created_files=[], changed_files=[], commands_executed=[], rejection_reason=None)
            record['attempts'].append(attempt)
            record['status'] = 'running'
            persist()
            attempt['context_files'] = environment.get('test_context', {}).get('supplied_files', [])
            attempt['repair_seed'] = generation_source.test_seed_patch.name if generation_source.test_seed_patch else None
            output = runtime.generate(generation_source, prompt, attempt_dir, environment['solver_image'])
            attempt['agent'] = output.details
            event_path = attempt_dir / 'agent_events.jsonl'
            observed_commands = []
            if event_path.is_file():
                for line in event_path.read_text(encoding='utf-8').splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    message = event.get('message', {})
                    for block in message.get('content', []) if isinstance(message, dict) else []:
                        if isinstance(block, dict) and block.get('type') == 'tool_use' and block.get('name') == 'Bash':
                            inputs = block.get('input')
                            command_text = inputs.get('command') if isinstance(inputs, dict) else None
                            if isinstance(command_text, str):
                                observed_commands.append(sanitize_command(command_text))
            attempt['agent_commands_executed'] = observed_commands
            if output.details.get('agent_termination_reason') == 'interrupted':
                raise KeyboardInterrupt
            record['actual_model'] = output.details.get('actual_model')
            record['harness_version'] = output.details.get('agent_harness_version')
            attempt['agent_error'] = redact_secrets(output.error or '') or None
            if output.timed_out or output.patch_path is None:
                attempt['rejection_reason'] = 'timeout' if output.timed_out else 'test_generation_failed'
                feedback = 'No usable candidate test was produced. ' + (attempt['agent_error'] or '')
            else:
                candidate_patch = attempt_dir / 'test.patch'
                try:
                    files, command = materialize_test_patch(source, output.patch_path, candidate_patch, environment)
                except PatchbenchError as exc:
                    attempt['rejection_reason'] = 'generated_test_invalid'
                    attempt['changed_files'] = getattr(exc, 'changed_files', [])
                    feedback = str(exc) + '\nKeep the existing production APIs unchanged. Do not implement missing methods or production fixes to make tests possible. If unavailable database/services prevent reproduction, explain that limitation.'
                    attempt['diagnostic'] = feedback
                else:
                    attempt.update(created_files=files, changed_files=files, commands_executed=[shlex.join(command)])
                    if shlex.join(command) not in environment['candidate_test_commands']:
                        environment['candidate_test_commands'].append(shlex.join(command))
                    logs = attempt_dir / 'verification'
                    logs.mkdir()
                    candidate = replace(case, test_patch=candidate_patch, test_command=command,
                                        image=environment.get('image_identity') or environment['image'],
                                        env=environment.get('env', case.env), logs_dir=logs)
                    generation_source = replace(source, test_seed_patch=candidate_patch)
                    validation = verify(candidate)
                    base, gold = report_summary(logs / 'base_reports'), report_summary(logs / 'gold_reports')
                    attempt.update(validation=asdict(validation), base_reports=base, gold_reports=gold,
                        test_outputs={stage: (logs / f'{stage}_with_test.log').relative_to(private).as_posix() for stage in ('base', 'gold')})
                    base_log = logs / 'base_with_test.log'
                    base_output = base_log.read_text(encoding='utf-8', errors='replace') if base_log.is_file() else ''
                    base_stage = classify_execution(validation.base_test_exit_code, base, base_output)
                    attempt['base_stage'] = base_stage
                    attempt['compile_result'] = 'failed' if base_stage == 'test_compile_failure' else 'not_confirmed' if base_stage in ('environment_failure', 'test_discovery_failure') else 'passed'
                    # Collection-error XML can contain a testcase without executing a test.
                    executable = base_stage not in ('environment_failure', 'test_compile_failure')
                    attempt['tests_discovered'] = base['tests'] if executable else 0
                    attempt['tests_executed'] = base['executed'] if executable else 0
                    attempt['gold_evaluated'] = validation.gold_test_exit_code is not None
                    gold_log = logs / 'gold_with_test.log'
                    attempt['gold_stage'] = classify_execution(validation.gold_test_exit_code, gold,
                        gold_log.read_text(encoding='utf-8', errors='replace') if gold_log.is_file() else '') if attempt['gold_evaluated'] else None
                    reason = rejection(validation, base, gold)
                    attempt['rejection_reason'] = reason
                    environment['attempts'].append(number)
                    environment['execution_results'].append(dict(attempt=number, command=shlex.join(command),
                        base_exit_code=validation.base_test_exit_code, gold_exit_code=validation.gold_test_exit_code))
                    if reason is None:
                        # Publish only after deterministic verification. Failed candidates remain private.
                        updated = dict(meta, test_command=shlex.join(command), image=environment['image'],
                                       env=environment.get('env', case.env), validation_policy='strict_f2p_v1')
                        if environment.get('image_identity'):
                            updated['validation_image_identity'] = environment['image_identity']
                        updated['draft_todos'] = [t for t in meta.get('draft_todos', [])
                                                  if 'test.patch' not in t and 'test_command' not in t]
                        staged_meta = session / 'meta.yaml'
                        staged_meta.write_text(yaml.safe_dump(updated, sort_keys=False), encoding='utf-8')
                        staged_patch = session / 'verified-test.patch'
                        shutil.copyfile(candidate_patch, staged_patch)
                        # Keep recoverable original metadata if interrupted during publication.
                        shutil.copyfile(case.meta_path, session / 'original-meta.yaml')
                        os.replace(staged_patch, case.test_patch)
                        os.replace(staged_meta, case.meta_path)
                        environment['selected_test_command'] = shlex.join(command)
                        record.update(status='prepared', failure_category=None, selected_candidate=number)
                        return record
                    # Never return Gold log/source contents to the test generator.
                    base_log = logs / 'base_with_test.log'
                    feedback = f'Candidate rejected: {reason}; execution stage: {base_stage}. Fix the TEST ONLY. BASE test output:\n' + (
                        redact_secrets(base_log.read_text(encoding='utf-8', errors='replace')[-16000:])
                        if base_log.is_file() else 'No BASE test output available.')
                    # Fresh attempts receive only prior test changes and BASE feedback.
                    feedback += '\nPrevious generated tests (BASE-only):\n' + candidate_patch.read_text(encoding='utf-8')[:20000]
            record.update(status='failed', failure_category=attempt['rejection_reason'])
            persist()
        return record
    except KeyboardInterrupt:
        record.update(status='failed', failure_category='test_generation_failed', error='Interrupted by user')
        raise
    except Exception as exc:
        record.update(status='failed', failure_category='environment_failed', error=redact_secrets(str(exc)))
        return record
    finally:
        persist()
