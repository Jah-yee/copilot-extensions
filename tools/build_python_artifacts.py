#!/usr/bin/env python3
"""Build promotion-time, content-addressed Python artifacts for one plugin:
its own wheel plus a wheel for every vendored `libs/<lib>` dependency it
resolves through the `uv`-editable canonical-reference form (see
`uv_editable_ref.find_uv_editable_refs`), recursing into a vendored lib's own
nested references the same way `materialize_main.py` does. This is Phase 2
of the `governed-python-artifact-promotion` effort
(`efforts/active/governed-python-artifact-promotion/README.md`) -- the first
implementation slice: wheel + manifest generation. Publication (a GitHub
Release), attestation (Sigstore/OIDC), and promotion-pipeline wiring are
separate, later slices; this script only builds artifacts and writes a
manifest describing them, and does not publish or sign anything.

**Artifact identity.** Per the effort's resolved Open Design Questions, an
artifact set's identity folds together:

* a **payload hash** -- the git tree SHA of the plugin's own directory plus
  every vendored lib directory it needs (so any content change to any of
  them changes the identity);
* the **platform/python tags** read directly off the built wheels'
  filenames (the canonical, self-describing source for this -- never
  guessed from the running interpreter); and
* the **build-tool closure** actually used -- read from each wheel's own
  `dist-info/WHEEL` ``Generator:`` line after the build, not assumed in
  advance. A promotion run that locks one shared toolchain version for
  every wheel it builds (the effort's own resolved direction) will
  naturally produce one shared value here; this script does not perform
  that locking itself -- it faithfully reports whatever toolchain a given
  invocation's `uv build` actually used.

Usage::

    python tools/build_python_artifacts.py agent-bridge --out-dir /tmp/dist
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import uv_editable_ref as uer  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PLUGINS_DIR = REPO / "plugins"
LIBS_DIR = REPO / "libs"
MANIFEST_SCHEMA = "copilot-extensions.python-artifact-manifest"
MANIFEST_SCHEMA_VERSION = 1

_WHEEL_NAME_RE = re.compile(
    r"^(?P<name>.+)-(?P<version>[^-]+)-(?P<python_tag>[^-]+)-(?P<abi_tag>[^-]+)"
    r"-(?P<platform_tag>[^-]+)\.whl$"
)
_GENERATOR_RE = re.compile(r"^Generator:\s*(.+?)\s*$", re.MULTILINE)

# Tags considered "universal" (compatible everywhere) -- any other tag is
# strictly more specific and wins when picking the artifact set's own
# overall platform/python identity (see `_more_specific`).
_UNIVERSAL_TAGS = {"none", "any", "py3", "py2.py3"}


class ArtifactBuildError(Exception):
    """A plugin/lib wheel could not be built, or the result could not be
    understood (unparseable filename, unreadable WHEEL metadata) -- callers
    must fail closed rather than emit a manifest describing a guess."""


def resolve_vendored_libs(consumer_dir: Path) -> list[tuple[str, Path]]:
    """Every vendored lib ``consumer_dir`` needs, recursively: each
    `[tool.uv.sources]` escaping entry (`uv_editable_ref.find_uv_editable_refs`),
    resolved to its canonical `libs/<lib>` directory, then the same lookup
    repeated on that lib's own `pyproject.toml` -- mirrors
    `materialize_main.py`'s own recursion so promotion's artifact set and
    its materialized-tree enumeration never disagree. Returns
    ``(lib_name, canonical_dir)`` pairs, each lib listed once (by name) even
    if more than one consumer along the walk references it."""
    out: dict[str, Path] = {}
    pending = [consumer_dir]
    seen_dirs: set[Path] = set()
    while pending:
        current = pending.pop()
        current_r = current.resolve()
        if current_r in seen_dirs:
            continue
        seen_dirs.add(current_r)
        try:
            refs = uer.find_uv_editable_refs(current)
        except uer.ManifestUnreadable as exc:
            raise ArtifactBuildError(str(exc)) from exc
        for _name, raw_path, lib, _editable in refs:
            canonical = (current / raw_path).resolve()
            if not uer.is_safe_lib_name(lib):
                raise ArtifactBuildError(
                    f"{current}: unsafe vendored-lib name {lib!r} in [tool.uv.sources]"
                )
            if lib not in out:
                out[lib] = canonical
                pending.append(canonical)
    return sorted(out.items())


def git_tree_sha(repo: Path, rel_path: str, *, rev: str = "HEAD") -> str:
    """The git tree object SHA for ``rel_path`` at ``rev`` -- content-addressed
    by git itself, so it changes exactly when the directory's tracked
    content changes and is stable across checkouts/platforms."""
    result = subprocess.run(
        ["git", "rev-parse", f"{rev}:{rel_path}"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ArtifactBuildError(
            f"git rev-parse {rev}:{rel_path} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def compute_payload_hash(dirs: list[Path], *, rev: str = "HEAD") -> str:
    """A single hash over every directory in ``dirs`` (plugin + vendored
    libs), each identified by its repo-relative path and git tree SHA.
    Sorted so key order never affects the hash, and the relative path is
    included so swapping which lib lives at which path is itself a change
    (not just the content)."""
    parts = []
    for d in dirs:
        rel = d.resolve().relative_to(REPO).as_posix()
        parts.append(f"{rel}={git_tree_sha(REPO, rel, rev=rev)}")
    digest = hashlib.sha256("\n".join(sorted(parts)).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def parse_wheel_filename(path: Path) -> dict[str, str]:
    m = _WHEEL_NAME_RE.match(path.name)
    if not m:
        raise ArtifactBuildError(f"{path}: not a well-formed wheel filename")
    return m.groupdict()


def _more_specific(a: str, b: str) -> str:
    """Prefer whichever of two same-slot tags is NOT a universal wildcard;
    if both (or neither) are universal, prefer the one already chosen."""
    if a in _UNIVERSAL_TAGS and b not in _UNIVERSAL_TAGS:
        return b
    return a


def overall_identity_tags(wheel_infos: list[dict[str, str]]) -> dict[str, str]:
    """The artifact set's own (python_tag, abi_tag, platform_tag): the most
    specific tag present in any single wheel wins per slot, since a
    platform-specific wheel anywhere in the set makes the whole set only
    valid for that platform even if other wheels in the set are universal
    pure-Python wheels."""
    python_tag = abi_tag = platform_tag = None
    for info in wheel_infos:
        python_tag = info["python_tag"] if python_tag is None else _more_specific(
            python_tag, info["python_tag"]
        )
        abi_tag = info["abi_tag"] if abi_tag is None else _more_specific(
            abi_tag, info["abi_tag"]
        )
        platform_tag = info["platform_tag"] if platform_tag is None else _more_specific(
            platform_tag, info["platform_tag"]
        )
    return {
        "python_tag": python_tag or "py3",
        "abi_tag": abi_tag or "none",
        "platform_tag": platform_tag or "any",
    }


def read_wheel_generator(wheel_path: Path) -> str | None:
    """The ``Generator:`` line from the wheel's own `dist-info/WHEEL` file
    -- the build backend + version that actually produced it (e.g.
    ``"setuptools (84.1.0)"``), read from the artifact itself rather than
    assumed from `pyproject.toml`'s open-floor `requires`."""
    with zipfile.ZipFile(wheel_path) as zf:
        wheel_meta_names = [
            n for n in zf.namelist() if n.endswith(".dist-info/WHEEL")
        ]
        if not wheel_meta_names:
            raise ArtifactBuildError(f"{wheel_path}: no dist-info/WHEEL entry found")
        text = zf.read(wheel_meta_names[0]).decode("utf-8", errors="replace")
    m = _GENERATOR_RE.search(text)
    return m.group(1) if m else None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def build_wheel(source_dir: Path, out_dir: Path, *, python: str | None = None) -> Path:
    """Builds a wheel for ``source_dir`` into ``out_dir`` via `uv build
    --wheel`, resolving its build-system `requires` the normal (isolated)
    way -- which, on a correctly governed-feed-configured machine, already
    resolves only from that feed; this script adds no index configuration
    of its own. Returns the built wheel's path, identified by snapshotting
    ``out_dir``'s `*.whl` contents before and after rather than parsing
    `uv`'s own stdout (whose exact phrasing is not a stable contract this
    script should depend on)."""
    before = {p.name for p in out_dir.glob("*.whl")} if out_dir.is_dir() else set()
    cmd = ["uv", "build", "--wheel", "-o", str(out_dir)]
    if python:
        cmd += ["--python", python]
    cmd.append(str(source_dir))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ArtifactBuildError(
            f"uv build failed for {source_dir}:\n{result.stdout}\n{result.stderr}"
        )
    after = {p.name for p in out_dir.glob("*.whl")} if out_dir.is_dir() else set()
    new_names = after - before
    if len(new_names) != 1:
        raise ArtifactBuildError(
            f"uv build for {source_dir} produced {len(new_names)} new wheel(s) "
            f"in {out_dir}, expected exactly 1: {sorted(new_names)}"
        )
    return out_dir / next(iter(new_names))


def build_plugin_artifacts(
    plugin: str, *, out_dir: Path, python: str | None = None
) -> dict:
    """Builds the plugin's own wheel plus every vendored lib wheel it needs,
    and returns the manifest describing the whole set (also written to
    ``out_dir`` as ``<plugin>-<version>-manifest.json``)."""
    plugin_dir = PLUGINS_DIR / plugin
    if not (plugin_dir / "pyproject.toml").is_file():
        raise ArtifactBuildError(f"{plugin_dir}: no pyproject.toml -- not a plugin")

    vendored_libs = resolve_vendored_libs(plugin_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    entries: list[dict] = []
    wheel_infos: list[dict[str, str]] = []
    generators: set[str] = set()
    all_dirs = [plugin_dir] + [d for _name, d in vendored_libs]

    plugin_wheel = build_wheel(plugin_dir, out_dir, python=python)
    plugin_info = parse_wheel_filename(plugin_wheel)
    plugin_generator = read_wheel_generator(plugin_wheel)
    if plugin_generator:
        generators.add(plugin_generator)
    wheel_infos.append(plugin_info)
    entries.append(
        {
            "role": "plugin",
            "name": plugin_info["name"],
            "source": f"plugins/{plugin}",
            "filename": plugin_wheel.name,
            "sha256": sha256_file(plugin_wheel),
            "generator": plugin_generator,
        }
    )

    for lib_name, lib_dir in vendored_libs:
        lib_wheel = build_wheel(lib_dir, out_dir, python=python)
        lib_info = parse_wheel_filename(lib_wheel)
        lib_generator = read_wheel_generator(lib_wheel)
        if lib_generator:
            generators.add(lib_generator)
        wheel_infos.append(lib_info)
        rel_source = lib_dir.resolve().relative_to(REPO).as_posix()
        entries.append(
            {
                "role": "vendored-lib",
                "name": lib_info["name"],
                "source": rel_source,
                "filename": lib_wheel.name,
                "sha256": sha256_file(lib_wheel),
                "generator": lib_generator,
            }
        )

    identity_tags = overall_identity_tags(wheel_infos)
    payload_hash = compute_payload_hash(all_dirs)
    toolchain = sorted(generators)
    artifact_id_input = "|".join(
        [
            payload_hash,
            identity_tags["python_tag"],
            identity_tags["abi_tag"],
            identity_tags["platform_tag"],
            ",".join(toolchain),
        ]
    )
    artifact_id = "sha256:" + hashlib.sha256(
        artifact_id_input.encode("utf-8")
    ).hexdigest()

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "plugin": plugin,
        "version": plugin_info["version"],
        "python_tag": identity_tags["python_tag"],
        "abi_tag": identity_tags["abi_tag"],
        "platform_tag": identity_tags["platform_tag"],
        "payload_hash": payload_hash,
        "build_toolchain": toolchain,
        "artifact_id": artifact_id,
        "wheels": entries,
    }
    manifest_path = out_dir / f"{plugin}-{plugin_info['version']}-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("plugin", help="plugin name under plugins/")
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--python", help="interpreter to build wheels against")
    args = ap.parse_args()
    try:
        manifest = build_plugin_artifacts(
            args.plugin, out_dir=args.out_dir, python=args.python
        )
    except ArtifactBuildError as exc:
        print(f"build-python-artifacts: FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
