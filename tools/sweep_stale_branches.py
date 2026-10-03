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

- Only ever deletes a branch whose name exactly matches a **merged** PR's
  head branch name. Never touches a protected branch (`main`, `dev`, or the
  repo's configured default branch).
- Never auto-deletes a branch with **no PR record at all** (open, closed, or
  merged) that merely looks disposable by naming convention
  (``worktree/*``, ``feature/*``, ``pr/*``) -- those need per-case human/agent
  judgment (the one-time sweep found genuinely live, minutes-old branches
  among them). Instead it opens/updates a single tracking issue listing them
  for triage, exactly as cautious as leaving them alone.
- Defaults to a dry run; nothing is deleted or filed without ``--execute``.

Usage::

    python tools/sweep_stale_branches.py                  # dry run, prints the plan
    python tools/sweep_stale_branches.py --execute         # actually delete + file
    python tools/sweep_stale_branches.py --repo owner/name # override for local testing

Exit code is 0 for a successful sweep (including a dry run, and including a
run that found nothing to do); nonzero only when `--execute` was asked to
actually act and a branch deletion or the tracking-issue file/update itself
failed, so a scheduled run's own CI goes red instead of silently reporting
success while leaving work undone.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys

DEFAULT_REPO = "ThomasMichon/copilot-extensions"
DEFAULT_PROTECTED = ("main", "dev")
FLAG_PATTERNS = ("worktree/*", "feature/*", "pr/*")
TRACKING_LABEL = "stale-branch-triage"


def plan_sweep(
    remote_branches: list[str],
    merged_branch_names: set[str],
    all_pr_branch_names: set[str],
    protected_branches: set[str],
    flag_patterns: tuple[str, ...] = FLAG_PATTERNS,
) -> tuple[list[str], list[str]]:
    """Return ``(deletable, flagged)`` branch names, both sorted.

    ``deletable``: a branch whose name exactly matches some **merged** PR's
    head branch name -- that PR is done, its branch's job is done. Excludes
    anything in ``protected_branches``.

    ``flagged``: a branch matching one of ``flag_patterns`` with **no** PR
    record at all (not even open or closed-unmerged) -- never deleted here,
    only surfaced for a human/agent to triage case-by-case.
    """
    deletable = sorted(
        branch
        for branch in remote_branches
        if branch in merged_branch_names and branch not in protected_branches
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


def _gh_json(args: list[str]) -> object:
    out = subprocess.run(["gh", *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise GhCallFailed(f"gh {' '.join(args)} failed: {out.stderr.strip()}")
    return json.loads(out.stdout or "null")


def _merged_branch_names(repo: str, limit: int) -> set[str]:
    rows = _gh_json(
        ["pr", "list", "--repo", repo, "--state", "merged", "--json", "headRefName", "--limit", str(limit)]
    )
    return {row["headRefName"] for row in rows}


def _all_pr_branch_names(repo: str, limit: int) -> set[str]:
    rows = _gh_json(
        ["pr", "list", "--repo", repo, "--state", "all", "--json", "headRefName", "--limit", str(limit)]
    )
    return {row["headRefName"] for row in rows}


def _remote_branches(repo: str) -> list[str]:
    # `--paginate` makes `gh api` follow every page itself; `--jq '.[].name'`
    # applied per-page yields one newline-separated stream across all pages
    # in a single call.
    out = subprocess.run(
        ["gh", "api", f"repos/{repo}/branches", "--paginate", "-F", "per_page=100", "--jq", ".[].name"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise GhCallFailed(f"gh api repos/{repo}/branches failed: {out.stderr.strip()}")
    return [line for line in out.stdout.splitlines() if line]


def _delete_branch(repo: str, branch: str) -> bool:
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
    parser.add_argument("--limit", type=int, default=5000, help="max PRs to fetch per state query (default 5000)")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually delete branches and file/update the tracking issue (default: dry-run/report only)",
    )
    args = parser.parse_args()

    try:
        merged_names = _merged_branch_names(args.repo, args.limit)
        all_pr_names = _all_pr_branch_names(args.repo, args.limit)
        remote_branches = _remote_branches(args.repo)
    except GhCallFailed as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        return 1

    protected = set(DEFAULT_PROTECTED)
    deletable, flagged = plan_sweep(remote_branches, merged_names, all_pr_names, protected)

    print(f"[INFO] {len(remote_branches)} remote branch(es) examined.")
    print(f"[INFO] {len(deletable)} deletable (merged PR, branch still present):")
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
        if not _delete_branch(args.repo, branch):
            failed = True

    if flagged:
        if not _file_or_update_tracking_issue(args.repo, flagged):
            failed = True
    else:
        print("[OK] nothing to flag for triage.")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
