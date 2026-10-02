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

* a **payload hash** -- a content hash of the plugin's own directory plus
  every vendored lib directory it needs, walked directly on disk (not
  `git HEAD`: promotion builds from a scratch tree already mutated by
  version bumps and materialization, so the bytes actually fed to the
  build can differ from `HEAD` even though both describe "this commit" --
  hashing the working tree is the only way the identity matches what was
  actually built);
* the **platform/python tags** read directly off the built wheels'
  filenames (the canonical, self-describing source for this -- never
  guessed from the running interpreter), with a conflicting pair of
  non-universal tags across the wheel set treated as a hard failure rather
  than resolved by whichever wheel happened to be built first;
* the **build-tool closure** actually used -- read from each wheel's own
  `dist-info/WHEEL` ``Generator:`` line after the build, not assumed in
  advance. A missing `Generator:` line fails the build closed rather than
  silently recording an unknown toolchain. A promotion run that locks one
  shared toolchain version for every wheel it builds (the effort's own
  resolved direction) will naturally produce one shared value here; this
  script does not perform that locking itself -- it faithfully reports
  whatever toolchain a given invocation's `uv build` actually used; and
* every **wheel's own filename and digest** -- two artifact sets with the
  same source/tags/toolchain but byte-different wheels must never collide
  on the same `artifact_id`.

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

_GENERATOR_RE = re.compile(r"^Generator:\s*(.+?)\s*$", re.MULTILINE)
_BUILD_TAG_RE = re.compile(r"^[0-9][^-]*$")

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
    its materialized-tree enumeration never disagree. Each discovered
    consumer directory is validated with `uv_editable_ref.uv_editable_problems`
    -- the same acceptance check `materialize_main.py` applies before
    trusting a reference (rejects a missing `editable = true`, a path
    resolving outside canonical `libs/<lib>`, a missing/incomplete
    directory, or a symlinked tree) -- so this script can never build or
    describe a source a real materialization would have refused. Returns
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
        consumer_label = current_r.name
        problems = uer.uv_editable_problems(consumer_label, current)
        if problems:
            raise ArtifactBuildError(
                f"{current}: rejected by uv_editable_problems: {'; '.join(problems)}"
            )
        try:
            refs = uer.find_uv_editable_refs(current)
        except uer.ManifestUnreadable as exc:
            raise ArtifactBuildError(str(exc)) from exc
        for _name, raw_path, lib, _editable in refs:
            canonical = (current / raw_path).resolve()
            if lib not in out:
                out[lib] = canonical
                pending.append(canonical)
    return sorted(out.items())


_PAYLOAD_IGNORE_DIR_NAMES = {
    ".git", "__pycache__", ".pytest_cache", "build", "dist",
    ".mypy_cache", ".ruff_cache",
}
_PAYLOAD_IGNORE_SUFFIXES = (".pyc", ".pyo")


def directory_content_hash(d: Path) -> str:
    """A hex digest over every regular file's relative path and content
    under ``d`` (skipping VCS/cache/build-artifact noise), sorted so
    traversal order never affects the result. This hashes the actual
    working tree `build_wheel` is about to read -- never `git HEAD` --
    because promotion builds from a scratch tree already mutated by
    version bumps and materialization: the bytes a build actually consumes
    can differ from `HEAD` even when both nominally describe the same
    commit, and the payload hash must track what was really built."""
    entries = []
    for p in sorted(d.rglob("*")):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(d).parts
        if any(part in _PAYLOAD_IGNORE_DIR_NAMES for part in rel_parts[:-1]):
            continue
        if p.suffix in _PAYLOAD_IGNORE_SUFFIXES:
            continue
        rel = p.relative_to(d).as_posix()
        entries.append(f"{rel}:{hashlib.sha256(p.read_bytes()).hexdigest()}")
    return hashlib.sha256("\n".join(sorted(entries)).encode("utf-8")).hexdigest()


def compute_payload_hash(dirs: list[Path]) -> str:
    """A single hash over every directory in ``dirs`` (plugin + vendored
    libs), each identified by its repo-relative path and its own working-
    tree content hash (`directory_content_hash`). Sorted so key order never
    affects the hash, and the relative path is included so swapping which
    lib lives at which path is itself a change (not just the content)."""
    parts = [
        f"{d.resolve().relative_to(REPO).as_posix()}={directory_content_hash(d)}"
        for d in dirs
    ]
    digest = hashlib.sha256("\n".join(sorted(parts)).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def parse_wheel_filename(path: Path) -> dict[str, str]:
    """Parses a wheel filename's identity components by tokenizing from the
    RIGHT (PEP 427's own grammar: `{name}-{version}(-{build tag})?-{python
    tag}-{abi tag}-{platform tag}.whl`), rather than a single greedy regex:
    a regex's leftmost-longest backtracking prefers absorbing the optional
    numeric build tag into an over-long `name`/`version` match whenever
    that also happens to satisfy the pattern, silently mis-parsing a wheel
    that legitimately carries one (e.g. `demo_pkg-1.2.3-1-py3-none-any.whl`
    would report `version="1"` instead of `"1.2.3"`). The three
    compatibility tags never contain hyphens, so they are unambiguously the
    last three '-'-delimited tokens; an optional build tag (if present) is
    exactly the token before them, and must start with a digit."""
    if not path.name.endswith(".whl"):
        raise ArtifactBuildError(f"{path}: not a well-formed wheel filename")
    tokens = path.name[: -len(".whl")].split("-")
    if len(tokens) < 5:
        raise ArtifactBuildError(f"{path}: not a well-formed wheel filename")
    platform_tag, abi_tag, python_tag = tokens[-1], tokens[-2], tokens[-3]
    rest = tokens[:-3]
    if len(rest) >= 3 and _BUILD_TAG_RE.match(rest[-1]):
        rest = rest[:-1]
    if len(rest) < 2:
        raise ArtifactBuildError(f"{path}: not a well-formed wheel filename")
    name = "-".join(rest[:-1])
    version = rest[-1]
    if not name or not version:
        raise ArtifactBuildError(f"{path}: not a well-formed wheel filename")
    return {
        "name": name,
        "version": version,
        "python_tag": python_tag,
        "abi_tag": abi_tag,
        "platform_tag": platform_tag,
    }


def _more_specific(a: str, b: str, *, slot: str) -> str:
    """Prefer whichever of two same-slot tags is NOT a universal wildcard.
    Two DIFFERENT non-universal tags in the same slot (e.g. `cp311` and
    `cp312`) mean the wheel set genuinely has no single valid identity for
    that slot -- silently keeping whichever was encountered first would
    advertise the narrower set as compatible with an environment that
    cannot actually use part of it. Fail closed instead of guessing."""
    if a == b:
        return a
    if a in _UNIVERSAL_TAGS and b not in _UNIVERSAL_TAGS:
        return b
    if b in _UNIVERSAL_TAGS and a not in _UNIVERSAL_TAGS:
        return a
    if a in _UNIVERSAL_TAGS and b in _UNIVERSAL_TAGS:
        return a
    raise ArtifactBuildError(
        f"conflicting {slot} tags in one artifact set: {a!r} vs {b!r} -- "
        "no single wheel-compatibility identity covers both"
    )


def overall_identity_tags(wheel_infos: list[dict[str, str]]) -> dict[str, str]:
    """The artifact set's own (python_tag, abi_tag, platform_tag): the most
    specific tag present in any single wheel wins per slot, since a
    platform-specific wheel anywhere in the set makes the whole set only
    valid for that platform even if other wheels in the set are universal
    pure-Python wheels. Raises `ArtifactBuildError` if two wheels disagree
    on a genuinely conflicting, non-universal tag for the same slot."""
    python_tag = abi_tag = platform_tag = None
    for info in wheel_infos:
        python_tag = info["python_tag"] if python_tag is None else _more_specific(
            python_tag, info["python_tag"], slot="python_tag"
        )
        abi_tag = info["abi_tag"] if abi_tag is None else _more_specific(
            abi_tag, info["abi_tag"], slot="abi_tag"
        )
        platform_tag = info["platform_tag"] if platform_tag is None else _more_specific(
            platform_tag, info["platform_tag"], slot="platform_tag"
        )
    return {
        "python_tag": python_tag or "py3",
        "abi_tag": abi_tag or "none",
        "platform_tag": platform_tag or "any",
    }


def read_wheel_generator(wheel_path: Path) -> str:
    """The ``Generator:`` line from the wheel's own `dist-info/WHEEL` file
    -- the build backend + version that actually produced it (e.g.
    ``"setuptools (84.1.0)"``), read from the artifact itself rather than
    assumed from `pyproject.toml`'s open-floor `requires`. A manifest that
    cannot name the toolchain that built a wheel is exactly the
    unreproducible state this effort's build-hermeticity resolution exists
    to close, so a wheel with no (or unreadable) `Generator:` line fails
    the build rather than recording an unknown toolchain."""
    with zipfile.ZipFile(wheel_path) as zf:
        wheel_meta_names = [
            n for n in zf.namelist() if n.endswith(".dist-info/WHEEL")
        ]
        if not wheel_meta_names:
            raise ArtifactBuildError(f"{wheel_path}: no dist-info/WHEEL entry found")
        text = zf.read(wheel_meta_names[0]).decode("utf-8", errors="replace")
    m = _GENERATOR_RE.search(text)
    if not m:
        raise ArtifactBuildError(
            f"{wheel_path}: dist-info/WHEEL has no Generator: line -- refusing "
            "to record an artifact with an unknown build toolchain"
        )
    return m.group(1)


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
    wheel_digest_input = "\n".join(
        sorted(f"{e['filename']}={e['sha256']}" for e in entries)
    )
    artifact_id_input = "|".join(
        [
            payload_hash,
            identity_tags["python_tag"],
            identity_tags["abi_tag"],
            identity_tags["platform_tag"],
            ",".join(toolchain),
            wheel_digest_input,
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
