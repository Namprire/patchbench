"""Only existing eligible production files; patches always come from Git state."""
from pathlib import PurePosixPath
import re
from patchbench.exceptions import PatchbenchError
from patchbench.runner import run_command


def production_file(name, component):
    if isinstance(component, (list, tuple)):
        return any(production_file(name, item) for item in component)
    path = PurePosixPath(name)
    return (not path.is_absolute() and '..' not in path.parts and '\\' not in name
        and ((component == 'lib/rucio' and name.startswith('lib/rucio/') and name.endswith('.py')
              and 'tests' not in path.parts and not path.name.startswith('test_'))
             or (name.startswith(component.rstrip('/') + '/src/main/java/') and name.endswith('.java'))))


def historical_source_patch(text, component):
    blocks = re.split(r'(?=^diff --git )', text, flags=re.M)
    selected, files = [], []
    for block in blocks:
        match = re.match(r'diff --git a/(\S+) b/(\S+)\n', block)
        if match and match[1] == match[2] and production_file(match[1], component):
            selected.append(block); files.append(match[1])
    return ''.join(selected), sorted(set(files))


def check_changes(checkout, allowed):
    run_command(['git', 'add', '-N', '--all'], cwd=checkout)
    status = run_command(['git', 'diff', '--name-status', 'HEAD'], cwd=checkout).stdout.splitlines()
    files = []
    for line in status:
        parts = line.split('\t')
        if len(parts) != 2 or parts[0] != 'M' or parts[1] not in allowed:
            raise PatchbenchError('forbidden_file_change: ' + line)
        path = checkout / parts[1]
        if path.is_symlink() or not path.is_file():
            raise PatchbenchError('forbidden_file_change')
        files.append(parts[1])
    if not files:
        raise PatchbenchError('no_f2p_tests: empty mirror')
    return sorted(files)


def write_patches(checkout, mirror, gold):
    # Exactly inverse diffs of the actual same tree. No LLM repair.
    mirror.write_text(run_command(['git', 'diff', '--binary', 'HEAD'], cwd=checkout).stdout)
    gold.write_text(run_command(['git', 'diff', '-R', '--src-prefix=b/', '--dst-prefix=a/', '--binary', 'HEAD'], cwd=checkout).stdout)


def exact_reverse(checkout, text):
    """Reverse uniquely matching full hunks, allowing line relocation but no fuzz.

    Stage all replacements before writing. New/deleted files are never reversed.
    Resulting artifacts are subsequently generated from Git and fully validated.
    """
    changes = {}
    for block in re.split(r'(?=^diff --git )', text, flags=re.M):
        header = re.match(r'diff --git a/(\S+) b/(\S+)\n', block)
        if not header:
            continue
        if header[1] != header[2] or '/dev/null' in block or 'rename from ' in block:
            return False, 'File creation/deletion/rename is not an exact existing-file inversion'
        path = checkout / header[1]
        if not path.is_file() or path.is_symlink() or '..' in PurePosixPath(header[1]).parts:
            return False, 'Historical file does not exist at reference'
        current = path.read_text()
        hunks = re.split(r'^@@[^\n]*\n', block, flags=re.M)[1:]
        if not hunks:
            return False, 'No textual hunks'
        for hunk in hunks:
            lines = hunk.splitlines(keepends=True)
            if any(line.startswith('\\ No newline') for line in lines):
                return False, 'Unsupported no-newline hunk'
            before = ''.join(line[1:] for line in lines if line[:1] in (' ', '+'))
            after = ''.join(line[1:] for line in lines if line[:1] in (' ', '-'))
            if not before or current.count(before) != 1:
                return False, 'Current hunk context is missing or ambiguous (no fuzzy matching)'
            current = current.replace(before, after, 1)
        changes[path] = current
    if not changes:
        return False, 'No invertible production hunks'
    for path, content in changes.items():
        path.write_text(content)
    return True, 'Unique exact hunk context; line numbers may have moved'
