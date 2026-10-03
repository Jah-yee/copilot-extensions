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
import signal
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEVCONTAINER_CONFIG = REPO / ".devcontainer" / "devcontainer.json"
CONTAINER_WORKSPACE = "/workspaces/copilot-extensions"
#: Must match ``.devcontainer/devcontainer.json``'s ``remoteUser``/
#: ``containerUser`` -- the non-root user tests actually run as.
REMOTE_USER = "vscode"

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
    override that. Also forces ``GIT_OPTIONAL_LOCKS=0``: without it, even a
    nominally read-only command (``git status`` in
    ``_warn_about_dirty_tracked_files``, run against the REAL host
    checkout, not a throwaway copy) can refresh and rewrite the index,
    violating this wrapper's own read-only-host guarantee and contending
    with any concurrent `git` process the caller is running -- the same
    safeguard `tools/agent_bridge_contract_git.py` already applies for the
    same reason.

    Also unconditionally forces ``GIT_NO_LAZY_FETCH=1`` and
    ``GIT_NO_REPLACE_OBJECTS=1`` (matching
    `tools/agent_bridge_contract_git.py`'s own hardened environment): in a
    partial clone, resolving ``HEAD``/the diff base for ``git bundle
    create`` could otherwise lazily fetch missing objects INTO the host
    repository -- a host mutation this wrapper exists to prevent -- and a
    locally configured replacement ref could silently substitute different
    history into the bundle than what ``HEAD``/``--base`` actually name.
    Forcing both to ``1`` unconditionally (rather than merely removing an
    inherited value) closes that gap regardless of what the caller's own
    environment does or doesn't set.
    """
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
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_NO_LAZY_FETCH"] = "1"
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
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
    files are the set contributors and CI already trust to keep secrets
    OUT OF THE REPOSITORY. ``include_untracked=True`` (the wrapper's own
    ``--include-untracked`` flag) additionally includes
    untracked-but-not-gitignored files via ``--others --exclude-standard``,
    for the deliberate, opt-in case of testing new, not-yet-committed files
    -- never the default.

    Known, accepted residual exposure: this boundary is about which PATHS
    are copied, not which bytes -- the content read for a tracked path is
    the CURRENT on-disk file (so uncommitted edits you're actively testing
    are included; see ``_write_tar_of_repo``), not the last-committed blob.
    A secret pasted directly into an otherwise-tracked, ordinarily-safe
    file (e.g. a config example) and never committed is therefore still
    copied in. A clean CI checkout has no such dirty state; a contributor's
    local checkout might. This is a deliberate tradeoff (the tool's whole
    point is testing in-progress, uncommitted changes), not an oversight --
    but "tracked" must never be read as "every byte in it is safe,"
    only as "this path itself isn't the kind of thing that normally
    carries secrets."
    """
    args = ["git", "-C", str(REPO), "ls-files", "-z", "--cached"]
    if include_untracked:
        args += ["--others", "--exclude-standard"]
    res = subprocess.run(args, capture_output=True, timeout=60, env=_scrubbed_git_env())
    if res.returncode != 0:
        raise SystemExit(
            f"git ls-files failed: {res.stderr.decode(errors='replace').strip()}"
        )
    # `os.fsdecode` (surrogate-escape), not a plain UTF-8 `.decode()` --
    # a git-tracked path on Linux is arbitrary bytes, and a plain decode
    # would raise `UnicodeDecodeError` outright for a valid tracked
    # filename that happens not to be valid UTF-8, aborting the whole
    # snapshot over one oddly-named file.
    paths = [p for p in os.fsdecode(res.stdout).split("\0") if p]
    excluded_prefixes = tuple(f"{name}/" for name in EXCLUDED_TOP_LEVEL)
    return [
        p for p in paths
        if p not in EXCLUDED_TOP_LEVEL and not p.startswith(excluded_prefixes)
    ]


def _warn_about_dirty_tracked_files() -> None:
    """Print a clear, explicit stderr warning naming every tracked file
    with an uncommitted modification -- the tracked-files-only boundary
    (see ``_tracked_paths``) is about which PATHS are copied, not which
    BYTES; the actual content copied for a tracked path is its current
    on-disk state, so a secret pasted into an otherwise-tracked file and
    never committed is still copied in. A clean CI checkout never hits
    this; a contributor's dirty local checkout might -- this surfaces that
    residual exposure at the moment it's actually relevant, not only in a
    docstring/doc page nobody reads before running the command.

    Fails CLOSED (raises) if ``git status`` itself cannot be run: this
    check is the runtime mitigation for accidental secret exposure, so an
    unknown dirty state must never be silently treated as "clean" and
    allowed to proceed -- that would defeat the whole point of the
    warning."""
    res = subprocess.run(
        ["git", "-C", str(REPO), "status", "--porcelain=v1", "--untracked-files=no"],
        capture_output=True, timeout=30, env=_scrubbed_git_env(),
    )
    if res.returncode != 0:
        raise SystemExit(
            "failed to check for uncommitted changes to tracked files "
            f"(refusing to build a snapshot with an unknown dirty state): "
            f"{res.stderr.decode(errors='replace').strip()}"
        )
    dirty = [
        line[3:] for line in os.fsdecode(res.stdout).splitlines() if line.strip()
    ]
    if not dirty:
        return
    print(
        "warning: the following tracked file(s) have uncommitted changes and "
        "their CURRENT on-disk content (not the last-committed version) will "
        "be copied into the test-isolation container, which has outbound "
        "network access -- do not run this against a checkout with an "
        "uncommitted secret pasted into an otherwise-tracked file:",
        file=sys.stderr,
    )
    for path in dirty:
        print(f"  {path}", file=sys.stderr)


def _warn_about_hidden_tracked_file_flags() -> None:
    """Print a clear, explicit stderr warning naming every tracked file
    whose index entry carries ``assume-unchanged`` or ``skip-worktree``.

    ``git status`` (and therefore ``_warn_about_dirty_tracked_files``) is
    NOT a fail-closed dirty-content check for these paths: both flags
    instruct git to SUPPRESS reporting an on-disk difference for that
    path, while ``_tracked_paths``/``_write_tar_of_repo`` still archive its
    actual current bytes regardless -- so a locally customized tracked
    file (a config override, for instance) carrying either flag could
    enter the network-enabled container with no warning at all. ``git
    ls-files -v`` marks a flagged entry with a lowercase letter
    (assume-unchanged) or an uppercase ``S`` (skip-worktree); an ordinary,
    unflagged entry is uppercase (``H`` for a normal cached entry).

    Fails CLOSED (raises) if ``git ls-files -v`` itself cannot be run, for
    the same reason ``_warn_about_dirty_tracked_files`` does: an unknown
    state must never be silently treated as safe."""
    res = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-v", "--cached"],
        capture_output=True, timeout=60, env=_scrubbed_git_env(),
    )
    if res.returncode != 0:
        raise SystemExit(
            "failed to check tracked files for assume-unchanged/skip-worktree "
            f"flags (refusing to build a snapshot with an unknown state): "
            f"{res.stderr.decode(errors='replace').strip()}"
        )
    flagged: list[str] = []
    for line in os.fsdecode(res.stdout).splitlines():
        if not line.strip():
            continue
        flag, _, path = line.partition(" ")
        if flag.islower() or flag == "S":
            flagged.append(path)
    if not flagged:
        return
    print(
        "warning: the following tracked file(s) carry a Git "
        "assume-unchanged/skip-worktree flag -- `git status` will NOT report "
        "an on-disk modification for them, but their CURRENT (possibly "
        "locally customized) content is still copied into the "
        "test-isolation container, which has outbound network access:",
        file=sys.stderr,
    )
    for path in flagged:
        print(f"  {path}", file=sys.stderr)


# Every `tools/run-plugin-tests.py` flag that consumes a SEPARATE following
# token as its value (as opposed to a bare `store_true` flag, or the
# single-token `--flag=value` form, which `.startswith("-")` already
# catches below) -- kept in sync by hand with that script's own
# `argparse` definitions, mirrored here only to tell a flag's value token
# apart from a positional plugin name, never to fully re-parse its CLI.
_VALUE_CONSUMING_FLAGS = frozenset({
    "--base", "-k", "--admission-wait", "--timeout", "--subsuite-timeout",
    "--plugin-timeout", "--test-timeout", "--max-files-per-sub-suite",
    "--max-processes", "--max-memory-mb", "--max-temp-mb", "--exclude",
})

# Every bare (`store_true`) `tools/run-plugin-tests.py` flag -- kept in
# sync by hand alongside `_VALUE_CONSUMING_FLAGS` above, for the same
# reason: distinguishing a recognized flag from a positional plugin name,
# never fully re-parsing that runner's CLI.
_BARE_FLAGS = frozenset({
    "--all", "--changed", "--reinstall", "--guards", "--collect-only",
    "--list", "--pre-push", "--allow-explicit-tiers",
})

_ALL_LONG_FLAGS = _VALUE_CONSUMING_FLAGS | _BARE_FLAGS


def _canonicalize_flag(name: str) -> str:
    """Resolve a bare (no ``=value`` suffix) long-flag token to its
    canonical name, honoring argparse's own unambiguous-prefix abbreviation
    support (e.g. ``--bas`` -> ``--base``, since no OTHER known
    `run-plugin-tests.py` flag also starts with ``--bas``) against
    `_ALL_LONG_FLAGS` -- mirrors that parser's own matching rather than
    guessing at a different one. Without this, an abbreviated ``--base``
    (silently accepted by `run-plugin-tests.py`'s own argparse) would go
    unrecognized here: `_resolve_base_ref` would keep the wrong
    (`origin/main`) default instead of the ref actually in play, and
    `_changed_mode_active` would misclassify the abbreviated flag's VALUE
    token as a positional plugin name -- in combination, silently building
    a snapshot against the wrong base AND disabling the fail-loud
    unresolvable-base guard for it. Returns `name` unchanged when it isn't
    a recognized abbreviation of exactly one known flag (ambiguous, a short
    flag like ``-k``, or genuinely unknown) -- callers fall through to
    their own existing unrecognized-flag handling in that case."""
    if name in _ALL_LONG_FLAGS or not name.startswith("--") or len(name) <= 2:
        return name
    matches = [flag for flag in _ALL_LONG_FLAGS if flag.startswith(name)]
    return matches[0] if len(matches) == 1 else name


def _resolve_base_ref(passthrough: list[str]) -> str:
    """Best-effort extraction of the ``--base`` value a passthrough
    invocation will use, so ``_materialized_git_dir`` can include exactly
    that ref's object closure (not the whole repository's history) in the
    bundled snapshot. Falls back to ``tools/run-plugin-tests.py``'s own
    ``--base`` default when it isn't present in ``passthrough`` -- mirroring
    that runner's own argparse default, not guessing at a different one.
    Mirrors argparse's own last-occurrence-wins behavior for a repeated
    flag -- keeps scanning instead of returning on the first match, since
    `run-plugin-tests.py`'s own argparse would use the LAST ``--base``.
    Recognizes an unambiguous abbreviated form too (``--bas``, ``--ba``,
    ...), since that runner's own argparse silently accepts one -- see
    `_canonicalize_flag`."""
    resolved = "origin/main"
    for i, arg in enumerate(passthrough):
        name, eq, value = arg.partition("=")
        if _canonicalize_flag(name) != "--base":
            continue
        if eq:
            resolved = value
        elif i + 1 < len(passthrough):
            resolved = passthrough[i + 1]
    return resolved


def _changed_mode_active(passthrough: list[str]) -> bool:
    """Whether a ``tools/run-plugin-tests.py`` invocation with these
    passthrough args will resolve its targets via ``changed_plugins()`` --
    true for an explicit ``--changed``, AND for that runner's own default
    (no ``--all``, no explicit plugin names) per its own
    ``else: targets = changed_plugins(args.base)`` fallback. Only in this
    case does an unresolvable ``--base`` actually matter -- an explicit
    plugin name or ``--all`` run never consults it at all."""
    has_all = False
    has_positional = False
    skip_next = False
    for arg in passthrough:
        if skip_next:
            skip_next = False
            continue
        # A single-token `--flag=value` form never consumes a SEPARATE
        # following token, so it needs no canonicalization here -- it
        # either already is (or isn't) recognized by the plain
        # `.startswith("-")` check below, and either way can't
        # misclassify a later arg as a positional.
        canonical = _canonicalize_flag(arg) if "=" not in arg else arg
        if canonical == "--all":
            has_all = True
        elif canonical in _VALUE_CONSUMING_FLAGS:
            skip_next = True
        elif arg.startswith("-"):
            continue
        else:
            has_positional = True
    return not has_all and not has_positional


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
    # An explicitly empty template directory for `git clone` below --
    # without it, `git clone` honors the HOST's global `init.templateDir`,
    # which can plant arbitrary files (not just `hooks`/`config`, both of
    # which are otherwise explicitly handled below) into the "clean"
    # synthetic `.git` directory, which then ships into the
    # network-enabled container.
    empty_template_dir = tmp_dir / "empty-template"
    empty_template_dir.mkdir()

    base_ref = _resolve_base_ref(passthrough)
    base_resolves = _git_rev_parse(base_ref) is not None
    # Changed-selection mode (explicit `--changed`, OR that runner's own
    # default when neither `--all` nor an explicit plugin name is given --
    # see `_changed_mode_active`) is the one mode that actually DIFFS
    # against `base_ref`. An unresolvable base there is a silent false
    # negative, not a safe degradation: `run-plugin-tests.py`'s own
    # `changed_plugins()` ignores a nonzero `git diff` and reports an EMPTY
    # target set rather than erroring, so a typo'd or never-fetched
    # `--base` would make the run silently exit "No plugin suites to run."
    # instead of surfacing the real problem. Fail loudly here instead,
    # before any snapshot work -- but only when changed-selection is
    # actually in play; an explicit plugin name or `--all` run never uses
    # `base_ref` at all, so an unresolvable default must not block those.
    if _changed_mode_active(passthrough) and not base_resolves:
        raise SystemExit(
            f"changed-selection mode is active (explicit --changed, or the "
            f"default with no --all/plugin names) but its diff base "
            f"({base_ref!r}) does not resolve on the host -- refusing to "
            "silently build a snapshot that would make the in-container run "
            "report \"no plugin suites to run\" instead of the real "
            "problem. Fetch or correct --base."
        )
    bundle_refs = ["HEAD", base_ref] if base_resolves else ["HEAD"]

    bundle_res = subprocess.run(
        ["git", "-C", str(REPO), "bundle", "create", str(bundle_file), *bundle_refs],
        capture_output=True, text=True, timeout=300, env=_scrubbed_git_env(),
    )
    if bundle_res.returncode != 0:
        raise SystemExit(f"git bundle create failed: {bundle_res.stderr.strip()}")

    clone_res = subprocess.run(
        ["git", "clone", "--bare", "--quiet", f"--template={empty_template_dir}",
         str(bundle_file), str(merged)],
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

    Also warns (``_warn_about_dirty_tracked_files``,
    ``_warn_about_hidden_tracked_file_flags``) about any tracked file with
    an uncommitted modification, or an assume-unchanged/skip-worktree flag
    that could hide one, before copying anything -- known, accepted
    residual exposures of the tracked-files boundary (which governs which
    PATHS are copied, not which bytes) are surfaced explicitly at the
    moment they're actually relevant.
    """
    _warn_about_dirty_tracked_files()
    _warn_about_hidden_tracked_file_flags()
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

    A freshly created Docker volume is root-owned, so the non-root
    ``vscode`` remote user (who actually runs the test suite) can't write
    into it yet -- a one-off root ``chmod`` opens up the empty volume root
    first (permission bits only; root remains the directory's OWNER, which
    is why the later chmod below is scoped to the directory's CONTENTS,
    not the root entry itself -- `chmod` requires file ownership, not just
    write access, and `vscode` never owns a directory entry it didn't
    create). Extraction itself then runs AS ``vscode``, not root: every
    extracted file is owned by the user that ran ``tar``, so this makes
    the checkout (including ``.git``) natively ``vscode``-owned with no
    chown step needed (and none would be possible anyway -- the runtime
    posture drops every Linux capability via ``--cap-drop=ALL``, so even
    root inside the container cannot ``chown``). Despite that, the
    directory ENTRY at ``CONTAINER_WORKSPACE`` itself (the volume's
    mountpoint, as opposed to anything extracted into it) stays
    root-owned for the container's entire lifetime -- nothing can ever
    chown it. Modern Git's "detected dubious ownership" check inspects
    the ownership of the WORKING TREE ROOT, not just ``.git``, so that one
    always-root-owned directory entry would still make every git
    invocation `run-plugin-tests.py` makes (e.g. its own changed-file
    diffing) fail -- and since that script treats a failed `git diff` as
    an EMPTY target set rather than an error, it would misleadingly report
    "no plugin suites to run" instead of the real problem. That residual
    gap is closed separately, not here: ``.devcontainer/devcontainer.json``
    exempts this exact path from the ownership check via
    ``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_0``/``GIT_CONFIG_VALUE_0``
    ``containerEnv`` entries (the one sanctioned way around the check that
    doesn't require a repo-local, and therefore untrusted-input-
    controllable, config file).

    The final permission-opening pass only targets regular files and
    directories, never a symlink entry: ``chmod`` on a symlink PATH
    dereferences it and affects whatever it points AT, not the link
    itself (Linux symlinks have no meaningful permission bits of their
    own). Passing symlink paths through ``find -exec chmod`` would
    therefore either fail outright for an intentionally preserved
    dangling symlink (nothing to dereference), or silently chmod a LIVE
    symlink's target -- which, for an absolute or ``..``-escaping
    symlink, could reach a path entirely outside the workspace volume.
    """
    chmod_root = subprocess.run(
        ["docker", "exec", "-u", "root", container_id,
         "chmod", "0777", CONTAINER_WORKSPACE],
        capture_output=True, text=True, timeout=60,
    )
    if chmod_root.returncode != 0:
        raise SystemExit(
            f"failed to open up the empty container workspace volume: "
            f"{chmod_root.stderr.strip()}"
        )
    with tempfile.NamedTemporaryFile(
        prefix="devcontainer-test-isolation-snapshot-", suffix=".tar",
    ) as tar_file:
        _write_tar_of_repo(Path(tar_file.name), passthrough, include_untracked=include_untracked)
        tar_file.seek(0)
        res = subprocess.run(
            [
                "docker", "exec", "-i", "-u", REMOTE_USER, container_id,
                "tar", "-xf", "-", "-C", CONTAINER_WORKSPACE,
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
        ["docker", "exec", "-u", REMOTE_USER, container_id,
         "find", CONTAINER_WORKSPACE, "-mindepth", "1",
         "(", "-type", "f", "-o", "-type", "d", ")", "-exec",
         "chmod", "u+rwX", "{}", "+"],
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
    secondary cleanup failure. Every failure (a nonzero exit OR a raised
    exception, at any step) is still reported to stderr -- silently
    treating a failed ``docker ps`` as "no orphan exists" would leave a
    partially created container un-removable with no indication to the
    user that manual cleanup is needed."""
    container_ids: list[str] = []
    try:
        find = subprocess.run(
            ["docker", "ps", "-aq", "--filter",
             f"label=devcontainer-test-isolation.instance={instance_label}"],
            capture_output=True, text=True, timeout=30,
        )
        if find.returncode != 0:
            print(f"warning: orphan-cleanup 'docker ps' failed: {find.stderr.strip()}", file=sys.stderr)
        else:
            container_ids = find.stdout.split()
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"warning: orphan-cleanup 'docker ps' failed: {exc}", file=sys.stderr)
    for container_id in container_ids:
        try:
            rm = subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, text=True, timeout=60)
            if rm.returncode != 0:
                print(f"warning: orphan-cleanup failed to remove container {container_id}: "
                      f"{rm.stderr.strip()}", file=sys.stderr)
        except (subprocess.SubprocessError, OSError) as exc:
            print(f"warning: orphan-cleanup failed to remove container {container_id}: {exc}",
                  file=sys.stderr)
    try:
        vol = subprocess.run(["docker", "volume", "rm", volume_name], capture_output=True, text=True, timeout=60)
        if vol.returncode != 0:
            print(f"warning: orphan-cleanup failed to remove volume {volume_name}: "
                  f"{vol.stderr.strip()}", file=sys.stderr)
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"warning: orphan-cleanup failed to remove volume {volume_name}: {exc}", file=sys.stderr)


class _TerminationRequested(BaseException):
    """Raised from the SIGTERM handler installed in ``main`` so the
    wrapper's own try/finally cleanup runs instead of the process dying
    silently. The default SIGTERM action terminates a Python process
    IMMEDIATELY, bypassing every ``finally`` block (including container
    and volume teardown) -- an outer timeout, CI cancellation, or service
    stop would otherwise leave both leaked, with no way for a LATER
    invocation to find and remove them (``_cleanup_orphan`` only ever
    searches by the new random instance label each fresh run gets, never
    a prior run's). Deliberately a ``BaseException`` subclass (matching
    ``KeyboardInterrupt``'s own hierarchy placement), so the existing
    ``except BaseException`` teardown/orphan-cleanup paths below already
    handle it with no further changes needed there."""


def _raise_on_sigterm(signum: int, frame: object) -> None:
    raise _TerminationRequested(f"received signal {signum}")


@contextlib.contextmanager
def _sigterm_deferred():
    """Temporarily ignore ``SIGTERM`` for the duration of a cleanup step
    (``_tear_down``/``_cleanup_orphan``), restoring whatever handler was
    previously installed afterward. Without this, a SECOND ``SIGTERM``
    arriving WHILE cleanup is already running (e.g. between removing the
    container and removing its volume in `_tear_down`, two separate
    sequential subprocess calls) would raise `_TerminationRequested` again
    right there -- `_tear_down`/`_cleanup_orphan` only catch
    `subprocess.SubprocessError`/`OSError`, so that second exception
    escapes immediately and can skip whichever removal step hadn't run
    yet, reopening the exact leak `_raise_on_sigterm` exists to prevent."""
    previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def main(argv: list[str] | None = None) -> int:
    # Converts a SIGTERM into a normal raised exception so this
    # function's own try/finally cleanup runs -- see
    # `_TerminationRequested`'s docstring. SIGINT needs no equivalent
    # handler: Python already raises `KeyboardInterrupt` for it by
    # default, which the same `except BaseException` paths already catch.
    signal.signal(signal.SIGTERM, _raise_on_sigterm)
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
            with _sigterm_deferred():
                _cleanup_orphan(instance_label, volume_name)
            raise
        # The primary test path's own result (a nonzero exit code) OR
        # exception must win over a secondary teardown failure -- a raised
        # `_tear_down` SystemExit in a bare `finally` would otherwise
        # silently replace either, discarding the real failure (and, for
        # an exception, its traceback too). `result`/`primary_failed` let
        # the `finally` below tell which case it's in: report (but don't
        # re-raise) a teardown failure whenever the primary path already
        # failed -- whether that failure raised or merely returned
        # nonzero -- and raise it directly only when the primary path
        # truly succeeded (a zero exit code, no exception).
        result: int | None = None
        primary_failed = False
        try:
            _populate_workspace(container_id, passthrough, include_untracked=ns.include_untracked)
            result = _run_tests(container_id, config_path, passthrough)
            primary_failed = result != 0
        except BaseException:
            primary_failed = True
            raise
        finally:
            if not ns.keep:
                try:
                    with _sigterm_deferred():
                        _tear_down(container_id, volume_name)
                except BaseException as teardown_exc:
                    if not primary_failed:
                        raise
                    print(f"warning: teardown also failed: {teardown_exc}", file=sys.stderr)
        return result
    finally:
        shutil.rmtree(config_path.parent, ignore_errors=True)



if __name__ == "__main__":
    raise SystemExit(main())
