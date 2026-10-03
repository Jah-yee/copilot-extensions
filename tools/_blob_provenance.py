"""Content-addressed git blob provenance helpers.

Used by ``check-agent-bridge-contracts.py`` to validate a recorded
``source_git_blob`` directly against the file's own object-store content and
history, instead of (or in addition to) the originating ``commit``. A git
blob hash is a content address: its existence, and its appearance at a given
path's history, remain independently verifiable even when the commit that
first introduced it is no longer resolvable.

Standalone (no dependency on the caller's globals) so it can be unit-tested
and copied alongside the checker script without needing a package install.
"""
from __future__ import annotations

import ast
import hashlib
import subprocess
from pathlib import Path


def _run(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=False, env=env
    )


def blob_exists(repo: Path, env: dict[str, str], blob: str) -> bool:
    """Is this hash an actual git **blob** object (``cat-file -t``, not
    ``-e`` which accepts any type -- a commit/tree SHA must not satisfy a
    field meant to name a blob)?"""
    result = _run(repo, env, "cat-file", "-t", blob)
    return result.returncode == 0 and result.stdout.decode().strip() == "blob"


def blob_content(repo: Path, env: dict[str, str], blob: str) -> bytes | None:
    result = _run(repo, env, "cat-file", "-p", blob)
    return result.stdout if result.returncode == 0 else None


def _sha256_bytes(data: bytes) -> str:
    """Hash with CRLF/CR normalized to LF -- matches the registry's own
    documented fingerprint definition (contract text is line-ending
    agnostic), so a blob's hash agrees with a working-tree file's."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        canonical = data
    else:
        canonical = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def blob_sha256(repo: Path, env: dict[str, str], blob: str) -> str | None:
    content = blob_content(repo, env, blob)
    return _sha256_bytes(content) if content is not None else None


def blob_at_commit(repo: Path, env: dict[str, str], commit: str, path: str) -> str | None:
    """The exact blob ``path`` resolves to at ``commit`` -- the strict,
    preferred check whenever ``commit`` itself is available."""
    result = _run(repo, env, "rev-parse", "--verify", f"{commit}:{path}")
    value = result.stdout.decode().strip()
    return value if result.returncode == 0 and value else None


def verify_source_blob(
    repo: Path,
    env: dict[str, str],
    history: PathHistory,
    *,
    commit: str,
    commit_available: bool,
    path: str,
    blob: str,
    label: str,
) -> str | None:
    """Verify a recorded ``blob`` against ``path``, preferring the exact
    ``commit:path`` resolution whenever ``commit`` is available, falling
    back to plain history-reachability only when it isn't. Shared by both
    the provenance and fixture validation paths (identical contract).
    Returns an error string, or ``None`` when it validates cleanly."""
    if commit_available:
        actual = blob_at_commit(repo, env, commit, path)
        if actual is None:
            return f"{label}: cannot resolve {commit}:{path}"
        if actual != blob:
            return f"{label}: source_git_blob is {blob}, actual {actual}"
        return None
    if not history.reachable(blob, path):
        return (
            f"{label}: source_git_blob {blob} is not reachable as {path}'s "
            "content at any commit in retained history"
        )
    return None


class PathHistory:
    """One ``git log --raw`` walk of ``path``'s own history, memoized per
    path so N provenance/fixture entries sharing the same (common) path
    don't each re-walk it or spawn a ``rev-parse`` per historical commit --
    a single pass here replaces what would otherwise be one Git subprocess
    per historical commit per entry."""

    def __init__(self, repo: Path, env: dict[str, str]) -> None:
        self._repo = repo
        self._env = env
        self._blobs_by_path: dict[str, set[str] | None] = {}

    def _blobs_for(self, path: str) -> set[str] | None:
        if path not in self._blobs_by_path:
            result = _run(
                self._repo, self._env, "log", "--format=", "--raw", "--no-abbrev", "--", path
            )
            if result.returncode != 0:
                self._blobs_by_path[path] = None
            else:
                blobs = set()
                for line in result.stdout.decode().splitlines():
                    if line.startswith(":"):
                        # ":<old-mode> <new-mode> <old-blob> <new-blob> <status>"
                        parts = line.split()
                        if len(parts) >= 4:
                            blobs.add(parts[3])
                self._blobs_by_path[path] = blobs
        return self._blobs_by_path[path]

    def reachable(self, blob: str, path: str) -> bool:
        """Does ``blob`` appear as ``path``'s content at some commit
        reachable from HEAD -- not merely present as a loose/packed object,
        which git gc can prune once nothing reachable references it (a PR
        can record an intermediate pre-squash blob that validates while its
        branch still exists, then fail again once deleted/gc'd)."""
        blobs = self._blobs_for(path)
        return blobs is not None and blob in blobs


def integer_constant_in_blob(
    repo: Path, env: dict[str, str], blob: str, name: str
) -> int | None:
    """Read an integer constant directly from a recorded blob (content-
    addressed, same rationale as ``blob_exists``) rather than resolving it
    via ``commit:path``."""
    content = blob_content(repo, env, blob)
    if content is None:
        return None
    try:
        tree = ast.parse(content.decode("utf-8"), filename=f"blob:{blob}")
    except (UnicodeDecodeError, SyntaxError):
        return None
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, int)
        ):
            return node.value.value
    return None
