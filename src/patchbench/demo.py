"""Generate original synthetic coding cases, without network services or a model."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
from textwrap import dedent

import yaml

from patchbench.exceptions import PatchbenchError


# These teaching examples are invented from scratch, not derived from research cases.
_EXAMPLES = {
    "demo-label": {
        "difficulty": "easy",
        "issue": """\
            # Synthetic demo: word labels

            `label(words)` is used to build compact labels. It currently inserts
            spaces. Join the supplied strings with a hyphen instead. Preserve the
            order and the strings exactly, including empty strings. An empty list
            produces an empty string; a single word is unchanged.
            """,
        "base": {"label.py": 'def label(words):\n    return " ".join(words)\n'},
        "fixed": {"label.py": 'def label(words):\n    return "-".join(words)\n'},
        "tests": """\
            import unittest
            from label import label

            class LabelTests(unittest.TestCase):
                def test_join(self):
                    self.assertEqual(label(["blue", "bird"]), "blue-bird")
                    self.assertEqual(label(["a", "b", "c"]), "a-b-c")

                def test_boundaries(self):
                    self.assertEqual(label([]), "")
                    self.assertEqual(label(["alone"]), "alone")
                    self.assertEqual(label(["", "a", ""]), "-a-")
                    self.assertEqual(label(["two words", "x"]), "two words-x")

            if __name__ == "__main__":
                unittest.main()
            """,
    },
    "demo-config": {
        "difficulty": "medium",
        "issue": """\
            # Synthetic demo: strict worker configuration

            `parse_config(text)` reads one `key=value` per line. Ignore blank lines
            and whole-line comments beginning with `#`, after trimming whitespace.
            Trim keys and values, but keep keys case-sensitive. Supported keys are
            `workers` (ASCII decimal integer from 1 to 32 inclusive) and `verbose`
            (`true` or `false`, case-insensitive). Omitted values default to workers=1
            and verbose=False. Return a dictionary containing both keys.

            The current parser silently accepts invalid settings and treats the
            string `false` as true. Raise ValueError for unknown or duplicate keys,
            missing or multiple equals signs, empty values, signed/nondecimal worker
            counts, out-of-range workers, and any other verbose spelling. Do not
            silently retain a partially valid configuration. Inline comments are
            not supported. Valid configurations must keep working.
            """,
        "base": {"config.py": """\
            def parse_config(text):
                result = {"workers": 1, "verbose": False}
                for line in text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    key, value = (part.strip() for part in line.split("=", 1))
                    if key == "workers":
                        result[key] = int(value)
                    elif key == "verbose":
                        result[key] = bool(value)
                return result
            """},
        "fixed": {"config.py": """\
            def parse_config(text):
                result = {"workers": 1, "verbose": False}
                seen = set()
                for line in text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.count("=") != 1:
                        raise ValueError("expected one key=value assignment")
                    key, value = (part.strip() for part in line.split("="))
                    if key not in result or key in seen or not value:
                        raise ValueError("unknown, repeated, or empty setting")
                    seen.add(key)
                    if key == "workers":
                        if not value.isascii() or not value.isdecimal():
                            raise ValueError("workers must be ASCII decimal")
                        workers = int(value)
                        if not 1 <= workers <= 32:
                            raise ValueError("workers must be between 1 and 32")
                        result[key] = workers
                    else:
                        if value.lower() not in {"true", "false"}:
                            raise ValueError("verbose must be true or false")
                        result[key] = value.lower() == "true"
                return result
            """},
        "tests": """\
            import unittest
            from config import parse_config

            class ConfigTests(unittest.TestCase):
                def test_false_and_true(self):
                    self.assertFalse(parse_config("verbose=false")["verbose"])
                    self.assertTrue(parse_config("verbose=TrUe")["verbose"])

                def test_defaults_whitespace_and_boundaries(self):
                    self.assertEqual(parse_config("  # comment\\n\\n"),
                                     {"workers": 1, "verbose": False})
                    self.assertEqual(parse_config(" workers = 32 \\nverbose = FALSE"),
                                     {"workers": 32, "verbose": False})
                    self.assertEqual(parse_config("workers=01")["workers"], 1)

                def test_invalid_assignments(self):
                    invalid = ["workers=0", "workers=33", "workers=-1", "workers=+2",
                               "workers=2.5", "workers=\\u0662", "workers=", "verbose=",
                               "workers=2\\nworkers=3", "verbose=true\\nverbose=false",
                               "unknown=x", "Workers=2", "workers", "=2",
                               "workers=2=3", "verbose=yes", "verbose=false # comment"]
                    for text in invalid:
                        with self.subTest(text=text), self.assertRaises(ValueError):
                            parse_config(text)

            if __name__ == "__main__":
                unittest.main()
            """,
    },
    "demo-ranges": {
        "difficulty": "harder",
        "issue": """\
            # Synthetic demo: half-open interval coverage

            The range utilities disagree about nested intervals and endpoints.
            `merge_ranges(ranges)` must return a sorted list of disjoint (start, end)
            tuples representing the same union of half-open integer intervals
            [start, end). Merge overlapping or touching ranges, preserve the outer
            endpoint of nested ranges, discard empty intervals, and reject reversed
            intervals with ValueError. Negative endpoints are valid. Do not mutate
            the input sequence. Empty input produces an empty list.

            `covered_length(ranges)` must use the same union and return its total
            length: a half-open interval [2, 5) contributes 3, not 4. Overlap must
            never be counted twice. Review both modules; correcting only merging or
            only length calculation leaves valid inputs broken.
            """,
        "base": {
            "ranges.py": """\
                def merge_ranges(ranges):
                    merged = []
                    for start, end in sorted(ranges):
                        if start > end:
                            raise ValueError("reversed interval")
                        if start == end:
                            continue
                        if merged and start <= merged[-1][1]:
                            merged[-1] = (merged[-1][0], end)
                        else:
                            merged.append((start, end))
                    return merged
                """,
            "coverage.py": """\
                from ranges import merge_ranges

                def covered_length(ranges):
                    return sum(end - start + 1 for start, end in merge_ranges(ranges))
                """,
        },
        "fixed": {
            "ranges.py": """\
                def merge_ranges(ranges):
                    merged = []
                    for start, end in sorted(ranges):
                        if start > end:
                            raise ValueError("reversed interval")
                        if start == end:
                            continue
                        if merged and start <= merged[-1][1]:
                            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                        else:
                            merged.append((start, end))
                    return merged
                """,
            "coverage.py": """\
                from ranges import merge_ranges

                def covered_length(ranges):
                    return sum(end - start for start, end in merge_ranges(ranges))
                """,
        },
        "tests": """\
            import unittest
            from ranges import merge_ranges
            from coverage import covered_length

            class RangeTests(unittest.TestCase):
                def test_nested_and_touching(self):
                    values = [(2, 4), (0, 10), (10, 12), (5, 6)]
                    self.assertEqual(merge_ranges(values), [(0, 12)])
                    self.assertEqual(covered_length(values), 12)

                def test_length_and_disjoint_ranges(self):
                    self.assertEqual(covered_length([(2, 5)]), 3)
                    self.assertEqual(covered_length([(5, 8), (-4, -2)]), 5)
                    self.assertEqual(covered_length([(0, 4), (2, 6)]), 6)

                def test_regressions(self):
                    values = [(4, 5), (-3, -1), (1, 1)]
                    original = list(values)
                    merged = merge_ranges(values)
                    self.assertEqual(merged, [(-3, -1), (4, 5)])
                    self.assertEqual(merge_ranges(merged), merged)
                    self.assertEqual(values, original)
                    self.assertEqual(merge_ranges([]), [])
                    self.assertEqual(covered_length([]), 0)
                    self.assertEqual(covered_length([(1, 1)]), 0)
                    with self.assertRaises(ValueError):
                        merge_ranges([(5, 4)])
                    with self.assertRaises(ValueError):
                        covered_length([(5, 4)])

            if __name__ == "__main__":
                unittest.main()
            """,
    },
}
EXAMPLE_IDS = tuple(_EXAMPLES)


def _new_root(root: Path) -> Path:
    root = root.expanduser().absolute()
    if root.exists() or root.is_symlink():
        raise PatchbenchError("Demo destination must not exist")
    root.mkdir(parents=True)
    return root


def _create_case(root: Path, case_id: str) -> Path:
    example = _EXAMPLES[case_id]
    source = root / "sources" / case_id
    case = root / "cases" / case_id
    source.mkdir(parents=True)
    case.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    # Fixed synthetic identities and dates make source commits independent of the host.
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_AUTHOR_DATE="2000-01-01T00:00:00+00:00",
               GIT_COMMITTER_DATE="2000-01-01T00:00:00+00:00")

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
             "-c", "core.autocrlf=false", *args],
            cwd=source, env=env, check=True, capture_output=True, text=True,
        ).stdout

    def write_files(files):
        for name, content in files.items():
            (source / name).write_text(dedent(content), encoding="utf-8")

    git("-c", "init.templateDir=", "init", "-q", "--initial-branch=main", "--object-format=sha1")
    git("config", "user.name", "Synthetic Demo")
    git("config", "user.email", "demo@example.invalid")
    write_files(example["base"])
    git("add", ".")
    git("commit", "-qm", f"Invented {case_id}: base")
    base = git("rev-parse", "HEAD").strip()
    write_files(example["fixed"])
    (case / "gold.patch").write_text(git("diff", "--binary"), encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", f"Invented {case_id}: reference repair")
    fixed = git("rev-parse", "HEAD").strip()
    test_name = "check_example.py"
    (source / test_name).write_text(dedent(example["tests"]), encoding="utf-8")
    git("add", "-N", test_name)
    (case / "test.patch").write_text(git("diff", "--binary", "--", test_name), encoding="utf-8")
    # Leave the source directory clean at Base; the evaluator retains the frozen Fix.
    git("reset", "-q")
    (source / test_name).unlink()
    git("checkout", "-q", "--detach", base)
    (case / "issue.md").write_text(dedent(example["issue"]), encoding="utf-8")
    (case / "provenance.json").write_text(json.dumps({
        "source": "invented-synthetic-demo", "research_case": False,
        "difficulty": example["difficulty"], "source_date_is_synthetic": True,
    }, indent=2) + "\n", encoding="utf-8")
    (case / "meta.yaml").write_text(yaml.safe_dump({
        "id": case_id, "case_type": "coding", "repo_path": f"../../sources/{case_id}",
        "base_commit": base, "fix_commit": fixed, "test_command": "python -B check_example.py",
        "problem_statement": "issue.md", "gold_patch": "gold.patch", "test_patch": "test.patch",
    }), encoding="utf-8")
    return case


def create_demo(root: Path, case_id: str = "demo-label") -> Path:
    """Create one invented case; preserve the original single-case Python API."""
    if case_id not in _EXAMPLES:
        raise PatchbenchError(f"Unknown synthetic example: {case_id}")
    return _create_case(_new_root(root), case_id)


def create_suite(root: Path) -> tuple[Path, ...]:
    """Create the three independent cases in one standalone corpus."""
    root = _new_root(root)
    return tuple(_create_case(root, case_id) for case_id in EXAMPLE_IDS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--case", choices=EXAMPLE_IDS, help="Generate only this example (default: all three)")
    args = parser.parse_args()
    try:
        cases = (create_demo(args.destination, args.case),) if args.case else create_suite(args.destination)
        for case in cases:
            print(case)
    except PatchbenchError as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
