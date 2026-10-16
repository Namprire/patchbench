"""An invented, credential-free coding task; no research case inputs are used."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess

import yaml

from patchbench.exceptions import PatchbenchError


def create_demo(root: Path) -> Path:
    """Create new standalone inputs without overwriting an existing directory."""
    root = root.expanduser().absolute()
    if root.exists() or root.is_symlink():
        raise PatchbenchError("Demo destination must not exist")
    root.mkdir(parents=True)
    source = root / "source"
    case = root / "cases" / "demo-label"
    source.mkdir()
    case.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_AUTHOR_DATE="2000-01-01T00:00:00+00:00",
               GIT_COMMITTER_DATE="2000-01-01T00:00:00+00:00")

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
            cwd=source, env=env, check=True, capture_output=True, text=True,
        ).stdout

    git("-c", "init.templateDir=", "init", "-q", "--initial-branch=main")
    git("config", "user.name", "Synthetic Demo")
    git("config", "user.email", "demo@example.invalid")
    (source / "label.py").write_text('def label(words):\n    return " ".join(words)\n')
    git("add", "label.py")
    git("commit", "-qm", "Invented label task: base")
    base = git("rev-parse", "HEAD").strip()
    (source / "label.py").write_text('def label(words):\n    return "-".join(words)\n')
    (case / "gold.patch").write_text(git("diff", "--binary"))
    git("add", "label.py")
    git("commit", "-qm", "Invented label task: reference repair")
    fixed = git("rev-parse", "HEAD").strip()
    (source / "check_label.py").write_text(
        'from label import label\n'
        'assert label(["blue", "bird"]) == "blue-bird"\n'
        'assert label([]) == ""\n'
    )
    git("add", "-N", "check_label.py")
    (case / "test.patch").write_text(git("diff", "--binary", "--", "check_label.py"))
    (case / "issue.md").write_text(
        "# Synthetic demo: word labels\n\n"
        "Make label(words) join the supplied words with a hyphen. "
        "An empty list must produce an empty string.\n"
    )
    (case / "provenance.json").write_text(json.dumps({
        "source": "invented-synthetic-demo", "research_case": False,
    }, indent=2) + "\n")
    (case / "meta.yaml").write_text(yaml.safe_dump({
        "id": "demo-label", "case_type": "coding", "repo_path": "../../source",
        "base_commit": base, "fix_commit": fixed, "test_command": "python -B check_label.py",
        "problem_statement": "issue.md", "gold_patch": "gold.patch", "test_patch": "test.patch",
    }))
    return case


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        print(create_demo(args.destination))
    except PatchbenchError as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
