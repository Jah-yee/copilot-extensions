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

Everything after the recognized flags below (or a literal ``--`` anywhere in
the remaining arguments) is passed straight through to
``tools/run-plugin-tests.py`` inside the container, so this wrapper's own
CLI surface stays intentionally small.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEVCONTAINER_CONFIG = REPO / ".devcontainer" / "devcontainer.json"
CONTAINER_WORKSPACE = "/workspaces/copilot-extensions"

# Must match the literal volume name baked into ``.devcontainer/
# devcontainer.json``'s ``workspaceMount`` -- ``_per_instance_config``
# below rewrites this to a unique, per-invocation name so concurrent and
# successive runs each get their own isolated, fresh workspace volume
# instead of silently sharing (and accumulating state in) one fixed volume.
BASE_VOLUME_NAME = "copilot-extensions-test-isolation-ws"

# Excluded from the point-in-time copy made into the container: large or
# host-specific artifacts the test run inside the container does not need
# and should not reproduce. Cached venvs are platform/arch-specific and are
# rebuilt fresh inside the container anyway. ``.git`` IS included (despite
# being large) because ``tools/run-plugin-tests.py --changed`` shells out to
# ``git diff``/``git status`` to resolve its target set -- without it,
# those commands fail, their (unchecked) empty output yields an empty
# target set, and the runner would silently report "no plugin suites to
# run" instead of actually running anything.
EXCLUDED_TOP_LEVEL = {
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


def _per_instance_config(instance_label: str) -> tuple[Path, str]:
    """Write a copy of ``DEVCONTAINER_CONFIG`` with its workspace volume
    name made unique to this invocation, so each run gets its own fresh,
    isolated workspace instead of reusing (and accumulating state in) one
    fixed, shared volume across every invocation. Returns the temp config
    path and the volume name it declares, so the caller can remove that
    exact volume at teardown.

    Written into a fresh temp DIRECTORY as literally ``devcontainer.json``
    (not a uniquely-named temp file) -- the devcontainer CLI rejects any
    ``--config`` path whose basename isn't ``devcontainer.json`` or
    ``.devcontainer.json``."""
    volume_name = f"{BASE_VOLUME_NAME}-{instance_label}"
    text = DEVCONTAINER_CONFIG.read_text()
    if BASE_VOLUME_NAME not in text:
        raise SystemExit(
            f"expected volume name '{BASE_VOLUME_NAME}' not found in {DEVCONTAINER_CONFIG}"
        )
    text = text.replace(BASE_VOLUME_NAME, volume_name)
    tmp_dir = Path(tempfile.mkdtemp(prefix="devcontainer-test-isolation-"))
    config_path = tmp_dir / "devcontainer.json"
    config_path.write_text(text)
    return config_path, volume_name


def _bring_up(instance_label: str, config_path: Path) -> str:
    """Run ``devcontainer up`` and return the resulting container id."""
    exe = _devcontainer_exe()
    args = [
        exe, "up",
        "--workspace-folder", str(REPO),
        "--config", str(config_path),
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
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        container_id = obj.get("containerId") or container_id
    if not container_id:
        raise SystemExit("could not determine containerId from `devcontainer up` output")
    return container_id


def _resolve_git_dirs() -> tuple[Path, Path]:
    """Return ``(git_dir, common_dir)`` as absolute paths for the host
    checkout. Equal for a normal checkout; different for a linked worktree
    (this repo's own required flow), where ``git_dir`` is the per-worktree
    private metadata dir and ``common_dir`` is the main checkout's shared
    ``.git`` (objects/refs)."""
    def _rev_parse(flag: str) -> Path:
        res = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", flag],
            capture_output=True, text=True, timeout=30,
        )
        if res.returncode != 0:
            raise SystemExit(f"git rev-parse {flag} failed: {res.stderr.strip()}")
        path = Path(res.stdout.strip())
        return path if path.is_absolute() else (REPO / path).resolve()

    return _rev_parse("--git-dir"), _rev_parse("--git-common-dir")


def _materialized_git_dir(stack: contextlib.ExitStack) -> Path:
    """Return a path to a self-contained ``.git`` directory to copy into
    the container.

    For a normal checkout this is just ``REPO/.git`` (no bug there --
    reviewed and confirmed). For a linked worktree, though, ``.git`` is a
    plain pointer FILE (``gitdir: <absolute host path>``) whose target is
    this HOST's own filesystem layout, meaningless inside the container --
    copying it verbatim would leave `git` inside the container pointing at
    a path that doesn't exist there, so ``run-plugin-tests.py --changed``'s
    `git diff`/`git status` calls would silently return nothing. Instead,
    build a merged, self-contained copy in a temp directory: the shared
    common dir's objects/refs (excluding its ``worktrees/`` subdir, which
    holds every OTHER worktree's unrelated private state) overlaid with
    THIS worktree's own private files (``HEAD``, ``index``, etc.), with the
    now-unnecessary ``commondir`` pointer removed -- the result behaves like
    an ordinary, non-worktree repository."""
    git_dir, common_dir = _resolve_git_dirs()
    if git_dir == common_dir:
        return REPO / ".git"
    tmp_dir = Path(tempfile.mkdtemp(prefix="devcontainer-test-isolation-git-"))
    stack.callback(shutil.rmtree, tmp_dir, ignore_errors=True)
    merged = tmp_dir / ".git"
    shutil.copytree(common_dir, merged, ignore=shutil.ignore_patterns("worktrees"))
    for item in git_dir.iterdir():
        dest = merged / item.name
        if item.is_dir():
            # MERGE (`dirs_exist_ok=True` overlays onto existing content)
            # rather than replace -- the per-worktree dir carries its OWN
            # near-empty `refs`/`logs` subdirectories (for worktree-private
            # refs like `bisect`), and wholesale-replacing the common dir's
            # already-copied `refs` with this one would silently wipe every
            # real branch ref the common dir actually held.
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            if dest.is_dir():
                shutil.rmtree(dest)
            shutil.copy2(item, dest)
    (merged / "commondir").unlink(missing_ok=True)
    return merged


def _tar_of_repo() -> bytes:
    """Build an in-memory tarball of the host checkout, excluding the paths
    in ``EXCLUDED_TOP_LEVEL``. Read-only over the host tree -- never writes
    anything back to it (``.git`` is handled via
    ``_materialized_git_dir``, which only ever READS the host's git
    metadata to build a separate, temporary, self-contained copy)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar, contextlib.ExitStack() as stack:
        for entry in sorted(REPO.iterdir()):
            if entry.name in EXCLUDED_TOP_LEVEL:
                continue
            if entry.name == ".git":
                tar.add(_materialized_git_dir(stack), arcname=".git")
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


def _run_tests(container_id: str, config_path: Path, passthrough: list[str]) -> int:
    exe = _devcontainer_exe()
    args = [
        exe, "exec",
        "--workspace-folder", str(REPO),
        "--config", str(config_path),
        "--container-id", container_id,
        "--", "python", "tools/run-plugin-tests.py", *passthrough,
    ]
    res = subprocess.run(args)
    return res.returncode


def _tear_down(container_id: str, volume_name: str) -> None:
    """Remove the container, then the per-invocation volume it owned --
    both failures are surfaced (never silently swallowed), since a failed
    removal leaves a live container (and any test-spawned descendants it
    holds) running, or an orphaned volume accumulating on the host."""
    errors: list[str] = []
    res = subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        errors.append(f"failed to remove container {container_id}: {res.stderr.strip()}")
    vol = subprocess.run(["docker", "volume", "rm", volume_name], capture_output=True, text=True, timeout=60)
    if vol.returncode != 0:
        errors.append(f"failed to remove volume {volume_name}: {vol.stderr.strip()}")
    if errors:
        raise SystemExit("; ".join(errors))


def _cleanup_orphan(instance_label: str, volume_name: str) -> None:
    """Best-effort cleanup when ``devcontainer up`` itself fails (timeout,
    a failure during ``onCreateCommand`` after the container already
    exists, or unparseable output): a container may have been created under
    this instance's id-label even though ``_bring_up`` never returned an
    id. Finds and removes it by label, then removes the volume, so a failed
    startup never leaks either. Failures here are swallowed deliberately --
    this runs while an already-failing startup error is propagating, and
    that original error is what should surface, not a secondary cleanup
    failure."""
    find = subprocess.run(
        ["docker", "ps", "-aq", "--filter",
         f"label=devcontainer-test-isolation.instance={instance_label}"],
        capture_output=True, text=True, timeout=30,
    )
    for container_id in find.stdout.split():
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, timeout=60)
    subprocess.run(["docker", "volume", "rm", volume_name], capture_output=True, timeout=60)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Run tools/run-plugin-tests.py inside the test-isolation devcontainer."
        ),
    )
    ap.add_argument("--keep", action="store_true",
                     help="leave the container running after the test run (debugging)")
    ns, passthrough = ap.parse_known_args(argv)
    # "--" is argparse's own flags/positionals separator, not a real
    # run-plugin-tests.py argument -- strip every occurrence (not just a
    # leading one), since it can appear anywhere in the extras list
    # (e.g. ``--all -- -k some_filter`` leaves it in the MIDDLE).
    passthrough = [arg for arg in passthrough if arg != "--"]

    instance_label = uuid.uuid4().hex[:12]
    config_path, volume_name = _per_instance_config(instance_label)
    try:
        try:
            container_id = _bring_up(instance_label, config_path)
        except BaseException:
            _cleanup_orphan(instance_label, volume_name)
            raise
        try:
            _populate_workspace(container_id)
            return _run_tests(container_id, config_path, passthrough)
        finally:
            if not ns.keep:
                _tear_down(container_id, volume_name)
    finally:
        shutil.rmtree(config_path.parent, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

