from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from pathlib import Path

from patchbench.models import CommandResult


_SECRET_KEY = re.compile(
    r"(?:TOKEN|API_KEY|PRIVATE_KEY|PASSWORD|BASIC_USERNAME|SECRET|CREDENTIAL|AUTHORIZATION|JWT)",
    re.IGNORECASE,
)


def is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY.search(key))


def redact_secrets(text: str, *environments: dict[str, str]) -> str:
    """Redact known secret values without logging an environment dump."""
    redacted = text
    for environment in (os.environ, *environments):
        for key, value in environment.items():
            if is_secret_key(key) and len(value) >= 4:
                redacted = redacted.replace(value, "<redacted>")
    return redacted


def sanitize_command(command: str) -> str:
    redacted = redact_secrets(command)
    redacted = re.sub(
        r"(?i)\b([A-Z0-9_]*(?:TOKEN|API_KEY|PASSWORD|SECRET|JWT)[A-Z0-9_]*)=([^\s]+)",
        r"\1=<redacted>",
        redacted,
    )
    return re.sub(
        r"(?i)(--?(?:token|api-key|password|secret)\s+)(\S+)",
        r"\1<redacted>",
        redacted,
    )


def redact_command_secrets(text: str, command: str) -> str:
    """Redact secret literals supplied directly in a configured command."""
    values = re.findall(
        r"(?i)\b[A-Z0-9_]*(?:TOKEN|API_KEY|PASSWORD|SECRET|JWT)[A-Z0-9_]*=([^\s]+)",
        command,
    )
    values.extend(
        re.findall(r"(?i)--?(?:token|api-key|password|secret)\s+(\S+)", command)
    )
    redacted = text
    for value in values:
        if len(value) >= 4:
            redacted = redacted.replace(value.strip("\"'"), "<redacted>")
    return redacted


def run_command(
    command: list[str],
    cwd: Path,
    env_overrides: dict[str, str] | None = None,
    timeout_seconds: float | None = None,
) -> CommandResult:
    overrides = dict(env_overrides or {})
    env = os.environ.copy()
    env.update(overrides)
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_group(process)
        stdout, stderr = process.communicate()
        message = f"Command timed out after {timeout_seconds:g} seconds."
        stderr = f"{stderr.rstrip()}\n{message}\n" if stderr else message + "\n"
    except BaseException:
        _terminate_process_group(process)
        raise
    finally:
        if process.poll() is None:
            _terminate_process_group(process)
    duration = time.monotonic() - started
    logged_overrides = {
        key: "<redacted>" if is_secret_key(key) else value
        for key, value in overrides.items()
    }
    return CommandResult(
        command=[sanitize_command(part) for part in command],
        cwd=str(cwd),
        env_overrides=logged_overrides,
        exit_code=124 if timed_out else process.returncode,
        duration_seconds=duration,
        stdout=redact_secrets(stdout, overrides),
        stderr=redact_secrets(stderr, overrides),
        timed_out=timed_out,
    )


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        process.terminate()
    try:
        process.wait(timeout=1)
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


def write_log(log_path: Path, result: CommandResult) -> None:
    log_path.write_text(result.to_log_text(), encoding="utf-8")
