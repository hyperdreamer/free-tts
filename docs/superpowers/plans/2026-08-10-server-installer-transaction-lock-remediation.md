# Server Installer Transaction Lock Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic
> subagent-driven-development controller to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve F-17, F-18, F-20, and F-21 by serializing server installer transactions, concentrating unit/link ownership in one lock-bound module, and making endpoint decoding total.

**Architecture:** `install_server` and `uninstall_server` acquire one secure nonblocking `flock` before any managed-state inspection and hold it through rollback. A private `_SystemdArtifacts` module stages known inodes, publishes missing artifacts without replacement, owns exact rollback/removal, and replaces the ineffective quarantine protocol; endpoint parsing catches decoder-level `ValueError`.

**Tech Stack:** Python 3.11+, Linux `fcntl.flock`, standard-library filesystem primitives, pytest, systemd user units.

## Global Constraints

- Python 3.11 is the version floor; the managed service and installer target Linux with systemd user sessions and standard-library `fcntl.flock`.
- Installer implementation and installer tests remain standard-library-only; do not add or change dependencies or packaging metadata.
- Implementation may modify only `install.py`, `tests/test_install.py`, and `README.md`; the plan itself is the only additional file.
- Never modify `desktop/install.py`, `tests/test_extension_split_sentences.py`, or `tests/test_media_session.py`.
- Tests must remain hermetic: no real `systemctl`, network calls, virtualenv creation, package installation, or live-service mutation; isolate the production lock path under each test's `tmp_path`.
- Every `install_server()` and `uninstall_server()` call must acquire the same exclusive lock before preflight, manifest loading, managed-state inspection, systemctl, or target mutation and hold it through complete success or rollback.
- Supported concurrency is cooperative: concurrent installer invocations are serialized; direct filesystem edits or independent mutating systemctl operations against managed paths while the lock is held are explicitly unsupported.
- Foreign state observed at transaction entry or at an atomic no-replace publication point must be retained and reported; do not claim compare-and-delete safety against an uncooperative same-UID process.
- `status()` remains read-only, tolerant, and unlocked.
- The systemd-managed server remains config-authoritative through `FREE_TTS_CONFIG_ONLY=1`; configured endpoint checks and stable MainPID/InvocationID health verification must remain intact.
- Preserve exact `inactive` cleanup gating, one `reset-failed`, failed-stop enablement compensation, strict manifests, retryable root deletion, desktop delegation, CLI aggregation, and every earlier fixed finding.
- `--force` continues to bypass endpoint occupancy only.
- Historical SDD states, specs, plans, and reports remain immutable; do not edit or reopen any terminal run.
- New regressions must demonstrate RED against production code at `42e973dff7b3eb729b4384765cef6ddee6dad51a` with a read-only archive or mechanism-scoped mutant, leaving active HEAD and status unchanged.
- Baseline at the approved design commit is 141 installer tests, 95 server tests, and 614 full-suite tests; counts may increase, but no existing test may fail without an explicit contract-based replacement in this plan.
- Do not merge the feature branch or migrate the live `free-tts.service`; final Frontier review must cover original merge base `5c9bac2a1940e2d64122decc810bd496eedc6470` through final HEAD and reconcile every carried finding.

## Task 1: Serialize server installer transactions

**Implementer tier:** Advanced

**Files:**

- Read: `docs/superpowers/specs/2026-08-10-server-installer-transaction-lock-remediation-design.md`
- Modify: `install.py:15-35,483-560,1029-1195,1592-1685`
- Test: `tests/test_install.py:1-45,960-1045,1600-1660`

**Interfaces:**

- Consumes: existing `PreflightError`, `install_server(...) -> dict`, and `uninstall_server(...) -> list[str]`.
- Produces: `_server_lock_path() -> pathlib.Path`; `_server_transaction_lock(path: pathlib.Path | None = None) -> contextlib.AbstractContextManager[None]`; `_serialized_server_transaction(operation: Callable) -> Callable`; decorated `install_server` and `uninstall_server` with unchanged call signatures.

- [ ] **Step 1: Add an isolated runtime fixture and failing real-lock tests**

Add the imports and fixture below near the top of `tests/test_install.py`. The autouse fixture prevents every existing installer test from touching the real user runtime directory.

```python
@pytest.fixture(autouse=True)
def isolated_server_runtime(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    return runtime / "free-tts-installer.lock"
```

Add these direct lock tests:

```python
def test_server_transaction_lock_rejects_a_second_open(isolated_server_runtime):
    with install._server_transaction_lock():
        with pytest.raises(install.PreflightError, match="another server installer"):
            with install._server_transaction_lock():
                pytest.fail("contender entered the critical section")

    with install._server_transaction_lock():
        pass


def test_server_transaction_lock_reuses_stale_unlocked_file(
    isolated_server_runtime,
):
    isolated_server_runtime.write_text("stale diagnostic\n")
    isolated_server_runtime.chmod(0o600)

    with install._server_transaction_lock():
        assert isolated_server_runtime.is_file()


@pytest.mark.parametrize("kind", ("symlink", "directory", "insecure-file"))
def test_server_transaction_lock_rejects_unsafe_lock_path(
    kind, isolated_server_runtime, tmp_path
):
    if kind == "symlink":
        target = tmp_path / "foreign-lock"
        target.write_text("foreign\n")
        isolated_server_runtime.symlink_to(target)
    elif kind == "directory":
        isolated_server_runtime.mkdir()
    else:
        isolated_server_runtime.write_text("insecure\n")
        isolated_server_runtime.chmod(0o644)

    with pytest.raises(install.PreflightError, match="installer lock"):
        with install._server_transaction_lock():
            pytest.fail("unsafe lock entered the critical section")


def test_server_transaction_lock_rejects_foreign_owner(
    isolated_server_runtime, monkeypatch
):
    real_fstat = install.os.fstat

    def foreign_fstat(fd):
        values = list(real_fstat(fd))
        values[4] = os.geteuid() + 1
        return os.stat_result(values)

    monkeypatch.setattr(install.os, "fstat", foreign_fstat)

    with pytest.raises(install.PreflightError, match="owned by the current user"):
        with install._server_transaction_lock():
            pytest.fail("foreign lock entered the critical section")


def test_server_lock_path_rejects_symlinked_runtime(
    isolated_server_runtime, tmp_path, monkeypatch
):
    real_runtime = tmp_path / "real-runtime"
    real_runtime.mkdir()
    linked_runtime = tmp_path / "linked-runtime"
    linked_runtime.symlink_to(real_runtime, target_is_directory=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(linked_runtime))

    with pytest.raises(install.PreflightError, match="XDG_RUNTIME_DIR"):
        install._server_lock_path()
```

Add public-operation contention tests. They prove lock acquisition precedes injected preflight and manifest/systemctl behavior.

```python
def test_contended_install_does_not_run_preflight_or_touch_targets(
    checkout, tmp_path, isolated_server_runtime, monkeypatch
):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    calls = []
    ownership_reads = []
    monkeypatch.setattr(
        install,
        "_check_root_ownership",
        lambda *args: ownership_reads.append(args),
    )

    with install._server_transaction_lock():
        with pytest.raises(install.PreflightError, match="another server installer"):
            install.install_server(
                checkout,
                root=root,
                unit_dir=unit_dir,
                preflight=lambda: calls.append("preflight"),
            )

    assert calls == []
    assert ownership_reads == []
    assert not root.exists()
    assert not unit_dir.exists()


def test_contended_uninstall_does_not_read_manifest_or_call_systemctl(
    tmp_path, isolated_server_runtime, monkeypatch
):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    calls = []
    manifest_reads = []
    monkeypatch.setattr(
        install,
        "_load_manifest",
        lambda *args, **kwargs: manifest_reads.append((args, kwargs)),
    )

    def runner(args, check=False):
        calls.append(args)
        return FakeCompleted()

    with install._server_transaction_lock():
        with pytest.raises(install.PreflightError, match="another server installer"):
            install.uninstall_server(
                root=root, unit_dir=unit_dir, systemctl=runner
            )

    assert manifest_reads == []
    assert calls == []
    assert not root.exists()
    assert not unit_dir.exists()


def test_status_remains_unlocked_without_xdg_runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    report = install.status(
        root=tmp_path / "missing",
        desktop_root=tmp_path / "desktop",
        unit_dir=tmp_path / "systemd" / "user",
        systemctl=fake_systemctl(),
    )

    assert report["server"]["installed"] is False
```

- [ ] **Step 2: Run the lock selection and confirm RED**

Run:

```bash
python3 -m pytest tests/test_install.py -q -k 'server_transaction_lock or server_lock_path or contended_install or contended_uninstall or status_remains_unlocked'
```

Expected: FAIL because `_server_transaction_lock` and `_server_lock_path` do not exist and server operations are not serialized.

- [ ] **Step 3: Implement secure lock resolution and acquisition**

Add `fcntl` and `functools` to the stdlib imports in `install.py`, and change the collections import to `from collections.abc import Callable, Iterator`. Immediately after `PreflightError`, add this implementation:

```python
def _server_lock_path() -> pathlib.Path:
    raw_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not raw_runtime:
        raise PreflightError(
            "XDG_RUNTIME_DIR is required for the server installer lock"
        )
    runtime = pathlib.Path(raw_runtime)
    if not runtime.is_absolute():
        raise PreflightError(
            f"XDG_RUNTIME_DIR must be absolute for the installer lock: {runtime}"
        )
    try:
        metadata = os.lstat(runtime)
    except OSError as exc:
        raise PreflightError(
            f"XDG_RUNTIME_DIR is not accessible for the installer lock: "
            f"{runtime}: {exc}"
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise PreflightError(
            f"XDG_RUNTIME_DIR must be a non-symlink directory: {runtime}"
        )
    if metadata.st_uid != os.geteuid():
        raise PreflightError(
            f"XDG_RUNTIME_DIR is not owned by the current user: {runtime}"
        )
    return runtime / "free-tts-installer.lock"


@contextlib.contextmanager
def _server_transaction_lock(
    path: pathlib.Path | None = None,
) -> Iterator[None]:
    lock_path = _server_lock_path() if path is None else pathlib.Path(path)
    descriptor = None
    locked = False
    try:
        descriptor = os.open(
            lock_path,
            os.O_CREAT
            | os.O_RDWR
            | os.O_CLOEXEC
            | os.O_NOFOLLOW,
            0o600,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PreflightError(
                f"installer lock must be a regular file: {lock_path}"
            )
        if metadata.st_uid != os.geteuid():
            raise PreflightError(
                f"installer lock is not owned by the current user: {lock_path}"
            )
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise PreflightError(
                f"installer lock has insecure permissions: {lock_path}"
            )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PreflightError(
                "another server installer operation is active; "
                f"lock: {lock_path}"
            ) from exc
        locked = True
        yield
    except PreflightError:
        raise
    except OSError as exc:
        raise PreflightError(
            f"could not open or lock installer lock {lock_path}: {exc}"
        ) from exc
    finally:
        if descriptor is not None:
            if locked:
                with contextlib.suppress(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _serialized_server_transaction(operation: Callable) -> Callable:
    @functools.wraps(operation)
    def serialized(*args, **kwargs):
        with _server_transaction_lock():
            return operation(*args, **kwargs)

    return serialized
```

Do not create or remove the runtime directory. Invalid runtime state is a preflight failure. Keep the lock file after release; kernel lock state, not contents or existence, is authoritative.

- [ ] **Step 4: Put both mutating public operations behind the lock seam**

Apply `@_serialized_server_transaction` immediately above the existing `install_server` definition and immediately above the existing `uninstall_server` definition:

```python
@_serialized_server_transaction
```

Preserve every existing parameter and function body. Do not decorate `status`, desktop operations, or `main`.

- [ ] **Step 5: Run lock and regression verification**

Run:

```bash
python3 -m pytest tests/test_install.py -q -k 'server_transaction_lock or server_lock_path or contended_install or contended_uninstall or preflight_before_touching_disk or main_help'
python3 -m pytest tests/test_install.py -q
.venv/bin/python -m pytest tests/test_server.py -q
.venv/bin/python -m pytest -q
python3 -m py_compile install.py tests/test_install.py
python3 install.py --help
```

Expected: all selections and suites PASS with no failures; totals are at least the 141 installer, 95 server, and 614 full-suite baseline plus new lock tests.

- [ ] **Step 6: Prove the lock regressions fail against the baseline implementation**

Use a read-only archive containing baseline production code and the current tests:

```bash
BASE=42e973dff7b3eb729b4384765cef6ddee6dad51a
ACTIVE_HEAD=$(git rev-parse HEAD)
ACTIVE_STATUS=$(git status --porcelain=v1)
TMP=$(mktemp -d)
git archive "$BASE" | tar -x -C "$TMP"
cp tests/test_install.py "$TMP/tests/test_install.py"
set +e
(cd "$TMP" && python3 -m pytest tests/test_install.py -q -k 'server_transaction_lock or server_lock_path or contended_install or contended_uninstall')
MUTANT_STATUS=$?
set -e
test "$MUTANT_STATUS" -ne 0
test "$(git rev-parse HEAD)" = "$ACTIVE_HEAD"
test "$(git status --porcelain=v1)" = "$ACTIVE_STATUS"
rm -rf "$TMP"
```

Expected: the archive selection fails because baseline production lacks the lock seam; active HEAD and status are unchanged.

- [ ] **Step 7: Commit**

```bash
git add install.py tests/test_install.py
git commit -m "fix(install): serialize server transactions"
```

## Task 2: Deepen install artifact ownership

**Implementer tier:** Capable

**Files:**

- Read: `docs/superpowers/specs/2026-08-10-server-installer-transaction-lock-remediation-design.md`
- Modify: `install.py:20-35,226-260,430-445,924-1195,1198-1540`
- Test: `tests/test_install.py:380-410,880-1600`

**Interfaces:**

- Consumes: `_server_transaction_lock(...)` and decorated `install_server(...)` from Task 1; existing `_PathSnapshot`, `_lstat_identity`, `_snapshot_is_owned_enablement`, `_observe_enablement`, `_ensure_directory`, `_remove_empty_directories`, and runtime rollback helpers.
- Produces: mutable `_SystemdArtifacts` with `capture_for_install(unit_path: pathlib.Path, enablement_path: pathlib.Path, upgrading: bool) -> _SystemdArtifacts`, `publish_unit(unit_text: str) -> None`, `ensure_enablement() -> None`, `verify_enablement() -> None`, and `restore() -> list[str]`; `_rollback_install(..., artifacts: _SystemdArtifacts, ...) -> list[str]`; install orchestration that never directly mutates unit or enablement paths.

- [ ] **Step 1: Replace unsupported install-race tests with lock-bound publication tests**

Delete these tests because their only premise is arbitrary direct mutation after lock-protected validation:

```text
test_install_server_rejects_foreign_replacement_after_restart
test_install_server_rollback_quarantines_replacement_after_observation
test_install_server_rollback_preserves_new_foreign_enablement
```

Retain all pre-entry foreign-state tests. Rewrite `test_install_server_retains_collision_before_enablement_creation` so collision occurs at the atomic no-replace primitive rather than through a systemctl hook. Add a corresponding unit publication test:

```python
def test_install_server_retains_unit_collision_at_no_replace_publication(
    checkout, tmp_path, monkeypatch
):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    runner = EnablementSystemctl(unit_dir)
    real_link = install.os.link
    collided = False

    def collide_at_link(source, destination, *args, **kwargs):
        nonlocal collided
        destination = pathlib.Path(destination)
        if destination == unit and not collided:
            collided = True
            unit.write_text("foreign unit\n")
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(install.os, "link", collide_at_link)

    with pytest.raises(install.OwnershipError, match="service unit"):
        _transaction_install(
            checkout, root, unit_dir, runner, fake_venv_builder, healthy_fetch
        )

    assert collided is True
    assert unit.read_text() == "foreign unit\n"
    assert not root.exists()
    assert not os.path.lexists(runner.link)
    assert list(root.parent.glob(".free-tts-server-failed-*")) == []


def test_install_server_retains_enablement_collision_at_no_replace_publication(
    checkout, tmp_path, monkeypatch
):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = unit_dir / "default.target.wants" / install.UNIT_NAME
    runner = EnablementSystemctl(unit_dir)
    real_link = install.os.link
    collided = False

    def collide_at_link(source, destination, *args, **kwargs):
        nonlocal collided
        destination = pathlib.Path(destination)
        if destination == link and not collided:
            collided = True
            link.write_text("foreign enablement\n")
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(install.os, "link", collide_at_link)

    with pytest.raises(install.OwnershipError, match="service enablement"):
        _transaction_install(
            checkout, root, unit_dir, runner, fake_venv_builder, healthy_fetch
        )

    assert collided is True
    assert link.read_text() == "foreign enablement\n"
    assert not unit.exists()
    assert not root.exists()
    assert list(root.parent.glob(".free-tts-server-failed-*")) == []
```

Add a known-inode publication test using the artifact interface:

```python
def test_systemd_artifacts_publish_known_unit_and_enablement_inodes(tmp_path):
    unit_dir = tmp_path / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = unit_dir / "default.target.wants" / install.UNIT_NAME
    artifacts = install._SystemdArtifacts.capture_for_install(
        unit, link, upgrading=False
    )

    artifacts.publish_unit("[Unit]\nDescription=test\n")
    artifacts.ensure_enablement()

    assert artifacts.unit_expected.identity == (
        os.lstat(unit).st_dev,
        os.lstat(unit).st_ino,
    )
    assert artifacts.enablement_expected.identity == (
        os.lstat(link).st_dev,
        os.lstat(link).st_ino,
    )
    assert os.readlink(link) == f"../{install.UNIT_NAME}"
```

- [ ] **Step 2: Run install-artifact tests and confirm RED**

Run:

```bash
python3 -m pytest tests/test_install.py -q -k 'no_replace_publication or publish_known_unit_and_enablement_inodes or fresh_restart_failure_restores_absent_enablement or upgrade_restart_failure_restores_exact_enablement'
```

Expected: FAIL because `_SystemdArtifacts` does not exist and baseline unit publication uses unconditional `os.replace` rather than no-replace known-inode publication.

- [ ] **Step 3: Add stable unit snapshots and snapshot comparison**

Change the dataclass import to `from dataclasses import dataclass, field`. Keep `_PathSnapshot` and add these helpers beside it:

```python
def _observe_unit(path: pathlib.Path) -> _PathSnapshot:
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return _PathSnapshot("missing")
    except OSError as exc:
        raise InstallError(f"could not inspect service unit {path}: {exc}") from exc
    if not stat.S_ISREG(before.st_mode):
        raise OwnershipError(
            f"owned service unit must be a regular non-symlink file: {path}"
        )
    try:
        data = path.read_bytes()
        after = os.lstat(path)
    except OSError as exc:
        raise InstallError(f"could not read service unit {path}: {exc}") from exc
    if _lstat_identity(before) != _lstat_identity(after):
        raise OwnershipError(f"service unit changed during observation: {path}")
    return _PathSnapshot(
        "file",
        data,
        stat.S_IMODE(after.st_mode),
        _lstat_identity(after),
    )


def _snapshot_matches(expected: _PathSnapshot, observed: _PathSnapshot) -> bool:
    if expected.kind != observed.kind:
        return False
    if expected.kind == "missing":
        return True
    return (
        expected.data == observed.data
        and expected.mode == observed.mode
        and expected.identity == observed.identity
    )
```

Do not route systemd units through `_snapshot_path`. After `_SystemdArtifacts` is integrated and no caller remains, delete `_snapshot_path` and `_restore_file_path`; unit capture, expected-current validation, and rollback use `_observe_unit` exclusively.

- [ ] **Step 4: Implement staged known-inode artifact publication**

Add `_SystemdArtifacts` after the enablement observation helpers. Use this exact state shape:

```python
@dataclass
class _SystemdArtifacts:
    unit_path: pathlib.Path
    enablement_path: pathlib.Path
    unit_initial: _PathSnapshot
    enablement_initial: _PathSnapshot
    unit_expected: _PathSnapshot
    enablement_expected: _PathSnapshot
    operation: str
    unit_touched: bool = False
    enablement_touched: bool = False
    created_directories: list[pathlib.Path] = field(default_factory=list)

    @classmethod
    def capture_for_install(
        cls,
        unit_path: pathlib.Path,
        enablement_path: pathlib.Path,
        upgrading: bool,
    ) -> "_SystemdArtifacts":
        unit_path = pathlib.Path(unit_path)
        enablement_path = pathlib.Path(enablement_path)
        unit = _observe_unit(unit_path)
        enablement = _observe_enablement(enablement_path)
        if enablement.kind != "missing" and not _snapshot_is_owned_enablement(
            enablement_path, unit_path, enablement
        ):
            raise OwnershipError(
                f"enablement path is foreign: {enablement_path}; retained "
                f"{_describe_enablement(enablement)}"
            )
        if not upgrading and unit.kind != "missing":
            raise OwnershipError(
                f"refusing to overwrite unowned service unit at {unit_path}"
            )
        if not upgrading and enablement.kind != "missing":
            raise OwnershipError(
                f"refusing to overwrite unowned enablement path {enablement_path}"
            )
        return cls(
            unit_path,
            enablement_path,
            unit,
            enablement,
            unit,
            enablement,
            "install",
        )
```

Implement the private staging and expected-current methods exactly through the module seam:

```python
    def _assert_unit_expected(self) -> None:
        observed = _observe_unit(self.unit_path)
        if not _snapshot_matches(self.unit_expected, observed):
            raise InstallError(
                f"service unit changed before mutation: {self.unit_path}; retained"
            )

    def _assert_enablement_expected(self) -> None:
        observed = _observe_enablement(self.enablement_path)
        if not _snapshot_matches(self.enablement_expected, observed):
            raise InstallError(
                f"service enablement changed before mutation: "
                f"{self.enablement_path}; retained "
                f"{_describe_enablement(observed)}"
            )

    def _stage_unit(
        self, data: bytes, mode: int
    ) -> tuple[pathlib.Path, _PathSnapshot]:
        descriptor, name = tempfile.mkstemp(
            dir=str(self.unit_path.parent),
            prefix=f".{UNIT_NAME}.staged-",
        )
        staged = pathlib.Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
            os.chmod(staged, mode)
            return staged, _observe_unit(staged)
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(descriptor)
            with contextlib.suppress(OSError):
                staged.unlink()
            raise

    def _stage_enablement(
        self, target: str
    ) -> tuple[pathlib.Path, _PathSnapshot]:
        staged = _reserve_sibling(
            self.enablement_path.parent,
            f".{UNIT_NAME}.staged-enablement-",
        )
        try:
            os.symlink(target, staged)
            return staged, _observe_enablement(staged)
        except BaseException:
            with contextlib.suppress(OSError):
                staged.unlink()
            raise
```

`_stage_unit` accepts exact bytes and mode so rollback can restore the captured unit without a text round trip. `_stage_enablement` accepts the raw target so failed-disable compensation can restore it byte-for-byte.

Implement publication so the staged inode is known before the no-replace operation:

```python
    def publish_unit(self, unit_text: str) -> None:
        _ensure_directory(self.unit_path.parent, self.created_directories)
        staged, staged_snapshot = self._stage_unit(
            unit_text.encode("utf-8"), 0o644
        )
        try:
            if self.unit_expected.kind == "missing":
                try:
                    os.link(staged, self.unit_path, follow_symlinks=False)
                except FileExistsError as exc:
                    observed = _observe_unit(self.unit_path)
                    raise OwnershipError(
                        f"refusing to replace service unit {self.unit_path}; "
                        f"retained {observed.kind}"
                    ) from exc
            else:
                self._assert_unit_expected()
                os.replace(staged, self.unit_path)
                staged = None
            self.unit_expected = staged_snapshot
            self.unit_touched = True
        finally:
            if staged is not None and os.path.lexists(staged):
                staged.unlink()

    def ensure_enablement(self) -> None:
        if self.enablement_expected.kind != "missing":
            self._assert_enablement_expected()
            return
        _ensure_directory(self.enablement_path.parent, self.created_directories)
        staged, staged_snapshot = self._stage_enablement(
            _enablement_target(self.enablement_path, self.unit_path)
        )
        try:
            try:
                os.link(
                    staged,
                    self.enablement_path,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                observed = _observe_enablement(self.enablement_path)
                raise OwnershipError(
                    f"refusing to replace service enablement "
                    f"{self.enablement_path}; retained "
                    f"{_describe_enablement(observed)}"
                ) from exc
            self.enablement_expected = staged_snapshot
            self.enablement_touched = True
        finally:
            if os.path.lexists(staged):
                staged.unlink()

    def verify_enablement(self) -> None:
        self._assert_enablement_expected()
        if self.enablement_expected.kind == "missing":
            raise InstallError(
                f"service enablement disappeared after restart: "
                f"{self.enablement_path}"
            )
```

On Linux, `os.link(staged_symlink, destination, follow_symlinks=False)` creates a hard link to the staged symlink inode and fails if `destination` exists. Do not replace it with `Path.resolve`, `os.replace`, or post-creation identity adoption.

- [ ] **Step 5: Implement lock-bound install rollback in the artifact module**

Implement `restore() -> list[str]` for `operation == "install"`:

1. If enablement was touched, assert the current enablement equals `enablement_expected`. If the initial snapshot was missing, unlink only that expected path. If initial was an owned symlink, recreate its raw target only into an absent path; an already exact raw target is accepted. Record any exception and continue.
2. If the unit was touched, assert the current unit equals `unit_expected`. If initial was missing, unlink only that expected path. If initial was a file, stage `unit_initial.data` with `unit_initial.mode`, atomically replace the expected current unit, and update expected state. Record any exception and continue.
3. Remove only empty directories recorded in `created_directories`.
4. Return error strings prefixed with `could not restore service enablement` or `could not restore service unit`.

Add exact restoration helpers before `restore()`:

```python
    def _restore_initial_unit(self) -> None:
        if self.unit_initial.kind != "file" or not isinstance(
            self.unit_initial.data, bytes
        ):
            raise InstallError("saved service unit snapshot is invalid")
        staged, restored = self._stage_unit(
            self.unit_initial.data,
            self.unit_initial.mode,
        )
        try:
            os.replace(staged, self.unit_path)
            staged = None
            self.unit_expected = restored
        finally:
            if staged is not None and os.path.lexists(staged):
                staged.unlink()

    def _restore_initial_enablement(self) -> None:
        target = self.enablement_initial.data
        if self.enablement_initial.kind != "symlink" or not isinstance(
            target, str
        ):
            raise InstallError("saved service enablement snapshot is invalid")
        observed = _observe_enablement(self.enablement_path)
        if (
            observed.kind == "symlink"
            and observed.data == target
            and _snapshot_is_owned_enablement(
                self.enablement_path,
                self.unit_path,
                observed,
            )
        ):
            self.enablement_expected = observed
            return
        if observed.kind != "missing":
            raise InstallError(
                f"refusing to replace service enablement "
                f"{self.enablement_path}; retained "
                f"{_describe_enablement(observed)}"
            )
        _ensure_directory(
            self.enablement_path.parent,
            self.created_directories,
        )
        staged, restored = self._stage_enablement(target)
        try:
            os.link(
                staged,
                self.enablement_path,
                follow_symlinks=False,
            )
            self.enablement_expected = restored
        finally:
            if os.path.lexists(staged):
                staged.unlink()
```

Use this concrete control shape so one compensation failure does not skip the other:

```python
    def restore(self) -> list[str]:
        errors = []
        if self.operation != "install":
            raise InstallError(
                f"artifact restore mode is not implemented: {self.operation}"
            )
        if self.enablement_touched:
            try:
                self._assert_enablement_expected()
                if self.enablement_initial.kind == "missing":
                    self.enablement_path.unlink()
                    self.enablement_expected = _PathSnapshot("missing")
                else:
                    self._restore_initial_enablement()
            except BaseException as exc:
                errors.append(
                    f"could not restore service enablement "
                    f"{self.enablement_path}: {exc}"
                )
        if self.unit_touched:
            try:
                self._assert_unit_expected()
                if self.unit_initial.kind == "missing":
                    self.unit_path.unlink()
                    self.unit_expected = _PathSnapshot("missing")
                else:
                    self._restore_initial_unit()
            except BaseException as exc:
                errors.append(
                    f"could not restore service unit {self.unit_path}: {exc}"
                )
        _remove_empty_directories(self.created_directories)
        return errors
```

The exact helpers above stage publication and preserve saved raw bytes, mode, and symlink target. They must not call generic `_restore_file_path` or the old enablement restore helper.

- [ ] **Step 6: Integrate `_SystemdArtifacts` into install and rollback**

In `install_server`, derive `unit_path` and `enablement_path`, then capture once:

```python
artifacts = _SystemdArtifacts.capture_for_install(
    unit_path,
    _enablement_path(unit_dir),
    upgrading,
)
```

Replace `write_unit`, direct enablement helper calls, and separate touched/snapshot/identity variables with:

```python
artifacts.publish_unit(unit_text)
service_touched = True
_run_systemctl(runner, ["daemon-reload"])
artifacts.ensure_enablement()
_run_systemctl(runner, ["restart", UNIT_NAME])
artifacts.verify_enablement()
```

Change `_rollback_install` to consume `artifacts: _SystemdArtifacts` instead of unit and enablement paths, snapshots, touched flags, and created identity. After runtime restoration and before rollback `daemon-reload`, append:

```python
errors.extend(artifacts.restore())
```

Update `_arm_install_failure` so the existing transaction matrices inject at the new module interface:

```python
elif boundary == "unit":
    real_publish_unit = install._SystemdArtifacts.publish_unit

    def failing_unit(artifacts, text):
        fail_once("unit boundary failed")
        return real_publish_unit(artifacts, text)

    monkeypatch.setattr(
        install._SystemdArtifacts,
        "publish_unit",
        failing_unit,
    )
elif boundary == "enablement":
    real_ensure_enablement = install._SystemdArtifacts.ensure_enablement

    def failing_enablement(artifacts):
        fail_once("enablement boundary failed")
        return real_ensure_enablement(artifacts)

    monkeypatch.setattr(
        install._SystemdArtifacts,
        "ensure_enablement",
        failing_enablement,
    )
```

Delete `test_write_unit_creates_then_overwrites`, the now-unused `SystemctlEnableOverwriteProbe` test double, and the now-unused `write_unit` function. Delete the obsolete install-only helpers `_ensure_enablement` and `_verify_enablement` after confirming no caller remains. Keep `_validate_enablement_link`, `_snapshot_enablement`, `_remove_owned_enablement`, and enablement restore helpers until Task 3 because the pre-refactor uninstall path still consumes them.

Do not call `_restore_file_path`, `_restore_enablement_snapshot`, `_ensure_enablement`, or `_verify_enablement` from install or install rollback. Leave uninstall helpers in place for Task 3.

- [ ] **Step 7: Run install transaction verification**

Run:

```bash
python3 -m pytest tests/test_install.py -q -k 'no_replace_publication or publish_known_unit_and_enablement_inodes or install_server_fresh or install_server_upgrade or enablement_collision or restart_failure or rollback or configured_probe or identity_change'
python3 -m pytest tests/test_install.py -q
.venv/bin/python -m pytest tests/test_server.py -q
.venv/bin/python -m pytest -q
python3 -m py_compile install.py tests/test_install.py
python3 install.py --help
```

Expected: all selections and suites PASS with no failures. Existing transaction matrix tests may need assertion updates only where they named removed internal `systemctl enable` or quarantine behavior; preserve their fresh/upgrade state guarantees.

- [ ] **Step 8: Prove new artifact tests fail against baseline production**

Archive `42e973d`, overlay current `tests/test_install.py`, and run:

```bash
python3 -m pytest tests/test_install.py -q -k 'no_replace_publication or publish_known_unit_and_enablement_inodes'
```

Expected: FAIL because baseline production lacks `_SystemdArtifacts`, overwrites a fresh unit through `_atomic_write`, and does not stage known link inodes. Confirm active HEAD/status are unchanged and remove the archive.

- [ ] **Step 9: Commit**

```bash
git add install.py tests/test_install.py
git commit -m "fix(install): centralize systemd artifact ownership"
```

## Task 3: Move uninstall cleanup into the artifact transaction

**Implementer tier:** Advanced

**Files:**

- Read: `docs/superpowers/specs/2026-08-10-server-installer-transaction-lock-remediation-design.md`
- Modify: `install.py:1198-1685`
- Test: `tests/test_install.py:1600-2515`

**Interfaces:**

- Consumes: `_SystemdArtifacts` and `_PathSnapshot` from Task 2; exact `_wait_for_unit_inactive(...) -> tuple[str, ...]`; decorated `uninstall_server(...) -> list[str]`; existing strict manifest and retryable root deletion helpers.
- Produces: `_SystemdArtifacts.capture_for_uninstall(unit_path: pathlib.Path, enablement_path: pathlib.Path) -> _SystemdArtifacts`; uninstall-aware `_SystemdArtifacts.restore() -> list[str]`; `_SystemdArtifacts.remove_for_uninstall() -> list[str]`; uninstall orchestration with no direct unit/link unlink, quarantine, or generic restore.

- [ ] **Step 1: Replace unsupported quarantine tests and add lock-bound uninstall tests**

Delete these tests because they inject arbitrary direct mutation after lock-protected validation or specifically test the removed quarantine implementation:

```text
test_uninstall_compensation_retains_replacement_after_validation
test_uninstall_compensation_retains_foreign_creation_race
test_uninstall_quarantines_replacement_after_removal_observation
test_uninstall_retains_original_path_collision_after_quarantine_move
test_uninstall_tolerates_enablement_enoent_during_quarantine_move
```

Replace `test_uninstall_compensation_retains_unexpected_enablement_from_missing_snapshot` with the supported systemctl-side reconciliation expectation:

```python
def test_uninstall_failed_stop_restores_missing_snapshot_after_disable_creates_link(
    checkout, tmp_path, monkeypatch
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = install._enablement_path(unit_dir)
    link.unlink()
    runner = DisableCreatesOwnedEnablementSystemctl(link)
    monkeypatch.setattr(install.time, "sleep", lambda delay: None)

    with pytest.raises(install.InstallError, match="did not become inactive") as exc:
        install.uninstall_server(
            root=root,
            unit_dir=unit_dir,
            systemctl=runner,
        )

    assert not os.path.lexists(link)
    assert "service enablement restored for retry" in str(exc.value)
    assert root.is_dir() and unit.is_file()
```

Retain all pre-entry foreign enablement tests, disable-removes-link, failed-stop compensation, and exact-inactive tests. Add these module and public-flow tests:

```python
def test_systemd_artifacts_uninstall_validates_both_before_removal(
    checkout, tmp_path
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = write_enablement_link(unit_dir)
    artifacts = install._SystemdArtifacts.capture_for_uninstall(unit, link)
    unit.write_text("changed before removal\n")

    with pytest.raises(install.InstallError, match="service unit changed"):
        artifacts.remove_for_uninstall()

    assert link.is_symlink()
    assert unit.read_text() == "changed before removal\n"
    assert root.is_dir()


def test_uninstall_retains_unit_changed_during_disable_before_cleanup(
    checkout, tmp_path
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = write_enablement_link(unit_dir)

    class UnitChangingDisable(StatefulSystemctl):
        def __call__(self, args, check=False):
            result = super().__call__(args, check=check)
            if args == ["disable", "--now", install.UNIT_NAME]:
                unit.write_text("changed during disable\n")
            return result

    runner = UnitChangingDisable(active=True, enabled=True)

    with pytest.raises(install.InstallError, match="service unit changed"):
        install.uninstall_server(
            root=root, unit_dir=unit_dir, systemctl=runner
        )

    assert unit.read_text() == "changed during disable\n"
    assert root.is_dir()


def test_uninstall_artifact_removal_reports_enablement_unit_and_root(
    checkout, tmp_path
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = write_enablement_link(unit_dir)
    runner = StatefulSystemctl(active=True, enabled=True)

    removed = install.uninstall_server(
        root=root, unit_dir=unit_dir, systemctl=runner
    )

    assert removed == [str(link), str(unit), str(root)]
    assert not os.path.lexists(link)
    assert not unit.exists()
    assert not root.exists()
```

- [ ] **Step 2: Run uninstall artifact tests and confirm RED**

Run:

```bash
python3 -m pytest tests/test_install.py -q -k 'systemd_artifacts_uninstall or unit_changed_during_disable or artifact_removal_reports or disable_removes_enablement or noninactive_state'
```

Expected: FAIL because `capture_for_uninstall` and `remove_for_uninstall` do not exist and uninstall still performs distributed direct cleanup.

- [ ] **Step 3: Add uninstall capture and failed-stop restoration**

Add this classmethod to `_SystemdArtifacts`:

```python
    @classmethod
    def capture_for_uninstall(
        cls,
        unit_path: pathlib.Path,
        enablement_path: pathlib.Path,
    ) -> "_SystemdArtifacts":
        unit_path = pathlib.Path(unit_path)
        enablement_path = pathlib.Path(enablement_path)
        unit = _observe_unit(unit_path)
        enablement = _observe_enablement(enablement_path)
        if enablement.kind != "missing" and not _snapshot_is_owned_enablement(
            enablement_path, unit_path, enablement
        ):
            raise OwnershipError(
                f"enablement path is foreign: {enablement_path}; retained "
                f"{_describe_enablement(enablement)}"
            )
        return cls(
            unit_path,
            enablement_path,
            unit,
            enablement,
            unit,
            enablement,
            "uninstall",
        )
```

At the start of Task 2's `restore()`, place the uninstall branch below before the existing `if self.operation != "install"` guard, then retain the install branch unchanged. It restores only the initial enablement state after failed inactivity proof and validates the unit without rewriting it:

```python
        if self.operation == "uninstall":
            errors = []
            try:
                current_unit = _observe_unit(self.unit_path)
                if not _snapshot_matches(self.unit_initial, current_unit):
                    raise InstallError(
                        f"service unit changed during failed uninstall: "
                        f"{self.unit_path}"
                    )
                current = _observe_enablement(self.enablement_path)
                if self.enablement_initial.kind == "missing":
                    if current.kind == "missing":
                        return errors
                    if not _snapshot_is_owned_enablement(
                        self.enablement_path,
                        self.unit_path,
                        current,
                    ):
                        raise InstallError(
                            f"foreign enablement retained at "
                            f"{self.enablement_path}: "
                            f"{_describe_enablement(current)}"
                        )
                    self.enablement_expected = current
                    self.enablement_path.unlink()
                    self.enablement_expected = _PathSnapshot("missing")
                    return errors
                target = self.enablement_initial.data
                if (
                    current.kind == "symlink"
                    and current.data == target
                    and _snapshot_is_owned_enablement(
                        self.enablement_path,
                        self.unit_path,
                        current,
                    )
                ):
                    self.enablement_expected = current
                    return errors
                if current.kind != "missing":
                    raise InstallError(
                        f"foreign or changed enablement retained at "
                        f"{self.enablement_path}: "
                        f"{_describe_enablement(current)}"
                    )
                self.enablement_expected = current
                self._restore_initial_enablement()
            except BaseException as exc:
                errors.append(
                    f"could not restore service enablement "
                    f"{self.enablement_path}: {exc}"
                )
            return errors
```

The install branch from Task 2 remains unchanged. A missing snapshot plus an exact owned link created by the installer-invoked disable path is removed; a foreign entry is retained and reported.

- [ ] **Step 4: Implement validate-all-then-remove uninstall semantics**

Implement `remove_for_uninstall()` with two phases while the server lock is held.

First observe and validate both artifacts without mutation:

```python
    def remove_for_uninstall(self) -> list[str]:
        if self.operation != "uninstall":
            raise InstallError(
                f"artifact removal mode is not uninstall: {self.operation}"
            )
        current_enablement = _observe_enablement(self.enablement_path)
        if current_enablement.kind != "missing" and not (
            _snapshot_is_owned_enablement(
                self.enablement_path,
                self.unit_path,
                current_enablement,
            )
        ):
            raise OwnershipError(
                f"enablement path became foreign before cleanup: "
                f"{self.enablement_path}; retained "
                f"{_describe_enablement(current_enablement)}"
            )
        current_unit = _observe_unit(self.unit_path)
        if not _snapshot_matches(self.unit_initial, current_unit):
            raise InstallError(
                f"service unit changed before cleanup: {self.unit_path}"
            )

        removed = []
        if current_enablement.kind != "missing":
            self.enablement_path.unlink()
            removed.append(str(self.enablement_path))
        if current_unit.kind != "missing":
            self.unit_path.unlink()
            removed.append(str(self.unit_path))
        return removed
```

This deliberately relies on the approved lock protocol between validation and unlink. Do not add quarantine, inode adoption, retry loops, or claims about direct same-UID mutation outside that protocol.

- [ ] **Step 5: Integrate artifacts into uninstall and remove obsolete helpers**

In `uninstall_server`, after strict manifest loading, replace direct unit/link snapshots with:

```python
artifacts = _SystemdArtifacts.capture_for_uninstall(
    pathlib.Path(owned["unit"]),
    _enablement_path(unit_dir),
)
```

On `_wait_for_unit_inactive` failure, call `artifacts.restore()`, include every returned error in the existing aggregate, and perform no cleanup.

After exact inactivity, replace `_remove_owned_enablement` and direct `unit_path.unlink()` with:

```python
removed.extend(artifacts.remove_for_uninstall())
```

Then retain daemon-reload and retryable root deletion ordering.

Delete the obsolete quarantine and distributed restore implementation once no caller remains:

```text
_ENABLEMENT_QUARANTINE_PREFIX
_validate_enablement_link
_snapshot_enablement
_remove_owned_enablement
_restore_enablement_snapshot
_restore_enablement_after_failed_disable
```

Keep `_observe_enablement`, `_snapshot_is_owned_enablement`, `_describe_enablement`, and target derivation inside the artifact module's implementation. Remove no helper still used by install capture or tests.

- [ ] **Step 6: Run uninstall, inactive-state, and full verification**

Run:

```bash
python3 -m pytest tests/test_install.py -q -k 'systemd_artifacts_uninstall or unit_changed_during_disable or artifact_removal_reports or uninstall_server or dangling_owned_enablement or disable_removes_enablement or noninactive_state or waits_from_deactivating or resets_failed_state or reset_failure or compensation or partial_root_delete'
python3 -m pytest tests/test_install.py -q
.venv/bin/python -m pytest tests/test_server.py -q
.venv/bin/python -m pytest -q
python3 -m py_compile install.py server.py tests/test_install.py tests/test_server.py
python3 install.py --help
git diff --check
```

Expected: all selections and suites PASS with no failures; exact-inactive and compensation tests remain green under the serialized contract.

- [ ] **Step 7: Prove uninstall regressions fail against baseline production**

Archive `42e973d`, overlay current `tests/test_install.py`, and run:

```bash
python3 -m pytest tests/test_install.py -q -k 'systemd_artifacts_uninstall or unit_changed_during_disable or artifact_removal_reports'
```

Expected: FAIL because baseline production lacks the artifact removal interface and can remove a unit changed during disable. Confirm active HEAD/status are unchanged and delete the archive.

- [ ] **Step 8: Commit**

```bash
git add install.py tests/test_install.py
git commit -m "fix(install): centralize systemd artifact removal"
```

## Task 4: Close endpoint parsing and document the supported contract

**Implementer tier:** Standard

**Files:**

- Read: `docs/superpowers/specs/2026-08-10-server-installer-transaction-lock-remediation-design.md`
- Modify: `install.py:487-535`
- Modify: `README.md:48-90`
- Test: `tests/test_install.py:760-855,2600-2645`

**Interfaces:**

- Consumes: `_load_service_endpoint(path: pathlib.Path) -> ServiceEndpoint`, `PreflightError`, `main(argv: list[str] | None = None) -> int`, and the serialized/artifact behavior from Tasks 1-3.
- Produces: total JSON/config endpoint preflight mapping decoder `ValueError` to `PreflightError`; README installation contract covering configured endpoint, config-only service mode, and cooperative serialization.

- [ ] **Step 1: Add oversized unquoted JSON endpoint and CLI regressions**

Add a direct endpoint test using a raw numeric token, not `json.dumps`:

```python
def test_load_service_endpoint_rejects_oversized_unquoted_json_integer(tmp_path):
    config = tmp_path / "config.json"
    config.write_text('{"port": ' + ("9" * 5000) + "}\n")

    with pytest.raises(install.PreflightError) as exc:
        install._load_service_endpoint(config)

    assert str(config) in str(exc.value)
    assert "JSON" in str(exc.value)
```

Parametrize the existing CLI malformed-port test over quoted Unicode and an unquoted oversized integer:

```python
@pytest.mark.parametrize(
    "raw_config",
    (
        json.dumps({"port": "\N{SUPERSCRIPT TWO}"}),
        '{"port": ' + ("9" * 5000) + "}",
    ),
)
def test_main_install_server_aggregates_malformed_port_without_traceback(
    raw_config, checkout, tmp_path, monkeypatch, capsys
):
    config = checkout / "config.example.json"
    config.write_text(raw_config)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    monkeypatch.setattr(install, "checkout_root", lambda: checkout)
    monkeypatch.setattr(install, "server_root", lambda: root)
    monkeypatch.setattr(install, "systemd_user_dir", lambda: unit_dir)
    monkeypatch.setattr(
        install,
        "default_systemctl",
        fake_systemctl(
            {"is-system-running": FakeCompleted(stdout="running\n")}
        ),
    )

    code = install.main(["install", "server"])

    captured = capsys.readouterr()
    assert code == 1
    assert str(config) in captured.out
    assert "Traceback" not in captured.out + captured.err
    assert not root.exists()
```

- [ ] **Step 2: Run endpoint tests and confirm RED**

Run:

```bash
python3 -m pytest tests/test_install.py -q -k 'oversized_unquoted or aggregates_malformed_port'
```

Expected: FAIL because `json.loads` raises raw `ValueError` for the oversized unquoted integer and `main` does not receive a `PreflightError` to aggregate.

- [ ] **Step 3: Make the JSON decode boundary total**

Change only the decode exception tuple in `_load_service_endpoint`:

```python
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError, ValueError) as exc:
    raise PreflightError(
        f"endpoint config is not readable JSON: {path}: {exc}"
    ) from exc
```

Do not add `ValueError` to `main`; malformed endpoint state must be normalized at the parsing seam. Preserve string-port ASCII checks, boolean rejection, integer support, defaults, and range validation.

- [ ] **Step 4: Update installation documentation**

Replace README's fixed-port preflight paragraph with text that states the authoritative behavior:

```markdown
The installer is stdlib-only and never needs root. It refuses to write into a
directory it does not own, and checks Python 3.11+, the systemd user session,
and the host/port selected by the installed `config.json` before mutation.
`--force` bypasses endpoint occupancy only.

Server install and uninstall operations are serialized by a per-user runtime
lock. A concurrent installer invocation fails before preflight or mutation.
Direct edits to managed unit/link paths, or independent mutating `systemctl`
commands, are unsupported while an installer transaction is active; foreign
state observed when the transaction starts is retained and reported.

The systemd-managed service runs in config-only mode. Its installed
`config.json` is authoritative for host, port, voice, and runtime settings;
inherited per-setting `TTS_*` variables do not override it. `INVOCATION_ID`
remains available for service identity verification.
```

Keep the separate manual/development configuration section accurate by replacing its unqualified precedence sentence with:

```markdown
When `server.py` runs manually, settings may come from `config.json` or the
listed environment variables, with environment variables taking precedence.
The installed systemd service uses the config-only behavior described above.
```

Do not alter desktop installation documentation.

- [ ] **Step 5: Run focused and complete verification**

Run:

```bash
python3 -m pytest tests/test_install.py -q -k 'service_endpoint or malformed_port or configured_probe or custom_port or server_transaction_lock'
python3 -m pytest tests/test_install.py -q
.venv/bin/python -m pytest tests/test_server.py -q
.venv/bin/python -m pytest -q
python3 -m py_compile install.py server.py tests/test_install.py tests/test_server.py
python3 install.py --help
git diff --check
git diff --exit-code 42e973dff7b3eb729b4384765cef6ddee6dad51a -- desktop/install.py tests/test_extension_split_sentences.py tests/test_media_session.py
```

Expected: every command passes; installer/server/full counts are at least the prior 141/95/614 baseline after contract-based test replacements and new regressions; protected files are unchanged.

- [ ] **Step 6: Prove the unquoted numeric regressions fail against baseline production**

Archive `42e973d`, overlay current `tests/test_install.py`, and run:

```bash
python3 -m pytest tests/test_install.py -q -k 'oversized_unquoted or aggregates_malformed_port'
```

Expected: FAIL because baseline `json.loads` lets decoder `ValueError` escape. Confirm active HEAD/status are unchanged and delete the archive.

- [ ] **Step 7: Commit**

Inspect the final branch state first:

```bash
git status --short
git diff --stat
git diff --check
```

Only `install.py`, `tests/test_install.py`, and `README.md` may differ from the Task 3 commit.

```bash
git add install.py tests/test_install.py README.md
git commit -m "fix(install): close serialized transaction contract"
```
