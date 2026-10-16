"""Resolve historical Java paths against a frozen Maven reactor, before execution."""
from pathlib import Path
import hashlib
import re
import xml.etree.ElementTree as ET
from patchbench.runner import run_command
from patchbench.exceptions import PatchbenchError


def reactor_modules(checkout: Path) -> list[str]:
    found = set()
    def visit(directory):
        pom = checkout / directory / 'pom.xml'
        if not pom.is_file() or pom.is_symlink():
            return
        name = directory.as_posix()
        if name in found:
            return
        found.add(name)
        root = ET.parse(pom).getroot()
        for child in root.findall('./{*}modules/{*}module'):
            target = (pom.parent / (child.text or '')).resolve()
            if target.is_relative_to(checkout.resolve()):
                visit(target.relative_to(checkout.resolve()))
    visit(Path('.'))
    return sorted(found - {'.'})


def map_historical_patch(text: str, checkout: Path, modules: list[str]):
    """Only exact paths or unique Java package/class suffixes; never guess renames."""
    index = {}
    for module in modules:
        directory = checkout / module / 'src/main/java'
        for path in directory.rglob('*.java'):
            if not path.is_symlink():
                index.setdefault(path.relative_to(directory).as_posix(), []).append(path.relative_to(checkout).as_posix())
    blocks, mapping, rejected = [], {}, []
    for block in re.split(r'(?=^diff --git )', text, flags=re.M):
        match = re.match(r'diff --git a/(\S+) b/(\S+)\n', block)
        if not match or match[1] != match[2]:
            continue
        old = match[1]
        if 'lib/rucio' in modules and old.startswith('lib/rucio/') and old.endswith('.py'):
            from .patches import production_file
            if not production_file(old, modules):
                continue
            if (checkout / old).is_file() and not (checkout / old).is_symlink() and '..' not in Path(old).parts:
                blocks.append(block); mapping[old] = old
            else:
                rejected.append(dict(path=old, reason='ambiguous_or_missing_current_file'))
            continue
        if '/src/main/java/' not in old or not old.endswith('.java'):
            continue
        module, suffix = old.split('/src/main/java/', 1)
        if module in modules and (checkout / old).is_file():
            current = old
        else:
            choices = index.get(suffix, [])
            if len(choices) != 1:
                rejected.append(dict(path=old, reason='ambiguous_or_missing_current_file'))
                continue
            current = choices[0]
        # Creation/deletion patches do not describe an invertible edit of an existing API.
        if '\n--- /dev/null\n' in block or '\n+++ /dev/null\n' in block:
            rejected.append(dict(path=old, reason='historical_file_creation_or_deletion'))
            # Still supply to the semantic mirror as provenance; it may edit an existing file.
        block = block.replace('a/' + old, 'a/' + current).replace('b/' + old, 'b/' + current)
        blocks.append(block)
        mapping[old] = current
    return ''.join(blocks), mapping, rejected


def automatic_policy(manifest, modules):
    base = manifest.config.get('benchmark_maven_policy')
    if not isinstance(base, dict) or not modules:
        raise PatchbenchError('No approved automatic Maven component policy')
    if base.get('external_services_required') is not False:
        raise PatchbenchError('Automatic policy must explicitly require offline local execution')
    selected = ','.join(sorted(modules))
    args = ['mvn', '-B', '-ntp', '-pl', selected, '-am']
    groups = base['test_policy'].get('excluded_groups', [])
    if groups:
        args.append('-DexcludedGroups=' + ','.join(groups))
    args.append('test')
    return {**base, 'path': sorted(modules)[0], 'paths': sorted(modules),
            'setup_commands': [args], 'test_command': [args[0], '-o', *args[1:]]}


def concrete_test_classes(checkout, test_paths):
    """Identify concrete Java classes before execution, including inherited tests."""
    result = set()
    for name in test_paths:
        path = checkout / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(checkout.resolve()):
            continue
        text = path.read_text(errors='replace')
        package = re.search(r'\bpackage\s+([\w.]+)\s*;', text)
        prefix = package[1] + '.' if package else ''
        if re.search(r'\babstract\s+class\s+' + re.escape(path.stem) + r'\b', text):
            module = name.split('/src/test/')[0]
            for child in (checkout / module / 'src/test/java').rglob('*.java'):
                if child.is_symlink() or not child.resolve().is_relative_to(checkout.resolve()):
                    continue
                source = child.read_text(errors='replace')
                if (re.search(r'\bextends\s+(?:[\w.]+\.)?' + re.escape(path.stem) + r'\b', source)
                        and not re.search(r'\babstract\s+class\b', source)):
                    declared = re.search(r'\bpackage\s+([\w.]+)\s*;', source)
                    result.add((declared[1] + '.' if declared else '') + child.stem)
        else:
            result.add(prefix + path.stem)
    return sorted(result)


def developer_regression_paths(patch, checkout, modules):
    """Map developer Java-test edits by exact path or unique package/class suffix."""
    result, missing = set(), []
    for old in re.findall(r'^diff --git a/\S+ b/(\S+)$', patch, flags=re.M):
        if '/src/test/java/' not in old or not old.endswith('.java'):
            continue
        suffix = old.split('/src/test/java/', 1)[1]
        choices = [module + '/src/test/java/' + suffix for module in modules
                   if (checkout / module / 'src/test/java' / suffix).is_file()
                   and not (checkout / module / 'src/test/java' / suffix).is_symlink()
                   and (checkout / module / 'src/test/java' / suffix).resolve().is_relative_to(checkout.resolve())]
        if old in choices:
            result.add(old)
        elif len(choices) == 1:
            result.add(choices[0])
        else:
            missing.append(old)
    return sorted(result), sorted(missing)


def detect_component(manifest, checkout, patch):
    if getattr(manifest, 'id', None) == 'rucio':
        selected, mapping, rejected = map_historical_patch(patch, checkout, ['lib/rucio'])
        components = manifest.config['benchmark_components']
        name = 'common-unit'
        for candidate, policy in components.items():
            paths = policy.get('source_paths', [])
            if mapping and paths and all(any(f == p or f.startswith(p.rstrip('/') + '/') for p in paths) for f in mapping.values()):
                name = candidate
                break
        evidence = dict(mapping=mapping, rejected=rejected, selection_policy='configured-python-test-scope-v1', selected_component=name)
        return (name, components[name], evidence) if mapping else (None, None, {**evidence, 'reason':'unsupported_scope'})
    selected, mapping, rejected = map_historical_patch(patch, checkout, reactor_modules(checkout))
    modules = sorted({f.split('/src/main/java/')[0] for f in mapping.values()})
    if not modules:
        return None, None, dict(mapping=mapping, rejected=rejected, reason='unsupported_scope')
    # Select a reusable component BEFORE any mirror execution, by static test evidence.
    # Never select/exclude tests based on their failing outcome after construction.
    from .context import related_test_context
    candidates = []
    developer_paths, missing_developer_paths = developer_regression_paths(patch, checkout, modules)
    for module in modules:
        files = [f for f in mapping.values() if f.startswith(module + '/src/main/java/')]
        _, related = related_test_context(checkout, files)
        public = list((checkout / module / 'src/test').rglob('*.java'))
        configured = next((name for name, policy in manifest.config.get('benchmark_components', {}).items()
                           if sorted(policy.get('paths', [policy['path']])) == [module]), None)
        developer = [p for p in developer_paths if p.startswith(module + '/src/test/java/')]
        candidates.append(dict(module=module, developer_test_files=developer,
                               related_test_files=related, public_test_files=len(public),
                               relevant_test_classes=concrete_test_classes(checkout, sorted(set(developer + related))),
                               configured_component=configured))
    candidates.sort(key=lambda c:(-len(c['developer_test_files']), -len(c['related_test_files']), -c['public_test_files'],
                                  not bool(c['configured_component']), c['module']))
    chosen = candidates[0]
    evidence = dict(mapping=mapping, rejected=rejected, candidates=candidates,
                    selected_module=chosen['module'], selection_policy='static-developer-and-existing-test-links-v2',
                    relevant_test_classes=chosen['relevant_test_classes'],
                    missing_developer_test_files=missing_developer_paths)
    if chosen['configured_component']:
        name = chosen['configured_component']
        return name, manifest.config['benchmark_components'][name], evidence
    name = 'maven-' + hashlib.sha256(chosen['module'].encode()).hexdigest()[:12]
    return name, automatic_policy(manifest, [chosen['module']]), evidence
