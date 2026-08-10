# Server Installer Residual Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic
> subagent-driven-development controller to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close server-installer findings F-10 through F-13 by making the installed
config authoritative, proving post-restart process identity, removing owned
dangling systemd enablement, and preserving retryable ownership after partial
uninstall deletion.

**Architecture:** Task 1 resolves one immutable endpoint from the config that will
govern the installed server, adds a systemd-only config mode, exposes systemd
process identity through health, and verifies a stable PID/invocation pair around
the health request. Task 2 validates the derived enablement symlink before
mutation and moves root deletion behind a recoverable rename/manifest transaction.

**Tech Stack:** Python 3.11+ standard library, Flask/Waitress in the existing
server runtime, pytest, and systemd user services.

Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).

## Global Constraints

- The approved design is `docs/superpowers/specs/2026-08-10-server-installer-remediation-design.md`; implement exactly F-10 through F-13 and preserve the existing fixes at baseline commit `562c1ee8ce713690a16c2d3ffe2a48026b0d00db`.
- The terminal run at `.superpowers/sdd/2026-08-10-server-installer` remains untouched in `FINAL_BLOCKED`; this plan executes in a fresh run root.
- Python 3.11 remains the minimum supported interpreter.
- `install.py` and `tests/test_install.py` remain standard-library-only. Do not add an entry to `requirements.txt`.
- Tests never invoke real `systemctl`, real virtualenv creation, or real network calls. Inject systemctl, endpoint fetch, socket occupancy, sleep, and filesystem failures.
- Never modify `desktop/install.py`, `tests/test_extension_split_sentences.py`, or `tests/test_media_session.py`.
- The server root remains `~/.local/share/free-tts-server`; server code never touches the independent desktop root `~/.local/share/free-tts`.
- The systemd-managed server reads `config.json` beside `server.py`, sets no `TTS_CONFIG`, and ignores every per-setting `TTS_*` environment override. Desktop/manual launches retain their existing environment-over-config precedence.
- `--force` bypasses endpoint occupancy only. It never bypasses config validation, ownership validation, post-restart identity, or uninstall safety.
- Ownership is established before mutation. Writes remain atomic. Failure leaves either the exact prior install or a manifest-bearing state that a later invocation can retry.
- Preserve the current public CLI, desktop delegation, tolerant status reads, strict mutation manifest loader, transactional install rollback, and systemd path quoting.
- Baseline verification is 83 installer tests, 91 server tests, and 552 full-suite tests. Different higher totals are expected; zero failures are required.
- Every new regression test must be shown failing for the intended reason against baseline production code before implementation and passing afterward. Use a temporary `git archive`; never move active `HEAD` or edit the active worktree for the baseline probe.
- Every task ends in one scoped commit. The new run's final Frontier review covers `5c9bac2a1940e2d64122decc810bd496eedc6470..HEAD` and reconciles the complete prior ledger, including F-10 through F-13.
- Do not merge the feature branch and do not migrate the live service during this plan.

## Task 1: Config-authoritative endpoint and systemd process identity

**Implementer tier:** Advanced

**Files:**

- Modify: `server.py:50-90,150-220,1059-1070`
- Modify: `install.py:350-660,780-915`
- Test: `tests/test_server.py:25-135,419-460`
- Test: `tests/test_install.py:320-1050`

**Interfaces:**

- Consumes: `_check_root_ownership(root: pathlib.Path, unit_dir: pathlib.Path) -> dict | None`, `bootstrap_config(root: pathlib.Path) -> bool`, `render_unit(root: pathlib.Path, python: pathlib.Path | str | None = None) -> str`, `check_port(...) -> str`, and the existing install transaction from baseline `562c1ee8`.
- Produces: `FREE_TTS_CONFIG_ONLY=1` as the unit's config-authoritative mode; `_CONFIG_ONLY: bool` and `_config_path(config_only: bool | None = None) -> pathlib.Path` in `server.py`; health fields `pid: int` and `invocation_id: str | None`.
- Produces: immutable `ServiceEndpoint(bind_host: str, probe_host: str, port: int)` with `health_url: str`; `_load_service_endpoint(path: pathlib.Path) -> ServiceEndpoint`; `UnitIdentity(main_pid: int, invocation_id: str)`; `_query_unit_identity(systemctl) -> UnitIdentity | None`.
- Changes: `probe_health` and `check_port` consume a `ServiceEndpoint`; the injected occupancy hook becomes `Callable[[str, int, float], bool]`; `_verify_service` requires the same endpoint plus a stable matching `UnitIdentity` before and after health.

- [ ] **Step 1: Write failing server config-mode and health-identity tests**

Add these tests to `tests/test_server.py` near the existing config and health tests:

```python
def test_config_only_mode_ignores_tts_setting_env(monkeypatch):
    monkeypatch.setenv("TTS_PORT", "7000")
    with (
        mock.patch.object(server, "_CONFIG_ONLY", True),
        mock.patch.object(server, "_CONFIG_CACHE", {"port": 6123}),
    ):
        assert server._cfg_int(
            "port", "TTS_PORT", 5000, minimum=1, maximum=65535
        ) == 6123


def test_config_only_path_ignores_external_tts_config(tmp_path, monkeypatch):
    monkeypatch.setenv("TTS_CONFIG", str(tmp_path / "foreign.json"))
    expected = pathlib.Path(server.__file__).resolve().parent / "config.json"
    assert server._config_path(config_only=True) == expected
    assert server._config_path(config_only=False) == tmp_path / "foreign.json"


def test_health_reports_systemd_process_identity(client, monkeypatch):
    invocation_id = "a" * 32
    monkeypatch.setenv("INVOCATION_ID", invocation_id)
    monkeypatch.setattr(server.os, "getpid", lambda: 4242)

    payload = client.get("/health").get_json()

    assert payload["pid"] == 4242
    assert payload["invocation_id"] == invocation_id


def test_health_reports_null_invocation_outside_systemd(client, monkeypatch):
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(server.os, "getpid", lambda: 4242)

    payload = client.get("/health").get_json()

    assert payload["pid"] == 4242
    assert payload["invocation_id"] is None
```

Keep the existing tests that prove ordinary env-over-config precedence. They are
the compatibility proof for desktop/manual launches.

- [ ] **Step 2: Write failing endpoint and stable-identity installer tests**

Extend `FakeCompleted`/`StatefulSystemctl` so a successful `show` call can return
one snapshot in this exact shape:

```text
ActiveState=active
MainPID=4242
InvocationID=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

Use deterministic identity defaults and expose the payload used by fetch doubles:

```python
TEST_MAIN_PID = 4242
TEST_INVOCATION_ID = "a" * 32


class StatefulSystemctl:
    def __init__(
        self,
        *,
        active=False,
        enabled=False,
        fail_once=None,
        main_pid=TEST_MAIN_PID,
        invocation_id=TEST_INVOCATION_ID,
    ):
        self.active = active
        self.enabled = enabled
        self.fail_once = fail_once
        self.failed = False
        self.hide_active = False
        self.main_pid = main_pid
        self.invocation_id = invocation_id
        self.calls = []

    def health_payload(self):
        return {
            "status": "ok",
            "service": "free-tts",
            "api_version": 1,
            "voice_cache_ready": True,
            "pid": self.main_pid,
            "invocation_id": self.invocation_id,
        }
```

Keep the existing command behavior and add this `show` branch before the existing
`is-active` branch:

```python
if command == "show":
    state = "active" if self.active and not self.hide_active else "inactive"
    return FakeCompleted(
        stdout=(
            f"ActiveState={state}\n"
            f"MainPID={self.main_pid if state == 'active' else 0}\n"
            f"InvocationID={self.invocation_id if state == 'active' else ''}\n"
        )
    )
```

Update `healthy_fetch` to return the same PID and invocation ID:

```python
def healthy_fetch(url, timeout):
    return {
        "status": "ok",
        "service": "free-tts",
        "api_version": 1,
        "voice_cache_ready": True,
        "pid": TEST_MAIN_PID,
        "invocation_id": TEST_INVOCATION_ID,
    }
```

Then add these tests to `tests/test_install.py`:

```python
def test_load_service_endpoint_accepts_custom_port(tmp_path):
    config = tmp_path / "config.json"
    config.write_text('{"host": "127.0.0.1", "port": "6123"}\n')

    endpoint = install._load_service_endpoint(config)

    assert endpoint == install.ServiceEndpoint("127.0.0.1", "127.0.0.1", 6123)
    assert endpoint.health_url == "http://127.0.0.1:6123/health"


@pytest.mark.parametrize(
    "bind_host,probe_host,url",
    (
        ("0.0.0.0", "127.0.0.1", "http://127.0.0.1:6123/health"),
        ("::", "::1", "http://[::1]:6123/health"),
        ("::1", "::1", "http://[::1]:6123/health"),
    ),
)
def test_service_endpoint_maps_probe_hosts(bind_host, probe_host, url):
    endpoint = install.ServiceEndpoint(bind_host, probe_host, 6123)
    assert endpoint.health_url == url


@pytest.mark.parametrize(
    "payload",
    (
        [],
        {"host": "", "port": 5000},
        {"host": "127.0.0.1", "port": True},
        {"host": "127.0.0.1", "port": 0},
        {"host": "127.0.0.1", "port": 65536},
        {"host": "127.0.0.1", "port": "not-a-port"},
    ),
)
def test_load_service_endpoint_rejects_invalid_config(tmp_path, payload):
    config = tmp_path / "config.json"
    config.write_text(json.dumps(payload))
    with pytest.raises(install.PreflightError):
        install._load_service_endpoint(config)


def test_load_service_endpoint_rejects_symlink(tmp_path):
    target = tmp_path / "target.json"
    target.write_text('{"port": 6123}\n')
    config = tmp_path / "config.json"
    config.symlink_to(target)
    with pytest.raises(install.PreflightError, match="regular non-symlink"):
        install._load_service_endpoint(config)


def test_check_port_uses_configured_probe_host_and_port():
    endpoint = install.ServiceEndpoint("127.0.0.1", "127.0.0.1", 6123)
    seen = []

    def occupancy(host, port, timeout):
        seen.append((host, port))
        return False

    assert install.check_port(endpoint=endpoint, occupancy_probe=occupancy) == "free"
    assert seen == [("127.0.0.1", 6123)]
```

Add a reinstall regression whose fetch double asserts the exact URL instead of
ignoring it:

```python
def test_install_server_reinstall_verifies_preserved_custom_port(checkout, tmp_path):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    runner = StatefulSystemctl()

    _transaction_install(
        checkout, root, unit_dir, runner, fake_venv_builder, healthy_fetch
    )
    (root / "config.json").write_text('{"host": "127.0.0.1", "port": 6123}\n')

    def custom_fetch(url, timeout):
        assert url == "http://127.0.0.1:6123/health"
        return runner.health_payload()

    _transaction_install(
        checkout, root, unit_dir, runner, fake_venv_builder, custom_fetch
    )
```

Add this local venv builder near `_transaction_install`:

```python
def fake_venv_builder(target):
    (target / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (target / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
```

Add these two identity failures:

```python
def test_verify_rejects_foreign_responder_after_forced_install(checkout, tmp_path):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    runner = StatefulSystemctl()

    def foreign_fetch(url, timeout):
        payload = runner.health_payload()
        payload["pid"] = 9999
        payload["invocation_id"] = "f" * 32
        return payload

    with pytest.raises(install.InstallError, match="identity"):
        _transaction_install(
            checkout, root, unit_dir, runner, fake_venv_builder, foreign_fetch
        )
    assert not os.path.lexists(root)


def test_verify_rejects_identity_change_during_health(checkout, tmp_path):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    runner = StatefulSystemctl()

    def racing_fetch(url, timeout):
        payload = runner.health_payload()
        runner.main_pid = 5252
        runner.invocation_id = "b" * 32
        return payload

    with pytest.raises(install.InstallError, match="identity"):
        _transaction_install(
            checkout, root, unit_dir, runner, fake_venv_builder, racing_fetch
        )
    assert not os.path.lexists(root)
```

Also assert `render_unit(root)` contains
`Environment=FREE_TTS_CONFIG_ONLY=1`, contains
`UnsetEnvironment=FLASK_DEBUG`, and still contains no `Environment=TTS_CONFIG`.

- [ ] **Step 3: Run the new tests and confirm the RED phase**

Run:

```bash
.venv/bin/python -m pytest tests/test_server.py -q -k 'config_only or health_reports_systemd_process_identity or health_reports_null_invocation'
python3 -m pytest tests/test_install.py -q -k 'service_endpoint or configured_probe or custom_port or foreign_responder or identity_change'
```

Expected: FAIL because config-only mode, `ServiceEndpoint`, configured URL routing,
health PID/invocation fields, and stable unit identity do not exist at baseline.
The foreign-responder and identity-race tests must fail by wrongly accepting the
response, not because the test double crashes.

- [ ] **Step 4: Implement config-only server mode and health identity**

In `server.py`, resolve config-only mode before logging/config globals:

```python
_CONFIG_ONLY = os.environ.get("FREE_TTS_CONFIG_ONLY") == "1"


def _config_path(config_only: bool | None = None) -> Path:
    authoritative = _CONFIG_ONLY if config_only is None else config_only
    if authoritative:
        return Path(__file__).resolve().parent / "config.json"
    return Path(os.environ.get("TTS_CONFIG", Path(__file__).parent / "config.json"))


_CONFIG_PATH = _config_path()
```

Change both `_cfg` and `_cfg_list` so `env_val` is `None` while `_CONFIG_ONLY` is
true; keep all existing coercion, fallback, and logging behavior. Add PID and
invocation ID to the existing health JSON:

```python
"pid": os.getpid(),
"invocation_id": os.environ.get("INVOCATION_ID") or None,
```

Do not change `API_VERSION`. Do not clear the process environment because the
identity field depends on systemd's `INVOCATION_ID`.

- [ ] **Step 5: Implement endpoint parsing and systemd identity verification**

In `install.py`, add immutable endpoint and identity types. Implement the endpoint
parser with strict regular-file/JSON/host/port checks and exact wildcard mapping:

```python
@dataclass(frozen=True)
class ServiceEndpoint:
    bind_host: str
    probe_host: str
    port: int

    @property
    def health_url(self) -> str:
        host = f"[{self.probe_host}]" if ":" in self.probe_host else self.probe_host
        return f"http://{host}:{self.port}/health"


@dataclass(frozen=True)
class UnitIdentity:
    main_pid: int
    invocation_id: str
```

Use `ServiceEndpoint("127.0.0.1", "127.0.0.1", 5000)` as the default. Change the
default socket probe to accept `(host, port, timeout)`. Make `probe_health`,
`check_port`, and `_verify_service` consume the same endpoint object.

Implement `_query_unit_identity` with one injected systemctl call requesting
`ActiveState`, `MainPID`, and `InvocationID`. Parse `key=value` lines. Return
`None` for non-active/transitional state. Treat a PID less than one, a missing
field, or an invocation ID that is not exactly 32 hexadecimal characters as not
ready. Raise `InstallError` for command execution or unrecognized output failures.

For every `_verify_service` attempt:

```python
before = _query_unit_identity(runner)
payload = probe_health(endpoint, fetch=fetch) if before is not None else None
after = _query_unit_identity(runner) if before is not None else None
if (
    before is not None
    and after == before
    and payload is not None
    and payload.get("service") == "free-tts"
    and not isinstance(payload.get("pid"), bool)
    and payload.get("pid") == before.main_pid
    and payload.get("invocation_id") == before.invocation_id
):
    return
```

Keep the bounded retry/sleeper behavior and report the last endpoint/identity
reason on failure.

In `install_server`, run the injected or Python/systemd preflight first, strictly
inspect ownership, select existing `root/config.json` or checkout
`config.example.json`, parse the endpoint before mutation, and run the default
port check with that endpoint. Pass it unchanged to post-restart verification.
Render the unit with:

```ini
Environment=FREE_TTS_CONFIG_ONLY=1
UnsetEnvironment=FLASK_DEBUG
```

Update the hermetic systemctl/fetch helpers and existing transaction tests to
return matching identities. Do not weaken any prior rollback assertion.

- [ ] **Step 6: Run scoped tests and confirm GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_server.py -q
python3 -m pytest tests/test_install.py -q
```

Expected: PASS with no failures (baseline 91 server and 83 installer tests; totals
increase with this task).

- [ ] **Step 7: Prove the regressions against baseline production code**

Run this read-only temporary-archive probe after the new tests pass:

```bash
BASE=562c1ee8ce713690a16c2d3ffe2a48026b0d00db
HEAD_BEFORE=$(git rev-parse HEAD)
STATUS_BEFORE=$(git status --porcelain=v1)
TMP=$(mktemp -d)
git archive HEAD | tar -x -C "$TMP"
cp tests/test_server.py "$TMP/tests/test_server.py"
cp tests/test_install.py "$TMP/tests/test_install.py"
ln -s "$PWD/.venv" "$TMP/.venv"
git show "$BASE:server.py" > "$TMP/server.py"
git show "$BASE:install.py" > "$TMP/install.py"
(cd "$TMP" && .venv/bin/python -m pytest tests/test_server.py -q -k 'config_only or health_reports_systemd_process_identity or health_reports_null_invocation')
SERVER_STATUS=$?
(cd "$TMP" && python3 -m pytest tests/test_install.py -q -k 'service_endpoint or custom_port or foreign_responder or identity_change')
INSTALL_STATUS=$?
rm -rf "$TMP"
test "$SERVER_STATUS" -ne 0
test "$INSTALL_STATUS" -ne 0
test "$(git rev-parse HEAD)" = "$HEAD_BEFORE"
test "$(git status --porcelain=v1)" = "$STATUS_BEFORE"
```

Expected: the selected tests FAIL against baseline mechanisms for the intended
missing endpoint/config/identity behavior, the temporary tree is removed, and the
active worktree's HEAD and pre-existing task diff stay unchanged. Record exact
failing test names in the report.

- [ ] **Step 8: Run full verification**

Run:

```bash
.venv/bin/python -m pytest -q
python3 -m py_compile install.py server.py tests/test_install.py tests/test_server.py
python3 install.py --help
git diff --check
```

Expected: full suite PASS with no failures (552 at baseline), help exits 0, and no
diff errors.

- [ ] **Step 9: Commit**

```bash
git add install.py server.py tests/test_install.py tests/test_server.py
git commit -m "fix(install): verify configured systemd service identity"
```

## Task 2: Retryable uninstall and owned enablement cleanup

**Implementer tier:** Advanced

**Files:**

- Modify: `install.py:917-990`
- Test: `tests/test_install.py:1050-1265`

**Interfaces:**

- Consumes: `_expected_manifest(root: pathlib.Path, unit_dir: pathlib.Path) -> dict[str, str]`, `_load_manifest(root, expected, missing_ok=...) -> dict | None`, `_canonical(path) -> pathlib.Path`, `_atomic_write(path, data, mode)`, `_reserve_sibling(parent, prefix) -> pathlib.Path`, `_systemctl_error(runner, args) -> str | None`, and `_query_unit_state(runner, command, expected) -> bool`.
- Produces: `_enablement_path(unit_dir: pathlib.Path) -> pathlib.Path`; `_validate_enablement_link(path: pathlib.Path, unit_path: pathlib.Path) -> bool`; `_remove_owned_root(root: pathlib.Path, manifest: dict) -> None`.
- Changes: `uninstall_server` prevalidates the unit and enablement path, establishes disable/inactive/link postconditions, and deletes the root through a recoverable sibling transaction.

- [ ] **Step 1: Write failing dangling-enablement and partial-deletion tests**

Add this helper near the existing uninstall tests:

```python
def write_enablement_link(unit_dir):
    link = unit_dir / "default.target.wants" / install.UNIT_NAME
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(pathlib.Path("..") / install.UNIT_NAME)
    return link
```

Add a missing-fragment runner whose `disable --now` returns status 1 with
`not-found`, while `is-active` reports inactive:

```python
class MissingFragmentSystemctl:
    def __init__(self):
        self.calls = []

    def __call__(self, args, check=True):
        args = list(args)
        self.calls.append(args)
        if args[0] == "disable":
            return FakeCompleted(returncode=1, stderr="Unit not found")
        if args[0] == "is-active":
            return FakeCompleted(returncode=3, stdout="inactive\n")
        return FakeCompleted()
```

Then add:

```python
def test_uninstall_removes_dangling_owned_enablement_without_fragment(
    checkout, tmp_path
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    unit.unlink()
    link = write_enablement_link(unit_dir)
    runner = MissingFragmentSystemctl()

    removed = install.uninstall_server(
        root=root, unit_dir=unit_dir, systemctl=runner
    )

    assert str(link) in removed
    assert str(root) in removed
    assert not os.path.lexists(link)
    assert not root.exists()
    assert ["disable", "--now", install.UNIT_NAME] in runner.calls
```

Prevalidation must protect foreign entries:

```python
@pytest.mark.parametrize("kind", ("file", "directory", "foreign-symlink"))
def test_uninstall_rejects_foreign_enablement_before_mutation(
    kind, checkout, tmp_path
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = unit_dir / "default.target.wants" / install.UNIT_NAME
    link.parent.mkdir(parents=True, exist_ok=True)
    if kind == "file":
        link.write_text("foreign\n")
    elif kind == "directory":
        link.mkdir()
    else:
        link.symlink_to(tmp_path / "foreign.service")
    before_root = snapshot_tree(root)
    before_unit = unit.read_bytes()
    runner = StatefulSystemctl(active=True, enabled=True)

    with pytest.raises(install.OwnershipError):
        install.uninstall_server(root=root, unit_dir=unit_dir, systemctl=runner)

    assert snapshot_tree(root) == before_root
    assert unit.read_bytes() == before_unit
    assert os.path.lexists(link)
    assert runner.calls == []
```

Pin partial deletion and retry:

```python
def test_uninstall_partial_root_delete_restores_manifest_and_retries(
    checkout, tmp_path, monkeypatch
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    runner = StatefulSystemctl(active=True, enabled=True)
    real_rmtree = install.shutil.rmtree
    failed = False

    def partial_delete(path, *args, **kwargs):
        nonlocal failed
        path = pathlib.Path(path)
        if path.name.startswith(".free-tts-server-delete-") and not failed:
            failed = True
            (path / install.MANIFEST_NAME).unlink()
            (path / "server.py").unlink()
            raise OSError("injected partial deletion")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(install.shutil, "rmtree", partial_delete)

    with pytest.raises(install.InstallError, match="ownership restored"):
        install.uninstall_server(root=root, unit_dir=unit_dir, systemctl=runner)

    expected = install._expected_manifest(root, unit_dir)
    assert install._load_manifest(root, expected, missing_ok=False)["component"] == "server"
    assert not (root / "server.py").exists()

    removed = install.uninstall_server(
        root=root, unit_dir=unit_dir, systemctl=runner
    )
    assert removed == [str(root)]
    assert not root.exists()
```

Add a compensation-failure test that injects failure while rewriting the
manifest in the deletion sibling:

```python
def test_uninstall_partial_delete_names_retained_tree_when_compensation_fails(
    checkout, tmp_path, monkeypatch
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    runner = StatefulSystemctl(active=True, enabled=True)
    real_rmtree = install.shutil.rmtree
    real_atomic_write = install._atomic_write

    def partial_delete(path, *args, **kwargs):
        path = pathlib.Path(path)
        if path.name.startswith(".free-tts-server-delete-"):
            (path / install.MANIFEST_NAME).unlink()
            raise OSError("injected partial deletion")
        return real_rmtree(path, *args, **kwargs)

    def fail_manifest_restore(path, data, mode=0o644):
        path = pathlib.Path(path)
        if (
            path.name == install.MANIFEST_NAME
            and path.parent.name.startswith(".free-tts-server-delete-")
        ):
            raise OSError("injected receipt failure")
        return real_atomic_write(path, data, mode)

    monkeypatch.setattr(install.shutil, "rmtree", partial_delete)
    monkeypatch.setattr(install, "_atomic_write", fail_manifest_restore)

    with pytest.raises(install.InstallError) as excinfo:
        install.uninstall_server(root=root, unit_dir=unit_dir, systemctl=runner)

    retained = list(root.parent.glob(".free-tts-server-delete-*"))
    assert len(retained) == 1
    assert str(retained[0]) in str(excinfo.value)
    assert "injected receipt failure" in str(excinfo.value)
    assert not root.exists()
```

The retained path stays under `tmp_path`, so pytest owns its cleanup.

- [ ] **Step 2: Run the new uninstall tests and confirm the RED phase**

Run:

```bash
python3 -m pytest tests/test_install.py -q -k 'dangling_owned_enablement or foreign_enablement or partial_root_delete or compensation_failure'
```

Expected: FAIL because baseline skips disable/link cleanup when the fragment is
missing, mutates before validating enablement, and makes a partial `rmtree`
non-retryable by losing the manifest.

- [ ] **Step 3: Implement enablement ownership validation**

Add:

```python
def _enablement_path(unit_dir: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(unit_dir) / "default.target.wants" / UNIT_NAME


def _validate_enablement_link(
    path: pathlib.Path, unit_path: pathlib.Path
) -> bool:
    if not os.path.lexists(path):
        return False
    if not path.is_symlink():
        raise OwnershipError(f"enablement path is not an owned symlink: {path}")
    target = pathlib.Path(os.readlink(path))
    resolved = _canonical(target if target.is_absolute() else path.parent / target)
    if resolved != _canonical(unit_path):
        raise OwnershipError(f"enablement symlink targets a foreign unit: {path}")
    return True
```

In `uninstall_server`, load the strict manifest, validate the unit file, and call
`_validate_enablement_link` before the first systemctl call. Then invoke
`disable --now` even when the unit fragment is absent. Query `is-active` afterward.
If still active, raise and keep all validated filesystem paths. If inactive,
remove a still-present validated enablement link directly. Tolerate a nonzero
disable result only when inactive and the owned link is absent; otherwise include
the systemctl diagnostic in `InstallError`. Remove the unit fragment when present,
then run `daemon-reload` and retain the manifest-bearing root on reload failure.

- [ ] **Step 4: Implement recoverable root deletion**

Add `_remove_owned_root(root, manifest)`:

```python
deleting = _reserve_sibling(root.parent, ".free-tts-server-delete-")
os.replace(root, deleting)
try:
    shutil.rmtree(deleting)
except OSError as delete_error:
    if not os.path.lexists(deleting):
        return
    compensation_errors = []
    try:
        _atomic_write(
            manifest_path(deleting),
            json.dumps(manifest, indent=2).encode("utf-8"),
            0o644,
        )
    except BaseException as exc:
        compensation_errors.append(f"could not restore ownership manifest: {exc}")
    if not compensation_errors:
        try:
            if os.path.lexists(root):
                raise InstallError(f"canonical root was recreated at {root}")
            os.replace(deleting, root)
        except BaseException as exc:
            compensation_errors.append(f"could not restore canonical root: {exc}")
    if compensation_errors:
        details = "; ".join(compensation_errors)
        raise InstallError(
            f"could not remove {root}: {delete_error}; retained partial tree at "
            f"{deleting}; {details}"
        ) from delete_error
    raise InstallError(
        f"could not remove {root}: {delete_error}; ownership restored for retry"
    ) from delete_error
```

Use the already strict-validated manifest payload. Replace direct
`shutil.rmtree(root)` with this helper and append the canonical `root` to the
removed list only after the helper succeeds. Preserve prior daemon-reload retry
behavior and the order of reported removals.

- [ ] **Step 5: Run scoped tests and confirm GREEN**

Run:

```bash
python3 -m pytest tests/test_install.py -q
```

Expected: PASS with no failures; all Task 1 tests and the baseline 83 installer
tests remain green.

- [ ] **Step 6: Prove the uninstall regressions against baseline production code**

Run:

```bash
BASE=562c1ee8ce713690a16c2d3ffe2a48026b0d00db
HEAD_BEFORE=$(git rev-parse HEAD)
STATUS_BEFORE=$(git status --porcelain=v1)
TMP=$(mktemp -d)
git archive HEAD | tar -x -C "$TMP"
cp tests/test_install.py "$TMP/tests/test_install.py"
git show "$BASE:install.py" > "$TMP/install.py"
(cd "$TMP" && python3 -m pytest tests/test_install.py -q -k 'dangling_owned_enablement or foreign_enablement or partial_root_delete')
STATUS=$?
rm -rf "$TMP"
test "$STATUS" -ne 0
test "$(git rev-parse HEAD)" = "$HEAD_BEFORE"
test "$(git status --porcelain=v1)" = "$STATUS_BEFORE"
```

Expected: the selected tests FAIL against baseline `install.py` for the intended
F-12/F-13 behavior, the temporary tree is absent, and active HEAD/status are
unchanged.

- [ ] **Step 7: Run full verification**

Run:

```bash
.venv/bin/python -m pytest -q
python3 -m py_compile install.py server.py tests/test_install.py tests/test_server.py
python3 install.py --help
git diff --check
```

Expected: full suite PASS with no failures (552 at baseline), help exits 0, and no
diff errors.

- [ ] **Step 8: Commit**

```bash
git add install.py tests/test_install.py
git commit -m "fix(install): make server uninstall cleanup retryable"
```
