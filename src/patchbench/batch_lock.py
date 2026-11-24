"""Coordinate benchmark batches in this workspace without a service or PID lock."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path

from patchbench.exceptions import PatchbenchError


def reject_older_batches(cases_dir: Path) -> None:
    """Also recognize live run-all processes started before locking existed."""
    cases_dir = cases_dir.resolve()
    active = []
    for process in Path("/proc").iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            args = (process / "cmdline").read_bytes().decode(errors="replace").split("\0")
            command = next((i for i, arg in enumerate(args) if arg in {"run-all", "runall"}), None)
            if command is None or not any("patchbench" in arg for arg in args[:command]):
                continue
            case_path = args[args.index("--cases-dir") + 1] if "--cases-dir" in args else "cases"
            if ((process / "cwd") / case_path).resolve() != cases_dir:
                continue
            run_id = args[args.index("--run-id") + 1] if "--run-id" in args else "run-all"
            active.append(f"PID {process.name}: {run_id}")
        except (OSError, ValueError, IndexError, RuntimeError):
            continue
    if active:
        raise PatchbenchError("Another benchmark batch is already running:\n" + "\n".join(active))


@contextmanager
def batch_lock(cases_dir):
    cases_dir = Path(cases_dir).expanduser().resolve()
    path = cases_dir.parent / ".patchbench-runtime" / "benchmark.lock"
    # The shell launcher passes its locked open-file description to its own
    # workers. Validate the descriptor, not just an environment-variable claim.
    inherited = os.environ.get("PATCHBENCH_BATCH_LOCK_FD")
    if inherited:
        try:
            fd = int(inherited)
            opened, expected = os.fstat(fd), path.stat()
            if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
                raise ValueError("Wrong lock descriptor")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ValueError) as exc:
            raise PatchbenchError("Invalid inherited benchmark batch lock") from exc
        yield
        return  # Never unlock the launcher's shared open-file description.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PatchbenchError("Another benchmark batch already holds the workspace lock") from exc
        reject_older_batches(cases_dir)
        yield


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases_dir", type=Path)
    args = parser.parse_args()
    try:
        with batch_lock(args.cases_dir):
            reject_older_batches(args.cases_dir)
    except PatchbenchError as exc:
        parser.exit(2, f"{exc}\n")


if __name__ == "__main__":
    main()
