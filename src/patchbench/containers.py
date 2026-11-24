"""Small Podman helpers for evaluation and solver execution."""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Callable, NamedTuple

from patchbench.repositories import load_repository
from patchbench.exceptions import PatchbenchError
from patchbench.runner import redact_command_secrets, redact_secrets
from patchbench.podman_backend import build_context, podman_command, remote_backend, remote_enabled, ensure_remote_image


class ContainerResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    interrupted: bool = False


def ensure_podman_available() -> None:
    try:
        subprocess.run(
            podman_command("info", "--format", "json"),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        raise PatchbenchError(
            "Podman is not available. Install Podman and verify with 'podman info'."
        ) from exc
    except FileNotFoundError as exc:
        raise PatchbenchError(
            "Podman not found. Install Podman or run without --container."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PatchbenchError("Podman did not respond within 30 seconds") from exc


def image_name_for_repo(repo: str) -> str:
    if (
        not repo
        or repo in {".", ".."}
        or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
            for character in repo
        )
    ):
        raise PatchbenchError(
            "repository image name must contain only letters, numbers, '.', '_', or '-'"
        )
    return f"patchbench-{repo}:latest"


def build_image(
    repo: str, *, profile: str | None = None, timeout_seconds: float = 1800
) -> str:
    """Build a versioned repository environment or an explicit shared profile."""
    ensure_podman_available()
    manifest = load_repository(repo)
    if manifest.config["evaluator"] != "patch_tests":
        raise PatchbenchError(f"Repository {repo} has no patch evaluator image")
    settings = manifest.config
    if profile is not None:
        profiles = manifest.config.get("environment_profiles", {})
        settings = profiles.get(profile) if isinstance(profiles, dict) else None
        if not isinstance(settings, dict):
            raise PatchbenchError(f"Unknown environment profile for {repo}: {profile}")
        if not settings.get("containerfile"):
            raise PatchbenchError(f"Environment profile {profile} uses an existing image; no build recipe")
    containerfile = (manifest.path.parent.parent / settings["containerfile"]).resolve()
    if not containerfile.is_file():
        raise PatchbenchError(f"Containerfile not found: {containerfile}")

    image_name = settings["image"]
    if not isinstance(image_name, str) or not image_name.strip():
        raise PatchbenchError(f"Environment profile requires a non-empty image: {profile}")
    try:
        build_args = settings.get('build_args', {})
        if not isinstance(build_args, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in build_args.items()):
            raise PatchbenchError('Environment build_args must map argument names to strings')
        arguments = [arg for key, value in sorted(build_args.items()) for arg in ('--build-arg', key + '=' + value)]
        completed = _build_podman_image(containerfile, image_name, manifest.path.parent.parent,
                                        arguments, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        raise PatchbenchError(f"Image build exceeded {timeout_seconds:g} seconds") from exc
    if completed.returncode != 0:
        raise PatchbenchError(
            f"Failed to build image: {completed.stderr or completed.stdout}"
        )
    return image_name


def _build_podman_image(containerfile, image_name, context, arguments=(), **kwargs):
    with build_context(containerfile, context) as (recipe, directory):
        return subprocess.run(
            podman_command("build", "-f", str(recipe), "-t", image_name, *arguments, "."),
            cwd=directory, capture_output=True, text=True, check=False, **kwargs)


def run_in_container(
    image_name: str,
    worktree_path: Path,
    command: str,
    workdir: str | None = None,
    env_overrides: dict[str, str] | None = None,
    additional_mounts: list[tuple[Path, str]] | None = None,
    insecure_mounts: bool = False,
    run_as_user: str | None = None,
    timeout_seconds: float | None = None,
    login_shell: bool = True,
    network: str | None = None,
) -> ContainerResult:
    """Run an evaluator command with a host directory mounted at `/host`."""
    ensure_podman_available()
    ensure_remote_image(image_name)
    # Give evaluators a real parent process and reap children. Historical tools
    # inspect their parent (e.g. psutil); PID 1 otherwise has parent PID 0.
    cmd = ["podman", "run", "--rm", "--init", "-v", f"{worktree_path.resolve()}:/host"]
    if network is not None:
        cmd.extend(["--network", network])
    if run_as_user:
        cmd.extend(["--user", run_as_user])
    if insecure_mounts:
        cmd.extend(["--userns=host", "--security-opt", "label=disable"])
    for host_path, container_path in additional_mounts or []:
        cmd.extend(["-v", f"{host_path.resolve()}:{container_path}"])
    cmd.extend(["-w", f"/host/{workdir}" if workdir else "/host"])
    for key in (env_overrides or {}):
        cmd.extend(["-e", key])
    cmd.extend([image_name, "bash", "-lc" if login_shell else "-c", command])
    process_environment = os.environ.copy()
    process_environment.update(env_overrides or {})

    return _run_streaming_container_process(
        cmd,
        timeout_seconds=timeout_seconds,
        environment=env_overrides or {},
        redaction_command=command,
        progress_callback=lambda *_args: None,
        process_environment=process_environment,
    )


def run_solver_in_container(
    image_name: str,
    worktree_path: Path,
    prompt_path: Path,
    command: str,
    timeout_seconds: float,
) -> ContainerResult:
    """Run a solver with only its checkout and read-only issue prompt mounted."""
    ensure_podman_available()
    ensure_remote_image(image_name)
    cmd = [
        "podman",
        "run",
        "--rm",
        "--network",
        "none",
        "--security-opt",
        "label=disable",
        "-v",
        f"{worktree_path.resolve()}:/workspace",
        "-v",
        f"{prompt_path.resolve()}:/run/patchbench/issue.md:ro",
        "-e",
        "PATCHBENCH_PROMPT_FILE=/run/patchbench/issue.md",
        "-w",
        "/workspace",
        image_name,
        "bash",
        "-lc",
        command,
    ]
    return _run_streaming_container_process(
        cmd,
        timeout_seconds=timeout_seconds,
        environment={},
        redaction_command=command,
        progress_callback=lambda *_args: None,
    )


def build_claude_code_image(
    image_name: str = "patchbench-rucio-claude:tests-v2",
    *,
    landb_mcp_repo: str | Path | None = None,
) -> str:
    """Build the dedicated Claude Code solver image without embedding credentials."""
    ensure_podman_available()
    containerfile = Path("images/claude-code.Containerfile")
    repository: Path | None = None
    revision: str | None = None
    dirty = False
    if landb_mcp_repo is not None:
        repository = Path(landb_mcp_repo).expanduser().resolve()
        revision, dirty = _validate_landb_mcp_source(repository)
        if dirty:
            raise PatchbenchError(
                "LanDB MCP checkout is dirty; commit or stash it before building"
            )
        containerfile = Path("images/claude-code-landb.Containerfile")
    if not containerfile.is_file():
        raise PatchbenchError(f"Containerfile not found: {containerfile}")
    if repository is None:
        completed = _build_podman_image(containerfile, image_name, Path.cwd())
    else:
        with tempfile.TemporaryDirectory(prefix="patchbench-agent-image-") as raw:
            build_context = Path(raw)
            shutil.copy(
                Path("images/cfmgr-agent-requirements.txt"),
                build_context / "cfmgr-agent-requirements.txt",
            )
            shutil.copy(
                Path("images/verify-landb-mcp.py"),
                build_context / "verify-landb-mcp.py",
            )
            shutil.copy(containerfile, build_context / containerfile.name)
            shutil.copytree(
                repository,
                build_context / "landb-mcp",
                ignore=shutil.ignore_patterns(
                    ".git",
                    ".venv",
                    ".env",
                    ".env.*",
                    ".pytest_cache",
                    ".ruff_cache",
                    "__pycache__",
                    "*.pyc",
                ),
            )
            completed = _build_podman_image(
                build_context / containerfile.name, image_name, build_context,
                ["--build-arg", f"LANDB_MCP_REVISION={revision}",
                 "--build-arg", f"LANDB_MCP_DIRTY={str(dirty).lower()}"])
    if completed.returncode != 0:
        raise PatchbenchError(
            f"Failed to build Claude Code solver image: {completed.stderr or completed.stdout}"
        )
    return image_name


def inspect_landb_mcp_image(image_name: str) -> tuple[str, bool]:
    """Read the pinned LanDB MCP revision from a versioned solver image."""
    ensure_podman_available()
    completed = subprocess.run(
        podman_command("image", "inspect", image_name),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise PatchbenchError(
            f"Could not inspect solver image {image_name}: "
            f"{completed.stderr or completed.stdout}"
        )
    try:
        payload = json.loads(completed.stdout)
        item = payload[0] if isinstance(payload, list) else payload
        labels = item.get("Config", {}).get("Labels", {})
        revision = labels.get("org.patchbench.landb-mcp.revision")
        dirty_text = labels.get("org.patchbench.landb-mcp.dirty")
    except (AttributeError, IndexError, TypeError, ValueError) as exc:
        raise PatchbenchError(
            f"Solver image {image_name} has malformed image metadata"
        ) from exc
    if not revision:
        raise PatchbenchError(
            f"Solver image {image_name} does not contain versioned LanDB MCP metadata"
        )
    return str(revision), str(dirty_text).lower() == "true"


def _validate_landb_mcp_source(repository: Path) -> tuple[str, bool]:
    if not (repository / "pyproject.toml").is_file() or not (
        repository / "uv.lock"
    ).is_file():
        raise PatchbenchError(f"Not a complete LanDB MCP checkout: {repository}")
    try:
        revision = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "-C", str(repository), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout.strip()
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PatchbenchError(
            f"Could not resolve LanDB MCP Git revision at {repository}"
        ) from exc
    if not revision:
        raise PatchbenchError(f"LanDB MCP checkout has no Git revision: {repository}")
    return revision, dirty


@contextmanager
def _claude_login_mount(login_dir: Path):
    """Serialize native credential refresh; never stage credentials in artifacts."""
    import fcntl

    login_dir = login_dir.expanduser().resolve()
    if not login_dir.is_dir() or login_dir == Path.home() / ".claude":
        raise PatchbenchError("Use a dedicated external Claude login directory")
    if login_dir.stat().st_uid != os.getuid() or login_dir.stat().st_mode & 0o077:
        raise PatchbenchError("Claude login directory must be owned by you and have mode 700")
    if any(path.is_symlink() for path in login_dir.rglob("*")):
        raise PatchbenchError("Claude login directory must not contain symlinks")
    with (login_dir / ".patchbench-login.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PatchbenchError("This Claude login is already in use; run one worker per login directory") from None
        yield login_dir


def run_claude_in_container(
    image_name: str,
    worktree_path: Path,
    prompt_path: Path,
    env_file: Path | None,
    command: str,
    timeout_seconds: float | None,
    environment: dict[str, str],
    network: str,
    progress_callback: Callable[[str, str, float, bool], None] | None = None,
    mcp_config_path: Path | None = None,
    provider_ca_path: Path | None = None,
    dependency_cache: Path | None = None,
    login_dir: Path | None = None,
    refresh_redactions: Callable[[], dict[str, str]] | None = None,
) -> ContainerResult:
    """Run Claude Code with a read-only credential script and provider connectivity.

    The shell command is responsible for sourcing ``env_file``.  The file is
    mounted directly rather than copied into the image or any run artifact.
    """
    ensure_podman_available()
    if (env_file is None) == (login_dir is None):
        raise PatchbenchError("Provide either a Claude provider env file or a login directory")
    if remote_enabled() and login_dir is not None:
        raise PatchbenchError("Native Claude login refresh is local-only; use local mode for --agent-login-dir")
    ensure_remote_image(image_name)
    mounted_env, cleanup = _claude_env_mount(image_name, env_file) if env_file is not None else (None, False)
    cmd = [
        "podman",
        "run",
        "--rm",
        "--network",
        network,
        "--security-opt",
        "label=disable",
        # The image runs as its non-root `node` user. Map that UID/GID to the
        # rootless Podman caller so it can modify the host-owned checkout.
        "--userns=keep-id:uid=1000,gid=1000",
        "-v",
        f"{worktree_path.resolve()}:/workspace",
        "-v",
        f"{prompt_path.resolve()}:/run/patchbench/issue.md:ro",
    ]
    if mounted_env is not None:
        cmd.extend(["-v", f"{mounted_env.resolve()}:/run/patchbench/claude-env.sh:ro"])
    if "/run/patchbench/kimi_compat.py" in command:
        adapter = Path(__file__).parent / "agents" / "kimi_compat.py"
        cmd.extend(["-v", f"{adapter.resolve()}:/run/patchbench/kimi_compat.py:ro"])
    if mcp_config_path is not None:
        cmd.extend(
            ["-v", f"{mcp_config_path.resolve()}:/run/patchbench/mcp.json:ro"]
        )
    if provider_ca_path is not None:
        cmd.extend(
            [
                "-v",
                f"{provider_ca_path.resolve()}:/run/patchbench/provider-ca.pem:ro",
            ]
        )
    if dependency_cache is not None:
        # Private copy-on-write storage permits Maven metadata/install writes
        # without changing the shared, frozen cache or another solver's view.
        cmd.extend(['-v', f'{dependency_cache.resolve()}/repository:/home/node/.m2/repository:O',
                    '-e', 'MAVEN_ARGS=-o'])
    cmd.extend(
        [
        "-e",
        "PATCHBENCH_PROMPT_FILE=/run/patchbench/issue.md",
        "-w",
        "/workspace",
        image_name,
        "bash",
        "-c",
        command,
        ]
    )
    try:
        with ExitStack() as stack:
            if login_dir is not None:
                mounted_login = stack.enter_context(_claude_login_mount(login_dir))
                # A directory mount permits atomic credential renewal by the CLI.
                # --safe-mode prevents customizations from being loaded.
                position = cmd.index("-w")
                cmd[position:position] = ["-v", f"{mounted_login}:/run/patchbench/claude-login:rw"]
            return _run_streaming_container_process(
                cmd,
                timeout_seconds=timeout_seconds,
                environment=environment,
                redaction_command=command,
                progress_callback=progress_callback or (lambda *_args: None),
                **({"refresh_redactions": refresh_redactions} if refresh_redactions is not None else {}),
            )
    finally:
        if cleanup:
            mounted_env.unlink(missing_ok=True)


def _run_streaming_container_process(
    command: list[str],
    *,
    timeout_seconds: float | None,
    environment: dict[str, str],
    redaction_command: str,
    progress_callback: Callable[[str, str, float, bool], None],
    process_environment: dict[str, str] | None = None,
    refresh_redactions: Callable[[], dict[str, str]] | None = None,
) -> ContainerResult:
    """Run Podman while forwarding redacted lines and periodic heartbeats."""
    backend = remote_backend()
    if backend is not None:
        try:
            with backend.stage_run(command, timeout_seconds) as staged:
                return _stream_container_process(
                    staged, timeout_seconds=timeout_seconds, environment=environment,
                    redaction_command=redaction_command, progress_callback=progress_callback,
                    process_environment=process_environment, refresh_redactions=refresh_redactions)
        except PatchbenchError as exc:
            raise PatchbenchError(redact_command_secrets(redact_secrets(str(exc), environment),
                                                        redaction_command)) from None
    return _stream_container_process(
        command, timeout_seconds=timeout_seconds, environment=environment,
        redaction_command=redaction_command, progress_callback=progress_callback,
        process_environment=process_environment, refresh_redactions=refresh_redactions)


def _stream_container_process(
    command: list[str], *, timeout_seconds: float | None, environment: dict[str, str],
    redaction_command: str, progress_callback: Callable[[str, str, float, bool], None],
    process_environment: dict[str, str] | None = None,
    refresh_redactions: Callable[[], dict[str, str]] | None = None,
) -> ContainerResult:
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
        env=process_environment,
    )
    output_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()

    def read_stream(stream, name: str) -> None:
        try:
            for line in iter(stream.readline, ""):
                output_queue.put((name, line))
        finally:
            stream.close()
            output_queue.put((name, None))

    readers = [
        threading.Thread(target=read_stream, args=(stream, name), daemon=True)
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))
        if stream is not None
    ]
    for reader in readers:
        reader.start()

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    closed_streams = 0
    timed_out = False
    interrupted = False

    def sanitize(line: str) -> str:
        if refresh_redactions is not None:
            for value in refresh_redactions().values():
                if value not in environment.values():
                    environment[f"REFRESHED_LOGIN_SECRET_{len(environment)}"] = value
        return redact_command_secrets(redact_secrets(line, environment), redaction_command)

    try:
        while True:
            elapsed = time.monotonic() - started
            if (
                timeout_seconds is not None
                and not timed_out
                and process.poll() is None
                and elapsed >= timeout_seconds
            ):
                timed_out = True
                _terminate_process(process)

            try:
                stream_name, line = output_queue.get(timeout=0.1)
            except queue.Empty:
                if process.poll() is None:
                    progress_callback("", "", elapsed, True)
                if process.poll() is not None and closed_streams == len(readers):
                    break
                continue

            if line is None:
                closed_streams += 1
                if process.poll() is not None and closed_streams == len(readers):
                    break
                continue

            sanitized = sanitize(line)
            if stream_name == "stdout":
                stdout_parts.append(sanitized)
            else:
                stderr_parts.append(sanitized)
            progress_callback(stream_name, sanitized, elapsed, False)
    except KeyboardInterrupt:
        interrupted = True
        _terminate_process(process)
    except BaseException:
        _terminate_process(process)
        raise
    finally:
        if process.poll() is None:
            _terminate_process(process)

    for reader in readers:
        reader.join(timeout=1)
    while True:
        try:
            stream_name, line = output_queue.get_nowait()
        except queue.Empty:
            break
        if line is None:
            continue
        sanitized = sanitize(line)
        if stream_name == "stdout":
            stdout_parts.append(sanitized)
        else:
            stderr_parts.append(sanitized)
    return ContainerResult(
        124 if timed_out else 130 if interrupted else process.wait(),
        "".join(stdout_parts),
        "".join(stderr_parts),
        time.monotonic() - started,
        timed_out,
        interrupted,
    )


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        process.terminate()
    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        process.kill()
    process.wait()


def _claude_env_mount(image_name: str, env_file: Path) -> tuple[Path, bool]:
    """Return a Podman-readable env script, copying only when a bind mount fails."""
    if remote_enabled():
        if not env_file.is_file():
            raise PatchbenchError("Claude provider env file does not exist")
        # Remote staging owns its tmpfs copy; never probe the local credential
        # path against a remote Podman host or copy it into project artifacts.
        return env_file, False
    if _podman_can_read_file(image_name, env_file):
        return env_file, False

    runtime_dir = Path(".patchbench-runtime").resolve()
    # Keep this shared directory: another process may be between mkdir and
    # mkstemp. Each caller cleans up only its own uniquely named file.
    runtime_dir.mkdir(mode=0o700, exist_ok=True)
    runtime_dir.chmod(0o700)
    descriptor, copied_name = tempfile.mkstemp(
        prefix="claude-env-", suffix=".sh", dir=runtime_dir
    )
    copied = Path(copied_name)
    try:
        with open(descriptor, "wb", closefd=True) as destination:
            with env_file.open("rb") as source:
                shutil.copyfileobj(source, destination)
        copied.chmod(0o600)
        if not _podman_can_read_file(image_name, copied):
            raise PatchbenchError(
                "Podman cannot read claude-env.sh even from .patchbench-runtime"
            )
    except BaseException:
        copied.unlink(missing_ok=True)
        raise
    return copied, True


def _podman_can_read_file(image_name: str, path: Path) -> bool:
    """Preflight a read-only file mount without sourcing its contents."""
    try:
        completed = subprocess.run(
            [
                "podman",
                "run",
                "--rm",
                "--network",
                "none",
                "--security-opt",
                "label=disable",
                "--userns=keep-id:uid=1000,gid=1000",
                "-v",
                f"{path.resolve()}:/run/patchbench/claude-env.sh:ro",
                image_name,
                "bash",
                "-lc",
                "test -r /run/patchbench/claude-env.sh",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False
    return completed.returncode == 0
