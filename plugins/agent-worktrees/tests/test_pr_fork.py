"""Unit tests for pr_fork.py -- the fork-confirmation gate + credential/host
resolution split out of pr_ops.py. See test_pr_ops.py's TestCreatePRForkFlow
for the integration-level tests exercising the full create_pr flow through
this module.
"""

from __future__ import annotations

from agent_worktrees import config as cfg
from agent_worktrees import pr_fork


class TestResolveForkCredential:
    """_resolve_fork_credential must reflect the identity that actually
    authenticates (mirroring providers.account_token_for_slug), not the bare
    account mapping -- an unmintable mapping silently falls back to ambient
    auth, and a confirmation scoped to the mapping alone would miss that.
    Returns (token, scope): the SAME token must flow to the real fork
    operation, and scope=="" means "unresolvable -- fail closed"."""

    def _cfg(self, provider="github"):
        import dataclasses
        return dataclasses.replace(cfg.PRConfig(enabled=True), provider=provider)

    def test_non_github_provider_returns_empty(self, monkeypatch):
        assert pr_fork._resolve_fork_credential("o/r", self._cfg("gitea")) == (None, "")

    def test_unmapped_repo_falls_back_to_active_account(self, monkeypatch):
        monkeypatch.setattr(
            "agent_worktrees.repos.account_for_github_slug", lambda s: None,
        )
        monkeypatch.setattr(
            "agent_worktrees.git_ops.active_gh_account", lambda: "whoami",
        )
        assert pr_fork._resolve_fork_credential("o/r", self._cfg()) == (None, "whoami")

    def test_unresolvable_identity_fails_closed_to_empty_scope(self, monkeypatch):
        """Neither a mapped account nor an active gh login -- the identity
        is genuinely unknown, so the scope must be '' (never a value two
        different unresolvable callers could collide on)."""
        monkeypatch.setattr(
            "agent_worktrees.repos.account_for_github_slug", lambda s: None,
        )
        monkeypatch.setattr(
            "agent_worktrees.git_ops.active_gh_account", lambda: None,
        )
        assert pr_fork._resolve_fork_credential("o/r", self._cfg()) == (None, "")

    def test_mapping_equal_to_active_uses_active(self, monkeypatch):
        monkeypatch.setattr(
            "agent_worktrees.repos.account_for_github_slug", lambda s: "Same",
        )
        monkeypatch.setattr(
            "agent_worktrees.git_ops.active_gh_account", lambda: "same",
        )
        monkeypatch.setattr(
            "agent_worktrees.git_ops.gh_token_for_account", lambda a: "should-not-be-called",
        )
        assert pr_fork._resolve_fork_credential("o/r", self._cfg()) == (None, "same")

    def test_mintable_cross_account_mapping_wins(self, monkeypatch):
        monkeypatch.setattr(
            "agent_worktrees.repos.account_for_github_slug", lambda s: "mapped",
        )
        monkeypatch.setattr(
            "agent_worktrees.git_ops.active_gh_account", lambda: "active",
        )
        monkeypatch.setattr(
            "agent_worktrees.git_ops.gh_token_for_account",
            lambda a: "tok" if a == "mapped" else None,
        )
        assert pr_fork._resolve_fork_credential("o/r", self._cfg()) == ("tok", "mapped")

    def test_unmintable_cross_account_mapping_falls_back_to_active(self, monkeypatch):
        """The exact gap this helper exists to close: a mapped account that
        cannot actually be minted silently resolves to ambient auth, so the
        confirmation scope must reflect 'active', not 'mapped'."""
        monkeypatch.setattr(
            "agent_worktrees.repos.account_for_github_slug", lambda s: "mapped",
        )
        monkeypatch.setattr(
            "agent_worktrees.git_ops.active_gh_account", lambda: "active",
        )
        monkeypatch.setattr(
            "agent_worktrees.git_ops.gh_token_for_account", lambda a: None,
        )
        assert pr_fork._resolve_fork_credential("o/r", self._cfg()) == (None, "active")

    def test_explicit_token_binding_takes_priority_and_is_scoped_by_value(self, monkeypatch):
        """pr.token_command/token_env is account_token_for_slug's FIRST
        priority -- a repo using it can authenticate as an identity no
        mapping/ambient lookup would ever reveal, so it must win here too.
        Scoped by the token's own value (not a guessed login) so a
        changed/rotated token safely re-prompts instead of silently trusting
        whichever identity the new token happens to belong to -- and the
        SAME token value is returned for the real fork operation to use."""
        import dataclasses
        config_with_token = dataclasses.replace(
            self._cfg(), token_env="SOME_TOKEN_ENV_VAR_UNSET",
        )
        monkeypatch.setattr(
            "agent_worktrees.providers.base.resolve_token", lambda prcfg: "secret-token-abc",
        )
        first_token, first_scope = pr_fork._resolve_fork_credential("o/r", config_with_token)
        assert first_token == "secret-token-abc"
        assert first_scope.startswith("token:")

        monkeypatch.setattr(
            "agent_worktrees.providers.base.resolve_token", lambda prcfg: "secret-token-xyz",
        )
        _second_token, second_scope = pr_fork._resolve_fork_credential("o/r", config_with_token)
        assert second_scope.startswith("token:")
        assert second_scope != first_scope  # a different token value -> a different scope


class TestResolveLiveForkOwner:
    """_resolve_live_fork_owner must be the NON-mutating half the
    confirmation gate's pre-check relies on -- it must never reach any
    mutating provider call, and must fail soft (None) rather than raise."""

    def _cfg(self, provider="github"):
        import dataclasses
        return dataclasses.replace(cfg.PRConfig(enabled=True), provider=provider)

    def test_non_github_provider_returns_none(self):
        assert pr_fork._resolve_live_fork_owner(self._cfg("gitea"), None) is None

    def test_delegates_to_provider_resolve_fork_owner(self, monkeypatch):
        class _FakeProvider:
            def resolve_fork_owner(self, *, token=None):
                assert token == "tok"
                return "live-owner"

        monkeypatch.setattr(
            "agent_worktrees.providers.get_provider", lambda name: _FakeProvider(),
        )
        assert pr_fork._resolve_live_fork_owner(self._cfg(), "tok") == "live-owner"

    def test_provider_error_is_swallowed_to_none(self, monkeypatch):
        from agent_worktrees import providers

        class _FailingProvider:
            def resolve_fork_owner(self, *, token=None):
                raise providers.ProviderError("boom")

        monkeypatch.setattr(
            "agent_worktrees.providers.get_provider", lambda name: _FailingProvider(),
        )
        assert pr_fork._resolve_live_fork_owner(self._cfg(), None) is None


class TestNonDefaultGhHost:
    """pr.fork's durable registry isn't scoped by GitHub authority (host),
    so it must refuse to operate at all when GH_HOST pins a non-default
    host -- rather than risk a confirmation recorded under github.com
    silently authorizing a fork/push against an unrelated same-named repo
    on a GitHub Enterprise instance."""

    def test_unset_returns_empty(self, monkeypatch):
        monkeypatch.delenv("GH_HOST", raising=False)
        assert pr_fork._non_default_gh_host() == ""

    def test_github_com_is_default_returns_empty(self, monkeypatch):
        monkeypatch.setenv("GH_HOST", "GitHub.com")
        assert pr_fork._non_default_gh_host() == ""

    def test_enterprise_host_is_non_default(self, monkeypatch):
        monkeypatch.setenv("GH_HOST", "github.example.com")
        assert pr_fork._non_default_gh_host() == "github.example.com"
