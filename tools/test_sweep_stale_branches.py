"""Regression tests for the stale-branch sweep's pure decision logic
(``plan_sweep``) and its `gh`-wrapping helpers' error handling. Mirrors
``tools/test_module_health_watchdog.py``'s convention: the actual branch
deletion / issue filing I/O is exercised only through monkeypatched
``subprocess.run``, never against a real repo.

Run:  python -m pytest tools/test_sweep_stale_branches.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "sweep_stale_branches.py"


def _load_sweep():
    spec = importlib.util.spec_from_file_location("sweep_stale_branches", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def sweep():
    return _load_sweep()


def test_plan_sweep_deletes_only_branches_whose_pr_is_merged(sweep):
    deletable, flagged = sweep.plan_sweep(
        remote_branches=["pr/already-merged", "pr/still-open", "main", "dev"],
        merged_branch_names={"pr/already-merged"},
        all_pr_branch_names={"pr/already-merged", "pr/still-open"},
        protected_branches={"main", "dev"},
    )

    assert deletable == ["pr/already-merged"]
    assert flagged == []


def test_plan_sweep_never_touches_protected_branches_even_if_merged(sweep):
    # A pathological case (a "merged PR" headRefName somehow equal to a
    # protected branch) must never result in that branch being deletable.
    deletable, _flagged = sweep.plan_sweep(
        remote_branches=["main", "dev"],
        merged_branch_names={"main", "dev"},
        all_pr_branch_names={"main", "dev"},
        protected_branches={"main", "dev"},
    )

    assert deletable == []


def test_plan_sweep_flags_disposable_named_branches_with_no_pr_record(sweep):
    deletable, flagged = sweep.plan_sweep(
        remote_branches=["worktree/orphaned-123", "feature/no-pr", "random-unrelated-branch"],
        merged_branch_names=set(),
        all_pr_branch_names=set(),
        protected_branches={"main", "dev"},
    )

    assert deletable == []
    # "random-unrelated-branch" matches no flag pattern -- left alone
    # entirely, neither deleted nor flagged.
    assert flagged == ["feature/no-pr", "worktree/orphaned-123"]


def test_plan_sweep_never_flags_a_disposable_named_branch_that_has_any_pr_record(sweep):
    # Even an OPEN (unmerged) PR's branch must not be flagged as "no PR
    # record" -- it has a record, it's just not merged yet.
    deletable, flagged = sweep.plan_sweep(
        remote_branches=["worktree/live-session"],
        merged_branch_names=set(),
        all_pr_branch_names={"worktree/live-session"},
        protected_branches={"main", "dev"},
    )

    assert deletable == []
    assert flagged == []


def test_plan_sweep_results_are_sorted(sweep):
    deletable, flagged = sweep.plan_sweep(
        remote_branches=["pr/zzz", "pr/aaa", "worktree/zzz", "worktree/aaa"],
        merged_branch_names={"pr/zzz", "pr/aaa"},
        all_pr_branch_names={"pr/zzz", "pr/aaa"},
        protected_branches=set(),
    )

    assert deletable == ["pr/aaa", "pr/zzz"]
    assert flagged == ["worktree/aaa", "worktree/zzz"]


def test_delete_branch_treats_already_gone_as_success(sweep, monkeypatch):
    class _AlreadyGone:
        returncode = 1
        stdout = ""
        stderr = "HTTP 422: Reference does not exist (https://docs.github.com/...)"

    monkeypatch.setattr(sweep.subprocess, "run", lambda *a, **k: _AlreadyGone())

    assert sweep._delete_branch("owner/repo", "pr/gone") is True


def test_delete_branch_reports_failure_on_a_real_error(sweep, monkeypatch, capsys):
    class _RealFailure:
        returncode = 1
        stdout = ""
        stderr = "HTTP 403: Resource not accessible by integration"

    monkeypatch.setattr(sweep.subprocess, "run", lambda *a, **k: _RealFailure())

    assert sweep._delete_branch("owner/repo", "pr/blocked") is False
    assert "failed to delete" in capsys.readouterr().err


def test_gh_json_raises_gh_call_failed_on_a_nonzero_exit(sweep, monkeypatch):
    class _Failed:
        returncode = 1
        stdout = ""
        stderr = "some gh error"

    monkeypatch.setattr(sweep.subprocess, "run", lambda *a, **k: _Failed())

    with pytest.raises(sweep.GhCallFailed):
        sweep._gh_json(["pr", "list"])


def test_main_dry_run_never_shells_out_to_delete_or_file(sweep, monkeypatch, capsys):
    monkeypatch.setattr(sweep, "_merged_branch_names", lambda repo, limit: {"pr/done"})
    monkeypatch.setattr(sweep, "_all_pr_branch_names", lambda repo, limit: {"pr/done"})
    monkeypatch.setattr(sweep, "_remote_branches", lambda repo: ["pr/done", "main"])

    def _boom(*_args, **_kwargs):
        raise AssertionError("dry run must never delete or file anything")

    monkeypatch.setattr(sweep, "_delete_branch", _boom)
    monkeypatch.setattr(sweep, "_file_or_update_tracking_issue", _boom)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])

    exit_code = sweep.main()

    assert exit_code == 0
    assert "dry run" in capsys.readouterr().out


def test_main_returns_nonzero_when_a_gh_call_needed_to_plan_fails(sweep, monkeypatch):
    def _fail(*_args, **_kwargs):
        raise sweep.GhCallFailed("simulated transient failure")

    monkeypatch.setattr(sweep, "_merged_branch_names", _fail)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])

    assert sweep.main() == 1


def test_main_execute_returns_nonzero_when_a_deletion_fails(sweep, monkeypatch):
    monkeypatch.setattr(sweep, "_merged_branch_names", lambda repo, limit: {"pr/done"})
    monkeypatch.setattr(sweep, "_all_pr_branch_names", lambda repo, limit: {"pr/done"})
    monkeypatch.setattr(sweep, "_remote_branches", lambda repo: ["pr/done"])
    monkeypatch.setattr(sweep, "_delete_branch", lambda repo, branch: False)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--execute"])

    assert sweep.main() == 1


def test_main_execute_files_tracking_issue_only_when_something_is_flagged(sweep, monkeypatch):
    monkeypatch.setattr(sweep, "_merged_branch_names", lambda repo, limit: set())
    monkeypatch.setattr(sweep, "_all_pr_branch_names", lambda repo, limit: set())
    monkeypatch.setattr(sweep, "_remote_branches", lambda repo: ["main"])

    def _boom(*_args, **_kwargs):
        raise AssertionError("must not file a tracking issue when nothing is flagged")

    monkeypatch.setattr(sweep, "_file_or_update_tracking_issue", _boom)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--execute"])

    assert sweep.main() == 0
