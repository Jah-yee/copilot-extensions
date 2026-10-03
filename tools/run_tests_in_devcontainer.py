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
point-in-time COPY (see ``_write_tar_of_repo`` below): the host checkout is
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
import json
import os
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

# The workspace volume's size is bounded (a tmpfs-backed Docker volume, not
# the default unbounded local-disk volume) so a buggy or adversarial test
# cannot fill the host's Docker storage before teardown runs -- matches the
# bounded-writable-surface model `agent-containers`' own restricted fleet
# uses (`plugins/agent-containers/src/agent_containers/fleet.py`'s tmpfs
# surfaces). The checkout snapshot plus a fresh venv comfortably fits.
WORKSPACE_VOLUME_SIZE = "4g"

# Excluded from the point-in-time copy made into the container even if
# `git ls-files` would otherwise include them: large, host-specific
# artifacts the test run inside the container does not need and should not
# reproduce. Belt-and-suspenders only -- `_tracked_and_untracked_paths`
# already excludes anything gitignored (including `.test-venvs`, which is
# git-ignored per `TESTING.md`).
EXCLUDED_TOP_LEVEL = {
    ".test-venvs",
    ".devcontainer",
    "node_modules",
    "__pycache__",
}

#: Ambient Git repository-selection variables that must never leak into a
#: subprocess here -- if the calling environment has e.g. `GIT_DIR` or
#: `GIT_WORK_TREE` set, it silently overrides our own explicit `-C REPO`,
#: so the snapshot could be built from an entirely different repository
#: than the one we were asked about. Mirrors
#: `tools/coverage_guided_selection/ancestor_resolution.py`'s
#: `scrubbed_git_env` (kept in sync by hand, not by import, matching that
#: module's own "dependency-free by design" precedent).
_REPOSITORY_CONTEXT_ENV = frozenset({
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_DIR",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_GRAFT_FILE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_INTERNAL_SUPER_PREFIX",
    "GIT_NAMESPACE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_QUARANTINE_PATH",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
    "GIT_WORK_TREE",
})


def _scrubbed_git_env() -> dict[str, str]:
    """Ambient environment with every repository-selection variable
    removed -- every git subprocess below supplies its target repository
    explicitly via ``-C``; any of these inherited variables would silently
    override that."""
    env = os.environ.copy()
    for name in list(env):
        upper = name.upper()
        if (
            upper in _REPOSITORY_CONTEXT_ENV
            or upper.startswith("GIT_CONFIG_KEY_")
            or upper.startswith("GIT_CONFIG_VALUE_")
        ):
            env.pop(name, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


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


def _create_bounded_volume(volume_name: str) -> None:
    """Create the per-invocation workspace volume up front, as a
    size-bounded tmpfs-backed volume (not the default unbounded local-disk
    volume) -- see ``WORKSPACE_VOLUME_SIZE``. ``devcontainer up`` creates
    the volume implicitly if it doesn't already exist, but implicitly means
    with Docker's own unbounded default; creating it explicitly first with
    these options means ``devcontainer up`` just reuses it instead."""
    res = subprocess.run(
        [
            "docker", "volume", "create",
            "--driver", "local",
            "--opt", "type=tmpfs",
            "--opt", "device=tmpfs",
            "--opt", f"o=size={WORKSPACE_VOLUME_SIZE}",
            volume_name,
        ],
        capture_output=True, text=True, timeout=30,
    )
    if res.returncode != 0:
        raise SystemExit(f"failed to create bounded workspace volume: {res.stderr.strip()}")


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


def _tracked_and_untracked_paths() -> list[str]:
    """Repo-relative paths of every file the snapshot should contain:
    git-tracked files plus untracked-but-not-ignored ones -- deliberately
    NOT every file physically present under ``REPO``. A plain directory
    walk would also copy gitignored, potentially secret-bearing files
    (local credentials, `.env`-style config) into a container that then has
    outbound network access, letting an adversarial/buggy test exfiltrate
    host-only state. ``git ls-files`` with ``--exclude-standard`` is the
    same boundary contributors and CI already trust to keep such files out
    of the repository in the first place."""
    res = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z",
         "--cached", "--others", "--exclude-standard"],
        capture_output=True, timeout=60, env=_scrubbed_git_env(),
    )
    if res.returncode != 0:
        raise SystemExit(
            f"git ls-files failed: {res.stderr.decode(errors='replace').strip()}"
        )
    paths = [p for p in res.stdout.decode().split("\0") if p]
    excluded_prefixes = tuple(f"{name}/" for name in EXCLUDED_TOP_LEVEL)
    return [
        p for p in paths
        if p not in EXCLUDED_TOP_LEVEL and not p.startswith(excluded_prefixes)
    ]


def _resolve_git_dirs() -> tuple[Path, Path]:
    """Return ``(git_dir, common_dir)`` as absolute paths for the host
    checkout. Equal for a normal checkout; different for a linked worktree
    (this repo's own required flow), where ``git_dir`` is the per-worktree
    private metadata dir and ``common_dir`` is the main checkout's shared
    ``.git`` (objects/refs)."""
    def _rev_parse(flag: str) -> Path:
        res = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", flag],
            capture_output=True, text=True, timeout=30, env=_scrubbed_git_env(),
        )
        if res.returncode != 0:
            raise SystemExit(f"git rev-parse {flag} failed: {res.stderr.strip()}")
        path = Path(res.stdout.strip())
        return path if path.is_absolute() else (REPO / path).resolve()

    return _rev_parse("--git-dir"), _rev_parse("--git-common-dir")


# A fresh, credential-free `.git/config` written into every materialized
# copy (see `_materialized_git_dir` below) -- deliberately NOT a copy of
# the host's own config, which may embed an authenticated remote URL,
# `credential.helper` settings, or other credential-bearing values. None of
# that is needed for `git diff`/`git status`/`git rev-parse` against
# already-resolved local refs; losing it only matters for `fetch`/`push`
# network operations this wrapper's own `git` calls never perform.
_MINIMAL_GIT_CONFIG = (
    "[core]\n"
    "\trepositoryformatversion = 0\n"
    "\tfilemode = true\n"
    "\tbare = false\n"
    "\tlogallrefupdates = true\n"
)


def _materialized_git_dir(stack: contextlib.ExitStack) -> Path:
    """Return a path to a self-contained ``.git`` directory to copy into
    the container.

    A normal checkout's ``.git`` is a real, self-contained directory
    already. A linked worktree's ``.git``, however, is a plain pointer FILE
    (``gitdir: <absolute host path>``) whose target is this HOST's own
    filesystem layout, meaningless inside the container -- copying it
    verbatim would leave `git` inside the container pointing at a path
    that doesn't exist there, so ``run-plugin-tests.py --changed``'s `git
    diff`/`git status` calls would silently return nothing. For that case,
    build a merged, self-contained copy in a temp directory instead: the
    shared common dir's objects/refs (excluding its ``worktrees/`` subdir,
    which holds every OTHER worktree's unrelated private state) MERGED with
    (not replaced by -- the per-worktree dir carries its own near-empty
    ``refs``/``logs`` subdirectories too, and replacing would silently wipe
    every real branch ref) this worktree's own private files (``HEAD``,
    ``index``, etc.). Either way, ``config`` is always replaced with a
    fresh, credential-free one (see ``_MINIMAL_GIT_CONFIG``) and ``hooks``
    is always dropped, since neither is needed for `diff`/`status`/
    `rev-parse` and either could carry credential-bearing or otherwise
    sensitive content."""
    git_dir, common_dir = _resolve_git_dirs()
    tmp_dir = Path(tempfile.mkdtemp(prefix="devcontainer-test-isolation-git-"))
    stack.callback(shutil.rmtree, tmp_dir, ignore_errors=True)
    merged = tmp_dir / ".git"
    if git_dir == common_dir:
        shutil.copytree(common_dir, merged)
    else:
        shutil.copytree(common_dir, merged, ignore=shutil.ignore_patterns("worktrees"))
        for item in git_dir.iterdir():
            dest = merged / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                if dest.is_dir():
                    shutil.rmtree(dest)
                shutil.copy2(item, dest)
        (merged / "commondir").unlink(missing_ok=True)
    (merged / "config").write_text(_MINIMAL_GIT_CONFIG)
    shutil.rmtree(merged / "hooks", ignore_errors=True)
    return merged


def _write_tar_of_repo(dest: Path) -> None:
    """Write a tarball of the host checkout to ``dest`` on disk (never held
    in memory as one ``bytes`` object -- a checkout with a large object
    store or build artifacts could otherwise need several times its own
    size in process memory, between an in-memory buffer and ``subprocess``'s
    own copy of an ``input=`` payload). Only ever READS the host tree --
    ``.git`` is handled via ``_materialized_git_dir``, which builds a
    separate, temporary, self-contained copy rather than touching the real
    one; everything else comes from ``_tracked_and_untracked_paths``, so
    gitignored (and potentially secret-bearing) files are never included."""
    with tarfile.open(dest, mode="w") as tar, contextlib.ExitStack() as stack:
        tar.add(_materialized_git_dir(stack), arcname=".git")
        for rel_path in _tracked_and_untracked_paths():
            tar.add(REPO / rel_path, arcname=rel_path)


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
    with tempfile.NamedTemporaryFile(
        prefix="devcontainer-test-isolation-snapshot-", suffix=".tar",
    ) as tar_file:
        _write_tar_of_repo(Path(tar_file.name))
        tar_file.seek(0)
        res = subprocess.run(
            [
                "docker", "exec", "-i", "-u", "root", container_id,
                "tar", "--no-same-owner", "-xf", "-", "-C", CONTAINER_WORKSPACE,
            ],
            stdin=tar_file,
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
    startup never leaks either. Every subprocess call here is individually
    guarded against ``subprocess.SubprocessError``/``OSError`` (a
    ``TimeoutExpired``, or the ``docker`` binary vanishing mid-cleanup) so
    one failing step never skips the rest, and this function itself never
    raises -- it runs while an already-failing startup error is
    propagating, and that original error is what must surface, not a
    secondary cleanup failure."""
    container_ids: list[str] = []
    try:
        find = subprocess.run(
            ["docker", "ps", "-aq", "--filter",
             f"label=devcontainer-test-isolation.instance={instance_label}"],
            capture_output=True, text=True, timeout=30,
        )
        container_ids = find.stdout.split()
    except (subprocess.SubprocessError, OSError):
        pass
    for container_id in container_ids:
        try:
            subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, timeout=60)
        except (subprocess.SubprocessError, OSError):
            pass
    try:
        subprocess.run(["docker", "volume", "rm", volume_name], capture_output=True, timeout=60)
    except (subprocess.SubprocessError, OSError):
        pass


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
            _create_bounded_volume(volume_name)
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
