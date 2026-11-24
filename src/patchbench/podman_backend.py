"""Opt-in remote Podman with SSH transport and explicit bind-mount staging.

Only this module knows about the remote filesystem. The harness, credentials and
authoritative artifacts remain on the client. No remote Python packages are used.
"""
from __future__ import annotations

import atexit
from contextlib import contextmanager
from functools import lru_cache
import hashlib
import fcntl
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
from uuid import uuid4

from patchbench.exceptions import PatchbenchError
from patchbench.runner import redact_secrets


def remote_enabled() -> bool:
    return bool(os.environ.get("PATCHBENCH_REMOTE_HOST"))


@lru_cache(maxsize=4)
def _backend(host: str, root: str, known_hosts: str) -> RemotePodman:
    return RemotePodman(host, root, known_hosts)


def remote_backend() -> RemotePodman | None:
    host = os.environ.get("PATCHBENCH_REMOTE_HOST", "")
    if not host:
        return None
    root = os.environ.get("PATCHBENCH_REMOTE_ROOT", "")
    if not root:
        raise PatchbenchError("Remote Podman requires an absolute PATCHBENCH_REMOTE_ROOT")
    return _backend(host, root, os.environ.get("PATCHBENCH_REMOTE_KNOWN_HOSTS", ""))


def podman_command(*arguments: str) -> list[str]:
    backend = remote_backend()
    if backend and arguments[:2] == ("image", "inspect"):
        backend.ensure_image(arguments[-1])
    return (backend.command if backend else ["podman"]) + list(arguments)


def ensure_remote_image(image: str) -> None:
    backend = remote_backend()
    if backend:
        backend.ensure_image(image)


def container_backend_metadata() -> dict:
    backend = remote_backend()
    if backend is None:
        return {}
    _ = backend.command
    return {"container_backend": "remote-podman", "container_host": backend.host,
            "container_graphroot": backend.info["store"]["graphRoot"]}


def tree_digest(path: Path) -> str:
    """Identify immutable cache contents, including filenames and file modes."""
    digest = hashlib.sha256()
    for child in sorted(path.rglob("*")):
        if child.is_symlink():
            raise PatchbenchError("Remote immutable caches must not contain symlinks")
        digest.update(child.relative_to(path).as_posix().encode() + b"\0")
        digest.update(str(child.stat().st_mode & 0o777).encode() + b"\0")
        if child.is_file():
            with child.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def _restore_tree(source: Path, destination: Path) -> None:
    """Reconcile one declared writable mount, including deletions and symlinks."""
    def remove(path):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()

    if source.is_symlink():
        if destination.exists() or destination.is_symlink():
            remove(destination)
        destination.symlink_to(os.readlink(source))
    elif source.is_dir():
        if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
            remove(destination)
        destination.mkdir(exist_ok=True)
        names = {p.name for p in source.iterdir()}
        for child in destination.iterdir():
            if child.name not in names:
                remove(child)
        for child in source.iterdir():
            _restore_tree(child, destination / child.name)
        shutil.copystat(source, destination)
    else:
        if destination.is_symlink() or destination.is_dir():
            remove(destination)
        # Git packfiles are deliberately read-only and can be hardlinked.
        # Replace the directory entry instead of opening an existing inode.
        descriptor, temporary = tempfile.mkstemp(prefix=".patchbench-sync-", dir=destination.parent)
        os.close(descriptor)
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def _archive_filter(member, destination):
    name = PurePosixPath(member.name)
    if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != "payload":
        raise PatchbenchError("Unsafe path in remote container output")
    if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
        raise PatchbenchError("Special file in remote container output")
    # Also rejects escaping links, links through existing symlinks, and setuid bits.
    filtered = tarfile.data_filter(member, destination)
    if filtered.isdir():
        # data_filter otherwise discards directory modes, expanding a private
        # 0700 scratch root to the extractor's default 0755 on download.
        filtered.mode = member.mode & 0o755
    return filtered


class RemotePodman:
    def __init__(self, host: str, root: str, known_hosts: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+", host) or host.startswith("-"):
            raise PatchbenchError("PATCHBENCH_REMOTE_HOST must be user@hostname")
        path = PurePosixPath(root)
        if not path.is_absolute() or ".." in path.parts or len(path.parts) < 4:
            raise PatchbenchError("Use a private absolute directory for PATCHBENCH_REMOTE_ROOT")
        self.host, self.root = host, str(path)
        self.ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                    "-o", "UpdateHostKeys=no", "-o", "ForwardAgent=no",
                    "-o", "ConnectTimeout=12", "-o", "ServerAliveInterval=15",
                    "-o", "ServerAliveCountMax=3"]
        if known_hosts:
            self.ssh += ["-o", f"UserKnownHostsFile={Path(known_hosts).expanduser().resolve()}"]
        self._temporary = None
        self._tunnel = None
        self._command = None
        self.info = {}

    def shell(self, script: str, *, check=True, timeout=60):
        result = subprocess.run(self.ssh + [self.host, script], capture_output=True,
                                text=True, timeout=timeout)
        if check and result.returncode:
            raise PatchbenchError("Remote Podman SSH operation failed: " +
                                  redact_secrets(result.stderr.strip())[-2000:])
        return result

    @property
    def command(self) -> list[str]:
        if self._command is not None:
            if self._tunnel.poll() is not None:
                raise PatchbenchError("Remote Podman SSH tunnel disconnected; no local fallback was attempted")
            return self._command
        uid = int(self.shell("id -u").stdout.strip())
        if uid == 0:
            raise PatchbenchError("Remote PatchBench requires rootless Podman")
        self.runtime = f"/run/user/{uid}"
        if self.shell("findmnt -n -o FSTYPE -T " + shlex.quote(self.runtime)).stdout.strip() != "tmpfs":
            raise PatchbenchError("Remote credential runtime directory must be on tmpfs")
        self.info = json.loads(self.shell("podman info --format json").stdout)
        if not self.info["host"]["security"]["rootless"]:
            raise PatchbenchError("Remote Podman is not rootless")
        graphroot = self.info["store"]["graphRoot"]
        fstype = self.shell("findmnt -n -o FSTYPE -T " + shlex.quote(graphroot)).stdout.strip()
        if fstype not in {"xfs", "ext4", "btrfs"}:
            raise PatchbenchError(f"Remote Podman graphroot filesystem is not approved: {fstype}")
        # Validate rather than overwrite an existing directory's permissions.
        validation = """import os, pathlib, sys
p=pathlib.Path(sys.argv[1])
if any(x.is_symlink() for x in (p, *p.parents)):
    raise SystemExit('Remote root must not contain symlinks')
p.mkdir(mode=0o700, parents=True, exist_ok=True)
if p.stat().st_uid != os.getuid() or p.stat().st_mode & 0o077:
    raise SystemExit('Remote root must be owned by you and have mode 700')
for name in ('jobs', 'caches'):
    child=p/name
    child.mkdir(mode=0o700, exist_ok=True)
    if child.is_symlink() or child.stat().st_uid != os.getuid() or child.stat().st_mode & 0o077:
        raise SystemExit('Unsafe remote staging directory')
"""
        self.shell(shlex.join(["python3", "-c", validation, self.root]))
        self.shell("systemctl --user start podman.socket")
        self._temporary = tempfile.TemporaryDirectory(prefix="patchbench-podman-")
        socket = str(Path(self._temporary.name) / "api.sock")
        self._tunnel = subprocess.Popen(
            self.ssh + ["-o", "ExitOnForwardFailure=yes", "-N", "-L",
                        f"{socket}:{self.runtime}/podman/podman.sock", self.host],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        atexit.register(self.close)
        for _ in range(200):
            if self._tunnel.poll() is not None:
                self.close()
                raise PatchbenchError("Could not open remote Podman SSH socket tunnel")
            if Path(socket).exists():
                self._command = ["podman", "--remote", "--url", f"unix://{socket}"]
                return self._command
            time.sleep(0.05)
        self.close()
        raise PatchbenchError("Timed out opening remote Podman SSH tunnel")

    def close(self):
        if self._tunnel is not None:
            if self._tunnel.poll() is None:
                self._tunnel.terminate()
                try:
                    self._tunnel.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._tunnel.kill()
                    self._tunnel.wait()
            if self._tunnel.stderr:
                self._tunnel.stderr.close()
        if self._temporary is not None:
            self._temporary.cleanup()

    def upload(self, source: Path, directory: str):
        """Stream a declared input; never create a client-side archive."""
        if (source / ".git").is_file():
            raise PatchbenchError("Remote mounts require a self-contained checkout, not a linked Git worktree")
        script = "umask 077; mkdir -p -- {0} && tar --no-same-owner -xf - -C {0}".format(shlex.quote(directory))
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(self.ssh + [self.host, script], stdin=subprocess.PIPE,
                                       stdout=subprocess.DEVNULL, stderr=errors)
            try:
                with tarfile.open(fileobj=process.stdin, mode="w|") as archive:
                    archive.add(source, arcname="payload")
                process.stdin.close()
                if process.wait(timeout=600):
                    raise PatchbenchError("Could not stage remote container input")
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()

    def download(self, directory: str, destination: Path):
        """Validate into a fresh tree before updating the declared local mount."""
        with tempfile.TemporaryDirectory(prefix="patchbench-download-") as temp, tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(self.ssh + [self.host, "tar -cf - -C " +
                                       shlex.quote(directory) + " payload"],
                                       stdout=subprocess.PIPE, stderr=errors)
            try:
                with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                    archive.extractall(temp, filter=_archive_filter)
                if process.wait(timeout=600):
                    raise PatchbenchError("Could not retrieve remote container output")
                source = Path(temp) / "payload"
                if not source.exists() or source.is_symlink():
                    raise PatchbenchError("Remote output mount root is missing or unsafe")
                _restore_tree(source, destination)
            finally:
                process.stdout.close()
                if process.poll() is None:
                    process.kill()
                process.wait()

    def stop_job(self, name: str):
        # A separate SSH request can recover even if the API tunnel was lost.
        result = self.shell(shlex.join(["podman", "container", "exists", name]), check=False, timeout=30)
        if result.returncode == 1:
            return
        if result.returncode:
            raise PatchbenchError(f"Cannot confirm remote container stopped: {name}")
        result = self.shell(shlex.join(["podman", "rm", "--force", "--ignore", name]), check=False, timeout=30)
        if result.returncode:
            raise PatchbenchError(f"Cannot stop this run's remote container: {name}")

    def ensure_image(self, image: str):
        """Lazily stream existing local images, preserving IDs and existing tags."""
        lock_root = Path.home() / ".cache" / "patchbench" / "remote-image-locks"
        lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        key = hashlib.sha256((self.host + "\0" + image).encode()).hexdigest()
        with (lock_root / key).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            remote = subprocess.run(self.command + ["image", "inspect", "--format", "{{.Id}}", image],
                                    capture_output=True, text=True, timeout=30)
            if remote.returncode == 0:
                # Remote builds may intentionally differ from local fallback
                # images. Frozen-case image-ID checks still run on this backend.
                return
            local = subprocess.run(["podman", "--remote=false", "image", "inspect", "--format", "{{.Id}}", image],
                                   capture_output=True, text=True, timeout=30)
            if local.returncode:
                raise PatchbenchError(f"Image {image} is absent locally and remotely; build it in remote mode first")
            # Save produces a stream, never a large archive on the VM.
            with tempfile.TemporaryFile() as errors:
                save = subprocess.Popen(["podman", "--remote=false", "save", "--format", "docker-archive", image],
                                        stdout=subprocess.PIPE, stderr=errors)
                try:
                    # SSH directly into the server-side loader; remote clients
                    # may spool stdin archives to disk before uploading them.
                    loaded = subprocess.run(self.ssh + [self.host, "podman load"], stdin=save.stdout,
                                            capture_output=True, timeout=600)
                    save.stdout.close()
                    if save.wait(timeout=30) or loaded.returncode:
                        raise PatchbenchError(f"Could not stream image {image} to remote Podman")
                finally:
                    if save.poll() is None:
                        save.kill()
                    save.wait()
            verified = subprocess.run(self.command + ["image", "inspect", "--format", "{{.Id}}", image],
                                      capture_output=True, text=True, check=True, timeout=30)
            if verified.stdout.strip() != local.stdout.strip():
                raise PatchbenchError(f"Remote image identity verification failed: {image}")

    @contextmanager
    def stage_run(self, command: list[str], timeout_seconds: float | None):
        prefix = self.command
        identifier = uuid4().hex
        name = "patchbench-" + identifier
        job = self.root + "/jobs/" + identifier
        secret = self.runtime + "/patchbench-secrets-" + identifier
        self.shell("umask 077; mkdir -- " + shlex.quote(job) + " " + shlex.quote(secret))
        mappings = []
        successful = False
        try:
            # Build a single mapping for aliases of the same source path. A rw
            # worktree also mounted ro as /patches must still be returned once.
            mounts = []
            for i, value in enumerate(command[:-1]):
                if value == "-v":
                    source, target, *options = command[i + 1].split(":", 2)
                    path = Path(source).resolve()
                    mode = options[0].split(",") if options else []
                    private = target in {"/run/patchbench/claude-env.sh", "/run/patchbench/mcp.json"}
                    if target == "/run/patchbench/claude-login":
                        raise PatchbenchError("Native Claude login refresh is local-only; use local mode for --agent-login-dir")
                    immutable = target.endswith("/.m2/repository") and ("ro" in mode or "O" in mode)
                    mounts.append((i + 1, path, target, mode, private, immutable))
            for _, private_path, _, _, private, _ in mounts:
                if private and any(not other_private and parent.is_dir() and private_path.is_relative_to(parent)
                                   for _, parent, _, _, other_private, _ in mounts):
                    raise PatchbenchError("Credential files must be outside ordinary remote worktree mounts")
            # Parent mounts precede child mounts to preserve filesystem aliases.
            for _, source, _, _, private, immutable in sorted(mounts, key=lambda m: len(m[1].parts)):
                if any(source == p or (p.is_dir() and source.is_relative_to(p)) for p, *_ in mappings):
                    continue
                directory = (secret if private else job) + "/mount-" + str(len(mappings))
                if immutable:
                    cached = self.root + "/caches/" + tree_digest(source)
                    if self.shell("test -f " + shlex.quote(cached + "/.complete"), check=False).returncode:
                        self.upload(source, directory)
                        self.shell("touch -- " + shlex.quote(directory + "/.complete") +
                                   "; if ! test -e " + shlex.quote(cached) + "; then mv -T -- " +
                                   shlex.quote(directory) + " " + shlex.quote(cached) +
                                   " || test -f " + shlex.quote(cached + "/.complete") + "; fi")
                    directory = cached
                else:
                    self.upload(source, directory)
                mappings.append((source, directory, private, immutable))
            rewritten = list(command[2:])  # discard the original 'podman run'
            writable = set()
            for index, source, target, mode, private, immutable in mounts:
                parent, directory, protected, frozen = next(
                    m for m in mappings if source == m[0] or (m[0].is_dir() and source.is_relative_to(m[0])))
                relative = source.relative_to(parent).as_posix()
                remote = directory + "/payload" + ("/" + relative if relative != "." else "")
                rewritten[index - 2] = remote + ":" + target + (":" + ",".join(mode) if mode else "")
                if "ro" not in mode and "O" not in mode and not protected and not frozen:
                    writable.add((parent, directory))
            args = prefix + ["run", "--name", name, "--label", "org.patchbench.job=" + identifier]
            if timeout_seconds is not None:
                args += ["--timeout", str(max(1, math.ceil(timeout_seconds)))]
            yield args + rewritten
            self.stop_job(name)
            for source, directory in sorted(writable, key=lambda m: str(m[0])):
                self.download(directory, source)
            successful = True
        except BaseException as exc:
            try:
                self.stop_job(name)
            except Exception:
                pass
            if isinstance(exc, KeyboardInterrupt):
                raise
            raise PatchbenchError(f"Remote container operation failed; workspace retained at {self.host}:{job}. "
                                  "The local result must not be used as a completed evaluation. " +
                                  redact_secrets(str(exc))) from exc
        finally:
            # Credentials are on /run tmpfs, outside all retained workspaces.
            # Only directories created for this unique job may be removed.
            cleanup = self.shell("podman unshare rm -rf -- " + shlex.quote(secret), check=False)
            if cleanup.returncode:
                raise PatchbenchError(f"Could not clean temporary remote credentials at {self.host}:{secret}; "
                                      f"inspect the retained workspace at {job} before resuming")
            if successful:
                self.shell("podman unshare rm -rf -- " + shlex.quote(job), check=False)


@contextmanager
def build_context(containerfile: Path, context: Path):
    """Upload only COPY/ADD inputs, never the whole result-bearing project root."""
    if not remote_enabled():
        yield containerfile, context
        return
    context = context.resolve()
    containerfile = containerfile.resolve()
    with tempfile.TemporaryDirectory(prefix="patchbench-build-") as raw:
        destination = Path(raw)
        recipe = containerfile.relative_to(context)
        inputs = {recipe}
        text = containerfile.read_text().replace("\\\n", " ")
        for line in text.splitlines():
            directive, _, arguments = line.strip().partition(" ")
            if directive.upper() not in {"COPY", "ADD"}:
                if "--mount=" in line:
                    raise PatchbenchError("Remote builds with RUN --mount need an explicit build context")
                continue
            if arguments.startswith("--from="):
                continue
            if arguments.startswith("--"):
                raise PatchbenchError("Remote build COPY options need an explicit build context")
            parts = json.loads(arguments) if arguments.startswith("[") else shlex.split(arguments)
            for source in parts[:-1]:
                if "://" in source:
                    continue
                if "$" in source or PurePosixPath(source).is_absolute() or ".." in PurePosixPath(source).parts:
                    raise PatchbenchError("Remote build input must be an explicit relative path")
                if PurePosixPath(source) == PurePosixPath("."):
                    raise PatchbenchError("Remote build input is too broad: " + source)
                matches = list(context.glob(source))
                if not matches or any(p.resolve() == context or not p.resolve().is_relative_to(context) for p in matches):
                    raise PatchbenchError("Remote build input is missing or too broad: " + source)
                inputs.update(p.relative_to(context) for p in matches)
        for relative in sorted(inputs):
            source, target = context / relative, destination / relative
            if source.is_symlink() or (source.is_dir() and any(
                    p.is_symlink() and not p.resolve().is_relative_to(context) for p in source.rglob("*"))):
                raise PatchbenchError("Remote build input contains an escaping symlink")
            if any(part in {".ssh", ".claude", ".codex", ".aws", ".venv"} or part.startswith(".env") for part in relative.parts):
                raise PatchbenchError("Credential/configuration paths cannot be remote build inputs")
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True,
                                ignore=shutil.ignore_patterns(".git", ".ssh", ".claude", ".codex", ".aws", ".env", ".env.*", ".venv"))
            else:
                shutil.copy2(source, target)
        yield destination / recipe, destination
