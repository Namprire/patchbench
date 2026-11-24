# Remote coding containers

PatchBench can run on one managing machine while rootless Podman executes coding
containers on another Linux host. The server needs SSH, Podman, systemd, tar and
Python's standard library. It does not need a PatchBench installation.

Keep the existing local mode as a fallback. First verify SSH access and the
server's host key through a trusted channel. On the server, check `podman info`,
available disk space and subordinate UID/GID mappings. Container storage must
use supported local storage, such as XFS with d_type support or ext4, rather
than a network filesystem. The per-user `/run/user/<uid>` directory must be tmpfs.
Use a dedicated private staging directory owned by the remote account, mode 700.

## Select the backend

Set these variables for one invocation, substituting your own host, private
staging path, validated case and provider files:

```bash
PATCHBENCH_REMOTE_HOST=runner@worker.example.org \
PATCHBENCH_REMOTE_ROOT=/home/runner/.cache/patchbench \
PATCHBENCH_REMOTE_KNOWN_HOSTS="$HOME/.ssh/known_hosts" \
patchbench run "$CASE_DIR" \
  --agent claude-code --container --live \
  --agent-model YOUR_PROVIDER_MODEL_ID \
  --agent-env-file "$PROVIDER_ENV" \
  --agent-max-turns 20 --agent-timeout-seconds 300 \
  --run-id "remote-model-$(date -u +%Y%m%d-%H%M%S)-$$"
```

Use `--agent-ca-file /path/to/provider-ca.pem` if your provider needs a private CA.
All existing API configuration stays on the client. The remote solver must have
network access to the provider. A model listed by a gateway is not proof that
inference works; validate one small real request before scheduling a study.
The example's turn/time limits bound activity, not monetary charges.

The backend starts the remote user's `podman.socket` and forwards its Unix
socket over SSH. It does not expose a TCP API, enable boot services or require
sudo. Existing SSH configuration is used without copying private keys. No
server-specific launcher or endpoint is bundled in the public profile.

## Files, credentials and results

Only declared bind sources are uploaded. Per-container workspaces are isolated
under the staging root. Writable scratch trees, including deletions and Git
changes, return before local evaluation continues. Immutable Maven cache copies
are reused by content digest; private overlay mounts preserve shared inputs.
Whole writable trees are currently retrieved, so local temporary disk space is
still needed. Linked Git worktrees with external `.git` paths are rejected.

API provider scripts and MCP configuration are staged separately in private
tmpfs directories and mounted read-only. They are excluded from images, build
contexts and retained job workspaces. Originals are unchanged. The remote host
necessarily handles runtime credential material; only use a trusted execution
host. Native `--agent-login-dir` refresh is currently local-only.

Run results, patches, prompts, events and logs are saved in the normal client-side
`artifacts/` tree. Metadata records the remote backend, host and graphroot. No
private corpus or model results are bundled with this distribution. Optional
service-specific answer experiments are not distributed in this public profile.

## Images, builds and interruption

Missing remote images are streamed from the local store over SSH and checked by
full image ID. Existing remote tags are not overwritten automatically. Frozen
case identity checks inspect the selected remote store. Supported build commands
also target that store, using restricted contexts containing the recipe and its
declared COPY/ADD inputs. Broad `COPY .`, dynamic inputs, unsupported COPY flags
and RUN bind mounts require a reviewed context extension and are rejected.

Timeout or Ctrl+C stops only the current run's uniquely named container and
retrieves partial work. Output archives are checked for unsafe paths and links.
Transfer failures retain the remote workspace and report an infrastructure error;
they never silently switch to local execution. A network outage can prevent
immediate cleanup: reconnect and inspect the reported workspace and matching
temporary credential directory before resuming. Never prune unrelated workloads.

## Local fallback

Leave the remote settings unset, or remove the host setting for one invocation:

```bash
env -u PATCHBENCH_REMOTE_HOST patchbench run "$CASE_DIR" \
  --agent gold --container --live \
  --run-id "local-gold-$(date -u +%Y%m%d-%H%M%S)-$$"
```

Existing local images and results remain available. Moving new container work to
the server does not reclaim existing local disk space. HTTP service binding
options such as `serve --allow-remote` are independent of container execution.
