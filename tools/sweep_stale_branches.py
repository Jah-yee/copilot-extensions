#!/usr/bin/env python3
"""Scheduled safety-net sweep for stale merged-PR branches.

This automates the exact manual technique used for a one-time cleanup of
3000+ accumulated branches on `ThomasMichon/copilot-extensions` (see the
`branch-hygiene` effort): cross-reference every merged pull request's head
branch against the branches that still physically exist on `origin`, and
delete the ones whose PR is already merged.

Why this still matters even after `delete_branch_on_merge` is enabled on the
repo and `agent-worktrees`' own merge tooling passes `--delete-branch` by
default: both of those only clean up *going forward*, for merges that go
through those exact paths. This sweep is the safety net for everything else
-- a PR merged through GitHub's own UI before the setting was retroactively
enabled, an external tool/bot that never requested branch deletion, or a
pre-existing backlog on any other repo this pattern gets adopted in.

Deliberately conservative, mirroring `tools/module-health-watchdog.py`'s own
convention:

- A branch name alone is never a stable identity: a name can be deleted and
  recreated, force-pushed with new commits, picked up by a brand-new open
  PR, or (for a same-named branch on a fork) never have belonged to this
  repository at all. A deletion only ever proceeds when the **same-repo**
  merged PR's recorded head commit OID still matches that branch's *current*
  commit OID on `origin` at delete time -- an identity/lease check, not a
  name match. A fork-originated PR's `headRefName` never denotes a branch
  on this repo at all and is excluded entirely.
- Never touches a protected branch (the repo's configured default branch,
  plus `dev` for this repo's own `main`+`dev` pair -- see
  ``DEFAULT_PROTECTED_EXTRA``).
- Never auto-deletes a branch with **no PR record at all** (open, closed, or
  merged) that merely looks disposable by naming convention
  (``worktree/*``, ``feature/*``, ``pr/*``) -- those need per-case human/agent
  judgment (the one-time sweep found genuinely live, minutes-old branches
  among them). Instead it opens/updates a single tracking issue listing them
  for triage, exactly as cautious as leaving them alone.
- Refuses to classify anything -- deletable or flagged -- from a PR-history
  query it cannot prove is complete: a branch absent from a truncated
  ``gh pr list`` result is indistinguishable from a branch with no PR record
  at all, and a merged PR outside a truncated window would be silently
  skipped as "not mergeable" too. See ``_pull_requests`` below.
- Defaults to a dry run; nothing is deleted or filed without ``--execute``.

Usage::

    python tools/sweep_stale_branches.py                  # dry run, prints the plan
    python tools/sweep_stale_branches.py --execute         # actually delete + file
    python tools/sweep_stale_branches.py --repo owner/name # override for local testing

Exit code is 0 for a successful sweep (including a dry run, and including a
run that found nothing to do); nonzero when `--execute` was asked to
actually act and a branch deletion or the tracking-issue file/update itself
failed, or when the PR-history/branch-list queries could not be proven
complete -- so a scheduled run's own CI goes red instead of silently
reporting success while leaving work undone or acting on partial data.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from dataclasses import dataclass

DEFAULT_REPO = "ThomasMichon/copilot-extensions"
# This repo's own second long-lived branch, alongside whatever its
# configured default branch is (resolved live -- see `_default_branch`).
DEFAULT_PROTECTED_EXTRA = ("dev",)
FLAG_PATTERNS = ("worktree/*", "feature/*", "pr/*")
TRACKING_LABEL = "stale-branch-triage"
# A `gh pr list`/`gh api` row count landing exactly on the requested
# `--limit` is the signal we use to detect a truncated result (see
# `_pull_requests`/`_remote_branches`) rather than genuinely having fetched
# every record.
DEFAULT_LIMIT = 20000


@dataclass(frozen=True)
class PullRequestHead:
    branch: str
    head_oid: str
    same_repo: bool


def plan_sweep(
    remote_branches: dict[str, str],
    merged_heads: dict[str, str],
    all_pr_branch_names: set[str],
    protected_branches: set[str],
    flag_patterns: tuple[str, ...] = FLAG_PATTERNS,
) -> tuple[list[str], list[str]]:
    """Return ``(deletable, flagged)`` branch names, both sorted.

    ``remote_branches``: current branch name -> current commit OID on
    ``origin``, as of the moment the caller fetched it.

    ``merged_heads``: a **same-repository** merged PR's head branch name ->
    the head commit OID that PR actually merged. A fork-originated PR's
    head branch never denotes a branch on this repo and must never appear
    here (callers are responsible for excluding it -- see
    :func:`_pull_requests`).

    ``deletable``: a branch whose *current* OID on origin still exactly
    matches a merged PR's recorded head OID for that same branch name --
    the identity/lease check described in this module's docstring. A branch
    reused since (force-pushed, or picked up by a new open PR with new
    commits) has a different current OID and is correctly left alone.
    Excludes anything in ``protected_branches``.

    ``flagged``: a branch matching one of ``flag_patterns`` with **no** PR
    record at all (not even open or closed-unmerged) -- never deleted here,
    only surfaced for a human/agent to triage case-by-case.
    """
    deletable = sorted(
        branch
        for branch, current_oid in remote_branches.items()
        if branch not in protected_branches
        and branch in merged_heads
        and merged_heads[branch] == current_oid
    )
    flagged = sorted(
        branch
        for branch in remote_branches
        if branch not in protected_branches
        and branch not in all_pr_branch_names
        and any(fnmatch.fnmatchcase(branch, pattern) for pattern in flag_patterns)
    )
    return deletable, flagged


class GhCallFailed(RuntimeError):
    """A `gh` invocation needed to compute or act on the plan failed."""


class TruncatedResult(RuntimeError):
    """A `gh` query returned exactly its requested limit's worth of rows --
    indistinguishable from a complete result, so this run refuses to
    classify anything from it rather than silently act on partial data."""


def _gh_json(args: list[str]) -> object:
    out = subprocess.run(["gh", *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise GhCallFailed(f"gh {' '.join(args)} failed: {out.stderr.strip()}")
    return json.loads(out.stdout or "null")


def _pull_requests(repo: str, limit: int) -> list[PullRequestHead]:
    """Every PR's head info (open, closed, and merged), fully resolved.

    Raises :class:`TruncatedResult` if the row count exactly equals
    ``limit`` -- that is indistinguishable from "there were exactly this
    many", so this run cannot prove it saw every PR and must not classify
    anything (a merged PR outside the window looks absent; so does a branch
    with a real, un-merged PR outside the window).
    """
    rows = _gh_json(
        [
            "pr", "list", "--repo", repo, "--state", "all",
            "--json", "headRefName,headRefOid,isCrossRepository,state",
            "--limit", str(limit),
        ]
    )
    if len(rows) == limit:
        raise TruncatedResult(
            f"gh pr list --state all returned exactly --limit {limit} rows for {repo} -- "
            "cannot prove this is a complete PR history; refusing to classify anything "
            "from it. Re-run with a higher --limit."
        )
    return [
        PullRequestHead(
            branch=row["headRefName"],
            head_oid=row["headRefOid"],
            same_repo=not row["isCrossRepository"],
        )
        for row in rows
    ]


def _default_branch(repo: str) -> str:
    row = _gh_json(["repo", "view", repo, "--json", "defaultBranchRef"])
    return row["defaultBranchRef"]["name"]


def _remote_branches(repo: str, limit: int) -> dict[str, str]:
    """Every branch currently on ``origin`` -> its current commit OID.

    Raises :class:`TruncatedResult` on the same "exactly hit the limit"
    signal as :func:`_pull_requests` -- a truncated branch list would make a
    genuinely stale branch outside the window invisible to this sweep
    entirely (silently under-acting, not over-acting, but still an
    unproven result this run must not pretend is complete).
    """
    out = subprocess.run(
        [
            "gh", "api", "--method", "GET", f"repos/{repo}/branches",
            "--paginate", "-F", "per_page=100",
            "--jq", r'.[] | .name + "\t" + .commit.sha',
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise GhCallFailed(f"gh api repos/{repo}/branches failed: {out.stderr.strip()}")
    lines = [line for line in out.stdout.splitlines() if line]
    if len(lines) >= limit:
        raise TruncatedResult(
            f"repos/{repo}/branches returned {len(lines)} rows (>= the {limit} safety "
            "bound) -- refusing to assume this sweep saw every branch. Re-run with a "
            "higher --limit."
        )
    branches: dict[str, str] = {}
    for line in lines:
        name, _, sha = line.partition("\t")
        branches[name] = sha
    return branches


def _delete_branch(repo: str, branch: str, expected_oid: str) -> bool:
    # Re-verify the identity/lease check immediately before deleting: the
    # plan was computed from a snapshot that may be stale by the time
    # `--execute` actually runs through the deletable list (a later branch
    # in the same run can take noticeable wall-clock time to reach).
    current = subprocess.run(
        ["gh", "api", "--method", "GET", f"repos/{repo}/branches/{branch}", "--jq", ".commit.sha"],
        capture_output=True,
        text=True,
    )
    if current.returncode == 0 and current.stdout.strip() != expected_oid:
        print(f"[SKIP] {branch} has moved since planning (expected {expected_oid}, now {current.stdout.strip()}) -- not deleting.")
        return True
    out = subprocess.run(
        ["gh", "api", "-X", "DELETE", f"repos/{repo}/git/refs/heads/{branch}"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        # A 422 "Reference does not exist" means someone/something already
        # deleted it between planning and acting -- not a real failure.
        if "Reference does not exist" in out.stderr:
            print(f"[OK] {branch} already gone -- nothing to do.")
            return True
        print(f"[ERROR] failed to delete {branch}: {out.stderr.strip()}", file=sys.stderr)
        return False
    print(f"[OK] deleted {branch}")
    return True


def _existing_tracking_issue(repo: str) -> int | None:
    rows = _gh_json(
        ["issue", "list", "--repo", repo, "--label", TRACKING_LABEL, "--state", "open", "--json", "number"]
    )
    return rows[0]["number"] if rows else None


def _file_or_update_tracking_issue(repo: str, flagged: list[str]) -> bool:
    body = (
        "## Branches with no pull-request record at all\n\n"
        "The scheduled stale-branch sweep (`tools/sweep_stale_branches.py`, "
        "`.github/workflows/stale-branch-sweep.yml`) found these branches "
        "matching a disposable-looking naming convention "
        f"(`{'`, `'.join(FLAG_PATTERNS)}`) with **no** associated pull "
        "request -- open, closed, or merged. That makes them unsafe to "
        "auto-delete (a one-time manual sweep found genuinely live, "
        "minutes-old branches in exactly this shape); each needs per-case "
        "human/agent judgment before deletion.\n\n"
        + "\n".join(f"- `{branch}`" for branch in flagged)
        + "\n\nThis issue is updated in place on each scheduled run -- do not "
        "expect a new issue every time.\n"
    )
    existing = _existing_tracking_issue(repo)
    if existing is not None:
        out = subprocess.run(
            ["gh", "issue", "edit", str(existing), "--repo", repo, "--body", body],
            capture_output=True,
            text=True,
        )
        if out.returncode != 0:
            print(f"[ERROR] failed to update tracking issue #{existing}: {out.stderr.strip()}", file=sys.stderr)
            return False
        print(f"[OK] updated tracking issue #{existing}")
        return True
    subprocess.run(
        ["gh", "label", "create", TRACKING_LABEL, "--repo", repo,
         "--color", "D4C5F9",
         "--description", "No-PR-record branch flagged by the stale-branch sweep for manual triage",
         "--force"],
        capture_output=True,
        text=True,
    )
    out = subprocess.run(
        [
            "gh", "issue", "create", "--repo", repo,
            "--title", "Stale-branch sweep: branches with no PR record need triage",
            "--label", TRACKING_LABEL,
            "--body", body,
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        print(f"[ERROR] failed to file tracking issue: {out.stderr.strip()}", file=sys.stderr)
        return False
    print(f"[OK] filed {out.stdout.strip()}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"owner/name to sweep (default {DEFAULT_REPO})")
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT,
        help=f"safety bound on PRs/branches fetched; a result hitting it aborts as possibly truncated (default {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually delete branches and file/update the tracking issue (default: dry-run/report only)",
    )
    args = parser.parse_args()

    try:
        pull_requests = _pull_requests(args.repo, args.limit)
        remote_branches = _remote_branches(args.repo, args.limit)
        default_branch = _default_branch(args.repo)
    except (GhCallFailed, TruncatedResult) as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        return 1

    merged_heads = {pr.branch: pr.head_oid for pr in pull_requests if pr.same_repo and pr.head_oid}
    # "Has any PR record at all" must still count a fork-originated PR (its
    # branch isn't deletable here -- it isn't this repo's branch -- but a
    # same-named branch on THIS repo with no record of its own should not be
    # misclassified as PR-less just because a fork PR happened to share the
    # name).
    all_pr_branch_names = {pr.branch for pr in pull_requests}
    protected = {default_branch, *DEFAULT_PROTECTED_EXTRA}

    deletable, flagged = plan_sweep(remote_branches, merged_heads, all_pr_branch_names, protected)

    print(f"[INFO] {len(remote_branches)} remote branch(es) examined; protected: {sorted(protected)}.")
    print(f"[INFO] {len(deletable)} deletable (merged PR, branch unchanged since merge):")
    for branch in deletable:
        print(f"  - {branch}")
    print(f"[INFO] {len(flagged)} flagged for triage (no PR record, disposable-looking name):")
    for branch in flagged:
        print(f"  - {branch}")

    if not args.execute:
        print("[INFO] dry run -- pass --execute to actually delete and file/update the tracking issue.")
        return 0

    failed = False
    for branch in deletable:
        if not _delete_branch(args.repo, branch, merged_heads[branch]):
            failed = True

    if flagged:
        if not _file_or_update_tracking_issue(args.repo, flagged):
            failed = True
    else:
        print("[OK] nothing to flag for triage.")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
