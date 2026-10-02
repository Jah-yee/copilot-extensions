from __future__ import annotations

import importlib.util
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "tools" / "build_python_artifacts.py"

_SPEC = importlib.util.spec_from_file_location("build_python_artifacts", MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
bpa = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bpa)


# --- parse_wheel_filename -----------------------------------------------


def test_parse_wheel_filename_pure_python():
    info = bpa.parse_wheel_filename(Path("agent_bridge-0.4.1.dev3-py3-none-any.whl"))
    assert info == {
        "name": "agent_bridge",
        "version": "0.4.1.dev3",
        "python_tag": "py3",
        "abi_tag": "none",
        "platform_tag": "any",
    }


def test_parse_wheel_filename_platform_specific():
    info = bpa.parse_wheel_filename(
        Path("pydantic_core-2.46.5-cp312-cp312-win_amd64.whl")
    )
    assert info["python_tag"] == "cp312"
    assert info["abi_tag"] == "cp312"
    assert info["platform_tag"] == "win_amd64"


def test_parse_wheel_filename_malformed_raises():
    with pytest.raises(bpa.ArtifactBuildError):
        bpa.parse_wheel_filename(Path("not-a-wheel.txt"))


# --- overall_identity_tags -----------------------------------------------


def test_overall_identity_tags_all_universal():
    infos = [
        {"python_tag": "py3", "abi_tag": "none", "platform_tag": "any"},
        {"python_tag": "py3", "abi_tag": "none", "platform_tag": "any"},
    ]
    assert bpa.overall_identity_tags(infos) == {
        "python_tag": "py3",
        "abi_tag": "none",
        "platform_tag": "any",
    }


def test_overall_identity_tags_one_platform_specific_wins():
    infos = [
        {"python_tag": "py3", "abi_tag": "none", "platform_tag": "any"},
        {"python_tag": "cp312", "abi_tag": "cp312", "platform_tag": "win_amd64"},
    ]
    assert bpa.overall_identity_tags(infos) == {
        "python_tag": "cp312",
        "abi_tag": "cp312",
        "platform_tag": "win_amd64",
    }


# --- read_wheel_generator -------------------------------------------------


def _make_fake_wheel(path: Path, *, generator: str | None) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        if generator is not None:
            zf.writestr(
                "fake_pkg-1.0.dist-info/WHEEL",
                f"Wheel-Version: 1.0\nGenerator: {generator}\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )
        else:
            zf.writestr(
                "fake_pkg-1.0.dist-info/WHEEL",
                "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )


def test_read_wheel_generator_present(tmp_path: Path):
    wheel = tmp_path / "fake_pkg-1.0-py3-none-any.whl"
    _make_fake_wheel(wheel, generator="setuptools (84.1.0)")
    assert bpa.read_wheel_generator(wheel) == "setuptools (84.1.0)"


def test_read_wheel_generator_absent(tmp_path: Path):
    wheel = tmp_path / "fake_pkg-1.0-py3-none-any.whl"
    _make_fake_wheel(wheel, generator=None)
    assert bpa.read_wheel_generator(wheel) is None


def test_read_wheel_generator_missing_dist_info_raises(tmp_path: Path):
    wheel = tmp_path / "empty-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr("not_dist_info.txt", "nothing here")
    with pytest.raises(bpa.ArtifactBuildError):
        bpa.read_wheel_generator(wheel)


# --- sha256_file -----------------------------------------------------------


def test_sha256_file_matches_hashlib(tmp_path: Path):
    import hashlib

    f = tmp_path / "data.bin"
    f.write_bytes(b"hello world" * 1000)
    expected = f"sha256:{hashlib.sha256(f.read_bytes()).hexdigest()}"
    assert bpa.sha256_file(f) == expected


# --- resolve_vendored_libs -------------------------------------------------


def _write_pyproject(path: Path, *, sources: dict[str, str] | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    lines = ['[project]', 'name = "whatever"', 'version = "0.1.0"']
    if sources:
        lines.append("")
        lines.append("[tool.uv.sources]")
        for name, rel in sources.items():
            lines.append(f'{name} = {{ path = "{rel}", editable = true }}')
    (path / "pyproject.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_resolve_vendored_libs_direct(tmp_path: Path):
    plugin_dir = tmp_path / "plugins" / "demo"
    lib_dir = tmp_path / "libs" / "widget"
    _write_pyproject(plugin_dir, sources={"demo-widget": "../../libs/widget"})
    _write_pyproject(lib_dir)

    libs = bpa.resolve_vendored_libs(plugin_dir)

    assert libs == [("widget", lib_dir.resolve())]


def test_resolve_vendored_libs_recurses_nested(tmp_path: Path):
    plugin_dir = tmp_path / "plugins" / "demo"
    lib_a = tmp_path / "libs" / "a"
    lib_b = tmp_path / "libs" / "b"
    _write_pyproject(plugin_dir, sources={"demo-a": "../../libs/a"})
    _write_pyproject(lib_a, sources={"demo-b": "../b"})
    _write_pyproject(lib_b)

    libs = dict(bpa.resolve_vendored_libs(plugin_dir))

    assert set(libs) == {"a", "b"}
    assert libs["b"] == lib_b.resolve()


def test_resolve_vendored_libs_no_sources_is_empty(tmp_path: Path):
    plugin_dir = tmp_path / "plugins" / "demo"
    _write_pyproject(plugin_dir)

    assert bpa.resolve_vendored_libs(plugin_dir) == []


def test_resolve_vendored_libs_unsafe_name_raises(tmp_path: Path):
    plugin_dir = tmp_path / "plugins" / "demo"
    # A `path` whose final component is ".." resolves outside the consumer
    # root (so `find_uv_editable_refs` includes it) but its derived `lib`
    # name (`Path(raw_path).name`) is empty -- `is_safe_lib_name` must
    # reject that rather than let an empty/unsafe name reach `libs/<lib>`.
    _write_pyproject(plugin_dir, sources={"demo-evil": "../../.."})

    with pytest.raises(bpa.ArtifactBuildError):
        bpa.resolve_vendored_libs(plugin_dir)


# --- git_tree_sha / compute_payload_hash -----------------------------------


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "plugins" / "demo").mkdir(parents=True)
    (repo / "plugins" / "demo" / "file.txt").write_text("hello\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    return repo


def test_git_tree_sha_stable_for_unchanged_content(git_repo: Path):
    sha1 = bpa.git_tree_sha(git_repo, "plugins/demo")
    sha2 = bpa.git_tree_sha(git_repo, "plugins/demo")
    assert sha1 == sha2
    assert len(sha1) == 40


def test_git_tree_sha_changes_with_content(git_repo: Path):
    before = bpa.git_tree_sha(git_repo, "plugins/demo")
    (git_repo / "plugins" / "demo" / "file.txt").write_text("changed\n", encoding="utf-8")
    _git("add", "-A", cwd=git_repo)
    _git("commit", "-q", "-m", "change", cwd=git_repo)
    after = bpa.git_tree_sha(git_repo, "plugins/demo")
    assert before != after


def test_git_tree_sha_missing_path_raises(git_repo: Path):
    with pytest.raises(bpa.ArtifactBuildError):
        bpa.git_tree_sha(git_repo, "plugins/does-not-exist")


def test_compute_payload_hash_deterministic_regardless_of_order(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(bpa, "REPO", git_repo)
    (git_repo / "libs" / "widget").mkdir(parents=True)
    (git_repo / "libs" / "widget" / "f.txt").write_text("x\n", encoding="utf-8")
    _git("add", "-A", cwd=git_repo)
    _git("commit", "-q", "-m", "add lib", cwd=git_repo)

    dirs = [git_repo / "plugins" / "demo", git_repo / "libs" / "widget"]
    h1 = bpa.compute_payload_hash(dirs)
    h2 = bpa.compute_payload_hash(list(reversed(dirs)))
    assert h1 == h2
    assert h1.startswith("sha256:")


def test_compute_payload_hash_changes_when_either_dir_changes(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(bpa, "REPO", git_repo)
    (git_repo / "libs" / "widget").mkdir(parents=True)
    (git_repo / "libs" / "widget" / "f.txt").write_text("x\n", encoding="utf-8")
    _git("add", "-A", cwd=git_repo)
    _git("commit", "-q", "-m", "add lib", cwd=git_repo)
    dirs = [git_repo / "plugins" / "demo", git_repo / "libs" / "widget"]
    before = bpa.compute_payload_hash(dirs)

    (git_repo / "libs" / "widget" / "f.txt").write_text("y\n", encoding="utf-8")
    _git("add", "-A", cwd=git_repo)
    _git("commit", "-q", "-m", "change lib", cwd=git_repo)
    after = bpa.compute_payload_hash(dirs)

    assert before != after


# --- build_wheel (mocked subprocess) ---------------------------------------


def test_build_wheel_identifies_new_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    out_dir = tmp_path / "dist"
    out_dir.mkdir()
    (out_dir / "preexisting-1.0-py3-none-any.whl").write_bytes(b"")

    def fake_run(cmd, capture_output, text):  # noqa: ARG001
        (out_dir / "new_pkg-2.0-py3-none-any.whl").write_bytes(b"")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(bpa.subprocess, "run", fake_run)
    wheel = bpa.build_wheel(tmp_path / "src", out_dir)
    assert wheel.name == "new_pkg-2.0-py3-none-any.whl"


def test_build_wheel_nonzero_exit_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def fake_run(cmd, capture_output, text):  # noqa: ARG001
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    monkeypatch.setattr(bpa.subprocess, "run", fake_run)
    with pytest.raises(bpa.ArtifactBuildError):
        bpa.build_wheel(tmp_path / "src", tmp_path / "dist")


def test_build_wheel_ambiguous_output_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    out_dir = tmp_path / "dist"
    out_dir.mkdir()

    def fake_run(cmd, capture_output, text):  # noqa: ARG001
        (out_dir / "a-1.0-py3-none-any.whl").write_bytes(b"")
        (out_dir / "b-1.0-py3-none-any.whl").write_bytes(b"")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(bpa.subprocess, "run", fake_run)
    with pytest.raises(bpa.ArtifactBuildError):
        bpa.build_wheel(tmp_path / "src", out_dir)


# --- build_plugin_artifacts (end-to-end, mocked build_wheel) ---------------


def test_build_plugin_artifacts_end_to_end(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(bpa, "REPO", git_repo)
    monkeypatch.setattr(bpa, "PLUGINS_DIR", git_repo / "plugins")
    monkeypatch.setattr(bpa, "LIBS_DIR", git_repo / "libs")

    plugin_dir = git_repo / "plugins" / "demo"
    _write_pyproject(plugin_dir, sources={"demo-widget": "../../libs/widget"})
    lib_dir = git_repo / "libs" / "widget"
    _write_pyproject(lib_dir)
    _git("add", "-A", cwd=git_repo)
    _git("commit", "-q", "-m", "add demo + widget", cwd=git_repo)

    out_dir = tmp_path / "dist"

    def fake_build_wheel(source_dir: Path, out: Path, *, python=None):  # noqa: ARG001
        out.mkdir(parents=True, exist_ok=True)
        name = source_dir.name.replace("-", "_")
        wheel = out / f"{name}-0.1.0-py3-none-any.whl"
        _make_fake_wheel(wheel, generator="setuptools (84.1.0)")
        return wheel

    monkeypatch.setattr(bpa, "build_wheel", fake_build_wheel)

    manifest = bpa.build_plugin_artifacts("demo", out_dir=out_dir)

    assert manifest["schema"] == bpa.MANIFEST_SCHEMA
    assert manifest["plugin"] == "demo"
    assert manifest["build_toolchain"] == ["setuptools (84.1.0)"]
    assert manifest["python_tag"] == "py3"
    assert {e["role"] for e in manifest["wheels"]} == {"plugin", "vendored-lib"}
    assert len(manifest["wheels"]) == 2
    manifest_path = out_dir / f"demo-0.1.0-manifest.json"
    assert manifest_path.is_file()
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == manifest


def test_build_plugin_artifacts_unknown_plugin_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(bpa, "PLUGINS_DIR", tmp_path / "plugins")
    with pytest.raises(bpa.ArtifactBuildError):
        bpa.build_plugin_artifacts("nope", out_dir=tmp_path / "dist")
