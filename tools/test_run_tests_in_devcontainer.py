"""Focused unit tests for the devcontainer test-isolation wrapper.

These tests never invoke Docker or the real ``devcontainer`` CLI -- they
exercise the wrapper's own logic (argument parsing, the host-copy exclusion
list, the per-instance config/volume rewrite, the privileged workspace
population, and the Docker/devcontainer-CLI invocation shape) via subprocess
mocking, matching the style of ``test_run_plugin_tests.py``. A real,
Docker-backed end-to-end run is exercised manually (see the effort
README's Phase 1 journal), not in the repository's default test portfolio,
since it requires a working Docker daemon and network access to pull a
base image -- neither of which this repo's unit-test tier guarantees.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
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


def test_tar_of_repo_excludes_host_only_artifacts_but_includes_git() -> None:
    data = wrapper._tar_of_repo()
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        names = set(tar.getnames())
    for excluded in wrapper.EXCLUDED_TOP_LEVEL:
        assert excluded not in names
    # A real, always-present tracked file proves the copy is not empty.
    assert "TESTING.md" in names
    # `.git` must be included -- `tools/run-plugin-tests.py --changed`
    # shells out to `git diff`/`git status` inside the container, which
    # silently produces an empty (not failing) target set without it.
    assert ".git" in names


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


def test_populate_workspace_runs_root_tar_extraction_then_chmod() -> None:
    tar_result = mock.Mock(returncode=0, stderr=b"")
    chmod_result = mock.Mock(returncode=0, stderr="")
    with mock.patch.object(wrapper, "_tar_of_repo", return_value=b"tarball-bytes"), \
         mock.patch.object(wrapper.subprocess, "run", side_effect=[tar_result, chmod_result]) as run:
        wrapper._populate_workspace("container-9")
    assert run.call_count == 2

    tar_call = run.call_args_list[0]
    tar_args = tar_call.args[0]
    assert tar_args[:5] == ["docker", "exec", "-i", "-u", "root"]
    assert "container-9" in tar_args
    assert "tar" in tar_args
    assert "--no-same-owner" in tar_args
    assert tar_call.kwargs["input"] == b"tarball-bytes"

    chmod_args = run.call_args_list[1].args[0]
    assert chmod_args[:4] == ["docker", "exec", "-u", "root"]
    assert "chmod" in chmod_args
    assert wrapper.CONTAINER_WORKSPACE in chmod_args


def test_populate_workspace_raises_when_tar_extraction_fails() -> None:
    tar_result = mock.Mock(returncode=1, stderr=b"tar: permission denied")
    with mock.patch.object(wrapper, "_tar_of_repo", return_value=b""), \
         mock.patch.object(wrapper.subprocess, "run", return_value=tar_result):
        try:
            wrapper._populate_workspace("container-9")
        except SystemExit as exc:
            assert "permission denied" in str(exc)
        else:
            raise AssertionError("expected SystemExit")


def test_populate_workspace_raises_when_chmod_fails() -> None:
    tar_result = mock.Mock(returncode=0, stderr=b"")
    chmod_result = mock.Mock(returncode=1, stderr="chmod: operation not permitted")
    with mock.patch.object(wrapper, "_tar_of_repo", return_value=b""), \
         mock.patch.object(wrapper.subprocess, "run", side_effect=[tar_result, chmod_result]):
        try:
            wrapper._populate_workspace("container-9")
        except SystemExit as exc:
            assert "operation not permitted" in str(exc)
        else:
            raise AssertionError("expected SystemExit")


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
        # Must not raise -- this runs while an already-failing startup
        # error is propagating, and that original error must surface, not
        # a secondary cleanup failure.
        wrapper._cleanup_orphan("instance-label", "fake-volume")


def test_main_cleans_up_orphan_and_reraises_when_bring_up_fails(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "cfgdir" / "devcontainer.json"
    config_path.parent.mkdir()
    config_path.write_text("{}")
    cleanup_calls: list[tuple[str, str]] = []

    def failing_bring_up(label: str, cfg: Path) -> str:
        raise SystemExit("devcontainer up failed: boom")

    monkeypatch.setattr(wrapper, "_per_instance_config",
                         lambda label: (config_path, "fake-volume"))
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


def test_resolve_git_dirs_parses_rev_parse_output() -> None:
    git_dir_result = mock.Mock(returncode=0, stdout="/abs/path/.git/worktrees/w\n", stderr="")
    common_dir_result = mock.Mock(returncode=0, stdout="/abs/path/.git\n", stderr="")
    with mock.patch.object(wrapper.subprocess, "run",
                            side_effect=[git_dir_result, common_dir_result]):
        git_dir, common_dir = wrapper._resolve_git_dirs()
    assert git_dir == Path("/abs/path/.git/worktrees/w")
    assert common_dir == Path("/abs/path/.git")


def test_resolve_git_dirs_raises_on_git_failure() -> None:
    fail_result = mock.Mock(returncode=128, stdout="", stderr="not a git repository")
    with mock.patch.object(wrapper.subprocess, "run", return_value=fail_result):
        try:
            wrapper._resolve_git_dirs()
        except SystemExit as exc:
            assert "not a git repository" in str(exc)
        else:
            raise AssertionError("expected SystemExit")


def test_materialized_git_dir_returns_repo_git_for_normal_checkout(monkeypatch) -> None:
    same = Path("/abs/path/.git")
    monkeypatch.setattr(wrapper, "_resolve_git_dirs", lambda: (same, same))
    with contextlib.ExitStack() as stack:
        result = wrapper._materialized_git_dir(stack)
    assert result == wrapper.REPO / ".git"


def test_materialized_git_dir_merges_worktree_refs_without_losing_common_refs(tmp_path: Path, monkeypatch) -> None:
    # Build a minimal common dir (shared branch ref + an unrelated OTHER
    # worktree's private state) and a per-worktree private dir (its own
    # near-empty `refs`, matching real git's on-disk layout) and confirm
    # the merge keeps the common branch ref instead of letting the private
    # dir's own near-empty `refs` wipe it.
    common_dir = tmp_path / "common" / ".git"
    (common_dir / "refs" / "heads").mkdir(parents=True)
    (common_dir / "refs" / "heads" / "main").write_text("deadbeef\n")
    (common_dir / "worktrees" / "other-worktree").mkdir(parents=True)
    (common_dir / "worktrees" / "other-worktree" / "HEAD").write_text("ref: refs/heads/other\n")
    (common_dir / "objects").mkdir()

    git_dir = tmp_path / "common" / ".git" / "worktrees" / "this-worktree"
    (git_dir / "refs").mkdir(parents=True)
    git_dir_joined = tmp_path / "common" / ".git" / "worktrees" / "this-worktree"
    (git_dir_joined / "HEAD").write_text("ref: refs/heads/main\n")
    (git_dir_joined / "commondir").write_text("../..\n")

    monkeypatch.setattr(wrapper, "_resolve_git_dirs", lambda: (git_dir_joined, common_dir))
    with contextlib.ExitStack() as stack:
        merged = wrapper._materialized_git_dir(stack)
        assert (merged / "refs" / "heads" / "main").read_text() == "deadbeef\n"
        assert (merged / "HEAD").read_text() == "ref: refs/heads/main\n"
        assert not (merged / "commondir").exists()
        # The OTHER worktree's own private state must not leak into this
        # merged, self-contained copy.
        assert not (merged / "worktrees").exists()
    # Outside the ExitStack, the temp dir must be cleaned up.
    assert not merged.parent.exists()


def test_tar_of_repo_uses_materialized_git_dir(monkeypatch, tmp_path: Path) -> None:
    fake_git_dir = tmp_path / "fake-git"
    fake_git_dir.mkdir()
    (fake_git_dir / "HEAD").write_text("ref: refs/heads/main\n")

    monkeypatch.setattr(wrapper, "_materialized_git_dir", lambda stack: fake_git_dir)
    data = wrapper._tar_of_repo()
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        names = set(tar.getnames())
    assert ".git/HEAD" in names


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


def test_main_strips_double_dash_separator_anywhere_in_passthrough(monkeypatch) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(wrapper, "_per_instance_config",
                         lambda label: (Path("/tmp/fake-devcontainer-dir/devcontainer.json"), "fake-volume"))
    monkeypatch.setattr(wrapper, "_bring_up", lambda label, config_path: "container-1")
    monkeypatch.setattr(wrapper, "_populate_workspace", lambda container_id: None)
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
    monkeypatch.setattr(wrapper, "_bring_up", lambda label, cfg: "container-2")
    monkeypatch.setattr(wrapper, "_populate_workspace", lambda container_id: None)
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
