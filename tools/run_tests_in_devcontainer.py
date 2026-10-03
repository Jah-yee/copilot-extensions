#!/usr/bin/env python3
"""Run a plugin's pytest suite inside the test-isolation devcontainer.

Phase 1 of the ``devcontainer-test-isolation`` effort
(``efforts/active/devcontainer-test-isolation/README.md``): invokes the
``.devcontainer/devcontainer.json`` spec and runs
``tools/run-plugin-tests.py`` *inside* it, for a real OS-level filesystem/
network boundary on top of (not instead of) that runner's existing
process-level containment.

This is a deliberately separate, opt-in wrapper -- it never replaces
``run-plugin-tests.py`` for contributors who aren't using the devcontainer
(the vast majority of local and CI runs), and it never mounts the host
checkout into the container. Everything the container's test run sees is a
point-in-time COPY (see ``_populate_workspace`` below): the host checkout is
only ever read from, never written to, by anything this script spawns.

Usage::

    python tools/run_tests_in_devcontainer.py agent-worktrees
    python tools/run_tests_in_devcontainer.py --changed
    python tools/run_tests_in_devcontainer.py --all -- -k some_filter

Everything after the recognized flags below (or after a literal ``--``) is
passed straight through to ``tools/run-plugin-tests.py`` inside the
container, so this wrapper's own CLI surface stays intentionally small.
"""

from __future__ import annotations

import argparse
import io
import shutil
import subprocess
import tarfile
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEVCONTAINER_CONFIG = REPO / ".devcontainer" / "devcontainer.json"
CONTAINER_WORKSPACE = "/workspaces/copilot-extensions"

# Excluded from the point-in-time copy made into the container: large or
# host-specific artifacts the test run inside the container does not need
# and should not reproduce (cached venvs are platform/arch-specific and are
# rebuilt fresh inside the container anyway; `.git` is excluded because the
# copy is a plain tarball, not a repository, and no test suite in this repo
# depends on it being present -- see the effort README's known-limitations
# note for the one tracked exception, copilot-extensions#5050's
# provenance-SHA check).
EXCLUDED_TOP_LEVEL = {
    ".git",
    ".test-venvs",
    ".devcontainer",
    "node_modules",
    "__pycache__",
}


def _devcontainer_exe() -> str:
    exe = shutil.which("devcontainer")
    if not exe:
        raise SystemExit(
            "devcontainer CLI not found. Install with `npm i -g @devcontainers/cli`."
        )
    return exe


def _bring_up(instance_label: str) -> str:
    """Run ``devcontainer up`` and return the resulting container id."""
    exe = _devcontainer_exe()
    args = [
        exe, "up",
        "--workspace-folder", str(REPO),
        "--config", str(DEVCONTAINER_CONFIG),
        "--id-label", f"devcontainer-test-isolation.instance={instance_label}",
    ]
    res = subprocess.run(args, capture_output=True, text=True, timeout=1800)
    if res.returncode != 0:
        raise SystemExit(f"devcontainer up failed: {res.stderr.strip() or res.stdout.strip()}")
    container_id = None
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        import json

        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        container_id = obj.get("containerId") or container_id
    if not container_id:
        raise SystemExit("could not determine containerId from `devcontainer up` output")
    return container_id


def _tar_of_repo() -> bytes:
    """Build an in-memory tarball of the host checkout, excluding the paths
    in ``EXCLUDED_TOP_LEVEL``. Read-only over the host tree -- never writes
    anything back to it."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for entry in sorted(REPO.iterdir()):
            if entry.name in EXCLUDED_TOP_LEVEL:
                continue
            tar.add(entry, arcname=entry.name)
    return buf.getvalue()


def _populate_workspace(container_id: str) -> None:
    """Copy a point-in-time snapshot of the host checkout into the
    container's workspace VOLUME (never a host bind -- see
    ``.devcontainer/devcontainer.json``'s workspace-storage-model comment).

    Extraction runs as the container's root user with ``--no-same-owner``:
    the runtime posture drops every Linux capability (``--cap-drop=ALL``),
    so even root cannot ``chown`` extracted files to the HOST checkout's
    original (and here, meaningless) uid/gid -- ``--no-same-owner`` avoids
    that chown attempt entirely by leaving new files owned by the extracting
    process (root) instead. The follow-up ``chmod`` grants the non-root
    ``vscode`` remote user (who actually runs the test suite) write access
    without needing a capability-gated ``chown``/``chgrp`` -- root may
    always ``chmod`` files it owns, no capability required.
    """
    payload = _tar_of_repo()
    res = subprocess.run(
        [
            "docker", "exec", "-i", "-u", "root", container_id,
            "tar", "--no-same-owner", "-xf", "-", "-C", CONTAINER_WORKSPACE,
        ],
        input=payload,
        capture_output=True,
        timeout=600,
    )
    if res.returncode != 0:
        raise SystemExit(
            f"failed to populate container workspace: {res.stderr.decode(errors='replace').strip()}"
        )
    chmod = subprocess.run(
        ["docker", "exec", "-u", "root", container_id,
         "chmod", "-R", "a+rwX", CONTAINER_WORKSPACE],
        capture_output=True, text=True, timeout=120,
    )
    if chmod.returncode != 0:
        raise SystemExit(f"failed to open up container workspace permissions: {chmod.stderr.strip()}")


def _run_tests(container_id: str, passthrough: list[str]) -> int:
    exe = _devcontainer_exe()
    args = [
        exe, "exec",
        "--workspace-folder", str(REPO),
        "--config", str(DEVCONTAINER_CONFIG),
        "--container-id", container_id,
        "--", "python", "tools/run-plugin-tests.py", *passthrough,
    ]
    res = subprocess.run(args)
    return res.returncode


def _tear_down(container_id: str) -> None:
    subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, timeout=60)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Run tools/run-plugin-tests.py inside the test-isolation devcontainer."
        ),
    )
    ap.add_argument("--keep", action="store_true",
                     help="leave the container running after the test run (debugging)")
    ns, passthrough = ap.parse_known_args(argv)
    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]

    instance_label = uuid.uuid4().hex[:12]
    container_id = _bring_up(instance_label)
    try:
        _populate_workspace(container_id)
        return _run_tests(container_id, passthrough)
    finally:
        if not ns.keep:
            _tear_down(container_id)


if __name__ == "__main__":
    raise SystemExit(main())
