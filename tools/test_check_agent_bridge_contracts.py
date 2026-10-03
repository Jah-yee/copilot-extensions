"""Regression tests for the agent-bridge contract registry checker."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

SCRIPT = Path(__file__).resolve().parent / "check-agent-bridge-contracts.py"
BLOB_PROVENANCE_MODULE = Path(__file__).resolve().parent / "_blob_provenance.py"
SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "agent-bridge"
    / "contract"
    / "registry.schema.json"
)
SOURCE = "plugins/agent-bridge/src/agent_bridge/protocol.py"
FIXTURE = "plugins/agent-bridge/contract/fixtures/http/current/health.json"
HOST_SOURCE = "plugins/agent-bridge/src/agent_bridge/session_host/protocol.py"
HOST_FIXTURE = (
    "plugins/agent-bridge/contract/fixtures/session-host/current/messages.json"
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write(repo: Path, relative: str, value: str | dict[str, Any]) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, indent=2) + "\n" if isinstance(value, dict) else value
    path.write_text(text, encoding="utf-8")


def _sha256(repo: Path, relative: str) -> str:
    data = (repo / relative).read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        canonical = data
    else:
        canonical = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(repo / "tools" / SCRIPT.name), *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_agent_bridge_contracts", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _registry(repo: Path, commit: str, blob: str) -> dict[str, Any]:
    http_contract = {
        "id": "agent-bridge.http-wire",
        "authority": "agent-bridge/http-wire",
        "owner": "agent-bridge",
        "kind": "wire-protocol",
        "normative": True,
        "declared_range": {
            "current": 12,
            "minimum": 1,
            "previous_generation": None,
            "previous_absent_reason": "Synthetic test registry has one generation.",
        },
        "evidence_window": {
            "generations": [12],
            "runtimes": ["1.0.0"],
        },
        "capability_versions": {
            "relay_interrupt": 2,
            "failed_acp_handshake": 3,
            "container_recreate": 4,
            "machine_metadata": 5,
            "result_snapshot": 6,
            "represented_result_snapshot": 7,
            "provider_target_refresh": 8,
            "at_rest_projection": 9,
            "attention_wait": 10,
            "remote_operations": 11,
            "conditional_idle_end": 12,
            "dispatch_task_session": 12,
        },
        "durable_records": [],
        "source_paths": [
            {
                "path": SOURCE,
                "sha256": _sha256(repo, SOURCE),
                "semantic": True,
                "non_semantic_reason": None,
            }
        ],
        "fixtures": [
            {
                "path": FIXTURE,
                "sha256": _sha256(repo, FIXTURE),
                "role": "current health",
                "generation": 12,
            }
        ],
        "provenance": [
            {
                "commit": commit,
                "plugin_version": "1.0.0",
                "generation": 12,
                "source_path": SOURCE,
                "source_git_blob": blob,
                "capture_method": "Read exact committed source.",
            }
        ],
        "support_window": "Generations 1 through 10.",
        "bridge_contract_rollback_window": "Retain generation 9.",
        "mixed_version_scenarios": ["old-client_new-daemon"],
        "removal_gate": "Prove zero references.",
    }
    host_blob = _git(repo, "rev-parse", f"{commit}:{HOST_SOURCE}")
    host_contract = {
        "id": "agent-bridge.session-host-wire",
        "authority": "agent-bridge/session-host-wire",
        "owner": "agent-bridge",
        "kind": "wire-protocol",
        "normative": True,
        "declared_range": {
            "current": 1,
            "minimum": 1,
            "previous_generation": None,
            "previous_absent_reason": "Generation 1 is the first envelope.",
        },
        "evidence_window": {
            "generations": [1],
            "runtimes": ["1.0.0"],
        },
        "capability_versions": {"length_prefixed_envelope": 1},
        "durable_records": [],
        "source_paths": [
            {
                "path": HOST_SOURCE,
                "sha256": _sha256(repo, HOST_SOURCE),
                "semantic": True,
                "non_semantic_reason": None,
            }
        ],
        "fixtures": [
            {
                "path": HOST_FIXTURE,
                "sha256": _sha256(repo, HOST_FIXTURE),
                "role": "current messages",
                "generation": 1,
            }
        ],
        "provenance": [
            {
                "commit": commit,
                "plugin_version": "1.0.0",
                "generation": 1,
                "source_path": HOST_SOURCE,
                "source_git_blob": host_blob,
                "capture_method": "Read exact committed source.",
            }
        ],
        "support_window": "Generation 1.",
        "bridge_contract_rollback_window": "Retain generation 1.",
        "mixed_version_scenarios": ["new-frontend_H1-host"],
        "removal_gate": "Prove zero references.",
    }
    return {
        "schema_version": 1,
        "contracts": [http_contract, host_contract],
        "deferred_contracts": [],
    }


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tools").mkdir(parents=True)
    (root / "tools" / SCRIPT.name).write_bytes(SCRIPT.read_bytes())
    (root / "tools" / BLOB_PROVENANCE_MODULE.name).write_bytes(
        BLOB_PROVENANCE_MODULE.read_bytes()
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "checkout", "-q", "-b", "main")

    _write(
        root,
        SOURCE,
        "\n".join(
            [
                "HTTP_PROTOCOL_VERSION = 12",
                "HTTP_PROTOCOL_MIN_SUPPORTED = 1",
                "RELAY_INTERRUPT_PROTOCOL_VERSION = 2",
                "FAILED_ACP_HANDSHAKE_PROTOCOL_VERSION = 3",
                "CONTAINER_RECREATE_PROTOCOL_VERSION = 4",
                "MACHINE_METADATA_PROTOCOL_VERSION = 5",
                "RESULT_SNAPSHOT_PROTOCOL_VERSION = 6",
                "REPRESENTED_RESULT_SNAPSHOT_PROTOCOL_VERSION = 7",
                "PROVIDER_TARGET_REFRESH_PROTOCOL_VERSION = 8",
                "AT_REST_PROJECTION_PROTOCOL_VERSION = 9",
                "ATTENTION_WAIT_PROTOCOL_VERSION = 10",
                "REMOTE_OPERATIONS_PROTOCOL_VERSION = 11",
                "CONDITIONAL_IDLE_END_PROTOCOL_VERSION = 12",
                "DISPATCH_TASK_SESSION_PROTOCOL_VERSION = 12",
                "",
            ]
        ),
    )
    _write(
        root,
        "plugins/agent-bridge/plugin.json",
        {"name": "agent-bridge", "version": "1.0.0"},
    )
    _write(root, HOST_SOURCE, "PROTOCOL_VERSION = 1\n")
    fixture = {
        "captured_from": {
            "commit": "pending",
            "plugin_version": "1.0.0",
            "protocol_generation": 12,
        },
        "response": {"status_code": 200},
    }
    _write(root, FIXTURE, fixture)
    host_fixture = {
        "captured_from": {
            "commit": "pending",
            "plugin_version": "1.0.0",
            "protocol_generation": 1,
        },
        "messages": {},
    }
    _write(root, HOST_FIXTURE, host_fixture)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "baseline source")
    commit = _git(root, "rev-parse", "HEAD")
    blob = _git(root, "rev-parse", f"{commit}:{SOURCE}")

    fixture["captured_from"]["commit"] = commit
    fixture["captured_from"]["source_path"] = SOURCE
    fixture["captured_from"]["source_git_blob"] = blob
    fixture["captured_from"]["source_sha256"] = _sha256(root, SOURCE)
    _write(root, FIXTURE, fixture)
    host_blob = _git(root, "rev-parse", f"{commit}:{HOST_SOURCE}")
    host_fixture["captured_from"].update(
        {
            "commit": commit,
            "source_path": HOST_SOURCE,
            "source_git_blob": host_blob,
            "source_sha256": _sha256(root, HOST_SOURCE),
        }
    )
    _write(root, HOST_FIXTURE, host_fixture)
    schema_path = root / "plugins/agent-bridge/contract/registry.schema.json"
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_bytes(SCHEMA.read_bytes())
    _write(
        root,
        "plugins/agent-bridge/contract/registry.json",
        _registry(root, commit, blob),
    )
    return root


def _mutate_registry(
    repo: Path,
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    path = repo / "plugins/agent-bridge/contract/registry.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    mutation(data)
    _write(repo, "plugins/agent-bridge/contract/registry.json", data)


def _mutate_fixture(
    repo: Path,
    relative: str,
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    path = repo / relative
    data = json.loads(path.read_text(encoding="utf-8"))
    mutation(data)
    _write(repo, relative, data)
    # The fixture file's own bytes just changed -- keep registry.json's
    # fixtures[].sha256 entry (a whole-file hash, separate from the
    # captured_from.source_sha256 this helper's callers are usually after)
    # in sync, or every call would also need to fix a stale-fixture-hash
    # error unrelated to what it's actually testing.
    new_hash = _sha256(repo, relative)

    def resync(registry: dict[str, Any]) -> None:
        for contract in registry["contracts"]:
            for entry in contract.get("fixtures", []):
                if entry.get("path") == relative:
                    entry["sha256"] = new_hash

    _mutate_registry(repo, resync)


def test_valid_registry_passes(repo: Path) -> None:
    result = _run(repo)
    assert result.returncode == 0, result.stderr
    assert "OK (2 contracts, 2 fixtures)" in result.stdout


def test_capability_constant_mismatch_fails(repo: Path) -> None:
    """The capability-versions cross-check (``_HTTP_CAPABILITY_CONSTANTS``)
    must actually catch a registry value that disagrees with the production
    constant -- proven here against ``dispatch_task_session``, the capability
    this test file previously left unexercised (a wrong or missing mapping
    would otherwise silently compare ``None`` to ``None``)."""

    def mutation(data: dict[str, Any]) -> None:
        http_contract = next(
            c for c in data["contracts"] if c["id"] == "agent-bridge.http-wire"
        )
        http_contract["capability_versions"]["dispatch_task_session"] = 999

    _mutate_registry(repo, mutation)
    result = _run(repo)
    assert result.returncode != 0
    assert "capability dispatch_task_session does not match" in result.stderr


def test_missing_provenance_commit_recovers_history_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = _load_checker()
    commit = "a" * 40
    calls: list[tuple[str, ...]] = []
    state = {"available": False}

    def fake_git(*args: str):
        calls.append(args)
        if args[:2] == ("cat-file", "-e"):
            return subprocess.CompletedProcess(args, 0 if state["available"] else 1, "", "")
        if args == (
            "fetch",
            "--quiet",
            "origin",
            checker._MAIN_REFSPEC,
        ):
            state["available"] = True
            return subprocess.CompletedProcess(args, 0, "", "")
        if args in {
            ("fetch", "--quiet", "--unshallow", "origin"),
        }:
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(checker, "_git", fake_git)
    checker._FETCH_RECOVERY_ATTEMPTED = False

    assert checker._ensure_commit_available(commit) is True
    assert checker._ensure_commit_available(commit) is True
    assert calls.count(
        ("fetch", "--quiet", "origin", checker._MAIN_REFSPEC)
    ) == 1


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda data: data.__setitem__("schema_version", 2), "only version 1"),
        (
            lambda data: data["contracts"][0].pop("owner"),
            "missing fields: owner",
        ),
        (
            lambda data: data["contracts"][0].__setitem__("owner", None),
            "owner: must be a non-empty string",
        ),
        (
            lambda data: data["contracts"][0]["fixtures"][0].__setitem__(
                "sha256", "not-a-hash"
            ),
            "sha256 must be 64 lowercase",
        ),
        (
            lambda data: data["contracts"][0].__setitem__("provenance", []),
            "provenance: must be a non-empty array",
        ),
    ],
)
def test_malformed_registry_fails_deterministically(
    repo: Path,
    mutation: Callable[[dict[str, Any]], None],
    expected: str,
) -> None:
    _mutate_registry(repo, mutation)
    first = _run(repo)
    second = _run(repo)
    assert first.returncode == 1
    assert first.stderr == second.stderr
    assert expected in first.stderr


def test_duplicate_id_and_authority_fail(repo: Path) -> None:
    def duplicate(data: dict[str, Any]) -> None:
        data["contracts"].append(copy.deepcopy(data["contracts"][0]))

    _mutate_registry(repo, duplicate)
    result = _run(repo)
    assert result.returncode == 1
    assert "duplicate contract id" in result.stderr
    assert "duplicate authority" in result.stderr


def test_invalid_required_contract_id_cannot_bypass_checks(repo: Path) -> None:
    def rename(data: dict[str, Any]) -> None:
        data["contracts"][0]["id"] = "INVALID ID"

    _mutate_registry(repo, rename)
    result = _run(repo)
    assert result.returncode == 1
    assert "must match ^[a-z0-9][a-z0-9.-]+$" in result.stderr
    assert "missing required protocol contracts: agent-bridge.http-wire" in result.stderr


def test_external_reference_requires_source_and_hash(repo: Path) -> None:
    def add_reference(data: dict[str, Any]) -> None:
        data["deferred_contracts"].append(
            {
                "id": "external.example",
                "owner": "#1",
                "classification": "externally-owned",
                "tracked_issue": "https://example.com/issues/1",
                "reason": "Owned elsewhere.",
            }
        )

    _mutate_registry(repo, add_reference)
    result = _run(repo)
    assert result.returncode == 1
    assert "externally-owned references require" in result.stderr


def test_deep_schema_corruption_fails(repo: Path) -> None:
    path = repo / "plugins/agent-bridge/contract/registry.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    schema["$defs"]["fixture"]["required"].remove("generation")
    _write(repo, "plugins/agent-bridge/contract/registry.schema.json", schema)

    result = _run(repo)
    assert result.returncode == 1
    assert "schema.$defs.fixture: required fields do not match" in result.stderr


def test_missing_fixture_fails(repo: Path) -> None:
    (repo / FIXTURE).unlink()
    result = _run(repo)
    assert result.returncode == 1
    assert f"missing file {FIXTURE}" in result.stderr


def test_fixture_path_escape_fails(repo: Path) -> None:
    _write(repo, "outside.json", {"value": 1})

    def escape(data: dict[str, Any]) -> None:
        fixture = data["contracts"][0]["fixtures"][0]
        fixture["path"] = "outside.json"
        fixture["sha256"] = _sha256(repo, "outside.json")

    _mutate_registry(repo, escape)
    result = _run(repo)
    assert result.returncode == 1
    assert "path escapes plugins/agent-bridge/contract" in result.stderr


def test_changed_registered_source_requires_registry_update(repo: Path) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "registry baseline")
    base = _git(repo, "rev-parse", "HEAD")
    source = repo / SOURCE
    source.write_text(source.read_text(encoding="utf-8") + "NEW_FIELD = 11\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change contract source only")

    result = _run(repo, "--base", base)
    assert result.returncode == 1
    assert "registered contract source changed without updating" in result.stderr
    assert SOURCE in result.stderr


def test_git_reads_ignore_contaminated_ambient_environment(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_DIR", str(repo / "wrong.git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo / "wrong-worktree"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.bare")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(repo / "missing-global-config"))

    result = _run(repo)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "check-agent-bridge-contracts: OK (2 contracts, 2 fixtures)\n"


def test_semantic_source_hash_cannot_advance_without_fixture(repo: Path) -> None:
    source = repo / SOURCE
    source.write_text(
        source.read_text(encoding="utf-8") + "NEW_FIELD = 11\n",
        encoding="utf-8",
    )

    def refresh_hash_only(data: dict[str, Any]) -> None:
        data["contracts"][0]["source_paths"][0]["sha256"] = _sha256(repo, SOURCE)

    _mutate_registry(repo, refresh_hash_only)
    result = _run(repo)
    assert result.returncode == 1
    assert "semantic current sources lack matching current fixtures" in result.stderr
    assert SOURCE in result.stderr


def test_provenance_passes_with_an_orphan_commit_but_reachable_blob(
    repo: Path,
) -> None:
    """A provenance entry's ``commit`` need not be resolvable at all, as
    long as its ``source_git_blob`` still exists -- exactly the shape a
    squash-merge-orphaned commit leaves behind once its content has
    (identically) landed via some other, real commit. Also proves the
    plugin_version cross-check correctly degrades to unverified (not a hard
    error) when the commit itself can't be read at all."""
    orphan_commit = "f" * 40

    def mutation(data: dict[str, Any]) -> None:
        http_contract = next(
            c for c in data["contracts"] if c["id"] == "agent-bridge.http-wire"
        )
        # A commit SHA that was never created in this repo at all.
        http_contract["provenance"][0]["commit"] = orphan_commit

    _mutate_registry(repo, mutation)

    def fixture_mutation(data: dict[str, Any]) -> None:
        data["captured_from"]["commit"] = orphan_commit

    _mutate_fixture(repo, FIXTURE, fixture_mutation)
    result = _run(repo)
    assert result.returncode == 0, result.stderr


def test_provenance_rejects_a_non_blob_source_git_blob(repo: Path) -> None:
    """A valid-looking SHA that resolves to a tree (or any non-blob object)
    must fail closed, not be silently accepted as content-address proof."""
    commit = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}")

    def mutation(data: dict[str, Any]) -> None:
        http_contract = next(
            c for c in data["contracts"] if c["id"] == "agent-bridge.http-wire"
        )
        http_contract["provenance"][0]["source_git_blob"] = tree

    _mutate_registry(repo, mutation)
    result = _run(repo)
    assert result.returncode != 0
    assert "does not exist as a git blob object" in result.stderr


def test_provenance_rejects_a_reachable_commit_with_unreadable_plugin_manifest(
    repo: Path,
) -> None:
    """A *reachable* commit whose ``plugin.json`` can't be read/parsed must
    still be a hard error -- the commit is available, so there's no excuse
    not to verify plugin_version; only a genuinely unreachable commit
    degrades this check to unverified."""
    (repo / "plugins/agent-bridge/plugin.json").write_text("not json", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "corrupt plugin manifest")
    broken_commit = _git(repo, "rev-parse", "HEAD")

    def mutation(data: dict[str, Any]) -> None:
        http_contract = next(
            c for c in data["contracts"] if c["id"] == "agent-bridge.http-wire"
        )
        # Same source content, now cited at a commit whose plugin.json is
        # unreadable -- the blob itself is untouched by this second commit.
        http_contract["provenance"][0]["commit"] = broken_commit

    _mutate_registry(repo, mutation)

    def fixture_mutation(data: dict[str, Any]) -> None:
        data["captured_from"]["commit"] = broken_commit

    _mutate_fixture(repo, FIXTURE, fixture_mutation)
    result = _run(repo)
    assert result.returncode != 0
    assert "is available but its plugin.json is missing or unparseable" in result.stderr




def test_provenance_rejects_an_available_commit_pointing_at_a_different_blob(
    repo: Path,
) -> None:
    """When the commit IS available, the exact commit:source_path check
    takes priority over the history-reachability fallback: a blob that is
    reachable somewhere in history but doesn't actually match this specific
    commit's tree must still be rejected, not waved through just because it
    once existed somewhere."""
    source = repo / SOURCE
    source.write_text(
        source.read_text(encoding="utf-8") + "# unrelated later edit\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "unrelated later source edit")
    later_commit = _git(repo, "rev-parse", "HEAD")
    original_blob = _git(repo, "rev-parse", f"HEAD~1:{SOURCE}")

    def mutation(data: dict[str, Any]) -> None:
        http_contract = next(
            c for c in data["contracts"] if c["id"] == "agent-bridge.http-wire"
        )
        # Commit is available and genuinely touched this path -- but its
        # tree now has different content than the still-reachable blob
        # recorded here.
        http_contract["provenance"][0]["commit"] = later_commit

    _mutate_registry(repo, mutation)

    def fixture_mutation(data: dict[str, Any]) -> None:
        data["captured_from"]["commit"] = later_commit

    _mutate_fixture(repo, FIXTURE, fixture_mutation)
    result = _run(repo)
    assert result.returncode != 0
    assert f"source_git_blob is {original_blob}, actual" in result.stderr


def test_blob_exists_benefits_from_commit_availability_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_ensure_commit_available``'s own fetch/unshallow recovery must run
    -- and have a chance to pull in the needed objects -- before a blob is
    ever declared missing. In a shallow checkout, an older blob can
    genuinely be absent until that recovery runs; checking blob existence
    first (before ensuring the commit is available) would record a
    permanent error even though the commit path's own recovery would have
    found it."""
    checker = _load_checker()
    commit = "b" * 40
    blob = "c" * 40
    state = {"recovered": False}

    def fake_git(*args: str):
        if args[:2] == ("cat-file", "-e") and args[2] == f"{commit}^{{commit}}":
            return subprocess.CompletedProcess(args, 0 if state["recovered"] else 1, "", "")
        if args == ("fetch", "--quiet", "origin", checker._MAIN_REFSPEC):
            state["recovered"] = True
            return subprocess.CompletedProcess(args, 0, "", "")
        if args in {("fetch", "--quiet", "--unshallow", "origin")}:
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected git call: {args}")

    def fake_blob_exists(repo, env, b):
        # The blob only becomes resolvable once the commit-availability
        # recovery has actually run -- exactly the shallow-checkout shape
        # this guards against.
        return state["recovered"]

    monkeypatch.setattr(checker, "_git", fake_git)
    monkeypatch.setattr(checker.blob_provenance, "blob_exists", fake_blob_exists)
    checker._FETCH_RECOVERY_ATTEMPTED = False

    assert checker._ensure_commit_available(commit) is True
    assert checker._blob_exists(blob) is True


def test_fixture_source_sha256_normalizes_crlf_in_blob_content(repo: Path) -> None:
    """The registry's documented fingerprint definition normalizes CRLF/CR
    to LF before hashing (contract text is line-ending agnostic) -- a blob
    containing CRLF must still agree with its normalized source_sha256,
    not fail because the raw bytes differ from the canonicalized hash."""
    source = repo / SOURCE
    crlf_content = source.read_text(encoding="utf-8").replace("\n", "\r\n")
    source.write_bytes(crlf_content.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "CRLF line endings")
    crlf_commit = _git(repo, "rev-parse", "HEAD")
    crlf_blob = _git(repo, "rev-parse", f"HEAD:{SOURCE}")
    normalized_sha256 = _sha256(repo, SOURCE)

    def mutation(data: dict[str, Any]) -> None:
        http_contract = next(
            c for c in data["contracts"] if c["id"] == "agent-bridge.http-wire"
        )
        http_contract["provenance"][0]["commit"] = crlf_commit
        http_contract["provenance"][0]["source_git_blob"] = crlf_blob
        http_contract["source_paths"][0]["sha256"] = normalized_sha256

    _mutate_registry(repo, mutation)

    def fixture_mutation(data: dict[str, Any]) -> None:
        data["captured_from"]["commit"] = crlf_commit
        data["captured_from"]["source_git_blob"] = crlf_blob
        data["captured_from"]["source_sha256"] = normalized_sha256

    _mutate_fixture(repo, FIXTURE, fixture_mutation)
    result = _run(repo)
    assert result.returncode == 0, result.stderr
