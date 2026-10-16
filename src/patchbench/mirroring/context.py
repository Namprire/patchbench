"""Bounded current-source windows and read-only existing-test context."""
from pathlib import Path
import re

SOURCE_LIMIT = 36000
TEST_LIMIT = 12000


def source_windows(text, historical, limit):
    if len(text) <= limit:
        return text
    lines = text.splitlines(keepends=True)
    anchors = [line[1:].strip() for line in historical.splitlines() if line.startswith(('+', '-'))
               and not line.startswith(('+++', '---')) and len(line[1:].strip()) > 8]
    hits = [i for i, line in enumerate(lines) if any(a in line for a in anchors)]
    # Spend the budget on affected code first, not on an arbitrarily long header.
    hits = [i for i in hits if not lines[i].lstrip().startswith(('import ', 'package ', '*', '//'))]
    result = ''; supplied = set()
    for hit in hits:
        if hit in supplied:
            continue
        start, end = max(0, hit-12), min(len(lines), hit+13)
        marker = f"\n/* source lines {start+1}-{end}; omitted context elsewhere */\n"
        fragment = ''.join(lines[start:end])
        if len(result) + len(marker) + len(fragment) > limit:
            # Even when neighbours are huge, retain the actual anchor and a small window.
            start, end = max(0, hit-2), min(len(lines), hit+3)
            marker = f"\n/* source lines {start+1}-{end}; omitted context elsewhere */\n"
            fragment = ''.join(lines[start:end])
        if len(result) + len(marker) + len(fragment) <= limit:
            result += marker + fragment; supplied.update(range(start, end))
    if not result:
        return text[:limit]
    # Imports/declarations are supplementary context; they cannot displace the target.
    header = ''.join(line for i, line in enumerate(lines[:60]) if i not in supplied
                     and (line.lstrip().startswith(('package ', 'import ')) or ' class ' in line))
    remaining = limit - len(result) - 36
    if remaining > 0 and header:
        result += '\n/* supplementary file header */\n' + header[:remaining]
    return result[:limit]


def related_test_context(checkout, files):
    classes = {Path(f).stem for f in files}
    modules = {f.split('/src/main/java/')[0] for f in files}
    records = []
    for module in sorted(modules):
        for path in sorted((checkout / module / 'src/test').rglob('*.java')):
            if path.is_symlink():
                continue
            text = path.read_text(errors='replace')
            matched = sorted(c for c in classes if re.search(r'\b'+re.escape(c)+r'\b', text))
            if matched:
                records.append(dict(path=path.relative_to(checkout).as_posix(), source=text, classes=matched))
    python_modules = {f[4:-3].replace('/', '.') for f in files if f.startswith('lib/rucio/') and f.endswith('.py')}
    if python_modules:
        for path in sorted((checkout / 'tests').rglob('test*.py')):
            if path.is_symlink():
                continue
            text = path.read_text(errors='replace')
            matched = sorted(m for m in python_modules if m in text)
            if matched:
                records.append(dict(path=path.relative_to(checkout).as_posix(), source=text, classes=matched))
    total = 0; bounded = []
    for item in records:
        if total >= TEST_LIMIT:
            break
        content = item['source'][:min(4000, TEST_LIMIT-total)]
        bounded.append({**item, 'source':content}); total += len(content)
    return bounded, [r['path'] for r in records]
