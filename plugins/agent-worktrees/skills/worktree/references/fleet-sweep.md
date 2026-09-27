# Worktree — Fleet-Wide Stale/Backlog Sweep

Full procedure for investigating a **backlog** of worktrees stuck showing
`active` (or otherwise non-terminal) with no clear live owner -- typically
surfaced after a mux/daemon crash left cached liveness hints stale, or
discovered incidentally while auditing a machine's worktrees. This is
distinct from [cleanup-details.md](cleanup-details.md), which covers
resolving a single `dirty` worktree once you already know which one needs
attention; this reference covers finding and triaging *many* candidates at
once, safely.

## Step 1 -- Baseline health (read-only)

Before touching anything, confirm the tooling itself is healthy and get an
authoritative read on what's actually still running:

```
<agent-worktrees catalog argv[0]> doctor --json
<agent-worktrees catalog argv[0]> handoffs-check --all --json
<agent-worktrees catalog argv[0]> reap-sessions --dry-run --json
<agent-worktrees catalog argv[0]> reclaim --all --json
```

`reclaim --all` in particular reports genuinely-live PIDs bound to a
worktree (`homing: mux`) -- treat those as **confirmed live**, not stale, no
matter how the picker/status field renders. A daemon that crashed can
self-heal (`status-monitor-restart` reports `"a current monitor already owns
the host"` when it's already back), and mux itself can reattach previously
orphaned processes over the following minutes -- re-run these checks rather
than trusting a single snapshot if you suspect recovery is still in
progress.

## Step 2 -- Release the claims-ledger backlog (mechanical, safe)

A worktree's `status: active` display and its `finalize`/`cleanup`
eligibility both consult the **claims/obligation ledger** (see
[obligations.md](obligations.md)). Crashed sessions routinely leave this
ledger cluttered with claims that are already safe to clear:

```
<agent-worktrees catalog argv[0]> claims fleet-audit          # read-only inventory
<agent-worktrees catalog argv[0]> claims sweep                # dry-run: provably-gone session claims
<agent-worktrees catalog argv[0]> claims sweep --apply
<agent-worktrees catalog argv[0]> claims reconcile-at-rest --apply   # already-settled child-worktree claims
```

Both are purpose-built as **never-wedge** operations: `sweep` only ever
touches obligations it can prove are gone-and-safe, and
`reconcile-at-rest` only releases claims already marked settled. Neither
touches git content, deletes a folder, or force-releases anything active or
ambiguous -- this is metadata bookkeeping, not a git operation, so it's safe
to run before you've finished investigating individual worktrees. Re-run
`cleanup` (report mode) afterward -- releasing stale claims frequently
reclassifies a worktree from `active`/blocked straight to `completed`
without touching anything else.

## Step 3 -- Verify session *content*, not just git + claims metadata

**This is the step that's easy to skip and the one that matters most.**
Zero uncommitted changes, zero commits ahead of the default branch, and a
clear claims ledger only prove nothing is sitting *unlanded* in that
worktree -- they don't prove the session's actual objective *resolved*.
A crashed session can be perfectly git-clean and still represent a real,
unfinished thread (a diagnosis that was never acted on, a PR that got
superseded and needs someone to notice, a handoff nobody picked up). Before
concluding a stale-`active` worktree is safe to clean, or even safe to
leave alone unremarked, read what its session actually did:

- **Small session (roughly under a few hundred events / ~10 turns or
  fewer):** read it directly -- `<agent-worktrees catalog argv[0]>
  session-transcript <session-id> --json`, or the `read-session-digest`
  command from the agent-logger catalog. Cheap enough not to need
  delegation; just check the last few user/assistant messages for how it
  actually ended.
- **Larger session:** delegate to the **`agent-logger:session-rampup`**
  sub-agent per the `ramp-up-session` skill instead of reading the raw
  transcript yourself -- that's exactly what it exists for (absorbing a
  potentially huge transcript and returning a bounded takeover briefing
  instead of flooding your own context). Ask it for an **explicit verdict**:
  did the objective land (possibly under a *different* worktree's PR after
  a handoff or supersession -- check, don't assume the local branch is the
  only place the work could have gone), is it genuinely done, or is there a
  real open thread even though git is clean (e.g. an informational/
  diagnosis-only session whose recommendation was never acted on)?

Only after this step should you treat a worktree as safe to prune, safe to
leave silently, or in need of a decision from the operator. A `cleanup`
report of `completed`/`unused` plus a clear claims ledger is necessary but
**not sufficient** on its own -- it tells you nothing was lost; it doesn't
tell you the story resolved.

## Step 4 -- Respect the tool's own refusals

Some worktree kinds are intentionally out of scope for the standard
`cleanup` flow and will say so rather than silently no-op:

```
<worktree-id>: skipped -- agent-owned bridge worktree (use the System menu)
```

Do not work around a refusal like this with raw git commands (`git worktree
remove`, manual branch deletion, etc. -- see the entrypoint's *Never
Finalize Manually* rule). It means a different subsystem (here, agent-bridge)
owns that worktree's lifecycle; retire it through that subsystem's own
mechanism, or leave it for the operator.

## Step 5 -- Ownership ambiguity is a stop, not a guess

If a worktree's `controllers`/`controller_findings` shows a
`reciprocal_relation.state` of `ambiguous`, or a `session-rampup` briefing
reveals the real owner of the work turned out to be a *different* worktree
or even a different project (a cross-repo handoff, a superseding PR opened
from elsewhere), do not decide unilaterally which side is authoritative.
Report the finding and hand it back to the operator -- see the
`tracing-claimant-graphs` skill for walking an ownership chain across
worktrees/projects when you need to trace it further.

## Step 6 -- Surface real findings; don't silently discard them

A session can be entirely "at rest" from a cleanup standpoint (nothing to
land, nothing blocking) while still containing a substantive, still-relevant
answer the operator hasn't acted on (a completed root-cause diagnosis, a
flagged follow-up decision). Report those findings back explicitly instead
of treating "safe to clean" as "nothing here was worth mentioning."

## Step 7 -- Track the systemic root cause once, not per-worktree

If the same pattern recurs across many worktrees on a sweep (e.g. crashed
sessions whose recorded `state` never leaves `active` even once `live`
correctly flips to `false`), that's a defect worth filing once rather than
hand-remediating the fleet every time it recurs -- see the `file-issue`
skill/`error-response` discipline (fix or track every issue, never dismiss
it as pre-existing and move on).
