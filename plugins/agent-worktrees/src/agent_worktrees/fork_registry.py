"""Fork registry -- durable, machine-local record of confirmed fork-based PR
publish targets.

Manages ``~/.agent-worktrees/forks.yaml``. ``create_pr``'s ``pr.fork``
confirmation gate (see :mod:`.pr_ops`) refuses to fork/push anywhere on a
caller's first call per repo+login for a repo whose resolved PR flow
publishes through a personal fork -- it returns
``needs_confirmation: "fork_setup"`` so a human can approve it. A GitHub fork
is a durable, account-scoped resource, not a per-worktree one, so that
approval should not have to be re-cleared for every worktree of the same
repo once a human has genuinely granted it.

This module is the durable side of that approval. Once a repo's fork has been
confirmed -- either by a live ``create_pr --confirm-fork`` call, or ahead of
time via ``agent-worktrees forks set <repo> --owner <login>`` during machine/
harness setup -- :func:`is_confirmed` lets :mod:`.pr_ops` skip the
confirmation gate for every future call for that repo, on this machine, until
the entry is removed.

A confirmation is scoped to the **effective login** that will actually
authenticate the repo's operations (:func:`.pr_ops._resolve_fork_credential`
-- the gh account that genuinely signs the git/API calls, not merely the
configured ``account_map`` entry, which can silently fall back to a
*different* ambient identity when no token can be minted for the mapped
account). Scoping to the bare mapping would let a confirmation keep applying
after the account mapping changes, or after ambient `gh` auth switches to a
different user, silently authorizing a fork/push under an identity the human
never actually approved.

Mirrors :mod:`.accounts`'s catalog shape and read/write style.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import output


def _quote(value: str) -> str:
    """Render *value* as an always-safe YAML double-quoted scalar.

    Unlike a conditional quoter that only wraps values containing a handful
    of punctuation characters, this always double-quotes and escapes, so a
    value is never misparsed as a YAML bool/null/number (``"yes"``,
    ``"null"``, ``"123"``), and an embedded newline/tab -- free-form via
    ``--notes`` -- can never corrupt the hand-written line-based file by
    splitting across lines unescaped.
    """
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'

_LOCK_ACQUIRE_TIMEOUT_S = 10.0
_LOCK_RETRY_INTERVAL_S = 0.1

if sys.platform == "win32":
    import msvcrt

    def _lock_file(fh) -> None:
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(b"\0")
            fh.flush()
        deadline = time.time() + _LOCK_ACQUIRE_TIMEOUT_S
        while True:
            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.time() >= deadline:
                    raise
                time.sleep(_LOCK_RETRY_INTERVAL_S)

    def _unlock_file(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

    def _unlock_file(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def _locked_registry_file():
    """Interprocess lock guarding read-modify-write access to forks.yaml.

    Without this, two concurrent ``create_pr`` calls for different repos
    (e.g. two parallel worktrees) can both load the registry, each add their
    own repo, and the second writer's save silently drops the first writer's
    just-added entry (read-modify-write race on a shared file). Same
    cross-platform primitive as ``git_ops._credential_pin_lock``/
    ``mux_link``'s lock helpers.
    """
    path = _forks_yaml_path()
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as fh:
        _lock_file(fh)
        try:
            yield
        finally:
            _unlock_file(fh)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ForkEntry:
    """A single confirmed fork-publish target in the catalog."""

    repo: str
    owner: str
    remote: str = "fork"
    account: str = ""
    confirmed_at: str = ""
    notes: str = ""


@dataclass
class ForkRegistry:
    """The full forks.yaml content, keyed by (normalized repo, account)."""

    forks: dict[tuple[str, str], ForkEntry] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def _forks_yaml_path() -> Path:
    """Path to the fork registry file."""
    return (
        Path.home()
        / ".agent-worktrees"  # marketplace-isolation: allow legacy-compatibility
        / "forks.yaml"
    )


def _normalize_repo(repo_slug: str) -> str:
    """Case-fold a repo slug (``Owner/Name``) for lookup/storage keys.

    GitHub owner/repo names are case-insensitive; normalize so
    ``Octo-Org/widgets`` and ``octo-org/Widgets`` resolve to the same catalog
    entry.
    """
    return repo_slug.strip().casefold()


def _registry_key(repo_slug: str, account: str) -> tuple[str, str]:
    """The composite (repo, account) key entries are stored/looked up under.

    A repo can legitimately have more than one confirmed account over time
    (an operator switching which identity publishes its PRs) -- keying by
    repo alone would let confirming account B silently overwrite account A's
    still-valid approval, re-prompting on a later switch back to A even
    though both were genuinely confirmed. Each (repo, account) pair gets its
    own durable entry instead.
    """
    return _normalize_repo(repo_slug), (account or "").casefold()


def read_registry() -> ForkRegistry:
    """Load forks.yaml, returning an empty registry if missing/invalid."""
    path = _forks_yaml_path()
    if not path.exists():
        return ForkRegistry()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return ForkRegistry()
        raw = data.get("forks", {})
        forks: dict[tuple[str, str], ForkEntry] = {}
        if isinstance(raw, dict):
            for repo, accounts in raw.items():
                if not isinstance(accounts, dict):
                    continue
                for account, entry in accounts.items():
                    if not isinstance(entry, dict):
                        continue
                    owner = str(entry.get("owner", "") or "")
                    if not owner:
                        continue
                    account_str = str(account)
                    forks[_registry_key(str(repo), account_str)] = ForkEntry(
                        repo=str(repo),
                        owner=owner,
                        remote=str(entry.get("remote", "fork") or "fork"),
                        account=account_str,
                        confirmed_at=str(entry.get("confirmed_at", "") or ""),
                        notes=str(entry.get("notes", "") or ""),
                    )
        return ForkRegistry(forks=forks)
    except Exception:
        return ForkRegistry()


def write_registry(registry: ForkRegistry) -> None:
    """Write forks.yaml with hand-formatted YAML (parallels accounts.py).

    Nested ``forks: { <repo>: { <account>: {...} } }`` so a repo confirmed
    under multiple accounts over time keeps one entry per account rather
    than the last write clobbering the others.
    """
    path = _forks_yaml_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# ~/.agent-worktrees/forks.yaml",  # marketplace-isolation: allow legacy
        "# Durable catalog of confirmed fork-based PR publish targets, keyed by",
        "# repo AND resolved account. Once a (repo, account) pair is listed",
        "# here, create-pr's pr.fork confirmation gate is skipped for every",
        "# future call under that SAME resolved account -- see fork_registry.py",
        "# and the 'forks' command's --help.",
        "",
    ]
    by_repo: dict[str, dict[str, ForkEntry]] = {}
    for (repo_key, account_key), e in registry.forks.items():
        by_repo.setdefault(repo_key, {})[account_key] = e
    if by_repo:
        lines.append("forks:")
        for repo_key in sorted(by_repo.keys()):
            accounts = by_repo[repo_key]
            any_entry = next(iter(accounts.values()))
            lines.append(f"  {_quote(any_entry.repo)}:")
            for account_key in sorted(accounts.keys()):
                e = accounts[account_key]
                lines.append(f"    {_quote(e.account)}:")
                lines.append(f"      owner: {_quote(e.owner)}")
                if e.remote and e.remote != "fork":
                    lines.append(f"      remote: {_quote(e.remote)}")
                if e.confirmed_at:
                    lines.append(f"      confirmed_at: {_quote(e.confirmed_at)}")
                if e.notes:
                    lines.append(f"      notes: {_quote(e.notes)}")
    # Write to a same-directory temp file and atomically replace the target
    # (os.replace) so a lock-FREE reader (is_confirmed/find_fork/list_forks)
    # always observes either the complete old file or complete new one --
    # never a truncated/partial one from a write still in progress.
    content = "\n".join(lines) + "\n"
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def list_forks() -> list[ForkEntry]:
    """Return all catalogued fork entries, sorted by repo slug then account."""
    registry = read_registry()
    return sorted(
        registry.forks.values(), key=lambda e: (e.repo.casefold(), e.account.casefold()),
    )


def find_forks_for_repo(repo_slug: str) -> list[ForkEntry]:
    """All confirmed entries for ``repo_slug`` (one per distinct account)."""
    if not repo_slug:
        return []
    registry = read_registry()
    norm = _normalize_repo(repo_slug)
    return sorted(
        (e for (r, _a), e in registry.forks.items() if r == norm),
        key=lambda e: e.account.casefold(),
    )


def find_fork(repo_slug: str, account: str = "") -> ForkEntry | None:
    """The catalog entry for ``repo_slug``+``account``, or None if unconfirmed."""
    if not repo_slug:
        return None
    registry = read_registry()
    return registry.forks.get(_registry_key(repo_slug, account))


def is_confirmed(repo_slug: str, *, account: str = "") -> bool:
    """Whether ``repo_slug``'s fork-publish target is confirmed for ``account``.

    ``account`` should be the repo's currently-resolved effective identity
    (e.g. ``pr_ops._resolve_fork_credential``'s scope, or ``""`` when truly
    unresolvable -- callers must treat an empty account as unconfirmable, not
    look it up here). A confirmation recorded under a different account no
    longer counts -- the account mapping changing since confirmation is
    exactly the case this scoping exists to catch.
    """
    return find_fork(repo_slug, account) is not None


def record_confirmation(
    repo_slug: str,
    owner: str,
    *,
    remote: str = "fork",
    account: str = "",
    notes: str | None = None,
) -> ForkEntry:
    """Record (or refresh) a confirmed fork for ``repo_slug``+``account``.

    Idempotent: calling this again for the same repo+account just refreshes
    ``confirmed_at`` (and ``owner``/``remote`` if they changed) rather than
    duplicating an entry. A *different* account gets its OWN entry alongside
    any existing one for the same repo -- switching which identity publishes
    a repo's PRs, then switching back, keeps both confirmations durable
    rather than the second overwriting the first. Called automatically by
    :mod:`.pr_ops` after a successful fork-and-remote setup, and directly by
    ``forks set`` for pre-seeding during machine/harness setup.

    Locked (see :func:`_locked_registry_file`): safe against a concurrent
    ``create_pr``/``forks set`` call for a different repo clobbering this
    write via an unsynchronized read-modify-write.
    """
    with _locked_registry_file():
        registry = read_registry()
        key = _registry_key(repo_slug, account)
        existing = registry.forks.get(key)
        entry = ForkEntry(
            repo=repo_slug,
            owner=owner,
            remote=remote,
            account=account or "",
            confirmed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            notes=notes if notes is not None else (existing.notes if existing else ""),
        )
        registry.forks[key] = entry
        write_registry(registry)
        return entry


def remove_fork(repo_slug: str, account: str | None = None) -> bool:
    """Remove confirmed-fork entry/entries for ``repo_slug``.

    With ``account`` omitted, removes EVERY account's entry for this repo
    (the common case: forgetting a repo entirely). Pass ``account`` to
    forget only that one identity's confirmation. Returns True if anything
    was removed.
    """
    with _locked_registry_file():
        registry = read_registry()
        if account is not None:
            keys = [_registry_key(repo_slug, account)]
        else:
            norm = _normalize_repo(repo_slug)
            keys = [k for k in registry.forks if k[0] == norm]
        removed = False
        for key in keys:
            if key in registry.forks:
                del registry.forks[key]
                removed = True
        if removed:
            write_registry(registry)
            output.ok(f"Fork confirmation for '{repo_slug}' removed from forks.yaml")
        return removed

