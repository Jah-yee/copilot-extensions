"""``pr.fork``'s confirmation gate + fork/remote bootstrap for ``create_pr``.

Split out of ``pr_ops.py`` (which only owns the *git* side of the PR
workflow) to keep that module under its own size discipline as this
feature's surface grew: identity/credential resolution, the durable
confirmation gate itself (see ``fork_registry.py`` for the durable half),
and the actual fork-create/remote-repoint call into the provider.

``create_pr`` calls :func:`resolve_fork_publish` once, right after it has a
resolved ``default_pr_repo`` and ``prcfg`` -- see that function's docstring
for the full contract.
"""

from __future__ import annotations


def _token_scope(token: str) -> str:
    """The durable confirmation scope for an opaque auth token's own value.

    Shared by :func:`_resolve_fork_credential` and ``forks_cli set --token``
    so a pre-seeded entry for a ``pr.token_command``/``token_env``-bound repo
    uses the EXACT same scope string ``create_pr`` will compute for it.
    """
    import hashlib
    return "token:" + hashlib.sha256(token.encode()).hexdigest()[:16]


def _resolve_fork_credential(repo_slug: str, prcfg) -> tuple[str | None, str]:
    """Resolve the (token, scope) pair for this repo's fork operations ONCE.

    Both the confirmation-gate scope and the token actually handed to
    ``provider.ensure_fork`` come from this single resolution (mirroring
    ``providers.account_token_for_slug``'s own priority: an explicit
    ``pr.token_command``/``token_env`` binding first, else the repo's
    resolved account mapping only when a token can actually be minted for
    it, else ambient ``gh`` auth) -- resolving it twice (once here, again
    inside ``account_token_for_slug``) could let a non-deterministic
    ``token_command`` or a transient mint authenticate the real operation as
    a different identity than the one the gate just confirmed/recorded.

    ``scope`` is ``""`` only when neither a configured token nor a
    mapped/ambient account can be resolved at all -- an opaque custom token
    still gets a non-empty hashed scope via :func:`_token_scope`. Callers
    must treat an empty scope as **fail-closed**: never persist or trust a
    durable confirmation for it, since two different unresolvable
    identities would otherwise collide on the same empty key.
    """
    if getattr(prcfg, "provider", "") != "github":
        return None, ""
    from .providers.base import resolve_token

    token = resolve_token(prcfg)
    if token:
        return token, _token_scope(token)

    from . import git_ops, repos

    account = repos.account_for_github_slug(repo_slug) or ""
    active = git_ops.active_gh_account() or ""
    if not account or (active and active.casefold() == account.casefold()):
        return None, active
    minted = git_ops.gh_token_for_account(account)
    return (minted, account) if minted else (None, active)


def _resolve_live_fork_owner(prcfg, token: str | None) -> str | None:
    """Non-mutating pre-check of the real fork-owner login a publish would
    resolve to, WITHOUT creating/verifying anything (see
    ``providers.PRProvider.resolve_fork_owner``). Lets the confirmation gate
    catch a stale/typo'd stored owner BEFORE :func:`_ensure_fork_and_remote`'s
    mutating POST/remote-repoint can run.

    Returns ``None`` (not a mismatch -- just "couldn't check") for an
    unsupported provider or a failed resolution; the caller then proceeds to
    the normal, mutating path, which will surface the same auth/provider
    failure there instead of here.
    """
    if getattr(prcfg, "provider", "") != "github":
        return None
    from . import providers
    try:
        provider = providers.get_provider(prcfg.provider)
        return provider.resolve_fork_owner(token=token)
    except (providers.ProviderError, OSError):
        return None


def _non_default_gh_host() -> str:
    """Ambient ``GH_HOST``, if pinning ``gh`` to a non-default (non-
    github.com) authority -- empty string otherwise. ``pr.fork``'s durable
    confirmation registry is keyed by repo+account only, not by GitHub
    authority/host, so the same ``owner/repo`` slug could otherwise
    identify two unrelated repositories (github.com vs. a GitHub Enterprise
    host) and silently reuse a confirmation across that boundary. Until the
    registry is host-scoped, this is used to refuse ``pr.fork`` entirely
    against a non-default host rather than risk that cross-host reuse.
    """
    import os
    host = (os.environ.get("GH_HOST") or "").strip().lower()
    return host if host and host != "github.com" else ""


def _ensure_fork_and_remote(
    worktree_path: str, repo_slug: str, prcfg, *, token: str | None,
) -> dict:
    """Ensure the caller's fork of ``repo_slug`` exists and a local git remote
    (``prcfg.fork.remote``) points at it.

    Returns ``{"owner": <fork-owner>}`` on success, or ``{"error": <message>}``
    on any failure (never raises) -- GitHub-only, matching ``pr.fork``'s scope.
    An explicit ``prcfg.fork.owner`` overrides the owner login used to build
    the PR head, in case the caller pushes through a differently-named fork
    than the one their own token would create/read. ``token`` is the SAME
    value :func:`_resolve_fork_credential` resolved for the confirmation
    scope -- never re-derived here, so the operation authenticates as
    exactly the identity that was confirmed.
    """
    if prcfg.provider != "github":
        return {"error": (
            f"pr.fork is only supported for provider 'github' today "
            f"(this repo is configured for provider {prcfg.provider!r})."
        )}
    non_default_host = _non_default_gh_host()
    if non_default_host:
        return {"error": (
            f"pr.fork does not support a non-default GH_HOST "
            f"('{non_default_host}') today -- its durable confirmation "
            f"registry is not scoped by authority. Unset GH_HOST (or "
            f"point it at github.com) to use pr.fork for this repo."
        )}
    from . import git_ops, providers
    try:
        provider = providers.get_provider(prcfg.provider)
        fork = provider.ensure_fork(repo_slug, token=token)
    except (providers.ProviderError, OSError) as exc:
        return {"error": f"Could not create/verify a fork of '{repo_slug}': {exc}"}
    if fork is None:
        return {"error": (
            f"Could not create/verify a fork of '{repo_slug}' (no 'gh' auth, "
            f"an API error, or an unsupported provider)."
        )}
    owner, clone_url = fork
    if prcfg.fork.owner:
        owner = prcfg.fork.owner
    if not git_ops.ensure_remote(prcfg.fork.remote, clone_url, cwd=worktree_path):
        return {"error": (
            f"Could not point local git remote '{prcfg.fork.remote}' at "
            f"'{clone_url}'."
        )}
    return {"owner": owner}


def resolve_fork_publish(
    worktree_path: str, default_pr_repo: str, prcfg, *, confirm_fork: bool,
) -> dict:
    """Resolve ``pr.fork``'s confirmation gate and, once cleared, the actual
    fork/remote bootstrap for one ``create_pr`` call.

    Returns exactly one of:

    - ``{"needs_confirmation": "fork_setup", "repo", "fork_remote", "message"}``
      -- nothing was mutated; relay ``message`` to the human and re-run with
      ``confirm_fork=True`` once they agree.
    - ``{"error": "..."}`` -- a hard failure (bad provider config, auth/API
      error); nothing further was mutated beyond what the error message
      itself describes.
    - ``{"publish_remote", "fork_owner", "warning": <optional str>}`` on
      success -- the fork/remote are ready; ``warning`` is set only when the
      fork succeeded but persisting the confirmation itself failed (a
      non-fatal, surfaced-to-the-caller condition).
    """
    from . import fork_registry

    # pr.fork's durable confirmation registry is not scoped by GitHub
    # authority (host) -- the same owner/repo slug can identify unrelated
    # repositories on github.com vs. a GitHub Enterprise host pinned via
    # ambient GH_HOST, and reusing a confirmation across that boundary would
    # silently authorize a fork/push against a DIFFERENT real repository.
    # Until the registry is host-scoped, restrict pr.fork to the default
    # github.com host entirely -- this check runs BEFORE any registry
    # lookup, so a confirmation recorded under github.com can never be
    # silently reused once GH_HOST later points elsewhere.
    non_default_host = _non_default_gh_host()
    if non_default_host:
        return {"error": (
            f"pr.fork does not support a non-default GH_HOST "
            f"('{non_default_host}') today -- its durable confirmation "
            f"registry is not scoped by authority. Unset GH_HOST (or "
            f"point it at github.com) to use pr.fork for this repo."
        )}

    # Resolve the credential ONCE: the same (token, scope) pair both gates
    # the confirmation decision and authenticates the actual fork operation
    # below -- see _resolve_fork_credential's docstring.
    fork_token, effective_account = _resolve_fork_credential(default_pr_repo, prcfg)
    # Fail closed on an unresolvable identity: never trust (or later
    # persist) a confirmation under an empty scope, which would let any
    # other equally-unresolvable caller silently reuse it.
    confirmed_entry = (
        fork_registry.find_fork(default_pr_repo, effective_account)
        if effective_account else None
    )
    # An explicit pr.fork.owner override deterministically decides the
    # owner login used for the PR head (see _ensure_fork_and_remote -- the
    # actual fork/remote clone_url still comes from the authenticated
    # provider) independent of which identity authenticates -- if it's
    # configured and doesn't match what was actually confirmed, this is a
    # DIFFERENT approval, not the same one under a new name; re-ask rather
    # than silently publishing there. The approved LOCAL REMOTE NAME is
    # likewise part of what was actually approved: if pr.fork.remote later
    # changes (including to an existing remote such as 'origin'), reusing
    # the old approval would let _ensure_fork_and_remote repoint a
    # DIFFERENT remote than the one the human actually approved touching.
    already_confirmed = confirmed_entry is not None and (
        not prcfg.fork.owner or prcfg.fork.owner == confirmed_entry.owner
    ) and prcfg.fork.remote == confirmed_entry.remote
    # A stored confirmation's owner can diverge from the actually resolved
    # fork owner (a typo at 'forks set' time, or a genuine upstream change)
    # when NO static pr.fork.owner override exists to check against ahead
    # of time. Validate it NON-MUTATINGLY, before _ensure_fork_and_remote's
    # mutating POST/remote-repoint can run -- a silent skip must not create
    # a fork or touch the checkout before discovering the approved owner no
    # longer matches. An explicit confirm_fork=True this call is itself a
    # fresh, live approval of whatever the real owner turns out to be, and
    # skips this pre-check. A FAILED/inconclusive lookup (live_owner is
    # None, e.g. a transient API error) must ALSO fail closed -- an
    # unverifiable owner must never silently reuse a stored approval, since
    # ensure_fork's own (separate) lookup moments later could legitimately
    # resolve a DIFFERENT owner and persist it without this call ever
    # having confirmed that was intended.
    if already_confirmed and not confirm_fork and not prcfg.fork.owner:
        live_owner = _resolve_live_fork_owner(prcfg, fork_token)
        if live_owner != confirmed_entry.owner:
            return {
                "needs_confirmation": "fork_setup",
                "repo": default_pr_repo,
                "fork_remote": prcfg.fork.remote,
                "message": (
                    f"Could not verify the previously confirmed fork owner "
                    f"for '{default_pr_repo}' ('{confirmed_entry.owner}') "
                    f"still matches the actual resolved fork owner "
                    f"({live_owner!r}). Ask the user to confirm publishing "
                    f"there, then re-run create-pr with --confirm-fork "
                    f"(or confirm_fork=True)."
                ),
            }
    if not confirm_fork and not already_confirmed:
        return {
            "needs_confirmation": "fork_setup",
            "repo": default_pr_repo,
            "fork_remote": prcfg.fork.remote,
            "message": (
                f"This repo's resolved PR flow publishes through a personal fork of "
                f"'{default_pr_repo}' rather than a direct push. Ask the user to confirm "
                f"forking it and pushing there, then re-run create-pr with --confirm-fork "
                f"(or confirm_fork=True) -- only needed once per repo+login (or pre-seed "
                f"via 'forks set')."
            ),
        }
    fork_setup = _ensure_fork_and_remote(
        worktree_path, default_pr_repo, prcfg, token=fork_token,
    )
    if fork_setup.get("error"):
        return {"error": fork_setup["error"]}
    fork_owner = fork_setup["owner"]
    result = {"publish_remote": prcfg.fork.remote, "fork_owner": fork_owner}
    # Re-persist whenever this is the first confirmation OR the real
    # resolved owner just changed (the explicit-reconfirm self-heal path
    # for a stale entry the pre-check above caught on a PRIOR call) --
    # purely bookkeeping here, never a gating decision (that already
    # happened, non-mutatingly, above).
    owner_changed = confirmed_entry is not None and confirmed_entry.owner != fork_owner
    if (not already_confirmed or owner_changed) and effective_account:
        try:
            fork_registry.record_confirmation(
                default_pr_repo, fork_owner,
                remote=prcfg.fork.remote, account=effective_account,
            )
        except OSError as exc:
            result["warning"] = f"Could not persist fork confirmation: {exc}"
    return result
