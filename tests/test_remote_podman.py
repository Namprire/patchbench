"""Remote transport boundaries: isolation, data fidelity and failure recovery."""
from pathlib import Path
from contextlib import contextmanager
import os
import subprocess
import tarfile

import pytest

from patchbench.exceptions import PatchbenchError
from patchbench.podman_backend import (
    RemotePodman, _archive_filter, _restore_tree, build_context, podman_command,
)


class FakeRemote(RemotePodman):
    def __init__(self):
        self.root = "/home/runner/.cache/patchbench"
        self.runtime = "/run/user/1001"
        self.host = "worker@server"
        self.uploads, self.downloads, self.stops, self.scripts = [], [], [], []
        self.cache_hit = False
        self.fail_download = False

    @property
    def command(self):
        return ["podman", "--remote", "--url", "unix:///client/api.sock"]

    def shell(self, script, **kwargs):
        self.scripts.append(script)
        return subprocess.CompletedProcess([], (0 if self.cache_hit else 1) if script.startswith("test -f") else 0, "", "")

    def upload(self, source, directory):
        self.uploads.append((source, directory))

    def download(self, directory, source):
        self.downloads.append((directory, source))
        if self.fail_download:
            raise OSError("connection lost")

    def stop_job(self, name):
        self.stops.append(name)


def test_default_backend_does_not_connect(monkeypatch):
    monkeypatch.delenv("PATCHBENCH_REMOTE_HOST", raising=False)
    assert podman_command("info") == ["podman", "info"]


@pytest.mark.parametrize("host,root", [("-o@bad", "/home/runner/jobs"), ("u@h;false", "/home/runner/jobs"),
                                       ("u@host", "/"), ("u@host", "/home/runner/../jobs")])
def test_reject_unsafe_configuration(host, root):
    with pytest.raises(PatchbenchError):
        RemotePodman(host, root, "")


def test_mount_aliases_private_credentials_and_writable_return(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    env = tmp_path / "provider.sh"
    env.write_text("export API_KEY=fixture-value\n")
    remote = FakeRemote()
    original = ["podman", "run", "--rm", "-v", f"{work}:/host", "-v", f"{work}:/patches:ro",
                "-v", f"{env}:/run/patchbench/claude-env.sh:ro", "image", "bash", "-c", "true"]
    with remote.stage_run(original, 2.2) as command:
        assert command[:4] == remote.command
        volumes = [command[i + 1] for i, arg in enumerate(command[:-1]) if arg == "-v"]
        assert volumes[0].split(":")[0] == volumes[1].split(":")[0]
        assert volumes[2].startswith("/run/user/1001/patchbench-secrets-")
        assert command[command.index("--timeout") + 1] == "3"
        assert "fixture-value" not in " ".join(command)
    assert len(remote.uploads) == 2
    assert [p for _, p in remote.downloads] == [work]
    assert remote.stops
    assert all("fixture-value" not in s for s in remote.scripts)
    assert any("rm -rf" in s and "/run/user/1001/patchbench-secrets-" in s for s in remote.scripts)


def test_frozen_cache_is_reused_and_never_downloaded(tmp_path):
    cache = tmp_path / "repository"
    cache.mkdir()
    (cache / "artifact.jar").write_bytes(b"frozen")
    remote = FakeRemote()
    remote.cache_hit = True
    with remote.stage_run(["podman", "run", "--rm", "-v", f"{cache}:/home/node/.m2/repository:O", "image", "true"], 1) as command:
        assert "/caches/" in command[command.index("-v") + 1]
    assert not remote.uploads
    assert not remote.downloads
    assert not any("rm -rf" in s and "/caches/" in s for s in remote.scripts)


def test_transfer_failure_retains_workspace_but_cleans_credentials(tmp_path):
    remote = FakeRemote()
    remote.fail_download = True
    with pytest.raises(PatchbenchError, match="workspace retained"):
        with remote.stage_run(["podman", "run", "-v", f"{tmp_path}:/host", "image", "true"], 1):
            pass
    assert remote.stops
    assert not any("rm -rf" in s and "/jobs/" in s for s in remote.scripts)
    assert any("rm -rf" in s and "/run/user/1001/patchbench-secrets-" in s for s in remote.scripts)


def test_credentials_cannot_be_copied_inside_ordinary_worktree(tmp_path):
    credential = tmp_path / "provider.sh"
    credential.write_text("export API_KEY=fixture-value\n")
    remote = FakeRemote()
    with pytest.raises(PatchbenchError, match="outside ordinary"):
        with remote.stage_run(["podman", "run", "-v", f"{tmp_path}:/workspace", "-v",
                               f"{credential}:/run/patchbench/claude-env.sh:ro", "image", "true"], 1):
            pass
    assert not remote.uploads


def test_existing_remote_image_is_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    remote = FakeRemote()
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "remote-image-id\n", "")
    monkeypatch.setattr("patchbench.podman_backend.subprocess.run", run)
    remote.ensure_image("existing:tag")
    assert commands == [remote.command + ["image", "inspect", "--format", "{{.Id}}", "existing:tag"]]


def test_exception_stops_only_owned_container_and_retains_workspace(tmp_path):
    remote = FakeRemote()
    with pytest.raises(PatchbenchError, match="workspace retained"):
        with remote.stage_run(["podman", "run", "-v", f"{tmp_path}:/host", "image", "true"], 1):
            raise RuntimeError("stream failed")
    assert len(remote.stops) == 1 and remote.stops[0].startswith("patchbench-")
    assert not remote.downloads


def test_restore_handles_deletions_modes_and_replaces_links_without_following(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    after.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("unchanged")
    (before / "old").write_text("remove")
    (before / "directory").symlink_to(outside, target_is_directory=True)
    (after / "directory").mkdir()
    (after / "directory" / "new").write_text("new")
    (after / "executable").write_text("run")
    (after / "executable").chmod(0o755)
    _restore_tree(after, before)
    assert not (before / "old").exists()
    assert (before / "directory" / "new").read_text() == "new"
    assert (outside / "keep").read_text() == "unchanged"
    assert (before / "executable").stat().st_mode & 0o777 == 0o755


def test_restore_replaces_readonly_git_pack_without_modifying_hardlink(tmp_path):
    source = tmp_path / "returned-pack"
    source.write_bytes(b"new pack")
    original = tmp_path / "original-pack"
    original.write_bytes(b"original pack")
    original.chmod(0o444)
    destination = tmp_path / "checkout-pack"
    os.link(original, destination)
    _restore_tree(source, destination)
    assert destination.read_bytes() == b"new pack"
    assert original.read_bytes() == b"original pack"


@pytest.mark.parametrize("name", ["/tmp/escape", "payload/../../escape", "elsewhere/file"])
def test_reject_return_archive_traversal(tmp_path, name):
    with pytest.raises(PatchbenchError):
        _archive_filter(tarfile.TarInfo(name), str(tmp_path))


def test_reject_return_archive_symlink_escape(tmp_path):
    member = tarfile.TarInfo("payload/link")
    member.type = tarfile.SYMTYPE
    member.linkname = "../../escape"
    with pytest.raises(tarfile.FilterError):
        _archive_filter(member, str(tmp_path))


def test_return_archive_preserves_private_directory_mode(tmp_path):
    member = tarfile.TarInfo("payload")
    member.type = tarfile.DIRTYPE
    member.mode = 0o700
    assert _archive_filter(member, str(tmp_path)).mode == 0o700


def test_remote_build_context_only_includes_declared_inputs(tmp_path, monkeypatch):
    monkeypatch.setenv("PATCHBENCH_REMOTE_HOST", "u@host")
    (tmp_path / "images").mkdir()
    recipe = tmp_path / "images" / "Containerfile"
    recipe.write_text("FROM base\nCOPY images/requirements.txt /tmp/requirements.txt\n")
    (tmp_path / "images" / "requirements.txt").write_text("dependency")
    (tmp_path / ".env").write_text("private")
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "large").write_text("old results")
    with build_context(recipe, tmp_path) as (selected, root):
        assert selected.is_file()
        assert (root / "images" / "requirements.txt").read_text() == "dependency"
        assert not (root / ".env").exists()
        assert not (root / "artifacts").exists()


def test_remote_build_rejects_whole_project_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("PATCHBENCH_REMOTE_HOST", "u@host")
    recipe = tmp_path / "Containerfile"
    recipe.write_text("FROM base\nCOPY . /app\n")
    with pytest.raises(PatchbenchError, match="too broad"):
        with build_context(recipe, tmp_path):
            pass


def test_remote_credential_mount_reuses_original_without_probe(tmp_path, monkeypatch):
    from patchbench.containers import _claude_env_mount
    monkeypatch.setenv("PATCHBENCH_REMOTE_HOST", "u@host")
    env = tmp_path / "provider.sh"
    env.write_text("export API_KEY=fixture-value\n")
    monkeypatch.setattr("patchbench.containers._podman_can_read_file", lambda *_: pytest.fail("local credential probe"))
    assert _claude_env_mount("image", env) == (env, False)


def test_transport_errors_redact_provider_values(monkeypatch):
    from patchbench.containers import _run_streaming_container_process
    class Broken:
        @contextmanager
        def stage_run(self, *_):
            raise PatchbenchError("fixture-error in a returned filename")
            yield
    monkeypatch.setattr("patchbench.containers.remote_backend", lambda: Broken())
    with pytest.raises(PatchbenchError) as raised:
        _run_streaming_container_process(["podman", "run"], timeout_seconds=1,
            environment={"API_KEY": "fixture-error"}, redaction_command="",
            progress_callback=lambda *_: None)
    assert "fixture-error" not in str(raised.value)
    assert "<redacted>" in str(raised.value)


def test_remote_image_identity_uses_selected_backend(monkeypatch):
    from patchbench import historical_pr_tests as module
    monkeypatch.setattr(module, "podman_command", lambda *args: ["remote-client", *args])
    def run(command, **kwargs):
        assert command[:3] == ["remote-client", "image", "inspect"]
        return type("Result", (), {"exit_code": 0, "stdout": "image-id\n"})()
    monkeypatch.setattr(module, "run_command", run)
    assert module.image_identity("image") == "image-id"
