"""Regression tests for the stale-branch sweep's pure decision logic
(``plan_sweep``) and its `gh`/`git`-wrapping helpers' error handling.
Mirrors ``tools/test_module_health_watchdog.py``'s convention: the actual
branch deletion / issue filing I/O is exercised only through monkeypatched
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
    # Must be registered in sys.modules BEFORE exec_module: the module
    # defines a `@dataclass` whose field-type resolution (with `from
    # __future__ import annotations` in effect) looks up its own module via
    # `sys.modules[cls.__module__]` -- without this, that lookup returns
    # None and the dataclass decorator itself raises at import time.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def sweep():
    return _load_sweep()


def _pr(sweep, branch, head_oid, *, same_repo=True, state="MERGED"):
    return sweep.PullRequestHead(branch=branch, head_oid=head_oid, same_repo=same_repo, state=state)


# --- plan_sweep (pure decision logic) ---------------------------------


def test_plan_sweep_deletes_only_branches_whose_pr_is_merged(sweep):
    deletable, flagged = sweep.plan_sweep(
        remote_branches={"pr/already-merged": "sha1", "pr/still-open": "sha2", "main": "sha3", "dev": "sha4"},
        merged_heads={"pr/already-merged": "sha1"},
        open_branch_names={"pr/still-open"},
        all_pr_branch_names={"pr/already-merged", "pr/still-open"},
        protected_branches={"main", "dev"},
    )

    assert deletable == ["pr/already-merged"]
    assert flagged == []


def test_plan_sweep_never_touches_protected_branches_even_if_merged(sweep):
    # A pathological case (a "merged PR" headRefName somehow equal to a
    # protected branch, with a matching OID) must never result in that
    # branch being deletable.
    deletable, _flagged = sweep.plan_sweep(
        remote_branches={"main": "sha1", "dev": "sha2"},
        merged_heads={"main": "sha1", "dev": "sha2"},
        open_branch_names=set(),
        all_pr_branch_names={"main", "dev"},
        protected_branches={"main", "dev"},
    )

    assert deletable == []


def test_plan_sweep_skips_a_branch_whose_current_oid_no_longer_matches_the_merged_pr(sweep):
    # The identity check: a branch reused since the PR merged (force-pushed,
    # or picked up fresh by something else) must never be deleted just
    # because its NAME once belonged to a merged PR.
    deletable, _flagged = sweep.plan_sweep(
        remote_branches={"pr/reused": "new-sha"},
        merged_heads={"pr/reused": "old-sha"},
        open_branch_names=set(),
        all_pr_branch_names={"pr/reused"},
        protected_branches=set(),
    )

    assert deletable == []


def test_plan_sweep_vetoes_a_branch_with_a_currently_open_pr_even_if_oid_matches(sweep):
    # Regression: a branch name can be deleted and recreated with a brand
    # new OPEN PR that coincidentally shares the old merged PR's head OID
    # (e.g. reopened from the same commit). The open-PR veto must win even
    # though the OID identity check alone would have said "deletable".
    deletable, _flagged = sweep.plan_sweep(
        remote_branches={"pr/reopened": "sha1"},
        merged_heads={"pr/reopened": "sha1"},
        open_branch_names={"pr/reopened"},
        all_pr_branch_names={"pr/reopened"},
        protected_branches=set(),
    )

    assert deletable == []


def test_plan_sweep_flags_disposable_named_branches_with_no_pr_record(sweep):
    deletable, flagged = sweep.plan_sweep(
        remote_branches={"worktree/orphaned-123": "sha1", "feature/no-pr": "sha2", "random-unrelated-branch": "sha3"},
        merged_heads={},
        open_branch_names=set(),
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
        remote_branches={"worktree/live-session": "sha1"},
        merged_heads={},
        open_branch_names={"worktree/live-session"},
        all_pr_branch_names={"worktree/live-session"},
        protected_branches={"main", "dev"},
    )

    assert deletable == []
    assert flagged == []


def test_plan_sweep_results_are_sorted(sweep):
    deletable, flagged = sweep.plan_sweep(
        remote_branches={"pr/zzz": "s1", "pr/aaa": "s2", "worktree/zzz": "s3", "worktree/aaa": "s4"},
        merged_heads={"pr/zzz": "s1", "pr/aaa": "s2"},
        open_branch_names=set(),
        all_pr_branch_names={"pr/zzz", "pr/aaa"},
        protected_branches=set(),
    )

    assert deletable == ["pr/aaa", "pr/zzz"]
    assert flagged == ["worktree/aaa", "worktree/zzz"]


# --- _pull_requests ----------------------------------------------------


def test_pull_requests_excludes_cross_repository_prs_from_merged_heads(sweep, monkeypatch):
    rows = [
        {"headRefName": "pr/same-repo", "headRefOid": "sha1", "isCrossRepository": False, "state": "MERGED"},
        {"headRefName": "pr/fork", "headRefOid": "sha2", "isCrossRepository": True, "state": "MERGED"},
    ]
    monkeypatch.setattr(sweep, "_gh_json", lambda args: rows)

    prs = sweep._pull_requests("owner/repo", limit=100)

    assert [p.branch for p in prs if p.same_repo] == ["pr/same-repo"]
    assert [p.branch for p in prs if not p.same_repo] == ["pr/fork"]


def test_pull_requests_preserves_state_for_every_pr(sweep, monkeypatch):
    rows = [
        {"headRefName": "pr/open", "headRefOid": "sha1", "isCrossRepository": False, "state": "OPEN"},
        {"headRefName": "pr/closed", "headRefOid": "sha2", "isCrossRepository": False, "state": "CLOSED"},
        {"headRefName": "pr/merged", "headRefOid": "sha3", "isCrossRepository": False, "state": "MERGED"},
    ]
    monkeypatch.setattr(sweep, "_gh_json", lambda args: rows)

    prs = sweep._pull_requests("owner/repo", limit=100)

    assert {p.branch: p.state for p in prs} == {
        "pr/open": "OPEN",
        "pr/closed": "CLOSED",
        "pr/merged": "MERGED",
    }


def test_pull_requests_raises_truncated_result_when_row_count_hits_the_limit(sweep, monkeypatch):
    rows = [{"headRefName": f"pr/{i}", "headRefOid": "sha", "isCrossRepository": False, "state": "MERGED"} for i in range(3)]
    monkeypatch.setattr(sweep, "_gh_json", lambda args: rows)

    with pytest.raises(sweep.TruncatedResult):
        sweep._pull_requests("owner/repo", limit=3)


# --- _remote_branches ----------------------------------------------------


def test_remote_branches_raises_truncated_result_when_row_count_hits_the_limit(sweep, monkeypatch):
    class _Result:
        returncode = 0
        stdout = "branch1\tsha1\nbranch2\tsha2\nbranch3\tsha3\n"
        stderr = ""

    monkeypatch.setattr(sweep.subprocess, "run", lambda *a, **k: _Result())

    with pytest.raises(sweep.TruncatedResult):
        sweep._remote_branches("owner/repo", limit=3)


def test_remote_branches_parses_name_and_sha_pairs(sweep, monkeypatch):
    class _Result:
        returncode = 0
        stdout = "branch1\tsha1\nbranch2\tsha2\n"
        stderr = ""

    monkeypatch.setattr(sweep.subprocess, "run", lambda *a, **k: _Result())

    branches = sweep._remote_branches("owner/repo", limit=100)

    assert branches == {"branch1": "sha1", "branch2": "sha2"}


def test_remote_branches_uses_explicit_get_method(sweep, monkeypatch):
    # Regression for a real bug: `-F` flags make `gh api` default to POST
    # unless `--method GET` is passed explicitly, which would break this
    # GET-only endpoint outright.
    captured_args = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(args, **_kwargs):
        captured_args.append(args)
        return _Result()

    monkeypatch.setattr(sweep.subprocess, "run", _fake_run)

    sweep._remote_branches("owner/repo", limit=100)

    assert "--method" in captured_args[0]
    assert captured_args[0][captured_args[0].index("--method") + 1] == "GET"


# --- _verify_remote_matches_repo ----------------------------------------


def test_verify_remote_matches_repo_accepts_a_matching_https_url(sweep, monkeypatch):
    class _Result:
        returncode = 0
        stdout = "https://github.com/owner/repo.git\n"
        stderr = ""

    monkeypatch.setattr(sweep.subprocess, "run", lambda *a, **k: _Result())

    sweep._verify_remote_matches_repo("origin", "owner/repo")  # must not raise


def test_verify_remote_matches_repo_accepts_a_matching_ssh_url(sweep, monkeypatch):
    class _Result:
        returncode = 0
        stdout = "git@github.com:owner/repo.git\n"
        stderr = ""

    monkeypatch.setattr(sweep.subprocess, "run", lambda *a, **k: _Result())

    sweep._verify_remote_matches_repo("origin", "owner/repo")  # must not raise


def test_verify_remote_matches_repo_rejects_a_mismatched_remote(sweep, monkeypatch):
    class _Result:
        returncode = 0
        stdout = "https://github.com/owner/other-repo.git\n"
        stderr = ""

    monkeypatch.setattr(sweep.subprocess, "run", lambda *a, **k: _Result())

    with pytest.raises(sweep.GhCallFailed):
        sweep._verify_remote_matches_repo("origin", "owner/repo")


# --- _delete_branch (atomic compare-and-delete lease) -------------------


def test_delete_branch_uses_force_with_lease_with_the_expected_oid(sweep, monkeypatch):
    captured_args = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(args, **_kwargs):
        captured_args.append(args)
        return _Result()

    monkeypatch.setattr(sweep.subprocess, "run", _fake_run)

    assert sweep._delete_branch("origin", "pr/done", "expected-sha") is True
    assert any("--force-with-lease=refs/heads/pr/done:expected-sha" in arg for arg in captured_args[0])
    assert ":refs/heads/pr/done" in captured_args[0]


def test_delete_branch_treats_a_rejected_lease_as_a_safe_skip(sweep, monkeypatch):
    class _Rejected:
        returncode = 1
        stdout = ""
        stderr = "! [rejected]          refs/heads/pr/moved (stale info)"

    monkeypatch.setattr(sweep.subprocess, "run", lambda *a, **k: _Rejected())

    assert sweep._delete_branch("origin", "pr/moved", "old-sha") is True


def test_delete_branch_treats_already_gone_as_success(sweep, monkeypatch):
    class _AlreadyGone:
        returncode = 1
        stdout = ""
        stderr = "error: unable to delete 'pr/gone': remote ref does not exist"

    monkeypatch.setattr(sweep.subprocess, "run", lambda *a, **k: _AlreadyGone())

    assert sweep._delete_branch("origin", "pr/gone", "expected-sha") is True


def test_delete_branch_reports_failure_on_a_real_error(sweep, monkeypatch, capsys):
    class _RealFailure:
        returncode = 1
        stdout = ""
        stderr = "error: failed to push some refs (permission denied)"

    monkeypatch.setattr(sweep.subprocess, "run", lambda *a, **k: _RealFailure())

    assert sweep._delete_branch("origin", "pr/blocked", "expected-sha") is False
    assert "failed to delete" in capsys.readouterr().err


# --- _gh_json ------------------------------------------------------------


def test_gh_json_raises_gh_call_failed_on_a_nonzero_exit(sweep, monkeypatch):
    class _Failed:
        returncode = 1
        stdout = ""
        stderr = "some gh error"

    monkeypatch.setattr(sweep.subprocess, "run", lambda *a, **k: _Failed())

    with pytest.raises(sweep.GhCallFailed):
        sweep._gh_json(["pr", "list"])


# --- main ----------------------------------------------------------------


def test_main_dry_run_never_shells_out_to_delete_or_file(sweep, monkeypatch, capsys):
    monkeypatch.setattr(
        sweep, "_pull_requests",
        lambda repo, limit: [_pr(sweep, "pr/done", "sha1")],
    )
    monkeypatch.setattr(sweep, "_remote_branches", lambda repo, limit: {"pr/done": "sha1", "main": "sha2"})
    monkeypatch.setattr(sweep, "_default_branch", lambda repo: "main")

    def _boom(*_args, **_kwargs):
        raise AssertionError("dry run must never delete, verify a remote, or file anything")

    monkeypatch.setattr(sweep, "_delete_branch", _boom)
    monkeypatch.setattr(sweep, "_verify_remote_matches_repo", _boom)
    monkeypatch.setattr(sweep, "_file_or_update_tracking_issue", _boom)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])

    exit_code = sweep.main()

    assert exit_code == 0
    assert "dry run" in capsys.readouterr().out


def test_main_never_deletes_a_branch_whose_pr_is_merely_open(sweep, monkeypatch):
    # Regression for the real bug this fixed: a non-MERGED PR (OPEN here)
    # must never end up in merged_heads, even if its branch's current OID
    # happens to equal its own headRefOid (the ordinary, expected case for
    # any open PR -- not a coincidence to engineer around, the normal state).
    monkeypatch.setattr(
        sweep, "_pull_requests",
        lambda repo, limit: [_pr(sweep, "pr/open", "sha1", state="OPEN")],
    )
    monkeypatch.setattr(sweep, "_remote_branches", lambda repo, limit: {"pr/open": "sha1"})
    monkeypatch.setattr(sweep, "_default_branch", lambda repo: "main")
    monkeypatch.setattr(sweep, "_verify_remote_matches_repo", lambda remote, repo: None)

    def _boom(*_args, **_kwargs):
        raise AssertionError("must never delete a branch backing a still-open PR")

    monkeypatch.setattr(sweep, "_delete_branch", _boom)
    monkeypatch.setattr(sweep, "_file_or_update_tracking_issue", lambda repo, flagged: True)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--execute"])

    assert sweep.main() == 0


def test_main_returns_nonzero_when_a_gh_call_needed_to_plan_fails(sweep, monkeypatch):
    def _fail(*_args, **_kwargs):
        raise sweep.GhCallFailed("simulated transient failure")

    monkeypatch.setattr(sweep, "_pull_requests", _fail)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])

    assert sweep.main() == 1


def test_main_returns_nonzero_when_a_query_is_truncated(sweep, monkeypatch):
    def _fail(*_args, **_kwargs):
        raise sweep.TruncatedResult("simulated truncation")

    monkeypatch.setattr(sweep, "_pull_requests", _fail)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])

    assert sweep.main() == 1


def test_main_returns_nonzero_when_the_remote_does_not_match_repo_on_execute(sweep, monkeypatch):
    monkeypatch.setattr(sweep, "_pull_requests", lambda repo, limit: [])
    monkeypatch.setattr(sweep, "_remote_branches", lambda repo, limit: {"main": "sha1"})
    monkeypatch.setattr(sweep, "_default_branch", lambda repo: "main")

    def _fail(remote, repo):
        raise sweep.GhCallFailed("remote mismatch")

    monkeypatch.setattr(sweep, "_verify_remote_matches_repo", _fail)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--execute"])

    assert sweep.main() == 1


def test_main_execute_returns_nonzero_when_a_deletion_fails(sweep, monkeypatch):
    monkeypatch.setattr(
        sweep, "_pull_requests",
        lambda repo, limit: [_pr(sweep, "pr/done", "sha1")],
    )
    monkeypatch.setattr(sweep, "_remote_branches", lambda repo, limit: {"pr/done": "sha1"})
    monkeypatch.setattr(sweep, "_default_branch", lambda repo: "main")
    monkeypatch.setattr(sweep, "_verify_remote_matches_repo", lambda remote, repo: None)
    monkeypatch.setattr(sweep, "_delete_branch", lambda remote, branch, oid: False)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--execute"])

    assert sweep.main() == 1


def test_main_execute_files_tracking_issue_only_when_something_is_flagged(sweep, monkeypatch):
    monkeypatch.setattr(sweep, "_pull_requests", lambda repo, limit: [])
    monkeypatch.setattr(sweep, "_remote_branches", lambda repo, limit: {"main": "sha1"})
    monkeypatch.setattr(sweep, "_default_branch", lambda repo: "main")
    monkeypatch.setattr(sweep, "_verify_remote_matches_repo", lambda remote, repo: None)

    def _boom(*_args, **_kwargs):
        raise AssertionError("must not file a tracking issue when nothing is flagged")

    monkeypatch.setattr(sweep, "_file_or_update_tracking_issue", _boom)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--execute"])

    assert sweep.main() == 0
