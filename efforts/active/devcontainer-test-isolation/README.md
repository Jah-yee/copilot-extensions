# Devcontainer Test Isolation

- **Slug:** `devcontainer-test-isolation`
- **Repo:** ThomasMichon/copilot-extensions
- **Branch(es):** TBD (one per phase)
- **Created:** 2026-10-02
- **Status:** Draft
- **Vision:** [`test-portfolio`](../../../visions/test-portfolio/README.md)'s
  containment boundary and host-safe-default behaviors; relates to, without
  changing, [`agent-containers`](../../../visions/plugins/agent-containers/README.md)'s
  trusted-vs-restricted venue posture.
- **Umbrella issue:** [#5040](https://github.com/ThomasMichon/copilot-extensions/issues/5040)

## Guiding Intent

Give `copilot-extensions` a `.devcontainer/devcontainer.json` spec whose
primary purpose is **test-execution isolation**: running a plugin's test
suite should not be able to leave side effects on, or depend on state from,
the contributor's (human or agent) host machine — regardless of what a buggy
or adversarial test actually does at the OS level. This is explicitly a
*different* concern from `agent-containers`' own **trusted development venue**
posture (see that plugin's vision) — a container used for headless, dispatched
*development* work (claiming issues, writing code, opening PRs) — not for
bounding test execution specifically, and not for interactive/local
contributor use.

## Context

### Prior art this effort must not duplicate or regress

**`tools/run-plugin-tests.py` already implements substantial process-level
test containment** (see `TESTING.md`) -- read this in full before planning
Phase 0's gap analysis:

- Every invocation redirects user/Copilot/plugin/XDG/temp state beneath a
  per-run sandbox, owns the pytest process job/group and its ordinary
  descendants, and enforces three time-scale budgets (30s/test, 300s per
  25-file sub-suite, 900s per plugin) plus per-sub-suite process/memory/temp
  ceilings (128 processes / 4096 MiB / 2048 MiB by default, all overridable).
- On Windows, contained runs set `COPILOT_EXTENSIONS_TEST_CONTAINED=1`;
  conforming installers virtualize persistent User/Machine environment
  reads/writes to Process scope, and the runner snapshots+diffs registry
  environment keys to catch any adapter that still mutates host state.
- The shared `agent-procutil` spawn helper detects contained runs and
  suppresses deliberate Windows Job breakaway / POSIX session detachment, so
  a test can't escape containment via a legitimate production detachment API
  either -- there's an explicit adversarial test proving the containment
  owner still reaps a detached descendant on timeout.
- A host-wide admission lease serializes heavy runs across every checkout/
  worktree so concurrent suites don't compete for the same CPU/memory/process
  budget.

**What this existing mechanism does NOT provide** (the likely gap a container
closes, to be confirmed, not assumed, in Phase 0 -- **and only once the
container boundary itself is established and tested**, since a naive
devcontainer is not automatically a stronger boundary: standard devcontainer
tooling bind-mounts the host workspace by default, so an adversarial test can
still modify the host checkout through that mount, and an exposed Docker
socket, leaked credentials, an unrestricted host-network mode, or an
unrestricted egress path would each independently invalidate the claims
below):
- A real OS-level filesystem/network boundary -- the containment above is
  process/env-level (job objects, env redirection, registry diffing), which
  bounds *well-behaved or moderately buggy* code but cannot stop e.g. a test
  that writes outside its redirected roots via an absolute path, opens a raw
  socket, or exploits a privilege a job object doesn't restrict. **Establishing
  this boundary is itself a Phase 0 prerequisite, not a given** -- see the
  revised Phase 0 checklist below.
- Any help for a HUMAN contributor running tests locally outside the
  `run-plugin-tests.py` harness (e.g. a bare test-runner invocation, or
  editor-integrated test running) -- the existing containment is opt-in by
  using the turn-key runner, not structurally enforced by the dev environment
  itself.

### Related, but distinct, existing machinery (do not conflate)

- `agent-containers`' own **trusted development venue** posture (see that
  plugin's vision, linked in the header above) -- headless, *dispatched
  development* work, not test-execution sandboxing specifically. A downstream
  adopter's own container-hardening work this same week is out of this
  effort's scope; this effort is deliberately a different concern (per
  operator decision, see Request).
- `agent-containers` also supports a **`devcontainer_path`-backed** fleet
  model (`plugins/agent-containers/src/agent_containers/devcontainer_launch.py`)
  that drives the real `devcontainer` CLI against a `.devcontainer/
  devcontainer.json` -- if this effort lands such a spec, that fleet model
  becomes available as a *future*, separate decision; this effort's own scope
  is the spec and test-execution isolation only, not wiring a new fleet.
- `agent-codespaces`' devcontainer-pinning pattern
  (`docs/patterns/codespace-repo-provenance.md`) is about *which* devcontainer
  a dispatched CodeSpace resolves for a *different* product's vessel repo --
  unrelated to this effort's own repo gaining its first devcontainer spec.

## Request

Operator's ask, verbatim (2026-10-02, mid-session on an unrelated,
downstream-adopter container-hardening stretch):

> We are getting to the point where we're going to want to device a
> .devcontainer spec for copilot-extensions, and force all copilot-extensions
> development to be done in a container, just to avoid the test runs from
> spilling into our machines

Follow-up scoping (same session, asked by the agent before carving this
effort):

- **Primary goal:** test execution isolation specifically (not general
  interactive-dev-session isolation, though the operator's literal phrasing
  above said "force all ... development to be done in a container" -- see
  **Scope note** below on this tension).
- **Relationship to `agent-containers`' trusted development venue:** a
  separate concern, not a replacement for or an alternative mode of that
  existing dispatched-worker posture.
- **Timing:** start this as a tracked effort now.

**Scope note (agent-recommended, resolved):** the verbatim Request's own
wording ("force all ... development to be done in a container") read broader
than a pure test-execution concern -- the agent explicitly surfaced this as a
three-way choice (interactive dev isolation / test-execution isolation only /
both) before carving this effort, and the operator picked **test-execution
isolation only**, explicitly separate from `agent-containers`' trusted
development venue posture. This is a resolved decision, not an open tension --
recorded here so a future reader sees that the narrower scope was a
deliberate choice offered and made, not the agent silently narrowing the
verbatim ask.

## Plan

### Phase 0 — Gap analysis (research, no code)

- [ ] **Prerequisite, must be done first:** establish and test the container's
      own host-boundary capability before relying on it for anything else --
      a devcontainer is not automatically a stronger boundary than the
      existing process-level containment. Concretely verify: the workspace
      bind-mount's actual write scope (does it expose more of the host than
      intended -- can a test modify files outside the repo checkout through
      it?), whether the Docker socket is exposed into the container (a
      exposed socket is a full host-escape vector), what credentials are
      visible inside the container and from where they're sourced, and
      whether networking is restricted to what a test genuinely needs or
      left wide open. Only once this boundary is concretely measured does the
      next bullet's comparison mean anything.
- [ ] Confirm, with a concrete reproduction, what a `.devcontainer`-based test
      run -- using the established, tested boundary above -- would actually
      catch that `tools/run-plugin-tests.py`'s existing process-level
      containment does not (see Context's "What this existing mechanism does
      NOT provide" -- confirm or revise that list with real evidence rather
      than assuming it's complete).
- [ ] Decide whether the spec targets Linux only (matching this repo's CI
      runners) or must also cover the Windows-specific containment paths
      `TESTING.md` describes (`COPILOT_EXTENSIONS_TEST_CONTAINED`,
      registry-key diffing, Job-breakaway suppression) -- a Linux-only
      devcontainer cannot exercise those paths at all, which may be an
      acceptable scope boundary or may leave a real gap, depending on the
      answer to the first two bullets.

### Phase 1 — Spec design
- [ ] _Pending Phase 0's findings._

### Phase 2 — Wire into CI / contributor flow
- [ ] _Pending Phase 0/1._

## Validation Plan

- [ ] _Pending Phase 0's findings -- a concrete validation plan requires
      knowing what gap the spec is closing._

## Proposal

_Pending._

## Journal

### 2026-10-02 — Created
Carved from a verbatim operator idea raised mid-session during an unrelated
downstream-adopter container-hardening stretch. Captured the Request
verbatim, scoped it via a short clarifying round (test-execution isolation
specifically -- resolving the verbatim ask's broader "force all development"
framing down to this narrower, explicitly chosen scope -- separate from
`agent-containers`' trusted development venue posture, start now), and read
`TESTING.md`'s existing `tools/run-plugin-tests.py` containment mechanism in
full before drafting Phase 0 -- that mechanism is substantial prior art this
effort must not duplicate or silently regress. No implementation work has
started; this is the plan awaiting the Phase 0 research above and its own
review gate before anything is built.

### 2026-10-02 — Review round 1: 4 findings addressed
Automated review on the plan PR raised four real findings, all addressed:
(1) Phase 0 assumed a container closes the claimed host-boundary gap without
first establishing/testing that boundary itself (bind-mount write scope,
Docker-socket exposure, credential visibility, network restriction) -- made
this an explicit, first Phase 0 prerequisite rather than an assumption;
(2) missing the required effort-header `Vision` field -- added, grounding this
effort in `visions/test-portfolio`'s containment-boundary/host-safe-default
behaviors and relating it (without changing) to `agent-containers`' own
trusted-venue vision; (3) PR description missing the required Documentation-
impact statement -- added (see the PR itself); (4) private downstream
organization/fleet identifiers (a private consumer's own fleet/effort names)
leaked into this public artifact -- replaced throughout with the public
`agent-containers` vision's own identifier-neutral terminology ("trusted
development venue" posture) instead of naming the private consumer or its
internal effort/fleet names.
