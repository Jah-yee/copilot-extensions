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
