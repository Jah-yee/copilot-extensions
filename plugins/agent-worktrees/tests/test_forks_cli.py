"""Dispatch-level tests for the ``forks`` CLI command (forks_cli.py)."""

from __future__ import annotations

import json

from agent_worktrees import forks_cli


def test_set_requires_owner(capfd):
    rc = forks_cli.cmd_forks_dispatch(["set", "octo-org/widgets"])
    assert rc == 1
    assert "requires --owner" in capfd.readouterr().out


def test_set_resolves_account_by_default(monkeypatch, capfd):
    monkeypatch.setattr(
        "agent_worktrees.pr_ops._resolve_fork_credential",
        lambda slug, prcfg: (None, "resolved-login"),
    )
    rc = forks_cli.cmd_forks_dispatch(
        ["set", "octo-org/widgets", "--owner", "octocat"],
    )
    assert rc == 0
    out = capfd.readouterr().out
    assert "resolved-login" in out

    rc = forks_cli.cmd_forks_dispatch(["show", "octo-org/widgets", "--json"])
    payload = json.loads(capfd.readouterr().out)
    assert rc == 0
    assert payload == {
        "version": 1,
        "forks": [
            {
                "repo": "octo-org/widgets",
                "owner": "octocat",
                "remote": "fork",
                "account": "resolved-login",
                "confirmed_at": payload["forks"][0]["confirmed_at"],
                "notes": "",
            }
        ]
    }
    assert payload["forks"][0]["confirmed_at"]


def test_set_explicit_account_overrides_resolution(monkeypatch, capfd):
    monkeypatch.setattr(
        "agent_worktrees.pr_ops._resolve_fork_credential",
        lambda slug, prcfg: (None, "would-be-resolved"),
    )
    rc = forks_cli.cmd_forks_dispatch(
        ["set", "octo-org/widgets", "--owner", "octocat", "--account", "explicit"],
    )
    assert rc == 0
    capfd.readouterr()
    forks_cli.cmd_forks_dispatch(["show", "octo-org/widgets", "--json"])
    payload = json.loads(capfd.readouterr().out)
    assert payload["forks"][0]["account"] == "explicit"


def test_set_with_token_derives_scope(monkeypatch, capfd):
    """``--token`` must derive the SAME scope create_pr's gate would compute
    for a token_command/token_env-bound repo, rather than guessing a login
    account alone cannot reproduce for an opaque token."""
    from agent_worktrees import pr_ops

    rc = forks_cli.cmd_forks_dispatch(
        [
            "set", "octo-org/widgets", "--owner", "octocat",
            "--token", "ghp_exampletoken",
        ],
    )
    assert rc == 0
    expected_scope = pr_ops._token_scope("ghp_exampletoken")
    out = capfd.readouterr().out
    assert expected_scope in out

    forks_cli.cmd_forks_dispatch(["show", "octo-org/widgets", "--json"])
    payload = json.loads(capfd.readouterr().out)
    assert payload["forks"][0]["account"] == expected_scope


def test_list_json_and_text(monkeypatch, capfd):
    monkeypatch.setattr(
        "agent_worktrees.pr_ops._resolve_fork_credential",
        lambda slug, prcfg: (None, ""),
    )
    forks_cli.cmd_forks_dispatch(["set", "octo-org/widgets", "--owner", "octocat"])
    capfd.readouterr()

    rc = forks_cli.cmd_forks_dispatch(["list", "--json"])
    assert rc == 0
    payload = json.loads(capfd.readouterr().out)
    assert [f["repo"] for f in payload["forks"]] == ["octo-org/widgets"]

    rc = forks_cli.cmd_forks_dispatch(["list"])
    assert rc == 0
    out = capfd.readouterr().out
    assert "octo-org/widgets" in out
    assert "owner=octocat" in out


def test_list_empty(capfd):
    rc = forks_cli.cmd_forks_dispatch(["list"])
    assert rc == 0
    assert "No forks confirmed yet." in capfd.readouterr().out


def test_show_unknown_repo_fails(capfd):
    rc = forks_cli.cmd_forks_dispatch(["show", "no/such-repo"])
    assert rc == 1
    assert "No confirmed fork" in capfd.readouterr().out


def test_remove(monkeypatch, capfd):
    monkeypatch.setattr(
        "agent_worktrees.pr_ops._resolve_fork_credential",
        lambda slug, prcfg: (None, ""),
    )
    forks_cli.cmd_forks_dispatch(["set", "octo-org/widgets", "--owner", "octocat"])
    capfd.readouterr()

    rc = forks_cli.cmd_forks_dispatch(["remove", "octo-org/widgets"])
    assert rc == 0

    rc = forks_cli.cmd_forks_dispatch(["remove", "octo-org/widgets"])
    assert rc == 1
    assert "No confirmed fork" in capfd.readouterr().out


def test_help_and_unknown_subcommand(capfd):
    rc = forks_cli.cmd_forks_dispatch(["--help"])
    assert rc == 0
    assert "forks <command>" in capfd.readouterr().out

    rc = forks_cli.cmd_forks_dispatch(["bogus"])
    assert rc == 1
    err = capfd.readouterr()
    assert "Unknown forks subcommand" in err.out
