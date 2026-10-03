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


def _init_repo(path: Path) -> None:
    import subprocess as real_subprocess
    real_subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    real_subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    real_subprocess.run(["git", "-C", str(path), "config", "user.email", "t@example.com"], check=True)


def test_materialized_git_dir_bundles_only_head_and_base_closure(tmp_path: Path, monkeypatch) -> None:
    import subprocess as real_subprocess

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("v1\n")
    real_subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    real_subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "base"], check=True)
    base_sha = real_subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
    ).stdout.strip()
    real_subprocess.run(["git", "-C", str(repo), "branch", "base-branch"], check=True)

    (repo / "tracked.txt").write_text("v2\n")
    real_subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    real_subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "head"], check=True)
    head_sha = real_subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
    ).stdout.strip()

    # A sibling branch with unique, placeholder-secret-shaped content that
    # is NEVER an ancestor of HEAD or the base ref -- this must NOT survive
    # into the materialized copy, proving the bundle closure is genuinely
    # minimal (not the whole repository's history).
    real_subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "secret-branch", base_sha],
                         check=True)
    (repo / "secret.txt").write_text("not-a-real-secret-placeholder\n")
    real_subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    real_subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "secret"], check=True)
    secret_sha = real_subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
    ).stdout.strip()
    real_subprocess.run(["git", "-C", str(repo), "checkout", "-q", head_sha], check=True)

    # A placeholder-credential-shaped remote URL in the real config --
    # must not survive into the materialized copy.
    placeholder_remote = "https://" + "not-a-real-credential" + "@example.com/repo.git"
    real_subprocess.run(
        ["git", "-C", str(repo), "config", "remote.origin.url", placeholder_remote],
        check=True,
    )

    monkeypatch.setattr(wrapper, "REPO", repo)
    with contextlib.ExitStack() as stack:
        merged = wrapper._materialized_git_dir(stack, ["--base", "base-branch"])

        rp = real_subprocess.run(
            ["git", f"--git-dir={merged}", "rev-parse", "HEAD"],
            capture_output=True, text=True,
        )
        assert rp.returncode == 0
        assert rp.stdout.strip() == head_sha

        diff = real_subprocess.run(
            ["git", f"--git-dir={merged}", "diff", "--name-only", "base-branch", "HEAD"],
            capture_output=True, text=True,
        )
        assert diff.returncode == 0
        assert "tracked.txt" in diff.stdout

        # The secret branch's commit must be UNRESOLVABLE in the
        # materialized copy -- its object is simply not present.
        secret_lookup = real_subprocess.run(
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
        status = real_subprocess.run(
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
    import subprocess as real_subprocess

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("v1\n")
    real_subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    real_subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "only commit"], check=True)

    # Stage a brand-new file whose blob is genuinely unreachable from HEAD.
    (repo / "staged-new.txt").write_text("staged content\n")
    real_subprocess.run(["git", "-C", str(repo), "add", "staged-new.txt"], check=True)

    monkeypatch.setattr(wrapper, "REPO", repo)
    with contextlib.ExitStack() as stack:
        merged = wrapper._materialized_git_dir(stack, [])
        status = real_subprocess.run(
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


def test_materialized_git_dir_skips_base_closure_when_base_unresolvable(tmp_path: Path, monkeypatch) -> None:
    import subprocess as real_subprocess

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("v1\n")
    real_subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    real_subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "only commit"], check=True)
    head_sha = real_subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True,
    ).stdout.strip()

    monkeypatch.setattr(wrapper, "REPO", repo)
    with contextlib.ExitStack() as stack:
        # "origin/main" (the default) does not exist in this tiny repo --
        # must degrade gracefully (HEAD alone), not raise.
        merged = wrapper._materialized_git_dir(stack, [])
        rp = real_subprocess.run(
            ["git", f"--git-dir={merged}", "rev-parse", "HEAD"],
            capture_output=True, text=True,
        )
        assert rp.returncode == 0
        assert rp.stdout.strip() == head_sha


def test_write_tar_of_repo_includes_materialized_git_dir_and_tracked_paths(tmp_path: Path, monkeypatch) -> None:
    fake_git_dir = tmp_path / "fake-git"
    fake_git_dir.mkdir()
    (fake_git_dir / "HEAD").write_text("ref: refs/heads/main\n")

    real_file = tmp_path / "tracked.txt"
    real_file.write_text("hello\n")

    monkeypatch.setattr(wrapper, "_materialized_git_dir", lambda stack, passthrough: fake_git_dir)
    monkeypatch.setattr(wrapper, "_tracked_paths", lambda *, include_untracked: ["tracked.txt"])
    monkeypatch.setattr(wrapper, "REPO", tmp_path)

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

    dest = tmp_path / "out.tar"
    wrapper._write_tar_of_repo(dest, [], include_untracked=False)
    with tarfile.open(dest) as tar:
        names = set(tar.getnames())
    assert "present.txt" in names
    assert "deleted.txt" not in names


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

    dest = tmp_path / "out.tar"
    wrapper._write_tar_of_repo(dest, [], include_untracked=False)
    with tarfile.open(dest) as tar:
        names = set(tar.getnames())
    assert "vendor/some-submodule" in names
    assert "vendor/some-submodule/secret-inside-submodule.txt" not in names


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
    tar_result = mock.Mock(returncode=0, stderr=b"")
    chmod_result = mock.Mock(returncode=0, stderr="")
    with mock.patch.object(wrapper.subprocess, "run",
                            side_effect=[tar_result, chmod_result]) as run:
        wrapper._populate_workspace("container-9", ["--changed"], include_untracked=True)
    assert run.call_count == 2
    assert len(written_paths) == 1
    assert written_passthrough == [["--changed"]]
    assert written_include_untracked == [True]

    tar_call = run.call_args_list[0]
    tar_args = tar_call.args[0]
    assert tar_args[:5] == ["docker", "exec", "-i", "-u", "root"]
    assert "container-9" in tar_args
    assert "tar" in tar_args
    assert "--no-same-owner" in tar_args
    # Streamed via `stdin=`, never buffered as an `input=` bytes payload.
    assert "stdin" in tar_call.kwargs
    assert "input" not in tar_call.kwargs

    chmod_args = run.call_args_list[1].args[0]
    assert chmod_args[:4] == ["docker", "exec", "-u", "root"]
    assert "chmod" in chmod_args
    assert wrapper.CONTAINER_WORKSPACE in chmod_args


def test_populate_workspace_raises_when_tar_extraction_fails(monkeypatch) -> None:
    monkeypatch.setattr(wrapper, "_write_tar_of_repo",
                         lambda dest, passthrough, *, include_untracked: dest.write_bytes(b""))
    tar_result = mock.Mock(returncode=1, stderr=b"tar: permission denied")
    with mock.patch.object(wrapper.subprocess, "run", return_value=tar_result):
        try:
            wrapper._populate_workspace("container-9", [], include_untracked=False)
        except SystemExit as exc:
            assert "permission denied" in str(exc)
        else:
            raise AssertionError("expected SystemExit")


def test_populate_workspace_raises_when_chmod_fails(monkeypatch) -> None:
    monkeypatch.setattr(wrapper, "_write_tar_of_repo",
                         lambda dest, passthrough, *, include_untracked: dest.write_bytes(b""))
    tar_result = mock.Mock(returncode=0, stderr=b"")
    chmod_result = mock.Mock(returncode=1, stderr="chmod: operation not permitted")
    with mock.patch.object(wrapper.subprocess, "run", side_effect=[tar_result, chmod_result]):
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


def test_cleanup_orphan_never_raises_even_if_every_removal_fails() -> None:
    find_result = mock.Mock(returncode=0, stdout="cid-a\n", stderr="")
    rm_result = mock.Mock(returncode=1)
    vol_result = mock.Mock(returncode=1)
    with mock.patch.object(wrapper.subprocess, "run",
                            side_effect=[find_result, rm_result, vol_result]):
        wrapper._cleanup_orphan("instance-label", "fake-volume")


def test_cleanup_orphan_never_raises_when_every_subprocess_call_itself_raises() -> None:
    import subprocess as real_subprocess

    def always_times_out(*args, **kwargs):
        raise real_subprocess.TimeoutExpired(cmd=args[0] if args else "docker", timeout=30)

    with mock.patch.object(wrapper.subprocess, "run", side_effect=always_times_out):
        # Must not raise -- this runs while an already-failing startup
        # error is propagating, and that original error must surface, not
        # a secondary cleanup failure (not even a raised TimeoutExpired
        # from one of the cleanup's own subprocess calls).
        wrapper._cleanup_orphan("instance-label", "fake-volume")


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
