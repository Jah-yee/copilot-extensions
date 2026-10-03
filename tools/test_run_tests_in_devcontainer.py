"""Focused unit tests for the devcontainer test-isolation wrapper.

These tests never invoke Docker or the real ``devcontainer`` CLI, but
several DO invoke the real ``git`` CLI against small, throwaway repositories
built in ``tmp_path`` (the ``_materialized_git_dir`` tests) -- that function
makes several sequential `git bundle`/`clone`/`fetch` calls whose real
behavior is the point being tested, not something subprocess mocking could
meaningfully stand in for. Everything else (argument parsing,
git-environment scrubbing, the tracked-file selection, the bounded-volume
and per-instance config/volume rewrite, the privileged workspace population,
and the Docker/devcontainer-CLI invocation shape) is exercised via
subprocess mocking, matching the style of ``test_run_plugin_tests.py``. A
real, Docker-backed end-to-end run is exercised manually (see the effort
README's Phase 1 journal), not in the repository's default test portfolio,
since it requires a working Docker daemon and network access to pull a base
image -- neither of which this repo's unit-test tier guarantees.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import subprocess as real_subprocess
import sys
import tarfile
import uuid
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent / "run_tests_in_devcontainer.py"
_previous_path = sys.path.copy()
sys.path.insert(0, str(SCRIPT.parent))
try:
    _spec = importlib.util.spec_from_file_location("run_tests_in_devcontainer", SCRIPT)
    assert _spec and _spec.loader
    wrapper = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = wrapper
    _spec.loader.exec_module(wrapper)
finally:
    sys.path[:] = _previous_path


def test_scrubbed_git_env_removes_repository_context_variables(monkeypatch) -> None:
    monkeypatch.setenv("GIT_DIR", "/somewhere/else/.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/somewhere/else")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.foo")
    monkeypatch.setenv("UNRELATED_VAR", "kept")
    env = wrapper._scrubbed_git_env()
    assert "GIT_DIR" not in env
    assert "GIT_WORK_TREE" not in env
    assert "GIT_CONFIG_KEY_0" not in env
    assert env.get("UNRELATED_VAR") == "kept"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    # Without this, even a nominally read-only `git status` against the
    # real host checkout (`_warn_about_dirty_tracked_files`) can refresh
    # and rewrite the index, violating the wrapper's read-only-host
    # guarantee.
    assert env["GIT_OPTIONAL_LOCKS"] == "0"


def test_tracked_paths_defaults_to_cached_only_and_filters_excluded_prefixes() -> None:
    fake_result = mock.Mock(
        returncode=0,
        stdout=b"TESTING.md\0.test-venvs/linux/foo\0.devcontainer/devcontainer.json\0tools/x.py\0",
        stderr=b"",
    )
    with mock.patch.object(wrapper.subprocess, "run", return_value=fake_result) as run:
        paths = wrapper._tracked_paths(include_untracked=False)
    assert paths == ["TESTING.md", "tools/x.py"]
    args, kwargs = run.call_args
    assert args[0] == ["git", "-C", str(wrapper.REPO), "ls-files", "-z", "--cached"]
    # Must use the scrubbed environment, not the ambient one.
    assert kwargs["env"] == wrapper._scrubbed_git_env()


def test_tracked_paths_include_untracked_adds_others_exclude_standard() -> None:
    fake_result = mock.Mock(returncode=0, stdout=b"TESTING.md\0", stderr=b"")
    with mock.patch.object(wrapper.subprocess, "run", return_value=fake_result) as run:
        wrapper._tracked_paths(include_untracked=True)
    args = run.call_args.args[0]
    assert args == ["git", "-C", str(wrapper.REPO), "ls-files", "-z",
                     "--cached", "--others", "--exclude-standard"]


def test_tracked_paths_decodes_non_utf8_bytes_via_surrogateescape() -> None:
    # A git-tracked path on Linux is arbitrary bytes -- a plain UTF-8
    # `.decode()` would raise `UnicodeDecodeError` outright for a valid
    # tracked filename that isn't valid UTF-8, aborting the whole snapshot.
    # `os.fsdecode` (surrogate-escape) must handle it instead.
    non_utf8_name = b"weird-\xff-name.txt"
    fake_result = mock.Mock(returncode=0, stdout=non_utf8_name + b"\0", stderr=b"")
    with mock.patch.object(wrapper.subprocess, "run", return_value=fake_result):
        paths = wrapper._tracked_paths(include_untracked=False)
    assert len(paths) == 1
    # Round-trips back to the original bytes via `os.fsencode`.
    assert os.fsencode(paths[0]) == non_utf8_name


def test_tracked_paths_raises_on_git_failure() -> None:
    fake_result = mock.Mock(returncode=128, stdout=b"", stderr=b"not a git repository")
    with mock.patch.object(wrapper.subprocess, "run", return_value=fake_result):
        try:
            wrapper._tracked_paths(include_untracked=False)
        except SystemExit as exc:
            assert "not a git repository" in str(exc)
        else:
            raise AssertionError("expected SystemExit")


def test_create_bounded_volume_invokes_tmpfs_backed_docker_volume_create() -> None:
    fake_result = mock.Mock(returncode=0, stderr="")
    with mock.patch.object(wrapper.subprocess, "run", return_value=fake_result) as run:
        wrapper._create_bounded_volume("fake-volume")
    args = run.call_args.args[0]
    assert args[:3] == ["docker", "volume", "create"]
    assert "fake-volume" in args
    assert f"o=size={wrapper.WORKSPACE_VOLUME_SIZE}" in args
    assert "type=tmpfs" in args


def test_create_bounded_volume_raises_on_failure() -> None:
    fake_result = mock.Mock(returncode=1, stderr="volume already exists")
    with mock.patch.object(wrapper.subprocess, "run", return_value=fake_result):
        try:
            wrapper._create_bounded_volume("fake-volume")
        except SystemExit as exc:
            assert "volume already exists" in str(exc)
        else:
            raise AssertionError("expected SystemExit")


def test_per_instance_config_rewrites_volume_name_uniquely() -> None:
    config_path, volume_name = wrapper._per_instance_config("abc123def456")
    try:
        text = config_path.read_text()
        assert volume_name == f"{wrapper.BASE_VOLUME_NAME}-abc123def456"
        assert volume_name in text
        # The base (unsuffixed) name must not remain anywhere in the
        # rewritten config, or `devcontainer up` would still target the
        # shared, non-unique volume.
        assert wrapper.BASE_VOLUME_NAME + "," not in text
    finally:
        config_path.unlink(missing_ok=True)


def test_per_instance_config_raises_if_base_volume_name_missing(tmp_path: Path, monkeypatch) -> None:
    bogus = tmp_path / "devcontainer.json"
    bogus.write_text("{}")
    monkeypatch.setattr(wrapper, "DEVCONTAINER_CONFIG", bogus)
    try:
        wrapper._per_instance_config("instance-label")
    except SystemExit as exc:
        assert wrapper.BASE_VOLUME_NAME in str(exc)
    else:
        raise AssertionError("expected SystemExit")


def test_bring_up_parses_container_id_from_devcontainer_up_output(tmp_path: Path) -> None:
    fake_result = mock.Mock(
        returncode=0,
        stdout='{"outcome":"success","containerId":"abc123"}\n',
        stderr="",
    )
    config_path = tmp_path / "devcontainer.json"
    config_path.write_text("{}")
    with mock.patch.object(wrapper.shutil, "which", return_value="/usr/bin/devcontainer"), \
         mock.patch.object(wrapper.subprocess, "run", return_value=fake_result) as run:
        container_id = wrapper._bring_up("instance-label", config_path)
    assert container_id == "abc123"
    args = run.call_args.args[0]
    assert args[0] == "/usr/bin/devcontainer"
    assert "up" in args
    assert "--config" in args
    assert str(config_path) in args


def test_bring_up_raises_when_devcontainer_cli_missing(tmp_path: Path) -> None:
    with mock.patch.object(wrapper.shutil, "which", return_value=None):
        try:
            wrapper._bring_up("instance-label", tmp_path / "devcontainer.json")
        except SystemExit as exc:
            assert "devcontainer CLI not found" in str(exc)
        else:
            raise AssertionError("expected SystemExit")


def test_bring_up_raises_when_container_id_missing_from_output(tmp_path: Path) -> None:
    fake_result = mock.Mock(returncode=0, stdout="no json here\n", stderr="")
    with mock.patch.object(wrapper.shutil, "which", return_value="/usr/bin/devcontainer"), \
         mock.patch.object(wrapper.subprocess, "run", return_value=fake_result):
        try:
            wrapper._bring_up("instance-label", tmp_path / "devcontainer.json")
        except SystemExit as exc:
            assert "containerId" in str(exc)
        else:
            raise AssertionError("expected SystemExit")


def test_resolve_base_ref_defaults_to_origin_main() -> None:
    assert wrapper._resolve_base_ref(["agent-worktrees"]) == "origin/main"
    assert wrapper._resolve_base_ref([]) == "origin/main"


def test_resolve_base_ref_extracts_space_separated_form() -> None:
    assert wrapper._resolve_base_ref(["--changed", "--base", "origin/dev"]) == "origin/dev"


def test_resolve_base_ref_extracts_equals_form() -> None:
    assert wrapper._resolve_base_ref(["--changed", "--base=origin/dev"]) == "origin/dev"


def test_resolve_base_ref_honors_last_of_repeated_flag() -> None:
    # Mirrors argparse's own last-occurrence-wins behavior for a repeated
    # flag -- the snapshot and the in-container runner must agree on which
    # `--base` is actually in effect.
    assert wrapper._resolve_base_ref(
        ["--base", "origin/main", "--base", "origin/dev"]
    ) == "origin/dev"
    assert wrapper._resolve_base_ref(
        ["--base=origin/main", "--base=origin/dev"]
    ) == "origin/dev"


def test_changed_mode_active_by_default_with_no_args() -> None:
    # Mirrors run-plugin-tests.py's own `else: targets =
    # changed_plugins(args.base)` fallback -- no --all, no plugin names.
    assert wrapper._changed_mode_active([]) is True
    assert wrapper._changed_mode_active(["--changed"]) is True
    assert wrapper._changed_mode_active(["-k", "some_filter"]) is True


def test_changed_mode_not_active_with_all_flag() -> None:
    assert wrapper._changed_mode_active(["--all"]) is False


def test_changed_mode_not_active_with_explicit_plugin_name() -> None:
    assert wrapper._changed_mode_active(["agent-worktrees"]) is False
    assert wrapper._changed_mode_active(["--base", "origin/dev", "agent-worktrees"]) is False


def test_canonicalize_flag_resolves_unambiguous_abbreviations() -> None:
    # `run-plugin-tests.py`'s own argparse silently accepts any unambiguous
    # prefix of a long flag -- `--base` is the only known flag starting
    # with `--b`, so `--b`/`--ba`/`--bas` must all canonicalize to it.
    for abbrev in ("--b", "--ba", "--bas", "--base"):
        assert wrapper._canonicalize_flag(abbrev) == "--base"


def test_canonicalize_flag_leaves_ambiguous_or_unknown_tokens_unchanged() -> None:
    assert wrapper._canonicalize_flag("--max") == "--max"  # ambiguous: 3 --max-* flags
    assert wrapper._canonicalize_flag("--nope") == "--nope"
    assert wrapper._canonicalize_flag("-k") == "-k"


def test_resolve_base_ref_recognizes_abbreviated_base_flag() -> None:
    # The exact gap an abbreviated `--base` would otherwise open: silently
    # keeping the wrong (`origin/main`) default instead of the ref the
    # in-container `run-plugin-tests.py` invocation will actually use.
    assert wrapper._resolve_base_ref(["--changed", "--bas", "origin/dev"]) == "origin/dev"
    assert wrapper._resolve_base_ref(["--changed", "--bas=origin/dev"]) == "origin/dev"


def test_changed_mode_active_with_abbreviated_base_flag_does_not_misclassify_value() -> None:
    # Without abbreviation-awareness, `--bas`'s value token
    # (`origin/dev`) would be misclassified as a positional plugin name,
    # wrongly reporting changed-mode as NOT active.
    assert wrapper._changed_mode_active(["--bas", "origin/dev"]) is True
    assert wrapper._changed_mode_active(["--bas=origin/dev"]) is True


def test_git_rev_parse_returns_sha_on_success() -> None:
    fake_result = mock.Mock(returncode=0, stdout="deadbeef\n", stderr="")
    with mock.patch.object(wrapper.subprocess, "run", return_value=fake_result) as run:
        result = wrapper._git_rev_parse("origin/dev")
    assert result == "deadbeef"
    args, kwargs = run.call_args
    assert args[0] == ["git", "-C", str(wrapper.REPO), "rev-parse", "--verify", "origin/dev"]
    assert kwargs["env"] == wrapper._scrubbed_git_env()


def test_git_rev_parse_returns_none_when_unresolvable() -> None:
    fake_result = mock.Mock(returncode=128, stdout="", stderr="unknown revision")
    with mock.patch.object(wrapper.subprocess, "run", return_value=fake_result):
        assert wrapper._git_rev_parse("no-such-ref") is None


def _run_git(args: list[str], **kwargs):
    """Run a real ``git`` subprocess for test setup/assertions, always
    through the scrubbed environment the production code itself uses
    (``wrapper._scrubbed_git_env()``) -- without it, an ambient
    ``GIT_DIR``/``GIT_WORK_TREE``/``GIT_INDEX_FILE`` could redirect even
    these real-git calls to target or mutate the CALLER's repository
    instead of the throwaway one under ``tmp_path``."""
    return real_subprocess.run(args, env=wrapper._scrubbed_git_env(), **kwargs)


def _init_repo(path: Path) -> None:
    _run_git(["git", "init", "-q", "-b", "main", str(path)], check=True)
    _run_git(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    _run_git(["git", "-C", str(path), "config", "user.email", "t@example.com"], check=True)


def test_materialized_git_dir_bundles_only_head_and_base_closure(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("v1\n")
    _run_git(["git", "-C", str(repo), "add", "."], check=True)
    _run_git(["git", "-C", str(repo), "commit", "-q", "-m", "base"], check=True)
    base_sha = _run_git(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
    ).stdout.strip()
    _run_git(["git", "-C", str(repo), "branch", "base-branch"], check=True)

    (repo / "tracked.txt").write_text("v2\n")
    _run_git(["git", "-C", str(repo), "add", "."], check=True)
    _run_git(["git", "-C", str(repo), "commit", "-q", "-m", "head"], check=True)
    head_sha = _run_git(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
    ).stdout.strip()

    # A sibling branch with unique, placeholder-secret-shaped content that
    # is NEVER an ancestor of HEAD or the base ref -- this must NOT survive
    # into the materialized copy, proving the bundle closure is genuinely
    # minimal (not the whole repository's history).
    _run_git(["git", "-C", str(repo), "checkout", "-q", "-b", "secret-branch", base_sha],
                         check=True)
    (repo / "secret.txt").write_text("not-a-real-secret-placeholder\n")
    _run_git(["git", "-C", str(repo), "add", "."], check=True)
    _run_git(["git", "-C", str(repo), "commit", "-q", "-m", "secret"], check=True)
    secret_sha = _run_git(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
    ).stdout.strip()
    _run_git(["git", "-C", str(repo), "checkout", "-q", head_sha], check=True)

    # A placeholder-credential-shaped remote URL in the real config --
    # must not survive into the materialized copy.
    placeholder_remote = "https://" + "not-a-real-credential" + "@example.com/repo.git"
    _run_git(
        ["git", "-C", str(repo), "config", "remote.origin.url", placeholder_remote],
        check=True,
    )

    monkeypatch.setattr(wrapper, "REPO", repo)
    with contextlib.ExitStack() as stack:
        merged = wrapper._materialized_git_dir(stack, ["--base", "base-branch"])

        rp = _run_git(
            ["git", f"--git-dir={merged}", "rev-parse", "HEAD"],
            capture_output=True, text=True,
        )
        assert rp.returncode == 0
        assert rp.stdout.strip() == head_sha

        diff = _run_git(
            ["git", f"--git-dir={merged}", "diff", "--name-only", "base-branch", "HEAD"],
            capture_output=True, text=True,
        )
        assert diff.returncode == 0
        assert "tracked.txt" in diff.stdout

        # The secret branch's commit must be UNRESOLVABLE in the
        # materialized copy -- its object is simply not present.
        secret_lookup = _run_git(
            ["git", f"--git-dir={merged}", "cat-file", "-e", secret_sha],
            capture_output=True, text=True,
        )
        assert secret_lookup.returncode != 0

        config_text = merged.joinpath("config").read_text()
        assert config_text == wrapper._MINIMAL_GIT_CONFIG
        assert "not-a-real-credential" not in config_text
        assert not (merged / "hooks").exists()

        # The rebuilt index (`git read-tree HEAD`) must exactly match
        # HEAD's tree -- no spurious staged differences.
        status = _run_git(
            ["git", f"--git-dir={merged}", f"--work-tree={repo}", "status", "--short"],
            capture_output=True, text=True,
        )
        assert status.returncode == 0
        assert status.stdout == ""
    assert not merged.parent.exists()


def test_materialized_git_dir_handles_staged_uncommitted_change_at_snapshot_time(
    tmp_path: Path, monkeypatch,
) -> None:
    # A staged (but not yet committed) new file's blob is reachable from
    # neither HEAD nor the base ref -- copying the real index verbatim
    # would reference that now-missing blob and break `git diff`/`status`
    # outright. Rebuilding the index from HEAD instead must not crash, even
    # though the staged state itself is not preserved as "staged".

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("v1\n")
    _run_git(["git", "-C", str(repo), "add", "."], check=True)
    _run_git(["git", "-C", str(repo), "commit", "-q", "-m", "only commit"], check=True)

    # Stage a brand-new file whose blob is genuinely unreachable from HEAD.
    (repo / "staged-new.txt").write_text("staged content\n")
    _run_git(["git", "-C", str(repo), "add", "staged-new.txt"], check=True)

    monkeypatch.setattr(wrapper, "REPO", repo)
    with contextlib.ExitStack() as stack:
        # An explicit plugin name keeps changed-selection mode inactive,
        # so the unresolvable default "origin/main" base in this tiny repo
        # doesn't trigger the fail-closed guard this test isn't about.
        merged = wrapper._materialized_git_dir(stack, ["agent-worktrees"])
        status = _run_git(
            ["git", f"--git-dir={merged}", f"--work-tree={repo}", "status", "--short"],
            capture_output=True, text=True,
        )
        # Must not crash (a copied-index approach referencing the missing
        # staged blob would fail here). The staged-new file's actual content
        # is still visible -- just reported as an ordinary untracked file
        # rather than "staged", since the rebuilt index exactly matches
        # HEAD (no entry for it) instead of preserving the real staging
        # state.
        assert status.returncode == 0
        assert status.stdout == "?? staged-new.txt\n"


def test_materialized_git_dir_skips_base_closure_when_base_unresolvable_and_not_changed_mode(
    tmp_path: Path, monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("v1\n")
    _run_git(["git", "-C", str(repo), "add", "."], check=True)
    _run_git(["git", "-C", str(repo), "commit", "-q", "-m", "only commit"], check=True)
    head_sha = _run_git(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
    ).stdout.strip()

    monkeypatch.setattr(wrapper, "REPO", repo)
    with contextlib.ExitStack() as stack:
        # "origin/main" (the default) does not exist in this tiny repo, but
        # an explicit plugin name means changed-selection mode is NOT
        # active -- must degrade gracefully (HEAD alone), not raise.
        merged = wrapper._materialized_git_dir(stack, ["agent-worktrees"])
        rp = _run_git(
            ["git", f"--git-dir={merged}", "rev-parse", "HEAD"],
            capture_output=True, text=True,
        )
        assert rp.returncode == 0
        assert rp.stdout.strip() == head_sha


def test_materialized_git_dir_raises_when_changed_mode_active_and_base_unresolvable(
    tmp_path: Path, monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("v1\n")
    _run_git(["git", "-C", str(repo), "add", "."], check=True)
    _run_git(["git", "-C", str(repo), "commit", "-q", "-m", "only commit"], check=True)

    monkeypatch.setattr(wrapper, "REPO", repo)
    with contextlib.ExitStack() as stack:
        # No --all, no plugin names -- changed-selection mode is active by
        # `run-plugin-tests.py`'s own default -- and "origin/main" doesn't
        # resolve in this tiny repo, so this must fail loudly rather than
        # silently building a snapshot that would make the in-container
        # run report "no plugin suites to run" for the wrong reason.
        try:
            wrapper._materialized_git_dir(stack, [])
        except SystemExit as exc:
            assert "origin/main" in str(exc)
            assert "does not resolve" in str(exc)
        else:
            raise AssertionError("expected SystemExit")


def test_write_tar_of_repo_includes_materialized_git_dir_and_tracked_paths(tmp_path: Path, monkeypatch) -> None:
    fake_git_dir = tmp_path / "fake-git"
    fake_git_dir.mkdir()
    (fake_git_dir / "HEAD").write_text("ref: refs/heads/main\n")

    real_file = tmp_path / "tracked.txt"
    real_file.write_text("hello\n")

    monkeypatch.setattr(wrapper, "_materialized_git_dir", lambda stack, passthrough: fake_git_dir)
    monkeypatch.setattr(wrapper, "_tracked_paths", lambda *, include_untracked: ["tracked.txt"])
    monkeypatch.setattr(wrapper, "REPO", tmp_path)
    monkeypatch.setattr(wrapper, "_warn_about_dirty_tracked_files", lambda: None)
    monkeypatch.setattr(wrapper, "_warn_about_hidden_tracked_file_flags", lambda: None)

    dest = tmp_path / "out.tar"
    wrapper._write_tar_of_repo(dest, ["agent-worktrees"], include_untracked=False)
    with tarfile.open(dest) as tar:
        names = set(tar.getnames())
    assert ".git/HEAD" in names
    assert "tracked.txt" in names


def test_write_tar_of_repo_skips_tracked_path_deleted_from_working_tree(tmp_path: Path, monkeypatch) -> None:
    # `git ls-files --cached` still lists a path for an unstaged deletion --
    # the index entry exists even though the working-tree file is gone.
    # `_write_tar_of_repo` must skip it (`os.path.lexists`) rather than
    # letting `tarfile.add` raise `FileNotFoundError`.
    fake_git_dir = tmp_path / "fake-git"
    fake_git_dir.mkdir()
    (fake_git_dir / "HEAD").write_text("ref: refs/heads/main\n")

    present_file = tmp_path / "present.txt"
    present_file.write_text("still here\n")
    # "deleted.txt" is deliberately NOT created on disk.

    monkeypatch.setattr(wrapper, "_materialized_git_dir", lambda stack, passthrough: fake_git_dir)
    monkeypatch.setattr(wrapper, "_tracked_paths",
                         lambda *, include_untracked: ["present.txt", "deleted.txt"])
    monkeypatch.setattr(wrapper, "REPO", tmp_path)
    monkeypatch.setattr(wrapper, "_warn_about_dirty_tracked_files", lambda: None)
    monkeypatch.setattr(wrapper, "_warn_about_hidden_tracked_file_flags", lambda: None)

    dest = tmp_path / "out.tar"
    wrapper._write_tar_of_repo(dest, [], include_untracked=False)
    with tarfile.open(dest) as tar:
        names = set(tar.getnames())
    assert "present.txt" in names
    assert "deleted.txt" not in names


def test_write_tar_of_repo_includes_a_tracked_dangling_symlink(tmp_path: Path, monkeypatch) -> None:
    # A tracked symlink whose target doesn't exist on disk must still be
    # archived (`os.path.lexists` reports True for a dangling symlink,
    # unlike a symlink-following `Path.exists()`) -- this is the
    # complementary half of the symlink-handling fix: the SNAPSHOT still
    # includes a dangling symlink entry as-is (never dereferenced), while
    # the separate `_populate_workspace` permission pass must not try to
    # `chmod` it (regression coverage for that lives in the
    # `_populate_workspace` tests, which assert the `find` invocation
    # excludes symlink entries entirely).
    fake_git_dir = tmp_path / "fake-git"
    fake_git_dir.mkdir()
    (fake_git_dir / "HEAD").write_text("ref: refs/heads/main\n")

    dangling_link = tmp_path / "dangling-link.txt"
    dangling_link.symlink_to(tmp_path / "does-not-exist.txt")

    monkeypatch.setattr(wrapper, "_materialized_git_dir", lambda stack, passthrough: fake_git_dir)
    monkeypatch.setattr(wrapper, "_tracked_paths",
                         lambda *, include_untracked: ["dangling-link.txt"])
    monkeypatch.setattr(wrapper, "REPO", tmp_path)
    monkeypatch.setattr(wrapper, "_warn_about_dirty_tracked_files", lambda: None)
    monkeypatch.setattr(wrapper, "_warn_about_hidden_tracked_file_flags", lambda: None)

    dest = tmp_path / "out.tar"
    wrapper._write_tar_of_repo(dest, [], include_untracked=False)
    with tarfile.open(dest) as tar:
        member = tar.getmember("dangling-link.txt")
    assert member.issym()


def test_write_tar_of_repo_does_not_recurse_into_submodule_directory(tmp_path: Path, monkeypatch) -> None:
    # `git ls-files` lists an initialized submodule as a single path that
    # happens to be a real DIRECTORY on disk. `tarfile.add` recursively
    # archives directories by default -- that would copy the submodule's
    # entire working tree (including its own untracked/ignored files and
    # `.git` metadata) wholesale, defeating the tracked-files-only
    # boundary. `recursive=False` must keep the directory entry itself
    # from being expanded.
    fake_git_dir = tmp_path / "fake-git"
    fake_git_dir.mkdir()
    (fake_git_dir / "HEAD").write_text("ref: refs/heads/main\n")

    submodule_dir = tmp_path / "vendor" / "some-submodule"
    submodule_dir.mkdir(parents=True)
    (submodule_dir / "secret-inside-submodule.txt").write_text("should not be archived\n")

    monkeypatch.setattr(wrapper, "_materialized_git_dir", lambda stack, passthrough: fake_git_dir)
    monkeypatch.setattr(wrapper, "_tracked_paths",
                         lambda *, include_untracked: ["vendor/some-submodule"])
    monkeypatch.setattr(wrapper, "REPO", tmp_path)
    monkeypatch.setattr(wrapper, "_warn_about_dirty_tracked_files", lambda: None)
    monkeypatch.setattr(wrapper, "_warn_about_hidden_tracked_file_flags", lambda: None)

    dest = tmp_path / "out.tar"
    wrapper._write_tar_of_repo(dest, [], include_untracked=False)
    with tarfile.open(dest) as tar:
        names = set(tar.getnames())
    assert "vendor/some-submodule" in names
    assert "vendor/some-submodule/secret-inside-submodule.txt" not in names


def test_warn_about_dirty_tracked_files_reports_modified_tracked_paths(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("v1\n")
    _run_git(["git", "-C", str(repo), "add", "."], check=True)
    _run_git(["git", "-C", str(repo), "commit", "-q", "-m", "initial"], check=True)

    # An uncommitted modification to an already-tracked file.
    (repo / "tracked.txt").write_text("v2 -- locally modified\n")

    monkeypatch.setattr(wrapper, "REPO", repo)
    wrapper._warn_about_dirty_tracked_files()
    err = capsys.readouterr().err
    assert "tracked.txt" in err
    assert "warning" in err.lower()


def test_warn_about_dirty_tracked_files_silent_when_clean(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("v1\n")
    _run_git(["git", "-C", str(repo), "add", "."], check=True)
    _run_git(["git", "-C", str(repo), "commit", "-q", "-m", "initial"], check=True)

    monkeypatch.setattr(wrapper, "REPO", repo)
    wrapper._warn_about_dirty_tracked_files()
    assert capsys.readouterr().err == ""


def test_warn_about_dirty_tracked_files_does_not_warn_about_untracked_files(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    # A brand-new untracked file is a DIFFERENT (already-covered) concern
    # -- `--untracked-files=no` means this function must stay silent about
    # it, not conflate the two.
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("v1\n")
    _run_git(["git", "-C", str(repo), "add", "."], check=True)
    _run_git(["git", "-C", str(repo), "commit", "-q", "-m", "initial"], check=True)
    (repo / "new-untracked.txt").write_text("brand new\n")

    monkeypatch.setattr(wrapper, "REPO", repo)
    wrapper._warn_about_dirty_tracked_files()
    assert capsys.readouterr().err == ""


def test_warn_about_dirty_tracked_files_fails_closed_when_status_itself_fails() -> None:
    # A failed `git status` must never be silently treated as "clean" --
    # this check is the runtime mitigation for accidental secret exposure,
    # so an unknown dirty state must abort the snapshot, not proceed.
    fake_result = mock.Mock(returncode=128, stdout=b"", stderr=b"not a git repository")
    with mock.patch.object(wrapper.subprocess, "run", return_value=fake_result):
        try:
            wrapper._warn_about_dirty_tracked_files()
        except SystemExit as exc:
            assert "not a git repository" in str(exc)
        else:
            raise AssertionError("expected SystemExit")


def test_warn_about_hidden_tracked_file_flags_reports_assume_unchanged_and_skip_worktree(capsys) -> None:
    # `H` is an ordinary cached entry (no flag); a lowercase letter means
    # assume-unchanged, and `S` means skip-worktree -- both suppress `git
    # status`'s own on-disk-modification reporting for that path, while
    # the snapshot still archives its real current content regardless.
    fake_result = mock.Mock(
        returncode=0,
        stdout=b"H normal.txt\nh assumed-unchanged.txt\nS skip-worktree.txt\n",
        stderr=b"",
    )
    with mock.patch.object(wrapper.subprocess, "run", return_value=fake_result):
        wrapper._warn_about_hidden_tracked_file_flags()
    err = capsys.readouterr().err
    assert "assumed-unchanged.txt" in err
    assert "skip-worktree.txt" in err
    assert "normal.txt" not in err


def test_warn_about_hidden_tracked_file_flags_silent_when_none_flagged() -> None:
    fake_result = mock.Mock(returncode=0, stdout=b"H normal.txt\n", stderr=b"")
    with mock.patch.object(wrapper.subprocess, "run", return_value=fake_result):
        wrapper._warn_about_hidden_tracked_file_flags()


def test_warn_about_hidden_tracked_file_flags_fails_closed_when_ls_files_fails() -> None:
    fake_result = mock.Mock(returncode=128, stdout=b"", stderr=b"not a git repository")
    with mock.patch.object(wrapper.subprocess, "run", return_value=fake_result):
        try:
            wrapper._warn_about_hidden_tracked_file_flags()
        except SystemExit as exc:
            assert "not a git repository" in str(exc)
        else:
            raise AssertionError("expected SystemExit")


def test_populate_workspace_streams_tar_file_as_stdin_then_chmod(monkeypatch) -> None:
    written_paths: list[Path] = []
    written_passthrough: list[list[str]] = []
    written_include_untracked: list[bool] = []

    def fake_write_tar(dest: Path, passthrough: list[str], *, include_untracked: bool) -> None:
        written_paths.append(dest)
        written_passthrough.append(passthrough)
        written_include_untracked.append(include_untracked)
        dest.write_bytes(b"not-empty")

    monkeypatch.setattr(wrapper, "_write_tar_of_repo", fake_write_tar)
    chmod_root_result = mock.Mock(returncode=0, stderr="")
    tar_result = mock.Mock(returncode=0, stderr=b"")
    chmod_result = mock.Mock(returncode=0, stderr="")
    with mock.patch.object(wrapper.subprocess, "run",
                            side_effect=[chmod_root_result, tar_result, chmod_result]) as run:
        wrapper._populate_workspace("container-9", ["--changed"], include_untracked=True)
    assert run.call_count == 3
    assert len(written_paths) == 1
    assert written_passthrough == [["--changed"]]
    assert written_include_untracked == [True]

    chmod_root_call = run.call_args_list[0]
    chmod_root_args = chmod_root_call.args[0]
    assert chmod_root_args[:4] == ["docker", "exec", "-u", "root"]
    assert "chmod" in chmod_root_args
    assert wrapper.CONTAINER_WORKSPACE in chmod_root_args

    tar_call = run.call_args_list[1]
    tar_args = tar_call.args[0]
    assert tar_args[:5] == ["docker", "exec", "-i", "-u", wrapper.REMOTE_USER]
    assert "container-9" in tar_args
    assert "tar" in tar_args
    # Extraction runs AS the non-root remote user, not root -- every
    # extracted file is then natively owned by that user with no chown
    # step needed (and none would be possible: `--no-same-owner` is no
    # longer necessary or present once extraction itself isn't root).
    assert "--no-same-owner" not in tar_args
    # Streamed via `stdin=`, never buffered as an `input=` bytes payload.
    assert "stdin" in tar_call.kwargs
    assert "input" not in tar_call.kwargs

    chmod_args = run.call_args_list[2].args[0]
    assert chmod_args[:4] == ["docker", "exec", "-u", wrapper.REMOTE_USER]
    assert "find" in chmod_args
    assert "chmod" in chmod_args
    assert wrapper.CONTAINER_WORKSPACE in chmod_args
    # Only regular files and directories are chmod'd -- `chmod` on a
    # symlink PATH dereferences it and would either fail outright (a
    # dangling symlink) or affect whatever a LIVE symlink points at,
    # possibly outside the workspace tree entirely.
    type_values = [
        chmod_args[i + 1] for i, arg in enumerate(chmod_args) if arg == "-type"
    ]
    assert set(type_values) == {"f", "d"}


def test_populate_workspace_raises_when_opening_up_the_empty_volume_fails(monkeypatch) -> None:
    monkeypatch.setattr(wrapper, "_write_tar_of_repo",
                         lambda dest, passthrough, *, include_untracked: dest.write_bytes(b""))
    chmod_root_result = mock.Mock(returncode=1, stderr="chmod: operation not permitted")
    with mock.patch.object(wrapper.subprocess, "run", return_value=chmod_root_result):
        try:
            wrapper._populate_workspace("container-9", [], include_untracked=False)
        except SystemExit as exc:
            assert "operation not permitted" in str(exc)
        else:
            raise AssertionError("expected SystemExit")


def test_populate_workspace_raises_when_tar_extraction_fails(monkeypatch) -> None:
    monkeypatch.setattr(wrapper, "_write_tar_of_repo",
                         lambda dest, passthrough, *, include_untracked: dest.write_bytes(b""))
    chmod_root_result = mock.Mock(returncode=0, stderr="")
    tar_result = mock.Mock(returncode=1, stderr=b"tar: permission denied")
    with mock.patch.object(wrapper.subprocess, "run",
                            side_effect=[chmod_root_result, tar_result]):
        try:
            wrapper._populate_workspace("container-9", [], include_untracked=False)
        except SystemExit as exc:
            assert "permission denied" in str(exc)
        else:
            raise AssertionError("expected SystemExit")


def test_populate_workspace_raises_when_chmod_fails(monkeypatch) -> None:
    monkeypatch.setattr(wrapper, "_write_tar_of_repo",
                         lambda dest, passthrough, *, include_untracked: dest.write_bytes(b""))
    chmod_root_result = mock.Mock(returncode=0, stderr="")
    tar_result = mock.Mock(returncode=0, stderr=b"")
    chmod_result = mock.Mock(returncode=1, stderr="chmod: operation not permitted")
    with mock.patch.object(wrapper.subprocess, "run",
                            side_effect=[chmod_root_result, tar_result, chmod_result]):
        try:
            wrapper._populate_workspace("container-9", [], include_untracked=False)
        except SystemExit as exc:
            assert "operation not permitted" in str(exc)
        else:
            raise AssertionError("expected SystemExit")


def test_run_tests_invokes_devcontainer_exec_with_full_passthrough_args(tmp_path: Path) -> None:
    fake_result = mock.Mock(returncode=3)
    config_path = tmp_path / "devcontainer.json"
    config_path.write_text("{}")
    with mock.patch.object(wrapper.shutil, "which", return_value="/usr/bin/devcontainer"), \
         mock.patch.object(wrapper.subprocess, "run", return_value=fake_result) as run:
        code = wrapper._run_tests("abc123", config_path, ["agent-worktrees", "-k", "foo"])
    assert code == 3
    args = run.call_args.args[0]
    assert args[:2] == ["/usr/bin/devcontainer", "exec"]
    assert "--container-id" in args
    assert str(config_path) in args
    # The complete expected suffix, not a looser subset -- a regression
    # that drops the final passthrough argument must fail this assertion.
    assert args[-5:] == ["python", "tools/run-plugin-tests.py", "agent-worktrees", "-k", "foo"]


def test_tear_down_removes_container_then_volume_on_success() -> None:
    container_result = mock.Mock(returncode=0, stderr="")
    volume_result = mock.Mock(returncode=0, stderr="")
    with mock.patch.object(wrapper.subprocess, "run",
                            side_effect=[container_result, volume_result]) as run:
        wrapper._tear_down("container-5", "volume-5")
    assert run.call_count == 2
    assert run.call_args_list[0].args[0] == ["docker", "rm", "-f", "container-5"]
    assert run.call_args_list[1].args[0] == ["docker", "volume", "rm", "volume-5"]


def test_tear_down_raises_when_container_removal_fails_but_still_attempts_volume() -> None:
    container_result = mock.Mock(returncode=1, stderr="container busy")
    volume_result = mock.Mock(returncode=0, stderr="")
    with mock.patch.object(wrapper.subprocess, "run",
                            side_effect=[container_result, volume_result]) as run:
        try:
            wrapper._tear_down("container-5", "volume-5")
        except SystemExit as exc:
            assert "container busy" in str(exc)
        else:
            raise AssertionError("expected SystemExit")
    # The volume removal must still be attempted even though the container
    # removal already failed -- a failed container removal must not skip
    # cleaning up the volume too.
    assert run.call_count == 2


def test_tear_down_raises_when_volume_removal_fails() -> None:
    container_result = mock.Mock(returncode=0, stderr="")
    volume_result = mock.Mock(returncode=1, stderr="volume in use")
    with mock.patch.object(wrapper.subprocess, "run",
                            side_effect=[container_result, volume_result]):
        try:
            wrapper._tear_down("container-5", "volume-5")
        except SystemExit as exc:
            assert "volume in use" in str(exc)
        else:
            raise AssertionError("expected SystemExit")


def test_cleanup_orphan_removes_containers_found_by_label_then_volume() -> None:
    find_result = mock.Mock(returncode=0, stdout="cid-a cid-b\n", stderr="")
    rm_a = mock.Mock(returncode=0)
    rm_b = mock.Mock(returncode=0)
    vol_result = mock.Mock(returncode=0)
    with mock.patch.object(wrapper.subprocess, "run",
                            side_effect=[find_result, rm_a, rm_b, vol_result]) as run:
        wrapper._cleanup_orphan("instance-label", "fake-volume")
    assert run.call_count == 4
    find_args = run.call_args_list[0].args[0]
    assert find_args[:2] == ["docker", "ps"]
    assert "label=devcontainer-test-isolation.instance=instance-label" in find_args
    assert run.call_args_list[1].args[0] == ["docker", "rm", "-f", "cid-a"]
    assert run.call_args_list[2].args[0] == ["docker", "rm", "-f", "cid-b"]
    assert run.call_args_list[3].args[0] == ["docker", "volume", "rm", "fake-volume"]


def test_cleanup_orphan_warns_but_does_not_raise_on_nonzero_results(capsys) -> None:
    find_result = mock.Mock(returncode=1, stdout="", stderr="docker ps failed")
    vol_result = mock.Mock(returncode=1, stderr="volume busy")
    with mock.patch.object(wrapper.subprocess, "run", side_effect=[find_result, vol_result]):
        # Must not raise -- a failed `docker ps` is reported, never
        # silently treated as "no orphan exists".
        wrapper._cleanup_orphan("instance-label", "fake-volume")
    err = capsys.readouterr().err
    assert "docker ps failed" in err
    assert "volume busy" in err


def test_cleanup_orphan_warns_but_does_not_raise_if_container_removal_fails(capsys) -> None:
    find_result = mock.Mock(returncode=0, stdout="cid-a\n", stderr="")
    rm_result = mock.Mock(returncode=1, stderr="container busy")
    vol_result = mock.Mock(returncode=0, stderr="")
    with mock.patch.object(wrapper.subprocess, "run",
                            side_effect=[find_result, rm_result, vol_result]):
        wrapper._cleanup_orphan("instance-label", "fake-volume")
    assert "container busy" in capsys.readouterr().err


def test_cleanup_orphan_never_raises_when_every_subprocess_call_itself_raises(capsys) -> None:
    import subprocess as real_subprocess

    def always_times_out(*args, **kwargs):
        raise real_subprocess.TimeoutExpired(cmd=args[0] if args else "docker", timeout=30)

    with mock.patch.object(wrapper.subprocess, "run", side_effect=always_times_out):
        # Must not raise -- this runs while an already-failing startup
        # error is propagating, and that original error must surface, not
        # a secondary cleanup failure (not even a raised TimeoutExpired
        # from one of the cleanup's own subprocess calls).
        wrapper._cleanup_orphan("instance-label", "fake-volume")
    # Still reported, just not raised.
    assert "warning" in capsys.readouterr().err.lower()


def test_raise_on_sigterm_raises_termination_requested() -> None:
    import signal as signal_module

    try:
        wrapper._raise_on_sigterm(signal_module.SIGTERM, None)
    except wrapper._TerminationRequested as exc:
        assert "SIGTERM" in str(exc) or str(int(signal_module.SIGTERM)) in str(exc)
    else:
        raise AssertionError("expected _TerminationRequested")


def test_main_installs_a_sigterm_handler(monkeypatch) -> None:
    # The default SIGTERM action terminates the process immediately,
    # bypassing every `finally` block (container/volume teardown
    # included) -- `main` must convert it into a normal raised exception
    # instead, so an outer timeout or CI cancellation can't leak a
    # container/volume with no later run able to find it.
    config_path = None

    def fake_per_instance_config(label: str):
        nonlocal config_path
        import tempfile as _tempfile
        d = _tempfile.mkdtemp()
        config_path = Path(d) / "devcontainer.json"
        config_path.write_text("{}")
        return config_path, "fake-volume"

    monkeypatch.setattr(wrapper, "_per_instance_config", fake_per_instance_config)
    monkeypatch.setattr(wrapper, "_create_bounded_volume", lambda volume_name: None)
    monkeypatch.setattr(wrapper, "_bring_up", lambda instance_label, config_path: "container-1")
    monkeypatch.setattr(wrapper, "_populate_workspace",
                         lambda container_id, passthrough, *, include_untracked: None)
    monkeypatch.setattr(wrapper, "_run_tests",
                         lambda container_id, config_path, passthrough: 0)
    monkeypatch.setattr(wrapper, "_tear_down", lambda container_id, volume_name: None)

    signal_calls: list[tuple] = []
    with mock.patch.object(wrapper.signal, "signal",
                            side_effect=lambda *a: signal_calls.append(a)) as signal_mock:
        wrapper.main([])
    assert signal_mock.call_count == 1
    registered_signum, registered_handler = signal_calls[0]
    assert registered_signum == wrapper.signal.SIGTERM
    assert registered_handler is wrapper._raise_on_sigterm


def test_main_strips_double_dash_separator_anywhere_in_passthrough(monkeypatch) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(wrapper, "_per_instance_config",
                         lambda label: (Path("/tmp/fake-devcontainer-dir/devcontainer.json"), "fake-volume"))
    monkeypatch.setattr(wrapper, "_create_bounded_volume", lambda volume_name: None)
    monkeypatch.setattr(wrapper, "_bring_up", lambda label, config_path: "container-1")
    monkeypatch.setattr(wrapper, "_populate_workspace", lambda container_id, passthrough, *, include_untracked: None)
    monkeypatch.setattr(wrapper, "_run_tests",
                         lambda container_id, config_path, passthrough: calls.append(passthrough) or 0)
    monkeypatch.setattr(wrapper, "_tear_down", lambda container_id, volume_name: None)
    monkeypatch.setattr(wrapper.shutil, "rmtree", lambda path, ignore_errors=False: None)

    rc = wrapper.main(["--", "--changed"])
    assert rc == 0
    assert calls == [["--changed"]]
    calls.clear()

    # The documented `--all -- -k some_filter` case: the separator lands in
    # the MIDDLE of the extras list, not just the front.
    rc = wrapper.main(["--all", "--", "-k", "some_filter"])
    assert rc == 0
    assert calls == [["--all", "-k", "some_filter"]]


def test_main_tears_down_container_and_volume_unless_keep_is_passed(monkeypatch, tmp_path: Path) -> None:
    torn_down: list[tuple[str, str]] = []

    def make_config_path() -> Path:
        d = tmp_path / f"cfg-{uuid.uuid4().hex[:8]}"
        d.mkdir()
        p = d / "devcontainer.json"
        p.write_text("{}")
        return p

    config_path = make_config_path()
    monkeypatch.setattr(wrapper, "_per_instance_config",
                         lambda label: (config_path, "fake-volume"))
    monkeypatch.setattr(wrapper, "_create_bounded_volume", lambda volume_name: None)
    monkeypatch.setattr(wrapper, "_bring_up", lambda label, cfg: "container-2")
    monkeypatch.setattr(wrapper, "_populate_workspace", lambda container_id, passthrough, *, include_untracked: None)
    monkeypatch.setattr(wrapper, "_run_tests", lambda container_id, cfg, passthrough: 0)
    monkeypatch.setattr(
        wrapper, "_tear_down",
        lambda container_id, volume_name: torn_down.append((container_id, volume_name)),
    )

    wrapper.main(["agent-worktrees"])
    assert torn_down == [("container-2", "fake-volume")]
    assert not config_path.parent.exists()

    torn_down.clear()
    config_path = make_config_path()
    monkeypatch.setattr(wrapper, "_per_instance_config",
                         lambda label: (config_path, "fake-volume"))
    wrapper.main(["--keep", "agent-worktrees"])
    assert torn_down == []


def test_main_raises_teardown_failure_when_primary_path_succeeded(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "cfgdir3" / "devcontainer.json"
    config_path.parent.mkdir()
    config_path.write_text("{}")

    monkeypatch.setattr(wrapper, "_per_instance_config", lambda label: (config_path, "fake-volume"))
    monkeypatch.setattr(wrapper, "_create_bounded_volume", lambda volume_name: None)
    monkeypatch.setattr(wrapper, "_bring_up", lambda label, cfg: "container-3")
    monkeypatch.setattr(wrapper, "_populate_workspace",
                         lambda container_id, passthrough, *, include_untracked: None)
    monkeypatch.setattr(wrapper, "_run_tests", lambda container_id, cfg, passthrough: 0)

    def failing_tear_down(container_id: str, volume_name: str) -> None:
        raise SystemExit("teardown failed: boom")

    monkeypatch.setattr(wrapper, "_tear_down", failing_tear_down)

    # The primary test path succeeded (exit 0) -- teardown's own failure
    # must surface directly (nothing to preserve over it).
    try:
        wrapper.main(["agent-worktrees"])
    except SystemExit as exc:
        assert "boom" in str(exc)
    else:
        raise AssertionError("expected SystemExit from the failed teardown")


def test_main_preserves_primary_exception_when_teardown_also_fails(monkeypatch, tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "cfgdir4" / "devcontainer.json"
    config_path.parent.mkdir()
    config_path.write_text("{}")

    monkeypatch.setattr(wrapper, "_per_instance_config", lambda label: (config_path, "fake-volume"))
    monkeypatch.setattr(wrapper, "_create_bounded_volume", lambda volume_name: None)
    monkeypatch.setattr(wrapper, "_bring_up", lambda label, cfg: "container-4")

    def failing_populate(container_id: str, passthrough: list[str], *, include_untracked: bool) -> None:
        raise SystemExit("primary failure: real test problem")

    def failing_tear_down(container_id: str, volume_name: str) -> None:
        raise SystemExit("secondary teardown failure")

    monkeypatch.setattr(wrapper, "_populate_workspace", failing_populate)
    monkeypatch.setattr(wrapper, "_run_tests", lambda container_id, cfg, passthrough: 0)
    monkeypatch.setattr(wrapper, "_tear_down", failing_tear_down)

    # The PRIMARY failure must win -- a `_tear_down` failure in the
    # `finally` must not silently replace it.
    try:
        wrapper.main(["agent-worktrees"])
    except SystemExit as exc:
        assert "primary failure" in str(exc)
    else:
        raise AssertionError("expected the primary SystemExit to propagate")
    # The secondary teardown failure is still reported, just not raised.
    assert "secondary teardown failure" in capsys.readouterr().err


def test_main_preserves_nonzero_test_result_when_teardown_also_fails(monkeypatch, tmp_path: Path, capsys) -> None:
    # A nonzero `_run_tests` exit code is a RETURNED value, not a raised
    # exception -- it must be treated the same as an exception for
    # teardown-masking purposes: a secondary `_tear_down` failure must not
    # replace it with a confusing, unrelated SystemExit.
    config_path = tmp_path / "cfgdir5" / "devcontainer.json"
    config_path.parent.mkdir()
    config_path.write_text("{}")

    monkeypatch.setattr(wrapper, "_per_instance_config", lambda label: (config_path, "fake-volume"))
    monkeypatch.setattr(wrapper, "_create_bounded_volume", lambda volume_name: None)
    monkeypatch.setattr(wrapper, "_bring_up", lambda label, cfg: "container-5")
    monkeypatch.setattr(wrapper, "_populate_workspace",
                         lambda container_id, passthrough, *, include_untracked: None)
    # A real test FAILURE (nonzero exit), not an exception.
    monkeypatch.setattr(wrapper, "_run_tests", lambda container_id, cfg, passthrough: 7)

    def failing_tear_down(container_id: str, volume_name: str) -> None:
        raise SystemExit("secondary teardown failure")

    monkeypatch.setattr(wrapper, "_tear_down", failing_tear_down)

    # The nonzero test result must still be returned, not masked by the
    # teardown's own SystemExit.
    assert wrapper.main(["agent-worktrees"]) == 7
    assert "secondary teardown failure" in capsys.readouterr().err


def test_main_cleans_up_orphan_and_reraises_when_bring_up_fails(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "cfgdir" / "devcontainer.json"
    config_path.parent.mkdir()
    config_path.write_text("{}")
    cleanup_calls: list[tuple[str, str]] = []

    def failing_bring_up(label: str, cfg: Path) -> str:
        raise SystemExit("devcontainer up failed: boom")

    monkeypatch.setattr(wrapper, "_per_instance_config",
                         lambda label: (config_path, "fake-volume"))
    monkeypatch.setattr(wrapper, "_create_bounded_volume", lambda volume_name: None)
    monkeypatch.setattr(wrapper, "_bring_up", failing_bring_up)
    monkeypatch.setattr(
        wrapper, "_cleanup_orphan",
        lambda instance_label, volume_name: cleanup_calls.append((instance_label, volume_name)),
    )

    try:
        wrapper.main(["agent-worktrees"])
    except SystemExit as exc:
        assert "boom" in str(exc)
    else:
        raise AssertionError("expected the original SystemExit to propagate")
    assert len(cleanup_calls) == 1
    assert cleanup_calls[0][1] == "fake-volume"
    # The per-instance config dir must still be cleaned up even on this
    # failure path.
    assert not config_path.parent.exists()


def test_main_cleans_up_orphan_when_create_bounded_volume_itself_fails(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "cfgdir2" / "devcontainer.json"
    config_path.parent.mkdir()
    config_path.write_text("{}")
    cleanup_calls: list[tuple[str, str]] = []

    def failing_create_volume(volume_name: str) -> None:
        raise SystemExit("failed to create bounded workspace volume: boom")

    monkeypatch.setattr(wrapper, "_per_instance_config",
                         lambda label: (config_path, "fake-volume"))
    monkeypatch.setattr(wrapper, "_create_bounded_volume", failing_create_volume)
    monkeypatch.setattr(
        wrapper, "_cleanup_orphan",
        lambda instance_label, volume_name: cleanup_calls.append((instance_label, volume_name)),
    )

    try:
        wrapper.main(["agent-worktrees"])
    except SystemExit:
        pass
    else:
        raise AssertionError("expected the original SystemExit to propagate")
    assert len(cleanup_calls) == 1


def _load_devcontainer_config() -> dict:
    # `.devcontainer/devcontainer.json` is JSONC (it carries extensive
    # `//` explanatory comments) -- strip full-line and trailing `//`
    # comments before parsing, mirroring the same crude-but-sufficient
    # approach used to hand-validate this file during development.
    text = wrapper.DEVCONTAINER_CONFIG.read_text()
    cleaned = re.sub(r"(?m)^\s*//.*$", "", text)
    cleaned = re.sub(r'(?<!:)//[^"\n]*$', "", cleaned, flags=re.MULTILINE)
    return json.loads(cleaned)


def test_devcontainer_config_never_mounts_a_docker_socket() -> None:
    # A mounted Docker socket is a full host-escape vector -- this spec's
    # entire point is a HARDENED isolation boundary, so this invariant
    # must never silently regress even though nothing here exercises
    # Docker itself.
    config = _load_devcontainer_config()
    run_args = config["runArgs"]
    assert not any("docker.sock" in str(arg) for arg in run_args)
    assert not any(str(arg).startswith("--privileged") for arg in run_args)
    assert "mounts" not in config or not any(
        "docker.sock" in str(m) for m in config["mounts"]
    )


def test_devcontainer_config_workspace_is_a_volume_not_a_host_bind() -> None:
    # The whole point of the workspace-storage-model fix (Phase 1, item 1)
    # is that the host checkout is never bind-mounted -- a regression back
    # to a host bind would silently reopen the original host-mutation gap
    # this effort exists to close.
    config = _load_devcontainer_config()
    assert "type=volume" in config["workspaceMount"]
    assert "type=bind" not in config["workspaceMount"]


def test_devcontainer_config_declares_the_runtime_hardening_invariants() -> None:
    # Fast structural coverage for the runtime-posture invariants
    # documented at length in the config's own comments: dropping these
    # flags (or the resource ceilings) would leave the suite green under
    # mocked Docker/devcontainer CI while silently reopening the exact
    # host-escape and resource-exhaustion risks Phase 1 closed.
    config = _load_devcontainer_config()
    run_args = [str(arg) for arg in config["runArgs"]]
    for required in (
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        "--memory=14g",
        "--memory-swap=14g",
        "--cpus=4",
        "--pids-limit=512",
    ):
        assert required in run_args, f"missing required runArg: {required!r}"
    tmpfs_mounts = [
        run_args[i + 1] for i, arg in enumerate(run_args) if arg == "--tmpfs"
    ]
    assert any(m.startswith("/home/vscode:") for m in tmpfs_mounts)
    assert any(m.startswith("/tmp:") for m in tmpfs_mounts)
    assert any(m.startswith("/run:") for m in tmpfs_mounts)


def test_devcontainer_config_bootstraps_uv_via_a_pinned_verified_download() -> None:
    # Closes the supply-chain gap a bare `curl ... | sh` pipeline would
    # reopen: the bootstrap must pin an exact `uv` version and verify the
    # downloaded archive's SHA-256 before ever executing anything from it.
    config = _load_devcontainer_config()
    on_create = config["onCreateCommand"]
    assert "UV_VERSION=" in on_create
    assert "sha256sum" in on_create
    assert "| sh" not in on_create
    assert "astral.sh/uv/install.sh" not in on_create


def test_devcontainer_config_exempts_the_workspace_from_dubious_ownership_checks() -> None:
    # The workspace volume's own top-level mountpoint is always root-owned
    # (Docker creates it that way, and `--cap-drop=ALL` means nothing can
    # ever `chown` it) even though `_populate_workspace` extracts its
    # CONTENTS as the non-root `vscode` user. Modern Git's own ownership
    # check inspects the working-tree ROOT, not just `.git`, so without
    # this exemption every git invocation inside the container -- including
    # `run-plugin-tests.py`'s own changed-file diffing -- would fail.
    config = _load_devcontainer_config()
    env = config["containerEnv"]
    assert env.get("GIT_CONFIG_KEY_0") == "safe.directory"
    assert env.get("GIT_CONFIG_VALUE_0") == wrapper.CONTAINER_WORKSPACE
    assert env.get("GIT_CONFIG_COUNT") == "1"
