"""Script-backed backlog-provider adapter (the ``script`` forge provider).

Implements ``repository_issue_loops.ForgeProvider`` by invoking a declared
script via subprocess for each of the four backlog operations
(``list_open_issues``/``reserve``/``claim``/``release``) -- the first
concrete realization of the vision's *extend-any-declaration* script-path-
hook model (``visions/plugins/agent-dispatch/README.md``):
``repository_issue_loop``'s own scheduling/lease/quiet-period/dedup
machinery (``repository_issue_loops.py``) is reused completely unchanged;
the script supplies only the domain-specific backlog source, never the loop
shape. Split into its own module purely to stay under this repo's
module-size cap, mirroring ``gitea_provider_stub.py``'s own precedent.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import posixpath
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from .issue_loop_markers import validate_marker_payload
from .registrar import RegistrarError

if TYPE_CHECKING:
    from .repository_issue_loops import Issue

#: ``forge`` keys meaningful only to ``forge.provider: script``.
SCRIPT_FORGE_KEYS = frozenset({"command", "cwd", "timeout_seconds", "namespace"})

#: Matches ``ScriptEvaluator``'s own default (``producers/evaluator.py``) --
#: the established local precedent for a trusted script invocation's bound.
DEFAULT_TIMEOUT_SECONDS = 30.0

#: Matches ``ScriptEvaluator``'s own ``MAX_SCRIPT_EVALUATOR_TIMEOUT``: an
#: excessively large but technically-finite value (e.g. ``1e308``) still
#: reaches ``Popen.communicate()`` and raises ``OverflowError`` before
#: waiting at all -- a practical upper bound is required, not just finiteness.
MAX_TIMEOUT_SECONDS = 1800.0


class ScriptProviderError(RegistrarError):
    """A script-backed backlog provider failed, timed out, or returned a
    malformed response."""


def _portable_script_prefix(script_path: str) -> list[str]:
    """Prefix a resolved script path with the interpreter/shell its suffix
    requires so it actually executes on Windows (where `CreateProcess`
    cannot run a `.py`/`.sh`/`.ps1` file directly, unlike POSIX's shebang
    support) -- mirrors ``companion.py``'s own established script-path
    resolver (`_resolve_companion_argv`) for the same three suffixes."""
    path = Path(script_path)
    suffix = path.suffix.casefold()
    if suffix == ".py":
        return [sys.executable, str(path)]
    if suffix == ".sh":
        shell = shutil.which("bash")
        if not shell:
            raise RegistrarError(
                "repository-issue-loop forge.command: a '.sh' script requires bash"
            )
        return [shell, str(path)]
    if suffix == ".ps1":
        shell = shutil.which("pwsh")
        if shell is None and os.name == "nt":
            fallback = (
                Path(os.environ.get("SystemRoot", r"C:\Windows"))
                / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            )
            shell = str(fallback) if fallback.is_file() else None
        if not shell:
            raise RegistrarError(
                "repository-issue-loop forge.command: a '.ps1' script requires PowerShell"
            )
        return [shell, "-NoProfile", "-NonInteractive", "-File", str(path)]
    return [str(path)]


def _normalize_declared_path(path: str) -> str:
    """Collapse spelling variants of the same relative/absolute path
    (``./scripts/poller.py`` vs ``scripts/poller.py``) to one canonical
    form, so the same backlog declared with cosmetically different path
    spelling still dedups -- forward-slash-normalized first so a
    declaration authored with Windows-style separators canonicalizes the
    same way everywhere."""
    return posixpath.normpath(path.replace("\\", "/"))


def _declaring_repo_identity(repo_root: str | Path | None) -> str:
    """A stable, cross-host identity for the declaring repository, used by
    ``script_resource_namespace`` so two *unrelated* repositories that
    happen to declare the same relative script path/producer_login/repo
    label do not collide on the coordinator's resource key. Delegates to
    the established, scrubbed-environment git-remote probe
    (``registrar_lane_aliases._derive_git_remote_alias`` -- clears ambient
    `GIT_DIR`/`GIT_CONFIG_KEY_*`/etc. that could otherwise redirect the
    probe to an unrelated repository despite an explicit ``-C``, and
    refuses to trust an *ancestor* repo's remote when ``repo_root`` isn't
    itself a git toplevel) for the canonicalized `origin` remote --
    device- and protocol-independent, stable across machines/checkouts.
    Falls back to the repo root's own absolute path when no git remote is
    resolvable (same-host collision avoidance only -- a non-git-remote
    checkout has no stronger cross-host identity available here)."""
    if repo_root is None:
        return ""
    root = Path(repo_root).expanduser().resolve()
    from .registrar_lane_aliases import _derive_git_remote_alias

    remote = _derive_git_remote_alias(root)
    return remote or str(root)


def validate_script_forge_config(
    forge: Mapping[str, Any],
    *,
    provider: Any,
    repo_root: str | Path | None = None,
) -> dict[str, Any] | None:
    """Validate the ``script``-only ``forge.command``/``forge.cwd``/
    ``forge.timeout_seconds`` fields, mirroring
    ``ado_discovery_scope.validate_discovery_scope``'s own provider-gating
    pattern: present but the wrong provider is a hard error (never a silent
    no-op); required but absent for ``provider == "script"`` is equally a
    hard error. Returns ``None`` when none of these fields apply (any
    non-``script`` provider with none of them set).

    ``command`` (its first element, the script path) and ``cwd``, when
    relative, resolve against ``repo_root`` -- the declaring repo,
    threaded the same way ``repository_issue_loop``'s own
    ``worker_identity`` already is (``validate_config``'s own ``cwd``
    parameter) -- never the daemon process's own incidental working
    directory, since the subprocess call runs synchronously inside each
    scheduled occurrence. The resolved script path is then prefixed with
    whatever interpreter/shell its suffix requires
    (``_portable_script_prefix``), so a `.py`/`.sh`/`.ps1` script declared
    without its own executable bit still runs on a platform (Windows) that
    cannot exec it directly.
    """
    present = {key: forge[key] for key in SCRIPT_FORGE_KEYS if key in forge}
    if provider != "script":
        if present:
            raise RegistrarError(
                "repository-issue-loop forge.command/cwd/timeout_seconds: "
                "only supported for forge.provider 'script'"
            )
        return None
    command = forge.get("command")
    if (
        not isinstance(command, (list, tuple))
        or not command
        or not all(isinstance(part, str) and part for part in command)
    ):
        raise RegistrarError(
            "repository-issue-loop forge.command: required for "
            "forge.provider 'script' -- expected a non-empty list of "
            "non-empty strings"
        )
    base = Path(repo_root).expanduser().resolve() if repo_root is not None else None
    resolved_command = [str(part) for part in command]
    script_path = Path(resolved_command[0])
    if not script_path.is_absolute():
        if base is None:
            raise RegistrarError(
                "repository-issue-loop forge.command: a relative script path "
                "requires a known declaring repo root (unreachable from a "
                "direct declaration read with no resolvable registrar "
                "context) -- use an absolute path instead of depending on "
                "the daemon process's own incidental working directory"
            )
        resolved_command[0] = str((base / script_path).resolve())
    script_path_resolved = resolved_command[0]
    resolved_command = _portable_script_prefix(resolved_command[0]) + resolved_command[1:]

    cwd = forge.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not cwd):
        raise RegistrarError(
            "repository-issue-loop forge.cwd: expected a non-empty string"
        )
    if base is None:
        if cwd is None or not Path(cwd).is_absolute():
            raise RegistrarError(
                "repository-issue-loop forge.cwd: requires an absolute path "
                "when no declaring repo root is known -- an unset or "
                "relative value would otherwise depend on the daemon "
                "process's own incidental working directory"
            )
        resolved_cwd = str(Path(cwd).resolve())
    else:
        resolved_cwd = cwd
        if cwd is not None:
            cwd_path = Path(cwd)
            if not cwd_path.is_absolute():
                resolved_cwd = str((base / cwd_path).resolve())
            else:
                resolved_cwd = str(cwd_path.resolve())
        else:
            # Unset `forge.cwd` still anchors to the declaring repo root,
            # never the daemon process's own incidental cwd -- the same
            # rule as `command`'s own default resolution.
            resolved_cwd = str(base)

    timeout = forge.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise RegistrarError(
            "repository-issue-loop forge.timeout_seconds: expected a number"
        )
    timeout_error = RegistrarError(
        "repository-issue-loop forge.timeout_seconds: expected a finite "
        f"number > 0 and <= {MAX_TIMEOUT_SECONDS:g}"
    )
    try:
        timeout = float(timeout)
    except OverflowError:
        # An arbitrarily large JSON/YAML integer (e.g. far beyond any
        # float's range) raises OverflowError on conversion rather than
        # producing a non-finite float -- treat it the same as any other
        # out-of-range timeout instead of letting the conversion itself
        # crash validation.
        raise timeout_error from None
    if timeout <= 0 or timeout > MAX_TIMEOUT_SECONDS or not math.isfinite(timeout):
        raise timeout_error
    namespace = forge.get("namespace")
    if namespace is not None and (not isinstance(namespace, str) or not namespace):
        raise RegistrarError(
            "repository-issue-loop forge.namespace: expected a non-empty string"
        )
    declared_command = [str(part) for part in command]
    declared_command[0] = _normalize_declared_path(declared_command[0])
    declared_cwd = _normalize_declared_path(cwd) if cwd else cwd
    return {
        "command": resolved_command,
        "cwd": resolved_cwd,
        "timeout_seconds": timeout,
        "namespace": namespace,
        # Internal-only markers for `script_resource_namespace` (never a
        # recognized schema key, ignored by anything else that reads
        # `forge`): `_script_path` is the plain resolved script path,
        # pre-interpreter-wrap -- `command[0]` after wrapping is the
        # interpreter/shell (e.g. `sys.executable`), not a usable
        # namespace. `_declared_command`/`_declared_cwd` are the
        # as-authored (pre-resolution), *path-normalized* values -- the
        # namespace must be a stable declaration-level identity
        # independent of both the machine-local absolute paths
        # `command`/`cwd` resolve to (a different checkout root or Python
        # installation on another host must not change it) and of
        # cosmetic spelling (`./poller.py` vs `poller.py` is the same
        # backlog). `_declared_repo_identity` additionally distinguishes
        # two *unrelated* repositories that happen to declare the same
        # relative path/producer_login/repo label -- but it is only
        # best-effort (a git remote probe re-run every tick), so an
        # explicit `forge.namespace` always takes precedence when set.
        "_script_path": script_path_resolved,
        "_declared_command": declared_command,
        "_declared_cwd": declared_cwd,
        "_declared_repo_identity": _declaring_repo_identity(repo_root),
    }


def _issue_to_dict(issue: "Issue") -> dict[str, Any]:
    return {
        "number": issue.number,
        "title": issue.title,
        "url": issue.url,
        "labels": list(issue.labels),
        "created_at": issue.created_at,
        "updated_at": issue.updated_at,
        "reservations": list(issue.reservations),
    }


def _validate_issue_payload(raw: Any) -> "Issue":
    """Strictly type-check a script response's issue object before
    constructing ``Issue`` -- `int(1.5)`/`str(None)` would otherwise
    silently coerce a wrong-typed field instead of raising, a string
    `labels` would iterate per-character, and a non-mapping `reservations`
    entry would crash downstream (`_latest_reservations`'s own `.get()`
    calls) instead of failing here with a clear error."""
    from .repository_issue_loops import Issue

    def _fail(reason: str) -> NoReturn:
        raise ScriptProviderError(
            f"script provider list_open_issues: malformed issue entry: {reason}"
        )

    if not isinstance(raw, Mapping):
        _fail("each issue must be a JSON object")
    number = raw.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        _fail("'number' must be an integer")
    title = raw.get("title")
    if not isinstance(title, str):
        _fail("'title' must be a string")
    url = raw.get("url")
    if not isinstance(url, str):
        _fail("'url' must be a string")
    labels = raw.get("labels", ())
    if not isinstance(labels, (list, tuple)) or not all(
        isinstance(label, str) for label in labels
    ):
        _fail("'labels' must be a list of strings")
    reservations = raw.get("reservations", ())
    if not isinstance(reservations, (list, tuple)) or not all(
        isinstance(reservation, Mapping) for reservation in reservations
    ):
        _fail("'reservations' must be a list of objects")
    validated_reservations = []
    for reservation in reservations:
        validated = validate_marker_payload(dict(reservation), issue_number=number)
        if validated is None:
            _fail(
                "each 'reservations' entry must match the "
                "loop/occurrence/state/at/label/issue/task_id/reason marker schema"
            )
        validated_reservations.append(validated)
    timestamps = {}
    for key in ("created_at", "updated_at"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _fail(f"{key!r} must be a finite number")
        if isinstance(value, float) and not math.isfinite(value):
            _fail(f"{key!r} must be a finite number")
        try:
            timestamps[key] = float(value)
        except OverflowError:
            # An arbitrarily large JSON integer is itself a "finite" int
            # but overflows on conversion to float -- treat it the same
            # as any other malformed timestamp instead of letting the
            # conversion itself crash discovery.
            _fail(f"{key!r} must be a finite number")
    return Issue(
        number=number,
        title=title,
        url=url,
        labels=tuple(labels),
        created_at=timestamps["created_at"],
        updated_at=timestamps["updated_at"],
        reservations=tuple(validated_reservations),
    )


class ScriptProvider:
    """A backlog provider backed by a declared, repo-packaged script.

    The script is invoked once per operation, named via a trailing
    ``--op <name>`` argument, with a structured JSON request object on
    stdin and expected to print a structured JSON response object on
    stdout. A non-zero exit is always a real error (the script's stderr is
    surfaced verbatim, truncated); a timeout or a malformed/non-JSON
    stdout are equally real errors, never silently treated as an empty
    success.

    Uses ``Popen`` + :func:`terminate_process_tree`
    (``agent_dispatch.procutil``) rather than a bare
    ``subprocess.run(timeout=)``: the latter's ``TimeoutExpired`` handling
    kills only the immediate child, but a venv ``python.exe`` launcher
    re-execs the base interpreter as a NEW child process on Windows (no
    true ``exec`` there) -- so a bare timeout kill leaks that re-exec'd
    grandchild running indefinitely, same leak class already documented
    and fixed for ``run_background_capture`` (``procutil.py``).
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        producer_login: str | None = None,
        popen: Callable[..., Any] = subprocess.Popen,
    ):
        if (
            isinstance(command, (str, bytes))
            or not command
            or not all(isinstance(part, str) and part for part in command)
        ):
            raise ValueError("command must be a non-empty list of non-empty strings")
        self.command = [str(part) for part in command]
        self.cwd = cwd
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("timeout_seconds must be a number")
        timeout_error = ValueError(
            f"timeout_seconds must be a finite number > 0 and <= {MAX_TIMEOUT_SECONDS:g}"
        )
        try:
            timeout_seconds = float(timeout_seconds)
        except OverflowError:
            raise timeout_error from None
        if (
            timeout_seconds <= 0
            or timeout_seconds > MAX_TIMEOUT_SECONDS
            or not math.isfinite(timeout_seconds)
        ):
            raise timeout_error
        self.timeout_seconds = timeout_seconds
        self.producer_login = producer_login
        self._popen = popen

    def _invoke(self, op: str, request: Mapping[str, Any]) -> dict[str, Any]:
        from .procutil import _process_tree_kwargs, terminate_process_tree

        body: dict[str, Any] = dict(request)
        if self.producer_login is not None:
            body.setdefault("producer_login", self.producer_login)
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        argv = [*self.command, "--op", op]
        try:
            proc = self._popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                shell=False,
                cwd=self.cwd,
                **_process_tree_kwargs(),
            )
        except OSError as exc:
            raise ScriptProviderError(
                f"script provider op {op!r} failed to start: {exc}"
            ) from exc
        from .companion import process_start_token

        proc_pid = getattr(proc, "pid", None)
        start_token = process_start_token(proc_pid) if isinstance(proc_pid, int) else None
        try:
            stdout, stderr = proc.communicate(input=payload, timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            terminate_process_tree(proc, expected_start_token=start_token)
            raise ScriptProviderError(
                f"script provider op {op!r} timed out after "
                f"{self.timeout_seconds:.1f}s"
            ) from None
        except OverflowError as exc:
            # An out-of-range timeout_seconds reaches the platform wait
            # call before any waiting happens -- the constructor's own
            # MAX_TIMEOUT_SECONDS bound should already prevent this, but
            # the process is still running and must still be reaped.
            terminate_process_tree(proc, expected_start_token=start_token)
            raise ScriptProviderError(
                f"script provider op {op!r}: timeout_seconds "
                f"{self.timeout_seconds!r} is out of range: {exc}"
            ) from exc
        except UnicodeError as exc:
            raise ScriptProviderError(
                f"script provider op {op!r} produced output that is not "
                f"valid UTF-8: {exc}"
            ) from exc
        if proc.returncode != 0:
            stderr = str(stderr or "").strip()[:400]
            raise ScriptProviderError(
                f"script provider op {op!r} exited {proc.returncode}: {stderr}"
            )
        stdout = str(stdout or "").strip()
        if not stdout:
            return {}
        try:
            response = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ScriptProviderError(
                f"script provider op {op!r} produced invalid JSON on stdout: {exc}"
            ) from exc
        if not isinstance(response, dict):
            raise ScriptProviderError(
                f"script provider op {op!r} response must be a JSON object, "
                f"got {type(response).__name__}"
            )
        return response

    def list_open_issues(self, repo: str) -> list["Issue"]:
        response = self._invoke("list_open_issues", {"repo": repo})
        raw_issues = response.get("issues")
        if not isinstance(raw_issues, list):
            raise ScriptProviderError(
                "script provider list_open_issues: response must have an "
                "'issues' list"
            )
        issues = [_validate_issue_payload(raw) for raw in raw_issues]
        seen: set[int] = set()
        for issue in issues:
            if issue.number in seen:
                raise ScriptProviderError(
                    "script provider list_open_issues: duplicate issue "
                    f"number {issue.number} in response -- the loop treats "
                    "'number' as the unique resource-owner identity"
                )
            seen.add(issue.number)
        return issues

    def reserve(self, repo: str, issue: "Issue", reservation: dict[str, Any]) -> None:
        self._invoke(
            "reserve",
            {"repo": repo, "issue": _issue_to_dict(issue), "reservation": reservation},
        )

    def claim(
        self, repo: str, issue: "Issue", reservation: dict[str, Any], task_id: str
    ) -> None:
        self._invoke(
            "claim",
            {
                "repo": repo,
                "issue": _issue_to_dict(issue),
                "reservation": reservation,
                "task_id": task_id,
            },
        )

    def release(
        self,
        repo: str,
        issue: "Issue",
        reservation: dict[str, Any],
        reason: str,
    ) -> None:
        self._invoke(
            "release",
            {
                "repo": repo,
                "issue": _issue_to_dict(issue),
                "reservation": reservation,
                "reason": reason,
            },
        )


def validate_repo_field(repo: Any, *, provider: Any) -> None:
    """Validate ``repository-issue-loop``'s own ``repo`` field shape --
    ``'owner/name'`` for a forge-shaped provider, any non-empty string for
    ``script`` (the script interprets ``repo`` itself; the motivating
    consumer's own case has no forge-shaped identifier at all). Kept
    here, not in ``repository_issue_loops.py``, purely to stay under that
    module's size cap."""
    import re

    if provider == "script":
        if not isinstance(repo, str) or not repo.strip():
            raise RegistrarError(
                "repository-issue-loop repo: expected a non-empty string"
            )
        return
    if not isinstance(repo, str) or not re.fullmatch(r"[^/\s]+/[^/\s]+", repo):
        raise RegistrarError(
            "repository-issue-loop repo: expected 'owner/name' (GitHub) or "
            "'organization/project' (Azure DevOps)"
        )


def script_resource_namespace(forge: Mapping[str, Any]) -> str:
    """The `script` provider's explicit per-declaration coordinator
    namespace, used by `_resource_key` (``repository_issue_loops.py``).
    Unlike a real `owner/name` or `organization/project`, a script
    backlog's own `repo` is an arbitrary provider-local label with no
    inherent global-uniqueness guarantee -- two unrelated script
    declarations could innocently pick the same `repo` string and collide
    on the coordinator's resource key.

    An explicit `forge.namespace` (adopter-chosen, stable by
    construction) always wins when set -- the only guarantee strong
    enough for a correctness-critical, redundant/failover deployment:
    everything else here is inherently **best-effort**, since
    `_declared_repo_identity` is a live git-remote probe re-derived fresh
    on every tick (`validate_script_forge_config` runs at discovery/each
    tick, not once at registration) and can transiently fall back to the
    machine-local repo-root path if that probe ever fails, silently
    changing the namespace between ticks or across redundant hosts.
    Adopters with a genuine cross-host/failover deployment should set
    `forge.namespace` explicitly rather than relying on auto-derivation.

    Absent an explicit namespace, hashes the *as-declared,
    path-normalized* command/cwd (``_declared_command``/``_declared_cwd``,
    ``validate_script_forge_config``'s own markers -- already collapsed to
    one canonical spelling, e.g. `./poller.py` and `poller.py` hash
    identically) together with `producer_login` and
    `_declared_repo_identity` (the declaring repository's own
    canonicalized git remote, or its absolute path when no remote is
    resolvable) -- never the machine-resolved absolute `command`/`cwd` or
    `_script_path`: the same logical declaration (the same repo checkout,
    run on a second host for redundancy/failover, or under a different
    local Python installation) should still dedup against itself through
    the coordinator in the common case, which a machine-local absolute
    path would break by producing a different namespace per host.
    `producer_login` is included because it is itself forwarded to the
    script and can select a distinct backlog even when `command`/`cwd`
    are otherwise identical; `_declared_repo_identity` keeps two
    *unrelated* repositories that happen to declare the same relative
    path/producer_login/repo label from colliding. Preserves filesystem
    case semantics throughout (never casefolds) since case-sensitive
    filesystems distinguish paths differing only by case. Falls back to
    the resolved `command`/`cwd` (and an empty repo identity) when the
    declared markers are absent (direct construction bypassing
    ``validate_script_forge_config``)."""
    namespace = forge.get("namespace")
    if isinstance(namespace, str) and namespace:
        return hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:16]
    declared_command = forge.get("_declared_command")
    if declared_command is None:
        declared_command = forge.get("command") or ()
    command = [str(part) for part in declared_command]
    cwd = forge.get("_declared_cwd", forge.get("cwd"))
    identity = "\x1e".join(
        [
            *command,
            str(cwd or ""),
            str(forge.get("producer_login") or ""),
            str(forge.get("_declared_repo_identity") or ""),
        ]
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def build_provider(
    forge: Mapping[str, Any],
    *,
    producer_login: str | None,
    repo_root: str | Path | None = None,
) -> ScriptProvider:
    """Construct the ``script`` forge provider from its validated/resolved
    config -- kept here (not in ``repository_issue_loops.py``) so that
    module needs no direct import of :class:`ScriptProvider` itself.

    Re-normalizes ``command``/``cwd`` through
    ``validate_script_forge_config`` first whenever ``forge`` hasn't
    already been through it (its own ``_script_path`` marker is the
    tell) -- regardless of whether ``repo_root`` is known:
    ``validate_script_forge_config`` itself requires a fully absolute
    `command`/`cwd` when ``repo_root`` is ``None``, so even a rootless
    direct declaration still needs this pass to apply the Windows
    interpreter/shell portability wrap (``.py``/``.sh``/``.ps1``) and
    attach the internal markers `script_resource_namespace` reads -- a
    caller that hands this the *raw* declaration straight out of
    ``spec.repository_issue_loop`` (the CLI's own `status`/`discover`
    commands, which never ran `run_tick`'s own `validate_config(config,
    cwd=cwd)` first) would otherwise reach the subprocess with a bare
    `.py`/`.sh`/`.ps1` argv and no interpreter prefix. Re-running it on an
    *already*-resolved ``forge`` is not merely redundant but actively
    wrong: `command[0]` by then is the prefixed interpreter/shell (e.g.
    `sys.executable`), not the original script path, so a second pass
    would misresolve the namespace and corrupt the portability wrap. The
    *full* `validate_config` is separately unsafe to re-run here
    regardless: its own output carries derived keys (e.g.
    `worker_filters`) that are not valid re-input.
    """
    if "_script_path" not in forge:
        forge = {**forge, **validate_script_forge_config(
            forge, provider="script", repo_root=repo_root
        )}
    return ScriptProvider(
        forge["command"],
        cwd=forge.get("cwd"),
        timeout_seconds=forge.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
        producer_login=producer_login,
    )
