"""Deterministic per-test transitions, never command-exit-only F2P."""
from pathlib import Path
import re
import xml.etree.ElementTree as ET


def read_reports(checkout: Path) -> dict:
    tests, invalid = {}, []
    paths = sorted(checkout.rglob('TEST-*.xml'))
    paths += sorted(checkout.rglob('patchbench-junit.xml'))
    for path in paths:
        if path.is_symlink():
            invalid.append(str(path)); continue
        try:
            root = ET.parse(path).getroot()
            nodes = list(root.iter('testcase'))
            names = [n.get('classname', '') + '::' + n.get('name', '') for n in nodes]
            occurrences = {}
            module = '/'.join(path.relative_to(checkout).parts[:-3])
            for node, name in zip(nodes, names):
                # Surefire reports shared architecture tests in each Maven module, and
                # some parameterized tests omit their invocation number in XML names.
                occurrences[name] = occurrences.get(name, 0) + 1
                suffix = '#' + str(occurrences[name]) if names.count(name) > 1 else ''
                key = (module + '::' if module else '') + name + suffix
                if key in tests:
                    invalid.append('duplicate report test: ' + key)
                failure = node.find('failure')
                error = node.find('error')
                status = 'SKIP' if node.find('skipped') is not None else 'ERROR' if error is not None else 'FAIL' if failure is not None else 'PASS'
                detail = failure if failure is not None else error
                failure_type = detail.get('type', '') if detail is not None else ''
                # pytest xunit2 omits type. A <failure> is a test-call failure;
                # fixture/setup/collection failures are emitted as <error>.
                if failure is not None and not failure_type:
                    tail = (failure.text or '').strip().splitlines()
                    if tail and re.search(r'(?:^|: )AssertionError(?:[: ].*)?$', tail[-1]):
                        failure_type = 'AssertionError'
                tests[key] = dict(status=status, type=failure_type,
                    message=(detail.get('message', '') if detail is not None else '')[:2000])
                if path.name == 'patchbench-junit.xml' and failure is not None:
                    # unittest hooks run inside pytest's call phase and therefore
                    # also use <failure>. They are setup/teardown, not the oracle.
                    hook_failure = re.search(r'(?m)^\s*(?:async )?def (?:setUp|tearDown|'
                        r'setUpClass|tearDownClass|asyncSetUp|asyncTearDown)\(', failure.text or '')
                    tests[key]['pytest_call_failure'] = not bool(hook_failure)
        except (ET.ParseError, OSError):
            invalid.append(str(path))
    return dict(tests=tests, invalid_reports=invalid, executed=sum(t['status'] in ('PASS', 'FAIL') for t in tests.values()))


def execution_reason(result: dict) -> str | None:
    if result['exit_code'] == 124:
        return 'timeout'
    output = result.get('output', '')
    failed_command = result['exit_code'] != 0
    if re.search(r'\[ERROR\].*COMPILATION ERROR', output) or (failed_command and re.search(r'COMPILATION ERROR|cannot find symbol|package .* does not exist|SyntaxError', output)):
        return 'compilation_failure'
    if failed_command and re.search(r'Could not resolve dependencies|Could not find artifact|Cannot access .*offline mode|ModuleNotFoundError|No module named', output):
        return 'missing_dependency'
    if (failed_command or any(t['status']=='ERROR' for t in result['report']['tests'].values())) and re.search(r'DB_(?:DEV|TEST)_PASSWORD|Could not resolve placeholder|ORA-\d+|Connection refused|UnknownHostException', output):
        return 'external_dependency'
    report = result['report']
    if report['invalid_reports'] or any(t['status'] == 'ERROR' for t in report['tests'].values()):
        return 'unrelated_test_failure'
    if not report['executed']:
        return 'no_tests_executed'
    if result['exit_code'] == 124:
        return 'timeout'
    return None


def green(result: dict) -> bool:
    return (execution_reason(result) is None and result['exit_code'] == 0
            and all(t['status'] in ('PASS', 'SKIP') for t in result['report']['tests'].values()))


def differential(reference: dict, bug: dict) -> dict:
    if not green(reference):
        return dict(reason='baseline_not_green', f2p_tests=[], p2p_tests=[])
    reason = execution_reason(bug)
    before, after = reference['report']['tests'], bug['report']['tests']
    if reason:
        return dict(reason=reason, f2p_tests=[], p2p_tests=[])
    if before.keys() != after.keys() or any((before[k]['status'] == 'SKIP') != (after[k]['status'] == 'SKIP') for k in before):
        return dict(reason='unrelated_test_failure', f2p_tests=[], p2p_tests=[])
    failures = [k for k in before if before[k]['status'] == 'PASS' and after[k]['status'] == 'FAIL']
    # Unexpected application exceptions and failed pytest.raises expectations
    # are behavioral failures too. Never infer this from a nonzero exit alone.
    def behavioral(test):
        if 'pytest_call_failure' in test:
            if not test['pytest_call_failure']:
                return False
            detail = test.get('type', '') + '\n' + test.get('message', '')
            return not re.search(r'\b(?:ImportError|ModuleNotFoundError|SyntaxError|'
                r'FileNotFoundError|PermissionError|ConnectionError|ConnectionRefusedError|'
                r'ConnectTimeout|ReadTimeout|OperationalError)\b', detail)
        return any(x in test['type'] for x in ('Assertion', 'ComparisonFailure'))
    if any(not behavioral(after[k]) for k in failures):
        return dict(reason='unrelated_test_failure', f2p_tests=[], p2p_tests=[])
    if not failures or bug['exit_code'] == 0:
        return dict(reason='no_f2p_tests', f2p_tests=[], p2p_tests=[])
    return dict(reason='valid_f2p', f2p_tests=sorted(failures),
                p2p_tests=sorted(k for k in before if before[k]['status'] == after[k]['status'] == 'PASS'))
