#!/usr/bin/env python3
"""Run a plugin's pytest suite inside the test-isolation devcontainer.

Phase 1 of the ``devcontainer-test-isolation`` effort
(``efforts/active/devcontainer-test-isolation/README.md``): invokes the
``.devcontainer/devcontainer.json`` spec and runs
``tools/run-plugin-tests.py`` *inside* it, for a real OS-level filesystem/
privilege boundary on top of (not instead of) that runner's existing
process-level containment. Networking is NOT (yet) part of that boundary --
the container keeps Docker's default bridge with full outbound reach (a
known, named, open design gap; see the effort README's Phase 1 journal).

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
import sys
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
# reproduce. Belt-and-suspenders only -- `_tracked_paths` already excludes
# anything gitignored (including `.test-venvs`, which is git-ignored per
# `TESTING.md`).
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


def _tracked_paths(*, include_untracked: bool) -> list[str]:
    """Repo-relative paths of files the snapshot should contain --
    deliberately NOT every file physically present under ``REPO``. Default
    (``include_untracked=False``) is git-TRACKED files only
    (``git ls-files --cached``): this repository has no blanket `.gitignore`
    rule for `.env`-style config or arbitrary credential filenames, so an
    untracked-but-not-ignored secret file sitting in the working tree would
    otherwise still be copied into a container that has outbound network
    access, letting an adversarial/buggy test exfiltrate it -- tracked
    files are the only set contributors and CI already trust as safe to
    share. ``include_untracked=True`` (the wrapper's own ``--include-
    untracked`` flag) additionally includes untracked-but-not-gitignored
    files via ``--others --exclude-standard``, for the deliberate, opt-in
    case of testing new, not-yet-committed files -- never the default."""
    args = ["git", "-C", str(REPO), "ls-files", "-z", "--cached"]
    if include_untracked:
        args += ["--others", "--exclude-standard"]
    res = subprocess.run(args, capture_output=True, timeout=60, env=_scrubbed_git_env())
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


def _resolve_base_ref(passthrough: list[str]) -> str:
    """Best-effort extraction of the ``--base`` value a passthrough
    invocation will use, so ``_materialized_git_dir`` can include exactly
    that ref's object closure (not the whole repository's history) in the
    bundled snapshot. Falls back to ``tools/run-plugin-tests.py``'s own
    ``--base`` default when it isn't present in ``passthrough`` -- mirroring
    that runner's own argparse default, not guessing at a different one.
    Mirrors argparse's own last-occurrence-wins behavior for a repeated
    flag -- keeps scanning instead of returning on the first match, since
    `run-plugin-tests.py`'s own argparse would use the LAST ``--base``."""
    resolved = "origin/main"
    for i, arg in enumerate(passthrough):
        if arg == "--base" and i + 1 < len(passthrough):
            resolved = passthrough[i + 1]
        elif arg.startswith("--base="):
            resolved = arg.split("=", 1)[1]
    return resolved


def _git_rev_parse(ref: str) -> str | None:
    """Resolve ``ref`` to a commit sha via the scrubbed environment.
    Returns ``None`` (rather than raising) when it doesn't resolve locally
    -- an unresolvable ``--base`` is a degraded-but-not-fatal condition for
    the snapshot (the container's own ``--changed`` run will then fail the
    same way a host run would against a ref nobody fetched)."""
    res = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--verify", ref],
        capture_output=True, text=True, timeout=30, env=_scrubbed_git_env(),
    )
    return res.stdout.strip() if res.returncode == 0 else None


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


def _materialized_git_dir(stack: contextlib.ExitStack, passthrough: list[str]) -> Path:
    """Return a path to a self-contained ``.git`` directory to copy into
    the container, containing ONLY the object closure of ``HEAD`` and the
    ``--changed`` diff base -- never the full repository history.

    Copying the full local git database (every branch, stash, reflog, and
    unreachable object) into a container that deliberately keeps outbound
    networking would let an adversarial/buggy test enumerate and exfiltrate
    local-only content having nothing to do with the plugin suite it's
    meant to run. Instead: ``git bundle create`` with only ``HEAD`` and
    (when it resolves locally) the ``--base`` ref `run-plugin-tests.py
    --changed`` will actually diff against, then ``git clone --bare`` that
    bundle into a fresh directory -- the clone contains exactly the commits
    reachable from those two tips, nothing else. The base ref is then
    fetched again under its own fully-qualified name (e.g.
    ``refs/remotes/origin/dev``) so `--changed`` can resolve it by that
    name, exactly as it would on the host. The index is then rebuilt from
    ``HEAD`` itself (``git read-tree HEAD``) rather than copied from the
    host: the host's real index can reference a staged blob that is
    genuinely unreachable from both ``HEAD`` and the base ref (a staged-
    but-uncommitted new/modified file), which the bundle would then be
    missing entirely -- a copied index pointing at a missing object breaks
    `git diff`/`status` outright. Rebuilding from `HEAD` instead means
    staging state isn't preserved as "staged" inside the container, but
    every modification (staged or not) is still visible as an ordinary
    working-tree difference, since the modified file's actual CURRENT
    on-disk content is what `_tracked_paths` copies in regardless. As with
    every materialized copy, ``config`` is replaced with a fresh,
    credential-free one (see ``_MINIMAL_GIT_CONFIG``) and ``hooks`` is
    dropped entirely, since neither is needed for `diff`/`status`/
    `rev-parse` and either could carry credential-bearing or otherwise
    sensitive content."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="devcontainer-test-isolation-git-"))
    stack.callback(shutil.rmtree, tmp_dir, ignore_errors=True)
    bundle_file = tmp_dir / "snapshot.bundle"
    merged = tmp_dir / ".git"

    base_ref = _resolve_base_ref(passthrough)
    base_resolves = _git_rev_parse(base_ref) is not None
    bundle_refs = ["HEAD", base_ref] if base_resolves else ["HEAD"]

    bundle_res = subprocess.run(
        ["git", "-C", str(REPO), "bundle", "create", str(bundle_file), *bundle_refs],
        capture_output=True, text=True, timeout=300, env=_scrubbed_git_env(),
    )
    if bundle_res.returncode != 0:
        raise SystemExit(f"git bundle create failed: {bundle_res.stderr.strip()}")

    clone_res = subprocess.run(
        ["git", "clone", "--bare", "--quiet", str(bundle_file), str(merged)],
        capture_output=True, text=True, timeout=120, env=_scrubbed_git_env(),
    )
    if clone_res.returncode != 0:
        raise SystemExit(f"git clone (from bundle) failed: {clone_res.stderr.strip()}")

    if base_resolves:
        full_name_res = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "--symbolic-full-name", base_ref],
            capture_output=True, text=True, timeout=30, env=_scrubbed_git_env(),
        )
        full_name = full_name_res.stdout.strip()
        if full_name_res.returncode == 0 and full_name:
            fetch_res = subprocess.run(
                ["git", f"--git-dir={merged}", "fetch", "--quiet", str(bundle_file),
                 f"{full_name}:{full_name}"],
                capture_output=True, text=True, timeout=120, env=_scrubbed_git_env(),
            )
            if fetch_res.returncode != 0:
                raise SystemExit(f"git fetch (base ref) failed: {fetch_res.stderr.strip()}")

    read_tree_res = subprocess.run(
        ["git", f"--git-dir={merged}", "read-tree", "HEAD"],
        capture_output=True, text=True, timeout=60, env=_scrubbed_git_env(),
    )
    if read_tree_res.returncode != 0:
        raise SystemExit(f"git read-tree HEAD failed: {read_tree_res.stderr.strip()}")

    (merged / "config").write_text(_MINIMAL_GIT_CONFIG)
    shutil.rmtree(merged / "hooks", ignore_errors=True)
    return merged


def _write_tar_of_repo(dest: Path, passthrough: list[str], *, include_untracked: bool) -> None:
    """Write a tarball of the host checkout to ``dest`` on disk (never held
    in memory as one ``bytes`` object -- a checkout with a large object
    store or build artifacts could otherwise need several times its own
    size in process memory, between an in-memory buffer and ``subprocess``'s
    own copy of an ``input=`` payload). Only ever READS the host tree --
    ``.git`` is handled via ``_materialized_git_dir``, which builds a
    separate, temporary, minimal-history copy rather than touching the real
    one; everything else comes from ``_tracked_paths``, so gitignored (and,
    unless ``include_untracked`` is explicitly set, untracked) files are
    never included.

    ``git ls-files --cached`` still lists a path for an unstaged (not yet
    `git add`-ed) deletion -- the index entry exists even though the file
    itself is gone from the working tree -- so each path is checked with
    ``os.path.lexists`` (not a symlink-following ``Path.exists()``, which
    would wrongly skip an intact symlink whose target happens to be
    missing) before being archived; a path absent from the working tree is
    silently skipped rather than raising. The rebuilt index (see
    ``_materialized_git_dir``'s ``git read-tree HEAD``) already represents
    that deletion correctly for `git status`/`git diff` -- only the
    physical tar entry is skipped.

    ``git ls-files`` also lists an initialized submodule as a single
    ``160000``-mode path that happens to be a real DIRECTORY on disk --
    ``tarfile.add`` recursively archives directories by default, which
    would copy that submodule's entire working tree (including its own
    ignored/untracked files and `.git` metadata) wholesale, defeating the
    tracked-files-only boundary this function exists to enforce.
    ``recursive=False`` below means a submodule path is still added (as an
    empty directory entry), but never its contents -- this repository has
    no submodules today, but the guard costs nothing and must not regress
    silently if one is ever added.
    """
    with tarfile.open(dest, mode="w") as tar, contextlib.ExitStack() as stack:
        tar.add(_materialized_git_dir(stack, passthrough), arcname=".git")
        for rel_path in _tracked_paths(include_untracked=include_untracked):
            abs_path = REPO / rel_path
            if os.path.lexists(abs_path):
                tar.add(abs_path, arcname=rel_path, recursive=False)


def _populate_workspace(container_id: str, passthrough: list[str], *, include_untracked: bool) -> None:
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
        _write_tar_of_repo(Path(tar_file.name), passthrough, include_untracked=include_untracked)
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
    holds) running, or an orphaned volume accumulating on the host. Each
    removal is individually guarded against
    ``subprocess.SubprocessError``/``OSError`` (a ``TimeoutExpired``, or the
    ``docker`` binary vanishing mid-teardown) so a raised exception from the
    container removal can never skip the volume removal that follows it --
    both are always attempted, and any failure (a nonzero exit OR a raised
    exception) from either is collected and surfaced together."""
    errors: list[str] = []
    try:
        res = subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            errors.append(f"failed to remove container {container_id}: {res.stderr.strip()}")
    except (subprocess.SubprocessError, OSError) as exc:
        errors.append(f"failed to remove container {container_id}: {exc}")
    try:
        vol = subprocess.run(["docker", "volume", "rm", volume_name], capture_output=True, text=True, timeout=60)
        if vol.returncode != 0:
            errors.append(f"failed to remove volume {volume_name}: {vol.stderr.strip()}")
    except (subprocess.SubprocessError, OSError) as exc:
        errors.append(f"failed to remove volume {volume_name}: {exc}")
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
    ap.add_argument("--include-untracked", action="store_true",
                     help=(
                         "also copy untracked-but-not-gitignored files into the "
                         "snapshot (default: tracked files only -- an untracked "
                         "secret-shaped file sitting in the working tree is not "
                         "necessarily gitignored, so this is opt-in, not default)"
                     ))
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
        # The primary test path's own exception (if any) must win over a
        # secondary teardown failure -- a raised `_tear_down` SystemExit in
        # a bare `finally` would otherwise silently replace it, discarding
        # both the real failure and its traceback. `result`/`primary_exc`
        # let the `finally` below tell which case it's in: report (but
        # don't re-raise) a teardown failure when the primary path already
        # failed; raise it directly only when the primary path succeeded.
        result: int | None = None
        primary_exc: BaseException | None = None
        try:
            _populate_workspace(container_id, passthrough, include_untracked=ns.include_untracked)
            result = _run_tests(container_id, config_path, passthrough)
        except BaseException as exc:
            primary_exc = exc
            raise
        finally:
            if not ns.keep:
                try:
                    _tear_down(container_id, volume_name)
                except BaseException as teardown_exc:
                    if primary_exc is None:
                        raise
                    print(f"warning: teardown also failed: {teardown_exc}", file=sys.stderr)
        return result
    finally:
        shutil.rmtree(config_path.parent, ignore_errors=True)



if __name__ == "__main__":
    raise SystemExit(main())
