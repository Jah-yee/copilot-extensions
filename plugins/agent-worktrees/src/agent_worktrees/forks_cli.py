"""``forks`` CLI dispatch: the durable confirmed fork-publish registry.

See :mod:`.fork_registry` for the catalog itself and why it exists (the
``pr.fork`` confirmation gate in :mod:`.pr_ops`). Kept as its own module
rather than folded into ``repos_cli.py`` to stay under this repo's per-module
line-count cap, mirroring how ``copilot_identity_cli.py`` and
``related_cli.py`` are split out.
"""

from __future__ import annotations

from . import config as cfg
from . import output


def _core():
    from . import __main__ as core

    return core


def _forks_usage() -> None:
    try:
        project = cfg.project_name()
    except Exception:
        project = "agent-worktrees"
    print(f"Usage: {project} forks <command>")
    print()
    print("Durable catalog of confirmed fork-based PR publish targets")
    _forks_path = "~/.agent-worktrees/forks.yaml"  # marketplace-isolation: allow legacy
    print(f"({_forks_path}). Once a repo is listed here, create-pr's")
    print("pr.fork confirmation gate ('needs_confirmation: fork_setup') is")
    print("skipped for every future call against that repo on this machine --")
    print("a GitHub fork is durable and account-scoped, not per-worktree.")
    print()
    print("Commands:")
    print("  list                                List confirmed forks")
    print("  show <repo> [--account A]           Show a repo's confirmed fork(s) --")
    print("                                      all accounts, or just one with --account")
    print("  set <repo> --owner <login> [--remote R] [--account A | --token T] [--notes T]")
    print("                                      Pre-approve a repo's fork (e.g. during")
    print("                                      setup) without waiting for create-pr to ask.")
    print("                                      --account defaults to the resolved account/")
    print("                                      ambient gh login. For a repo using")
    print("                                      pr.token_command/token_env, pass that SAME")
    print("                                      token via --token (scope is derived from it,")
    print("                                      matching what create-pr will compute) --")
    print("                                      --account alone cannot reproduce that scope.")
    print("  remove <repo> [--account A]         Forget a repo's confirmation(s) -- every")
    print("                                      account for this repo, or just one with")
    print("                                      --account (create-pr will ask again)")
    print()
    print("Examples:")
    print(f"  {project} forks set octo-org/widgets --owner octocat")
    print(f"  {project} forks list")


def cmd_forks_dispatch(argv: list[str]) -> int:
    """Route the top-level ``forks`` registry subcommands."""
    from . import fork_registry

    if argv and argv[0] in ("--help", "-h"):
        _forks_usage()
        return 0
    sub = argv[0] if argv else "list"
    rest = argv[1:] if argv else []
    if "--help" in rest or "-h" in rest:
        _forks_usage()
        return 0

    def _opt(flag: str) -> str | None:
        if flag in rest:
            idx = rest.index(flag)
            if idx + 1 < len(rest):
                return rest[idx + 1]
        return None

    if sub == "list":
        entries = fork_registry.list_forks()
        if "--json" in rest:
            _core()._json_output(
                {
                    "forks": [
                        {
                            "repo": e.repo,
                            "owner": e.owner,
                            "remote": e.remote,
                            "account": e.account,
                            "confirmed_at": e.confirmed_at,
                            "notes": e.notes,
                        }
                        for e in entries
                    ]
                }
            )
            return 0
        if not entries:
            print("No forks confirmed yet.")
            print("Pre-approve one with: forks set <repo> --owner <login>")
            return 0
        output.header("Confirmed forks")
        for e in entries:
            print(f"  {e.repo:<40} owner={e.owner}  remote={e.remote}  account={e.account or '(none)'}")
            if e.confirmed_at:
                print(f"  {'':40} confirmed: {e.confirmed_at}")
        return 0

    if sub == "show":
        if not rest or rest[0].startswith("-"):
            output.err("Usage: forks show <repo> [--account A]")
            return 1
        repo = rest[0]
        account_filter = _opt("--account")
        entries = (
            [fork_registry.find_fork(repo, account_filter)]
            if account_filter is not None
            else fork_registry.find_forks_for_repo(repo)
        )
        entries = [e for e in entries if e is not None]
        if not entries:
            output.err(f"No confirmed fork for '{repo}' in forks.yaml")
            return 1
        if "--json" in rest:
            _core()._json_output(
                {
                    "forks": [
                        {
                            "repo": e.repo,
                            "owner": e.owner,
                            "remote": e.remote,
                            "account": e.account,
                            "confirmed_at": e.confirmed_at,
                            "notes": e.notes,
                        }
                        for e in entries
                    ]
                }
            )
            return 0
        for e in entries:
            output.header(f"Fork: {e.repo} (account={e.account or '(none)'})")
            print(f"  owner:        {e.owner}")
            print(f"  remote:       {e.remote}")
            print(f"  confirmed_at: {e.confirmed_at or '(unknown)'}")
            if e.notes:
                print(f"  notes:        {e.notes}")
        return 0

    if sub == "set":
        if not rest or rest[0].startswith("-"):
            output.err(
                "Usage: forks set <repo> --owner <login> [--remote R] "
                "[--account A | --token T] [--notes T]"
            )
            return 1
        repo = rest[0]
        owner = _opt("--owner")
        if not owner:
            output.err("forks set requires --owner <login>")
            return 1
        account = _opt("--account")
        token_opt = _opt("--token")
        if token_opt is not None:
            from . import pr_ops

            # The SAME scope create_pr derives for a token_command/token_env
            # -bound repo (see _resolve_fork_credential) -- pass the repo's
            # real token here so the pre-seeded entry actually matches what
            # create-pr will look up; --account alone cannot reproduce this,
            # since create-pr never guesses a login for an opaque token.
            account = pr_ops._token_scope(token_opt)
        elif account is None:
            from . import pr_ops

            # Same resolver create_pr's gate checks against (not the bare
            # account mapping) -- see pr_ops._resolve_fork_credential. Built
            # with a BARE PRConfig (no token_command/token_env): this default
            # covers the common account-mapping/ambient-auth case only. Use
            # --token instead for a repo using pr.token_command/token_env.
            _token, account = pr_ops._resolve_fork_credential(
                repo, cfg.PRConfig(provider="github"),
            )
        fork_registry.record_confirmation(
            repo,
            owner,
            remote=_opt("--remote") or "fork",
            account=account,
            notes=_opt("--notes"),
        )
        output.ok(
            f"Fork for '{repo}' confirmed (owner={owner}, account={account or '(none)'}) "
            f"-- future create-pr calls for this repo under the same resolved "
            f"account will skip the fork confirmation gate."
        )
        return 0

    if sub in ("remove", "rm"):
        if not rest or rest[0].startswith("-"):
            output.err("Usage: forks remove <repo> [--account A]")
            return 1
        if fork_registry.remove_fork(rest[0], _opt("--account")):
            return 0
        output.err(f"No confirmed fork for '{rest[0]}' in forks.yaml")
        return 1

    output.err(f"Unknown forks subcommand: {sub}")
    _forks_usage()
    return 1
