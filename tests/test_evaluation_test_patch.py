"""Regressions from the SWE-bench PR #539 feasibility checks.

https://github.com/SWE-bench/SWE-bench/pull/539
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from patchbench import commands
from patchbench.case import load_case
from patchbench.containers import ContainerResult
from patchbench.gitutils import apply_patch, diff_stats, evaluation_test_patch

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


VARIANTS = ("modified-only", "new-only", "mixed", "deleted-existing")
MODEL_TEST = "from app import VALUE\nassert VALUE in (0, 1)  # model test\n"
BASE_TEST = "from app import VALUE\nassert VALUE == 1\n"


def _scenario(tmp_path, variant="mixed", *, fix=True):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text("VALUE = 0\n")
    (repo / "test_existing.py").write_text(BASE_TEST)
    (repo / "setup.conf").write_text("base setup\n")
    (repo / "check.py").write_text(
        "from pathlib import Path\n"
        "Path('tests-ran').touch()\n"
        "for path in sorted(Path('.').glob('test_*.py')):\n"
        "    exec(compile(path.read_text(), str(path), 'exec'), {})\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD").strip()
    modified = ["test_existing.py"] if variant != "new-only" else []
    new = ["test_new.py"] if variant in {"new-only", "mixed"} else []
    if variant in {"modified-only", "mixed"}:
        (repo / "test_existing.py").write_text(BASE_TEST + "assert VALUE != 0\n")
    if variant == "deleted-existing":
        (repo / "test_existing.py").unlink()
    for name in new:
        (repo / name).write_text(BASE_TEST)
    _git(repo, "add", "-N", ".")
    # HEAD includes staged deletions as well as intent-to-add new files.
    official = _git(repo, "diff", "--binary", "HEAD")
    _git(repo, "reset", "--hard", base)
    _git(repo, "clean", "-fd")

    if fix:
        (repo / "app.py").write_text("VALUE = 1\n")
    (repo / "test_existing.py").write_text(MODEL_TEST)
    for name in new:
        (repo / name).write_text("raise RuntimeError('model collision')\n")
    (repo / "test_agent_extra.py").write_text(MODEL_TEST)
    _git(repo, "add", "-N", ".")
    model = _git(repo, "diff", "--binary", "HEAD")
    _git(repo, "reset", "--hard", base)
    _git(repo, "clean", "-fd")

    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "issue.md").write_text("Fix VALUE\n")
    (case_dir / "test.patch").write_text(official)
    (case_dir / "gold.patch").write_text(model)
    (case_dir / "meta.yaml").write_text(yaml.safe_dump({
        "id": "reset-tests",
        "repo_path": str(repo),
        "base_commit": base,
        "fix_commit": base,
        "test_command": shlex.join([sys.executable, "check.py"]),
        "problem_statement": "issue.md",
        "gold_patch": "gold.patch",
        "test_patch": "test.patch",
        "image": "unused-test-image",
    }))
    patch = tmp_path / "agent.patch"
    patch.write_text(model)
    return load_case(case_dir), patch, modified, new


def _assert_cleanup(checkout, modified, new, *, fix=True):
    for name in modified:
        assert (checkout / name).read_text() == BASE_TEST
    for name in new:
        assert not (checkout / name).exists()
    assert (checkout / "app.py").read_text() == f"VALUE = {int(fix)}\n"
    assert (checkout / "test_agent_extra.py").read_text() == MODEL_TEST
    if not modified:
        assert (checkout / "test_existing.py").read_text() == MODEL_TEST


@pytest.mark.parametrize("variant", VARIANTS)
def test_targeted_reset_preserves_full_model_patch_and_setup(tmp_path, variant):
    case, model, modified, new = _scenario(tmp_path, variant)
    original = model.read_bytes()
    repo = case.repo_path
    assert apply_patch(repo, model)[0]
    diff_stats(repo)  # Evaluation also records the full submission first.
    (repo / "setup.conf").write_text("post-base setup\n")

    # These complete model submissions collide with the official test patch.
    assert not apply_patch(repo, case.test_patch)[0]
    with evaluation_test_patch(repo, case.base_commit, case.test_patch) as (applied, error):
        assert applied, error
        result = subprocess.run(case.test_command, cwd=repo, capture_output=True)
        assert result.returncode == 0, result.stderr

    _assert_cleanup(repo, modified, new)
    assert (repo / "setup.conf").read_text() == "post-base setup\n"
    assert model.read_bytes() == original


def test_cleanup_runs_after_exception_and_test_runtime_changes(tmp_path):
    case, model, modified, new = _scenario(tmp_path)
    assert apply_patch(case.repo_path, model)[0]
    with pytest.raises(RuntimeError, match="evaluation failed"):
        with evaluation_test_patch(case.repo_path, case.base_commit, case.test_patch) as (applied, error):
            assert applied, error
            (case.repo_path / modified[0]).write_text("runtime test change\n")
            raise RuntimeError("evaluation failed")
    _assert_cleanup(case.repo_path, modified, new)


def _evaluate(entrypoint, case, model, run_dir):
    if entrypoint == "direct":
        return commands.evaluate_patch(case.case_dir, model)
    evaluator = getattr(commands, f"_evaluate_patch_{entrypoint}")
    return evaluator(case, model, run_dir)


def _observe_checkout(monkeypatch, entrypoint, modified, new, *, fix, tests_ran):
    name = "temporary_checkout" if entrypoint == "container" else "temporary_worktree"
    original = getattr(commands, name)
    checked = []

    @contextmanager
    def observed(*args):
        with original(*args) as checkout:
            yield checkout
            # Verify cleanup before the temporary checkout itself is deleted.
            _assert_cleanup(checkout, modified, new, fix=fix)
            assert (checkout / "tests-ran").exists() is tests_ran
            checked.append(True)

    monkeypatch.setattr(commands, name, observed)
    return checked


def _local_container(monkeypatch):
    def run(image, root, script, **kwargs):
        # Exercise the generated evaluator script; only replace the mount path.
        script = script.replace("/host/status", shlex.quote(str(root / "status")))
        result = subprocess.run(
            ["bash", "-c", script], cwd=root / kwargs["workdir"],
            text=True, capture_output=True,
        )
        return ContainerResult(result.returncode, result.stdout, result.stderr, 0.01)

    monkeypatch.setattr(commands, "run_in_container", run)


@pytest.mark.parametrize("entrypoint", ("direct", "host", "container"))
@pytest.mark.parametrize("variant,fix", [
    (variant, True) for variant in VARIANTS
] + [
    # A deletion-only patch leaves no official assertion in this small fixture.
    # The other variants must reject the missing fix using the official tests.
    (variant, False) for variant in VARIANTS if variant != "deleted-existing"
])
def test_all_evaluators_reset_only_official_paths(tmp_path, monkeypatch, entrypoint, variant, fix):
    case, model, modified, new = _scenario(tmp_path, variant, fix=fix)
    metadata_reader = commands.read_metadata
    monkeypatch.setattr(commands, "read_metadata", lambda path: {
        **metadata_reader(path),
        "historical_pr_tests": {"test_side_files": modified + new},
    })
    saved_model, saved_tests = model.read_bytes(), case.test_patch.read_bytes()
    checked = _observe_checkout(monkeypatch, entrypoint, modified, new, fix=fix, tests_ran=True)
    _local_container(monkeypatch)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    result = _evaluate(entrypoint, case, model, run_dir)

    assert result.patch_applied
    assert (result.resolved if entrypoint == "direct" else result.tests_pass) is fix
    assert checked == [True]
    # Reporting retains the entire model submission, including its test edits.
    expected_files = {"test_existing.py", "test_agent_extra.py", *new}
    if fix:
        expected_files.add("app.py")
    assert set(result.files_changed) == expected_files
    assert model.read_bytes() == saved_model
    assert case.test_patch.read_bytes() == saved_tests


@pytest.mark.parametrize("entrypoint", ("direct", "host", "container"))
def test_official_patch_failure_cannot_pass_or_run_tests(tmp_path, monkeypatch, entrypoint):
    case, model, modified, new = _scenario(tmp_path)
    case.test_patch.write_text(case.test_patch.read_text().replace(
        " assert VALUE == 1\n", " assert VALUE == 999\n"
    ))
    checked = _observe_checkout(monkeypatch, entrypoint, modified, new, fix=True, tests_ran=False)

    def unexpected(*args, **kwargs):
        pytest.fail("Tests must not run after an official patch failure")

    monkeypatch.setattr(commands, "run_in_container", unexpected)
    monkeypatch.setattr(commands, "_run_setup_commands", unexpected)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    result = _evaluate(entrypoint, case, model, run_dir)

    assert result.patch_applied
    assert not (result.resolved if entrypoint == "direct" else result.tests_pass)
    assert "Failed to apply test patch" in result.error
    assert checked == [True]
