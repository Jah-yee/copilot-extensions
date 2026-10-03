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

## Participants

Single-driver effort at this stage -- no multi-agent split yet.

| Participant | Role in this effort | Reached via |
|-------------|---------------------|-------------|
| Driving session | Plans and (once this plan clears review) implements the devcontainer spec | this worktree/branch |

## Coordination

- **Topology:** independent per-phase PRs (no shared feature branch needed
  yet -- revisit if a phase grows a genuine multi-agent split).
- **Host (owns PRs):** the driving session above.
- **Delegates:** none at this stage.
- **Handoff:** n/a -- single driver; re-evaluate if a future phase is
  delegated.

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

- [x] **Prerequisite, must be done first:** establish and test the container's
      own host-boundary capability before relying on it for anything else --
      a devcontainer is not automatically a stronger boundary than the
      existing process-level containment, and checking mounts/credentials/
      network alone is not sufficient either (a permissive runtime posture
      defeats even a correctly-scoped mount). Concretely verify, and prefer a
      **container-local or read-only/overlay workspace** over a plain
      read-write bind of the host checkout (a read-write bind lets an
      adversarial test modify the host checkout directly, regardless of how
      "correctly scoped" the mount path looks):
      - the workspace mount's actual write scope -- does it expose more of
        the host than intended, and can a test modify files outside (or, with
        a plain bind mount, even inside) the checkout through it;
      - whether the Docker socket is exposed into the container (an exposed
        socket is a full host-escape vector);
      - what credentials are visible inside the container and from where
        they're sourced;
      - whether networking is restricted to what a test genuinely needs or
        left wide open;
      - the broader **runtime posture**, not just mounts/credentials/network:
        privileged mode, added Linux capabilities, `no-new-privileges`/seccomp
        confinement, host device access, and shared host namespaces (PID/IPC/
        UTS/user). This repository's own restricted-container boundary
        already treats all of these as fixed, checked invariants --
        `plugins/agent-containers/src/agent_containers/lifecycle.py`'s
        `restricted_policy_errors` (roughly lines 280-410) is the concrete
        reference for what "runtime posture" means in practice and the
        checks worth adapting here, even though this effort's container is a
        different (test-isolation, not dispatched-development) use case and
        may land on a different point on the trusted/restricted spectrum.
      Only once this boundary is concretely measured does the next bullet's
      comparison mean anything.

      **Done 2026-10-03, with a confirmed, live-reproduced finding, not just
      a theoretical concern:** built and ran a throwaway devcontainer from a
      minimal spec (`@devcontainers/cli` against the stock
      `mcr.microsoft.com/devcontainers/python` base image, no extra
      configuration -- i.e. the naive baseline a contributor would get from
      following public devcontainer docs with zero extra hardening).
      `docker inspect`'s `HostConfig`/`Mounts` against the running container
      confirmed, and a live write-through test proved:
      - **The workspace mount is a plain read-write bind of the host
        checkout** (`Mounts[0]`: `Type: bind`, `RW: true`,
        `Source: <host workspace path>`, `Destination: /workspaces/<name>`).
        Live-reproduced the exact risk the review raised: a file written from
        *inside* the container to the mounted path was immediately visible,
        modified, on the **host** filesystem -- i.e. an adversarial/buggy
        test inside this naive container genuinely can, and does, mutate the
        real host checkout. This alone means a default devcontainer spec
        would be a **regression**, not an improvement, over
        `run-plugin-tests.py`'s existing containment (which explicitly
        redirects state away from the real checkout) unless Phase 1
        deliberately designs around it (e.g. a container-local clone/copy of
        the checkout, or a read-only mount with an overlay for writes).
      - **No Docker socket exposure** by default (`Mounts` has no
        `/var/run/docker.sock` entry; confirmed via `docker exec ... ls
        /var/run/docker.sock` failing with "No such file or directory") --
        a real host-escape vector is NOT present in the naive baseline unless
        a feature like docker-outside-of-docker is explicitly added later.
      - **Default Docker capability set is active, nothing dropped**
        (`CapDrop: null` in `HostConfig`; the container's own
        `/proc/1/status` `CapEff`/`CapBnd` show Docker's standard default
        bits, not all-zero) -- this is a materially looser posture than the
        all-capabilities-dropped invariant this repo's own restricted-
        container boundary enforces (`lifecycle.py`'s
        `restricted_policy_errors`).
      - **No `no-new-privileges`/seccomp hardening declared**
        (`SecurityOpt: null`) -- Docker's own default seccomp profile still
        applies (this is NOT the same as `unconfined`), but nothing beyond
        that default is enforced.
      - **`ReadonlyRootfs: false`, `Privileged: false`, standard `bridge`
        networking with full outbound internet reach** confirmed live
        (a plain `curl` to an external host from inside the container
        succeeded with a 200).
      **Conclusion carried into Phase 1:** a devcontainer spec that merely
      follows public defaults does not close the gap this effort exists for
      -- it would need **deliberate** design choices (a container-local or
      overlay/copy-on-write workspace instead of a plain RW host bind being
      the single highest-priority one, since it's the difference between
      "isolated" and "directly mutates the host") to actually improve on
      `run-plugin-tests.py`'s existing containment rather than quietly
      regressing it while looking more isolated on the surface.
- [x] Confirm, with a concrete reproduction, what a `.devcontainer`-based test
      run -- using the established, tested boundary above -- would actually
      catch that `tools/run-plugin-tests.py`'s existing process-level
      containment does not (see Context's "What this existing mechanism does
      NOT provide" -- confirm or revise that list with real evidence rather
      than assuming it's complete). **Done 2026-10-03, revised from the
      original assumption**: a real OS-level filesystem/network boundary IS
      achievable (process escape via an absolute path, a raw socket, or a
      privilege a job object doesn't restrict is genuinely a class of attack
      `run-plugin-tests.py`'s containment cannot stop), but **only if Phase 1
      actually designs the container to provide that boundary** -- the naive
      baseline measured above does NOT provide it (the RW host-bind-mount
      finding is a direct counterexample: it's LESS isolated than the
      existing containment's redirected-state model for exactly the
      filesystem axis this effort cares about most). The "help for a human
      contributor running tests outside the turn-key runner" gap stands
      as originally stated -- confirmed unaffected by this finding, since
      it concerns opt-in-vs-structural enforcement, not the boundary's
      technical strength.
- [x] Decide whether the spec targets Linux only (matching this repo's CI
      runners) or must also cover the Windows-specific containment paths
      `TESTING.md` describes (`COPILOT_EXTENSIONS_TEST_CONTAINED`,
      registry-key diffing, Job-breakaway suppression) -- a Linux-only
      devcontainer cannot exercise those paths at all, which may be an
      acceptable scope boundary or may leave a real gap, depending on the
      answer to the first two bullets. **Decided 2026-10-03 (agent-
      recommended, open to revision at Phase 1's own review): Linux-only
      scope.** Reasoning: Docker Dev Containers are overwhelmingly a Linux-
      container technology in practice (Windows containers exist but are
      rarely used for this tooling and add substantial complexity for
      minimal benefit here); this repo's CI already runs a dedicated
      `windows-latest` test-runner job exercising exactly the Windows-
      specific paths `TESTING.md` describes, so those paths already have
      real coverage independent of this effort. A Linux-only devcontainer
      spec therefore narrows this effort's own scope to the Linux test-
      execution path without leaving the Windows paths uncovered overall --
      it simply doesn't duplicate coverage that already exists elsewhere.
      This closes Phase 0.

### Phase 1 — Spec design
- [x] Design the workspace storage model to actually close the gap Phase 0
      found: a container-local clone/copy of the checkout, or a read-only
      host bind plus an in-container overlay for writes -- NOT a plain
      read-write bind of the host checkout (confirmed live to let an
      adversarial test mutate the real host checkout, which is a regression
      versus the existing `run-plugin-tests.py` containment, not an
      improvement). **Done 2026-10-03, live-validated**: `.devcontainer/
      devcontainer.json`'s `workspaceMount` overrides the default bind
      entirely with a container-local Docker VOLUME -- the host checkout is
      never mounted into the container at all, in any form. The new
      `tools/run_tests_in_devcontainer.py` wrapper populates that volume
      from a point-in-time COPY of the host checkout (a `tar` pipe through
      `docker exec`, mirroring the already-reviewed `docker cp`-based repo
      materialization `agent-containers`' own `devcontainer_launch.py` uses
      for its `devcontainer_path` fleet backend) after the container is up.
      Live-reproduced the fix this time: a file written from inside the
      container never appears on the host, and `git status` on the host
      checkout stayed clean across multiple real container runs.
- [x] Design the runtime-posture hardening the naive baseline lacked: drop
      all Linux capabilities (add back only what the test suite genuinely
      needs), enforce `no-new-privileges`, decide on Docker-socket exclusion
      (default -- no feature should add it back without a deliberate,
      documented reason), and scope networking to what tests actually
      require rather than leaving the default bridge's full outbound reach.
      **Done 2026-10-03, live-validated against real `docker inspect`
      output**: `--cap-drop=ALL`, `--security-opt=no-new-privileges`, and
      `--read-only` root filesystem, deliberately mirroring the
      already-reviewed restricted-fleet invariants
      `agent-containers`' own `lifecycle.py` (`restricted_policy_errors`)
      checks for its dispatched-development containers -- including that
      policy's own fixed writable-surface set, `{workspace, home, /tmp,
      /run}`, reproduced here as the workspace volume plus three bounded
      `tmpfs` mounts. No Docker-socket mount is present (confirmed via
      `docker inspect`'s `Mounts`). **Two real runtime-posture pitfalls
      live validation actually caught, not merely anticipated:** (1) a bare
      tmpfs mount is `root:root 0755` by default, which the non-root
      `vscode` remote user cannot write into at all -- without an explicit
      `mode=1777` on the `$HOME` and `/run` tmpfs, the devcontainer CLI's
      own lifecycle-hook bookkeeping (a marker file under `$HOME`) silently
      failed, which in turn silently skipped `onCreateCommand` entirely;
      (2) Docker's tmpfs default additionally bakes in `noexec`, which
      blocks executing the installed `uv` binary from `$HOME/.local/bin`
      ("Permission denied") until explicitly overridden with `exec` on the
      `$HOME` and `/tmp` tmpfs (`/run` has no such need and stays
      `noexec`). **Networking is the one item left as a deliberate, named
      residual gap, not closed**: left at Docker's default bridge network
      with full outbound reach, because dependency resolution (`uv sync`,
      invoked transparently inside `tools/run-plugin-tests.py`) and the
      test run itself currently share one process/container lifetime and
      have not yet been split into a network-enabled "prime the venv
      cache" pass and a network-disconnected "run pytest" pass. A future
      iteration could add that split (e.g. `docker network disconnect
      bridge <container>` between the two passes); this effort's Validation
      Plan below tracks it as open rather than silently leaving it
      unaddressed.
- [x] Decide how this devcontainer spec is invoked for Linux test execution
      specifically -- a new `tools/run-plugin-tests.py` mode, a separate
      wrapper script, or direct `devcontainer exec` -- and how it relates to
      (without duplicating) the existing turn-key runner's own containment
      for contributors who aren't using the devcontainer. **Done
      2026-10-03: a separate, opt-in wrapper script**,
      `tools/run_tests_in_devcontainer.py` -- NOT a new mode baked into
      `run-plugin-tests.py` itself, so that runner's own interface and the
      vast majority of local/CI runs (which don't use the devcontainer at
      all) stay completely unchanged. The wrapper brings the container up,
      populates its workspace volume, then invokes
      `tools/run-plugin-tests.py` *inside* the container via `devcontainer
      exec` and passes through every one of that runner's own flags
      unmodified (`--changed`, `--all`, `-k`, etc.) -- so the container
      adds a real OS-level boundary strictly on top of (never instead of,
      never duplicating) the turn-key runner's existing process-level
      containment. Live-validated end-to-end against a real plugin suite
      (`ai-attribution`, 98 passed / 6 skipped) via both the wrapper's own
      internal functions and a full `python tools/run_tests_in_devcontainer.py
      ai-attribution` invocation -- confirmed the container tore itself
      down afterward and the host checkout's `git status` showed no
      unexpected changes. Unit tests
      (`tools/test_run_tests_in_devcontainer.py`) cover the wrapper's own
      logic (argument parsing, the host-copy exclusion list, the
      Docker/devcontainer-CLI invocation shape) via subprocess mocking, in
      the style of `test_run_plugin_tests.py`; the real, Docker-backed
      end-to-end run above is a manual validation step, not part of the
      default test portfolio, since it needs a working Docker daemon and
      network access to pull a base image.

### Phase 2 — Wire into CI / contributor flow
- [ ] _Pending Phase 1 review. Candidate shape: an opt-in CI lane (not a
      required gate, since the existing turn-key runner already gates
      every push/PR) that runs a representative subset of plugin suites
      through `tools/run_tests_in_devcontainer.py` on `ubuntu-latest`, to
      catch any future regression in the container boundary itself without
      slowing down the default fast path._
- [ ] _Pending: close the networking residual gap flagged in Phase 1's
      second item (split dependency-resolution and test-execution into
      separate network-enabled/network-disconnected passes), if judged
      worth the added complexity at review._

## Validation Plan

- [x] A throwaway container's workspace mount is confirmed, via
      `docker inspect`, to be a Docker volume (never a host bind) -- done
      live in Phase 1 (`Mounts[0].Type == "volume"`, no `Binds` entry in
      `HostConfig`).
- [x] A file written from inside the container is confirmed NOT to appear
      on the host filesystem, and the host checkout's `git status` stays
      clean across a real container run -- done live in Phase 1.
- [x] `docker inspect`'s `HostConfig` confirms `ReadonlyRootfs: true`,
      `CapDrop: ["ALL"]`, `CapAdd: null`, and `no-new-privileges` present in
      `SecurityOpt`, with no Docker-socket mount anywhere in `Mounts` --
      done live in Phase 1.
- [x] A real plugin's pytest suite (`ai-attribution`) passes end-to-end
      through `tools/run_tests_in_devcontainer.py`, with the container torn
      down afterward -- done live in Phase 1.
- [ ] Phase 2: the chosen CI-wiring shape (see Phase 2's first item) is
      implemented and itself green in CI, not just locally.
- [ ] Phase 2 (or a later revision of Phase 1): the networking residual gap
      is either closed (network-disconnected test-execution pass) or
      explicitly re-affirmed as an accepted, documented tradeoff rather than
      left open indefinitely.

## Proposal

Phase 1 delivered `.devcontainer/devcontainer.json` (the hardened,
workspace-volume-backed spec) and `tools/run_tests_in_devcontainer.py` (the
opt-in wrapper that brings it up, populates it, and runs
`tools/run-plugin-tests.py` inside it), both live-validated end-to-end
against a real plugin suite. Phase 2 remains: decide and implement how (or
whether) this gets wired into CI as an additional, non-blocking lane, and
decide whether to close the networking residual gap now or defer it.

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

### 2026-10-03 — Phase 0's host-boundary prerequisite done, with a real finding
Built and ran a throwaway devcontainer (`@devcontainers/cli` against the stock
`mcr.microsoft.com/devcontainers/python` image, zero extra hardening --
the naive baseline). Confirmed live, not assumed: the default workspace mount
is a plain read-write bind of the host checkout, and a file written from
inside the container was immediately visible, modified, on the host
filesystem. This means a naive devcontainer spec would be a **regression**
versus `run-plugin-tests.py`'s existing containment for exactly the axis this
effort cares about most (host-checkout safety), not an improvement -- Phase 1
now carries an explicit, highest-priority design requirement to use a
container-local or overlay/copy-on-write workspace instead of a plain RW host
bind. Also confirmed: no Docker-socket exposure by default (good), default
(non-empty) Linux capability set active with nothing dropped, no
`no-new-privileges`/seccomp hardening declared beyond Docker's own default
profile, and unrestricted outbound networking. Revised the second Phase 0
checklist item's conclusion accordingly: a real OS-level boundary is
achievable and would close a genuine gap, but only if Phase 1 deliberately
designs for it -- the naive baseline does not provide it "for free." Still
open: the Linux-only vs. cross-platform scope decision.

### 2026-10-03 — Phase 0 closed; Phase 1 scoped
Decided (agent-recommended, open to revision at Phase 1's own review
gate): Linux-only scope, since this repo's CI already runs a dedicated
Windows test-runner job covering `TESTING.md`'s Windows-specific containment
paths independently of this effort. All three Phase 0 checklist items are
now done. Expanded Phase 1 into three concrete design items derived directly
from Phase 0's findings: the workspace storage model (container-local/
overlay, not a plain RW host bind), runtime-posture hardening (capability
drop, `no-new-privileges`, Docker-socket exclusion, scoped networking), and
how the spec is actually invoked for Linux test execution without
duplicating `run-plugin-tests.py`'s existing containment for contributors
not using it.

### 2026-10-03 — Phase 1 done, all three items live-validated
Built `.devcontainer/devcontainer.json` and `tools/run_tests_in_devcontainer.py`
and validated every claim against a real container rather than reasoning
about it in the abstract -- caught two real bugs doing so, not zero:
(1) a bare Docker `tmpfs` mount is `root:root 0755` by default, which
silently broke the devcontainer CLI's own `$HOME`-based lifecycle-hook
bookkeeping (and would have broken installing `uv` the same way) until an
explicit `mode=1777` was added to the `$HOME` and `/run` tmpfs; (2) Docker's
tmpfs default additionally bakes in `noexec`, which blocked executing the
installed `uv` binary ("Permission denied") until an explicit `exec` option
was added to the `$HOME` and `/tmp` tmpfs. Both were found and fixed through
live iteration (build container -> hit the real error -> fix the spec ->
rebuild), not anticipated up front. Final live validation: `docker inspect`
confirmed the workspace is a volume (never a host bind, never any `Binds`
entry), `ReadonlyRootfs: true`, `CapDrop: ["ALL"]`, `no-new-privileges`
present, and no Docker-socket mount anywhere; a file written from inside the
container never appeared on the host and `git status` on the host checkout
stayed clean across multiple container runs; and a real plugin's pytest
suite (`ai-attribution`, 98 passed / 6 skipped) ran to completion end-to-end
through `python tools/run_tests_in_devcontainer.py ai-attribution`, including
automatic container teardown afterward. Added
`tools/test_run_tests_in_devcontainer.py` (7 unit tests, subprocess-mocked,
no Docker required) covering the wrapper's own logic, and a new
"Optional devcontainer-based isolation (Linux)" section in `TESTING.md`
documenting the invocation for future contributors. One item was
deliberately left open rather than silently resolved: outbound networking
still uses Docker's default bridge with full reach, because dependency
resolution and test execution currently share one container lifetime and
haven't been split into network-enabled/network-disconnected passes --
recorded as a named Phase 2 candidate and a Validation Plan item, not
dropped. Phase 2 (CI/contributor-flow wiring) is next.

### 2026-10-03 — Review rounds 1-2: 9 findings addressed, one caught live mid-fix
Automated review on the PR (#5058) raised six findings in round 1, all
addressed: (1) the fixed named workspace volume was reused across every
invocation -- files deleted on the host or artifacts left by a prior run
would stay visible, and concurrent runs would mutate the same volume --
fixed by generating a per-invocation devcontainer config with the volume
name made unique (`_per_instance_config`), and removing that exact volume
(not just the container) at teardown; (2) excluding `.git` from the copied
snapshot silently broke `--changed` mode (`git diff`/`git status` inside the
container would fail, and their unchecked empty output would produce an
empty, not erroring, target set) -- fixed by including `.git` in the copy
instead of trying to resolve changed targets on the host; (3) the
privileged workspace-population path (`_populate_workspace`) had no
automated coverage -- added subprocess-mocked tests covering the root
tar-extraction command, the follow-up chmod, and both commands' failure
branches; (4) a failed `docker rm -f` at teardown was silently discarded --
`_tear_down` now checks both the container and volume removal results and
raises if either fails; (5) the `--` passthrough separator was only
stripped when it was the very first extra argument, so
`--all -- -k some_filter` silently dropped the `-k` filter -- fixed to strip
every `--` occurrence, not just a leading one; (6) one test's assertion
allowed a regression that drops the final passthrough argument to pass
anyway -- narrowed to assert the complete expected suffix only.

Round 2 (after pushing round 1's fixes) raised three more, including a
real, HIGH-severity bug the reviewer caught from static analysis that live
validation then confirmed directly: (7) **in this repo's own required
linked-worktree flow, `.git` is a pointer FILE** (`gitdir: <absolute host
path>`), not a self-contained directory -- copying it verbatim (round 1's
fix for finding #2) left `git` inside the container pointing at a host
path that doesn't exist there, so `--changed` would have silently gone back
to running nothing despite `.git` technically being "included." Fixed with
`_resolve_git_dirs`/`_materialized_git_dir`: for a normal checkout this is
just `REPO/.git` (confirmed no bug there); for a linked worktree, it builds
a merged, self-contained copy in a temp dir -- the shared common dir's
objects/refs overlaid with this worktree's own private `HEAD`/`index`, with
the stale `commondir` pointer removed. **Live validation during this exact
fix caught a second bug the first merge attempt introduced**: a worktree's
private git dir carries its OWN near-empty `refs`/`logs` subdirectories (for
worktree-private refs), and wholesale-replacing the common dir's
already-copied `refs` with those (the first implementation's approach)
silently wiped every real branch ref -- `git rev-parse HEAD` failed with
"unknown revision" against the merged copy. Fixed by merging (`dirs_exist_ok=True`)
those specific subdirectories instead of replacing them outright, re-verified
live: `git rev-parse HEAD`, `git status --short`, and `git diff --name-only
origin/dev` all now resolve correctly against the materialized copy, and a
real end-to-end `--changed --base origin/dev` run through the full wrapper
completed cleanly (correctly reporting "No plugin suites to run" for this
PR's own non-plugin diff, rather than erroring). (8) teardown behavior
itself had no DIRECT test (only an indirect one replacing `_tear_down` with
a lambda) -- added tests invoking `_tear_down` and a new `_cleanup_orphan`
directly, covering success and both failure branches. (9) a `devcontainer
up` failure (timeout, a mid-`onCreateCommand` failure, or unparseable
output) raised before the teardown `finally` was ever entered, leaking a
partially created container and its unique volume -- added `_cleanup_orphan`
(found-by-label best-effort removal, swallowing its own failures so the
original startup error still surfaces) and wired it around `_bring_up` in
`main()`.

All nine fixes are re-validated: the full unit test suite (23 tests) passes,
and the real Docker-backed end-to-end run (`ai-attribution`, 98 passed / 6
skipped) was re-executed from scratch after every fix in this round,
including the `--changed` mode confirmation above -- not merely re-run once
at the start, since finding #7's live-validation-while-fixing is exactly
what caught finding #7's own follow-up bug.

