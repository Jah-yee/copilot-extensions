"""Focused unit tests for the devcontainer test-isolation wrapper.

These tests never invoke Docker or the real ``devcontainer`` CLI -- they
exercise the wrapper's own logic (argument parsing, the host-copy exclusion
list, and the Docker/devcontainer-CLI invocation shape) via subprocess
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


def test_tar_of_repo_excludes_host_only_artifacts() -> None:
    data = wrapper._tar_of_repo()
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        names = set(tar.getnames())
    for excluded in wrapper.EXCLUDED_TOP_LEVEL:
        assert excluded not in names
    # A real, always-present tracked file proves the copy is not empty.
    assert "TESTING.md" in names


def test_bring_up_parses_container_id_from_devcontainer_up_output() -> None:
    fake_result = mock.Mock(
        returncode=0,
        stdout='{"outcome":"success","containerId":"abc123"}\n',
        stderr="",
    )
    with mock.patch.object(wrapper.shutil, "which", return_value="/usr/bin/devcontainer"), \
         mock.patch.object(wrapper.subprocess, "run", return_value=fake_result) as run:
        container_id = wrapper._bring_up("instance-label")
    assert container_id == "abc123"
    args = run.call_args.args[0]
    assert args[0] == "/usr/bin/devcontainer"
    assert "up" in args
    assert "--config" in args
    assert str(wrapper.DEVCONTAINER_CONFIG) in args


def test_bring_up_raises_when_devcontainer_cli_missing() -> None:
    with mock.patch.object(wrapper.shutil, "which", return_value=None):
        try:
            wrapper._bring_up("instance-label")
        except SystemExit as exc:
            assert "devcontainer CLI not found" in str(exc)
        else:
            raise AssertionError("expected SystemExit")


def test_bring_up_raises_when_container_id_missing_from_output() -> None:
    fake_result = mock.Mock(returncode=0, stdout="no json here\n", stderr="")
    with mock.patch.object(wrapper.shutil, "which", return_value="/usr/bin/devcontainer"), \
         mock.patch.object(wrapper.subprocess, "run", return_value=fake_result):
        try:
            wrapper._bring_up("instance-label")
        except SystemExit as exc:
            assert "containerId" in str(exc)
        else:
            raise AssertionError("expected SystemExit")


def test_run_tests_invokes_devcontainer_exec_with_passthrough_args() -> None:
    fake_result = mock.Mock(returncode=3)
    with mock.patch.object(wrapper.shutil, "which", return_value="/usr/bin/devcontainer"), \
         mock.patch.object(wrapper.subprocess, "run", return_value=fake_result) as run:
        code = wrapper._run_tests("abc123", ["agent-worktrees", "-k", "foo"])
    assert code == 3
    args = run.call_args.args[0]
    assert args[:2] == ["/usr/bin/devcontainer", "exec"]
    assert "--container-id" in args
    assert args[-4:] == ["python", "tools/run-plugin-tests.py", "agent-worktrees", "-k"] \
        or args[-5:] == ["python", "tools/run-plugin-tests.py", "agent-worktrees", "-k", "foo"]


def test_main_strips_leading_double_dash_from_passthrough(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_bring_up(label: str) -> str:
        return "container-1"

    def fake_populate(container_id: str) -> None:
        return None

    def fake_run_tests(container_id: str, passthrough: list[str]) -> int:
        calls.append(passthrough)
        return 0

    def fake_tear_down(container_id: str) -> None:
        return None

    monkeypatch.setattr(wrapper, "_bring_up", fake_bring_up)
    monkeypatch.setattr(wrapper, "_populate_workspace", fake_populate)
    monkeypatch.setattr(wrapper, "_run_tests", fake_run_tests)
    monkeypatch.setattr(wrapper, "_tear_down", fake_tear_down)

    rc = wrapper.main(["--", "--changed"])
    assert rc == 0
    assert calls == [["--changed"]]


def test_main_tears_down_container_unless_keep_is_passed(monkeypatch) -> None:
    torn_down: list[str] = []
    monkeypatch.setattr(wrapper, "_bring_up", lambda label: "container-2")
    monkeypatch.setattr(wrapper, "_populate_workspace", lambda container_id: None)
    monkeypatch.setattr(wrapper, "_run_tests", lambda container_id, passthrough: 0)
    monkeypatch.setattr(wrapper, "_tear_down", lambda container_id: torn_down.append(container_id))

    wrapper.main(["agent-worktrees"])
    assert torn_down == ["container-2"]

    torn_down.clear()
    wrapper.main(["--keep", "agent-worktrees"])
    assert torn_down == []
