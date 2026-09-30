#!/usr/bin/env python3
"""Keep installer-engine adopters consistent with canonical.

The installer engine is a **vendored** source surface, not a runtime
cross-plugin dependency: every adopting plugin ships its own byte-identical
copy under ``scripts/installer-engine.*`` because marketplace plugins are
installed independently. The canonical sources live under
``libs/installer-engine/``.

Adopters can now exist in one of two valid dev-time forms:

* the older byte-vendored copy under ``scripts/installer-engine.*``; or
* the Phase-2 canonical-reference form, where ``install.sh``/``install.ps1``
  source ``libs/installer-engine/installer-engine.{sh,ps1}`` directly and no
  plugin-local copy exists on ``dev`` at all.

This tool's ``--check`` verifies both forms correctly. Its write mode keeps
vendored adopters byte-identical and removes stale local copies from
canonical-reference adopters.

Usage::

    python tools/sync-installer-engine.py          # copy canonical -> plugins
    python tools/sync-installer-engine.py --check  # verify in sync
"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import installer_engine_ref as ier

REPO = ier.REPO
CANONICAL_DIR = ier.CANONICAL_DIR
FILES = ier.FILES
# Add future adopters here as later rollout phases land. The list stays explicit
# so the canonical engine can migrate one plugin at a time.
ADOPTERS = ier.ADOPTERS


def vendor_pairs() -> list[tuple[Path, Path]]:
    return [
        (
            CANONICAL_DIR / name,
            REPO / "plugins" / plugin / "scripts" / name,
        )
        for plugin in ADOPTERS
        for name in FILES
    ]


def _plugin_dir(plugin: str) -> Path:
    return REPO / "plugins" / plugin


def _canonical_ref_problems(plugin: str) -> list[str]:
    """Validity problems for a plugin using the dev-time canonical reference."""
    plugin_dir = _plugin_dir(plugin)
    problems: list[str] = []
    for ext in ("ps1", "sh"):
        script_path = plugin_dir / "scripts" / f"install.{ext}"
        match_count = ier.source_match_count(script_path, ext)
        if match_count > 1:
            problems.append(
                f"plugins/{plugin}/scripts/install.{ext} contains {match_count} "
                "installer-engine source lines; expected exactly one"
            )
            continue
        ref = ier.find_engine_ref(script_path, ext)
        if ref is None:
            problems.append(f"plugins/{plugin}/scripts/install.{ext} does not source installer-engine")
            continue
        if not ier.ref_escapes_plugin_root(ref, plugin_dir):
            problems.append(
                f"plugins/{plugin}/scripts/install.{ext} still sources a plugin-local "
                f"{ref.file_name}; canonical-reference adopters must remove the local copy on dev"
            )
            continue
        canonical = ier.canonical_file(ext, repo_root=REPO)
        if not canonical.is_file():
            problems.append(f"canonical source missing: {canonical.relative_to(REPO)}")
        elif ref.resolved() != canonical.resolve():
            problems.append(
                f"plugins/{plugin}/scripts/install.{ext} references {ref.raw_path} "
                f"(resolved {ref.resolved()}) which is not {canonical.relative_to(REPO)}"
            )
    return problems


def _uses_canonical_ref(plugin: str) -> bool:
    plugin_dir = _plugin_dir(plugin)
    refs = ier.plugin_ref_map(plugin_dir)
    return bool(refs) and all(
        ref is not None and ier.is_canonical_ref(ref, plugin_dir) for ref in refs.values()
    )


def _has_escaping_ref(plugin: str) -> bool:
    plugin_dir = _plugin_dir(plugin)
    return any(
        ier.ref_escapes_plugin_root(ref, plugin_dir)
        for ref in ier.plugin_ref_map(plugin_dir).values()
    )


def _has_any_ref(plugin: str) -> bool:
    plugin_dir = _plugin_dir(plugin)
    return any(
        ier.source_match_count(plugin_dir / "scripts" / f"install.{ext}", ext) > 0
        for ext in ("ps1", "sh")
    )


def unregistered_adopters() -> list[str]:
    """Plugins that vendor an installer-engine file but are absent from ``ADOPTERS``.

    A plugin that ships ``scripts/installer-engine.ps1``/``.sh`` dot-sources it
    from its own installer, so a copy outside the adopter list never receives
    canonical updates while this tool still reports everything in sync. That
    drift is invisible until the stale copy misbehaves, so name it as a
    problem instead of staying silent.
    """
    plugins_root = REPO / "plugins"
    if not plugins_root.is_dir():
        return []
    return sorted(
        candidate.name
        for candidate in plugins_root.iterdir()
        if candidate.name not in ADOPTERS
        and (
            any((candidate / "scripts" / name).is_file() for name in FILES)
            or _has_any_ref(candidate.name)
        )
    )


def verify() -> list[str]:
    problems: list[str] = []
    canonical_ref_plugins = {
        plugin for plugin in ADOPTERS if _has_escaping_ref(plugin)
    }
    for plugin in unregistered_adopters():
        problems.append(
            f"plugins/{plugin} uses installer-engine but is not listed in "
            "ADOPTERS, so the tool does not track its expected dev-time form"
        )
    for plugin in canonical_ref_plugins:
        problems.extend(_canonical_ref_problems(plugin))
        for name in FILES:
            destination = REPO / "plugins" / plugin / "scripts" / name
            relative = destination.relative_to(REPO).as_posix()
            if destination.exists() or destination.is_symlink():
                problems.append(
                    f"{relative} should not exist in dev once {plugin} uses the canonical reference"
                )
    for source, destination in vendor_pairs():
        plugin = destination.parts[-3]
        if plugin in canonical_ref_plugins:
            continue
        relative = destination.relative_to(REPO).as_posix()
        if not source.is_file():
            problems.append(f"canonical source missing: {source.relative_to(REPO)}")
        elif not destination.is_file():
            problems.append(f"{relative} is missing")
        elif destination.read_bytes() != source.read_bytes():
            problems.append(f"{relative} differs from {source.relative_to(REPO)}")
        elif os.name != "nt" and stat.S_IMODE(destination.stat().st_mode) != stat.S_IMODE(
            source.stat().st_mode
        ):
            problems.append(f"{relative} mode differs from {source.relative_to(REPO)}")
    return problems


def sync() -> list[str]:
    written: list[str] = []
    canonical_ref_plugins = {
        plugin for plugin in ADOPTERS if _has_escaping_ref(plugin) and not _canonical_ref_problems(plugin)
    }
    for plugin in ADOPTERS:
        if plugin in canonical_ref_plugins:
            for name in FILES:
                destination = REPO / "plugins" / plugin / "scripts" / name
                if destination.exists() or destination.is_symlink():
                    destination.unlink()
                    written.append(f"removed {destination.relative_to(REPO).as_posix()}")
            continue
        for name in FILES:
            source = CANONICAL_DIR / name
            destination = REPO / "plugins" / plugin / "scripts" / name
            if not source.is_file():
                raise FileNotFoundError(f"canonical source missing: {source}")
            content_matches = destination.is_file() and destination.read_bytes() == source.read_bytes()
            mode_matches = destination.is_file() and (
                os.name == "nt"
                or stat.S_IMODE(destination.stat().st_mode) == stat.S_IMODE(source.stat().st_mode)
            )
            if content_matches and mode_matches:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not content_matches:
                shutil.copyfile(source, destination)
            if os.name != "nt":
                shutil.copymode(source, destination)
            written.append(destination.relative_to(REPO).as_posix())
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify vendored copies without changing files",
    )
    arguments = parser.parse_args()
    if arguments.check:
        problems = verify()
        if problems:
            print("installer-engine vendoring is out of sync:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            print("\nRun: python tools/sync-installer-engine.py", file=sys.stderr)
            return 1
        print(f"installer-engine files in sync across {len(ADOPTERS)} adopter(s).")
        return 0

    written = sync()
    if written:
        print(f"Synced installer-engine files ({len(written)} file(s)):")
        for path in written:
            print(f"  + {path}")
    else:
        print("Installer-engine vendoring already in sync.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
