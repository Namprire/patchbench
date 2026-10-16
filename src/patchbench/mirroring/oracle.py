"""Hidden test injection for mirrored cases; never used in solver workspaces."""
from pathlib import Path, PurePosixPath
from patchbench.exceptions import PatchbenchError
from patchbench.gitutils import apply_patch
from patchbench.runner import run_command


def test_path(name, modules):
    p = PurePosixPath(name)
    return (not p.is_absolute() and '..' not in p.parts and '\\' not in name
            and ((any(name.startswith(m + '/src/test/java/') for m in modules) and p.name.endswith('Test.java'))
                 or ('lib/rucio' in modules and len(p.parts) == 2 and p.parts[0] == 'tests'
                     and p.name.startswith('test_patchbench_') and p.name.endswith('.py'))))


def inject_oracle(checkout, patch, modules):
    """Require one NEW Java test; commit locally so production checks remain strict."""
    applied, error = apply_patch(checkout, patch)
    if not applied:
        raise PatchbenchError('generated_test_invalid: ' + error)
    result = run_command(['git', 'status', '--porcelain', '--untracked-files=all'], cwd=checkout)
    rows = result.stdout.splitlines()
    if len(rows) != 1 or not rows[0].startswith('?? ') or not test_path(rows[0][3:], modules):
        raise PatchbenchError('generated_test_invalid: oracle must add exactly one new test')
    name = rows[0][3:]
    path = checkout / name
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(checkout.resolve()):
        raise PatchbenchError('generated_test_invalid: unsafe test file')
    run_command(['git', 'add', '--', name], cwd=checkout)
    committed = run_command(['git', '-c', 'user.name=PatchBench', '-c', 'user.email=patchbench@localhost',
                             'commit', '-m', 'Evaluator-only targeted regression oracle'], cwd=checkout)
    if committed.exit_code:
        raise PatchbenchError('generated_test_invalid: cannot retain isolated oracle')
    return name
