"""Tests for the durable fork-confirmation registry (forks.yaml)."""

from __future__ import annotations

from pathlib import Path

from agent_worktrees import fork_registry


def test_empty_registry_when_missing():
    assert fork_registry.list_forks() == []
    assert fork_registry.find_fork("octo-org/widgets") is None
    assert fork_registry.is_confirmed("octo-org/widgets") is False


def test_write_registry_is_atomic_no_temp_file_left_behind():
    """write_registry must leave the registry file whole (never a partial
    write visible to a lock-free reader) and never leak its temp file."""
    fork_registry.record_confirmation("owner/repo", "alice")
    path = fork_registry._forks_yaml_path()
    siblings = list(path.parent.iterdir())
    assert path in siblings
    assert not any(p.name.startswith(f"{path.name}.tmp-") for p in siblings)
    # A second write (e.g. a confirmation refresh) must also land atomically.
    fork_registry.record_confirmation("owner/repo2", "bob")
    names = {p.name for p in path.parent.iterdir()}
    assert names == {path.name, f"{path.name}.lock"}


def test_quote_round_trips_yaml_reserved_scalars_and_multiline_notes():
    """A value that LOOKS like a YAML bool/null/number, or a multiline note,
    must never corrupt the hand-written file or change type on read-back --
    the exact two classes of value a conditional quoter can mis-handle."""
    fork_registry.record_confirmation(
        "owner/repo", "yes", account="null", notes="line one\nline two\ttabbed",
    )
    e = fork_registry.find_fork("owner/repo", "null")
    assert e.owner == "yes"
    assert e.account == "null"
    assert e.notes == "line one\nline two\ttabbed"
    # The file itself must still be exactly one 'forks:' mapping entry -- a
    # raw (unescaped) embedded newline would have split it across more lines.
    raw = fork_registry._forks_yaml_path().read_text(encoding="utf-8")
    assert raw.count("owner/repo") == 1


def test_record_and_read_round_trip():
    fork_registry.record_confirmation(
        "octo-org/widgets", "octocat", remote="fork",
    )
    e = fork_registry.find_fork("octo-org/widgets")
    assert e is not None
    assert e.owner == "octocat"
    assert e.remote == "fork"
    assert e.confirmed_at  # non-empty timestamp was stamped
    assert fork_registry.is_confirmed("octo-org/widgets") is True


def test_find_fork_case_insensitive():
    fork_registry.record_confirmation("Octo-Org/Widgets", "octocat")
    assert fork_registry.is_confirmed("octo-org/widgets") is True
    assert fork_registry.find_fork("OCTO-ORG/widgets") is not None


def test_record_confirmation_is_idempotent_and_refreshes():
    first = fork_registry.record_confirmation("owner/repo", "alice")
    second = fork_registry.record_confirmation("owner/repo", "alice")
    assert fork_registry.list_forks() == [second]
    # Owner can change on a later confirmation (e.g. re-pointed at a
    # differently-owned fork) without leaving a stale duplicate entry.
    third = fork_registry.record_confirmation("owner/repo", "bob")
    assert third.owner == "bob"
    assert len(fork_registry.list_forks()) == 1
    assert first  # silence unused-var lint; documents the first call's shape


def test_record_confirmation_preserves_notes_unless_overridden():
    fork_registry.record_confirmation("owner/repo", "alice", notes="pre-seeded")
    refreshed = fork_registry.record_confirmation("owner/repo", "alice")
    assert refreshed.notes == "pre-seeded"
    overridden = fork_registry.record_confirmation(
        "owner/repo", "alice", notes="updated",
    )
    assert overridden.notes == "updated"


def test_is_confirmed_scoped_to_account():
    """A confirmation recorded under one account must not silently cover a
    later call resolved to a DIFFERENT account -- the repo's account mapping
    changing since confirmation is exactly the case this scoping exists to
    catch (a stale repo-only confirmation must not authorize forking/pushing
    under a newly-mapped identity without renewed consent)."""
    fork_registry.record_confirmation("owner/repo", "alice", account="account-a")
    assert fork_registry.is_confirmed("owner/repo", account="account-a") is True
    assert fork_registry.is_confirmed("owner/repo", account="account-b") is False
    assert fork_registry.is_confirmed("owner/repo") is False  # default account=""


def test_record_confirmation_changed_account_adds_separate_entry():
    """Confirming under a DIFFERENT account must NOT overwrite an existing
    confirmation for another account on the same repo -- an operator
    switching which identity publishes a repo's PRs, then switching back,
    must not have to re-confirm the one that was never actually revoked."""
    fork_registry.record_confirmation("owner/repo", "alice", account="account-a")
    fork_registry.record_confirmation("owner/repo", "bob", account="account-b")
    assert fork_registry.is_confirmed("owner/repo", account="account-a") is True
    assert fork_registry.is_confirmed("owner/repo", account="account-b") is True
    a = fork_registry.find_fork("owner/repo", "account-a")
    b = fork_registry.find_fork("owner/repo", "account-b")
    assert a.owner == "alice"
    assert b.owner == "bob"
    assert {e.account for e in fork_registry.find_forks_for_repo("owner/repo")} == {
        "account-a", "account-b",
    }


def test_record_confirmation_is_locked(monkeypatch, tmp_path: Path):
    """record_confirmation/remove_fork serialize via an interprocess lock
    file next to the registry, not just an in-process read-modify-write."""
    acquired = []

    real_locked = fork_registry._locked_registry_file

    from contextlib import contextmanager

    @contextmanager
    def _tracking_locked():
        acquired.append(True)
        with real_locked():
            yield

    monkeypatch.setattr(fork_registry, "_locked_registry_file", _tracking_locked)
    fork_registry.record_confirmation("owner/repo", "alice")
    fork_registry.remove_fork("owner/repo")
    assert acquired == [True, True]
    assert fork_registry._forks_yaml_path().with_suffix(".yaml.lock").exists()


def _mp_worker_record(repo: str, owner: str, home: str) -> None:
    """Module-level (picklable) worker: record one confirmation.

    Re-targets ``Path.home`` for *this* (spawned) process explicitly, since a
    multiprocessing worker does not inherit the parent test process's
    monkeypatches/env mutations -- only a fresh interpreter importing this
    module from scratch.
    """
    from pathlib import Path as _Path

    from agent_worktrees import fork_registry as _fr

    _Path.home = classmethod(lambda cls: _Path(home))
    _fr.record_confirmation(repo, owner)


def test_record_confirmation_locking_survives_concurrent_processes(tmp_path: Path):
    """A real cross-process race: N processes concurrently confirming
    DIFFERENT repos against the SAME registry file must not lose any
    entry to an unsynchronized read-modify-write -- the actual failure
    mode the in-process-only tracking test above cannot exercise."""
    import multiprocessing

    home = tmp_path / "mp-home"
    home.mkdir()
    repos = [(f"owner/repo-{i}", f"user-{i}") for i in range(8)]

    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(target=_mp_worker_record, args=(repo, owner, str(home)))
        for repo, owner in repos
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0

    import agent_worktrees.fork_registry as fork_registry_mod
    original_home = fork_registry_mod.Path.home
    fork_registry_mod.Path.home = classmethod(lambda cls: home)
    try:
        entries = {e.repo: e.owner for e in fork_registry_mod.list_forks()}
    finally:
        fork_registry_mod.Path.home = original_home

    assert entries == {repo: owner for repo, owner in repos}


def test_remove_fork():
    fork_registry.record_confirmation("owner/repo", "alice")
    assert fork_registry.remove_fork("owner/repo") is True
    assert fork_registry.is_confirmed("owner/repo") is False
    assert fork_registry.remove_fork("owner/repo") is False


def test_list_forks_sorted_by_repo():
    fork_registry.record_confirmation("zeta/repo", "alice")
    fork_registry.record_confirmation("alpha/repo", "bob")
    entries = fork_registry.list_forks()
    assert [e.repo for e in entries] == ["alpha/repo", "zeta/repo"]


def test_malformed_registry_file_is_ignored(monkeypatch, tmp_path: Path):
    path = tmp_path / ".agent-worktrees" / "forks.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not: [valid, {structure", encoding="utf-8")
    monkeypatch.setattr(fork_registry, "_forks_yaml_path", lambda: path)
    assert fork_registry.list_forks() == []


def test_entry_missing_owner_is_skipped(monkeypatch, tmp_path: Path):
    path = tmp_path / ".agent-worktrees" / "forks.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "forks:\n  owner/repo:\n    '':\n      remote: fork\n", encoding="utf-8",
    )
    monkeypatch.setattr(fork_registry, "_forks_yaml_path", lambda: path)
    assert fork_registry.list_forks() == []
