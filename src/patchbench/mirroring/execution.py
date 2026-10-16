"""Shared evaluator runner with an offline, credential-free verification phase."""
from pathlib import Path
import shutil
from patchbench.runner import run_command
import shlex
import re
from patchbench.containers import run_in_container
from patchbench.io import write_json
from patchbench.runner import redact_secrets
from .reports import read_reports


def bounded_output(output: str) -> str:
    """Keep the root diagnostic and the final summary; never only the cascade tail."""
    if len(output) <= 24000:
        return output
    error = re.search(r'^\[ERROR\]|^.*COMPILATION ERROR', output, re.M)
    diagnostic = output[error.start():error.start()+10000] if error else ''
    return (output[:4000] + '\n[... omitted build output ...]\n' + diagnostic
            + '\n[... final output ...]\n' + output[-8000:])


def first_diagnostics(output: str) -> str:
    error = re.search(r'^\[ERROR\]|^.*COMPILATION ERROR', output, re.M)
    return output[error.start():error.start()+10000] if error else output[-10000:]


def execute(checkout: Path, environment: dict, destination: Path, *, setup=False) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    # Fresh report inventory for every invocation, including setup vs offline verification.
    for p in list(checkout.rglob('TEST-*.xml')) + list(checkout.rglob('patchbench-junit.xml')):
        if p.is_file():
            p.unlink()
    commands = environment['setup_commands'] if setup else [environment['test_command']]
    script = 'set -e\n' + '\n'.join(shlex.join(c) for c in commands)
    mounts = []
    cache = environment.get('dependency_cache')
    if cache:
        mounts.append((Path(cache) / 'repository', '/root/.m2/repository' + ('' if setup else ':ro')))
    result = run_in_container(environment.get('image_identity') or environment['image'], checkout, script,
        workdir=environment.get('workdir'), additional_mounts=mounts,
        insecure_mounts=True, env_overrides=environment.get('env', {}), network='slirp4netns' if setup else 'none',
        timeout_seconds=environment.get('timeout_seconds', 900))
    output = redact_secrets(result.stdout + '\n' + result.stderr)
    (destination / 'execution.log').write_text(output)
    report = read_reports(checkout)
    junit = checkout / 'patchbench-junit.xml'
    if junit.is_file() and not junit.is_symlink():
        shutil.copyfile(junit, destination / 'patchbench-junit.xml')
        # Evaluator output is not a production edit. Only remove the reserved
        # generated report when it is untracked; preserve tracked input files.
        tracked = run_command(['git', 'ls-files', '--error-unmatch', '--', 'patchbench-junit.xml'], cwd=checkout)
        if tracked.exit_code != 0:
            junit.unlink()
    record = dict(exit_code=result.returncode, runtime_seconds=result.duration_seconds,
                  network_required=setup, report=report, output=bounded_output(output))
    write_json(destination / 'execution.json', record)
    return record
