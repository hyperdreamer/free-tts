# Server Installer Inactive-State Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic
> subagent-driven-development controller to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close F-16 by requiring an exact terminal inactive systemd state before
uninstall filesystem cleanup and restoring the prior enablement state whenever
that postcondition cannot be proven.

**Architecture:** Add an exact activity-state query plus bounded wait around
`disable --now`. Snapshot the validated wants path before disable; on timeout,
query/reset failure, or any nonterminal outcome, compensate that path without
overwriting foreign data and retain root/unit.

**Tech Stack:** Python 3.11+ standard library, pytest, and injected systemd user
service doubles.

Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).

## Global Constraints

- The approved design is `docs/superpowers/specs/2026-08-10-server-installer-inactive-remediation-design.md`; implement only F-16 from terminal review `/data/home/guest/Development/ai/free-tts/.worktrees/server-installer/.superpowers/sdd/2026-08-10-server-installer-remediation/final-rereview-report.md`.
- Both prior runs remain untouched in `FINAL_BLOCKED`; this plan executes in a third fresh run root.
- Baseline commit is `b1fa605a37041128e0b48604800bdcaa34dd537c`; original merge base remains `5c9bac2a1940e2d64122decc810bd496eedc6470`.
- Python 3.11 remains the minimum supported interpreter.
- `install.py` and `tests/test_install.py` remain standard-library-only. Do not modify dependency files.
- Tests never invoke real systemctl, real virtualenv creation, or real network calls. Inject systemctl state sequences, sleeper, paths, venv builder, occupancy, and fetch.
- Never modify `desktop/install.py`, `tests/test_extension_split_sentences.py`, or `tests/test_media_session.py`.
- Ownership is established before mutation. Failed inactivity proof restores the exact prior absent/owned enablement state and retains root/unit, or reports every compensation failure without overwriting foreign data.
- Preserve config-authoritative systemd operation, configured endpoint checks, PID/InvocationID verification, install enablement ownership/rollback, strict manifests, retryable root deletion, desktop delegation, CLI behavior, and every earlier fixed finding.
- `--force` remains endpoint-occupancy-only.
- Baseline verification is 118 installer tests, 95 server tests, and 591 full-suite tests. Higher totals are expected; zero failures are required.
- New regressions must fail for the intended F-16 mechanisms against baseline production code in a temporary `git archive`. Never move active `HEAD`.
- The task ends in one scoped commit. Final Frontier review covers `5c9bac2a1940e2d64122decc810bd496eedc6470..HEAD` and reconciles all findings from all prior runs, including F-16.
- Do not merge the feature branch or migrate the live service during this plan.

## Task 1: Exact inactive gate and enablement compensation

**Implementer tier:** Advanced

**Files:**

- Modify: `install.py:550-620,1190-1275`
- Test: `tests/test_install.py:1630-1870`

**Interfaces:**

- Consumes: `_systemctl_error(runner, args) -> str | None`, `_run_systemctl(runner, args) -> None`, `_snapshot_path(path) -> _PathSnapshot`, `_restore_path(path, snapshot) -> None`, `_validate_enablement_link(path, unit_path) -> bool`, and the existing strict `uninstall_server` ownership flow.
- Produces: `_query_unit_active_state(runner: Callable[..., object]) -> str`; `_wait_for_unit_inactive(runner, *, attempts: int, delay: float, sleeper: Callable[[float], None]) -> tuple[str, ...]`; `_restore_enablement_after_failed_disable(path: pathlib.Path, unit_path: pathlib.Path, snapshot: _PathSnapshot) -> None`.
- Changes: `uninstall_server(..., stop_attempts: int = 10, stop_delay: float = 0.2, sleeper: Callable[[float], None] | None = None) -> list[str]` resolves `sleeper or time.sleep` at call time, snapshots enablement before disable, waits for exact `inactive`, compensates on failure, and performs no unit/root removal before success.

- [ ] **Step 1: Write the failing filesystem-backed activity-state tests**

Add this deterministic runner near existing uninstall doubles in
`tests/test_install.py`:

```python
class DisableActivitySequenceSystemctl:
    """Disable removes the wants link, then reports a scripted activity sequence."""

    def __init__(
        self,
        link,
        states,
        *,
        disable_returncode=0,
        reset_returncode=0,
        replacement=None,
    ):
        self.link = link
        self.states = list(states)
        self.last_state = self.states[-1]
        self.disable_returncode = disable_returncode
        self.reset_returncode = reset_returncode
        self.replacement = replacement
        self.calls = []

    def __call__(self, args, check=True):
        args = list(args)
        self.calls.append(args)
        if args[0] == "disable":
            if os.path.lexists(self.link):
                self.link.unlink()
            if self.replacement == "file":
                self.link.write_text("foreign replacement\n")
            return FakeCompleted(
                returncode=self.disable_returncode,
                stderr="disable failed" if self.disable_returncode else "",
            )
        if args[0] == "is-active":
            state = self.states.pop(0) if self.states else self.last_state
            return FakeCompleted(
                returncode=0 if state == "active" else 3,
                stdout=f"{state}\n",
            )
        if args[0] == "reset-failed":
            return FakeCompleted(
                returncode=self.reset_returncode,
                stderr="reset failed" if self.reset_returncode else "",
            )
        return FakeCompleted()
```

Add an exact snapshot helper for the owned link:

```python
def assert_owned_enablement_restored(link, target):
    assert link.is_symlink()
    assert os.readlink(link) == target
```

Add these regressions:

```python
@pytest.mark.parametrize("state", ("active", "activating", "reloading", "deactivating"))
def test_uninstall_noninactive_state_restores_enablement_and_keeps_files(
    state, checkout, tmp_path, monkeypatch
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = write_enablement_link(unit_dir)
    target = os.readlink(link)
    before_root = snapshot_tree(root)
    before_unit = unit.read_bytes()
    runner = DisableActivitySequenceSystemctl(link, [state, state])
    monkeypatch.setattr(install.time, "sleep", lambda delay: None)

    with pytest.raises(install.InstallError, match="did not become inactive"):
        install.uninstall_server(
            root=root,
            unit_dir=unit_dir,
            systemctl=runner,
        )

    assert snapshot_tree(root) == before_root
    assert unit.read_bytes() == before_unit
    assert_owned_enablement_restored(link, target)


def test_uninstall_waits_from_deactivating_to_inactive(
    checkout, tmp_path, monkeypatch
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = write_enablement_link(unit_dir)
    sleeps = []
    runner = DisableActivitySequenceSystemctl(link, ["deactivating", "inactive"])
    monkeypatch.setattr(install.time, "sleep", sleeps.append)

    removed = install.uninstall_server(
        root=root,
        unit_dir=unit_dir,
        systemctl=runner,
    )

    assert sleeps == [0.2]
    assert not os.path.lexists(link)
    assert not unit.exists()
    assert not root.exists()
    assert str(unit) in removed and str(root) in removed


def test_uninstall_resets_failed_state_before_cleanup(
    checkout, tmp_path, monkeypatch
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    link = write_enablement_link(unit_dir)
    runner = DisableActivitySequenceSystemctl(link, ["failed", "inactive"])
    monkeypatch.setattr(install.time, "sleep", lambda delay: None)

    install.uninstall_server(
        root=root,
        unit_dir=unit_dir,
        systemctl=runner,
    )

    assert ["reset-failed", install.UNIT_NAME] in runner.calls
    assert not root.exists()
```

- [ ] **Step 2: Write failing compensation-error and absent-state tests**

Add:

```python
def test_uninstall_reset_failure_restores_enablement(checkout, tmp_path):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = write_enablement_link(unit_dir)
    target = os.readlink(link)
    runner = DisableActivitySequenceSystemctl(
        link, ["failed"], reset_returncode=7
    )

    with pytest.raises(install.InstallError, match="reset-failed"):
        install.uninstall_server(
            root=root,
            unit_dir=unit_dir,
            systemctl=runner,
        )

    assert root.is_dir() and unit.is_file()
    assert_owned_enablement_restored(link, target)


def test_uninstall_compensation_retains_foreign_replacement(
    checkout, tmp_path, monkeypatch
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = write_enablement_link(unit_dir)
    runner = DisableActivitySequenceSystemctl(
        link, ["active"], replacement="file"
    )
    monkeypatch.setattr(install.time, "sleep", lambda delay: None)

    with pytest.raises(install.InstallError) as excinfo:
        install.uninstall_server(
            root=root,
            unit_dir=unit_dir,
            systemctl=runner,
        )

    assert link.read_text() == "foreign replacement\n"
    assert "could not restore service enablement" in str(excinfo.value)
    assert root.is_dir() and unit.is_file()


def test_uninstall_failed_stop_preserves_absent_enablement(
    checkout, tmp_path, monkeypatch
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = install._enablement_path(unit_dir)
    assert not os.path.lexists(link)
    runner = DisableActivitySequenceSystemctl(link, ["deactivating"])
    monkeypatch.setattr(install.time, "sleep", lambda delay: None)

    with pytest.raises(install.InstallError):
        install.uninstall_server(
            root=root,
            unit_dir=unit_dir,
            systemctl=runner,
        )

    assert not os.path.lexists(link)
    assert root.is_dir() and unit.is_file()


def test_uninstall_unknown_activity_restores_enablement(checkout, tmp_path):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = write_enablement_link(unit_dir)
    target = os.readlink(link)
    runner = DisableActivitySequenceSystemctl(link, ["unknown"])

    with pytest.raises(install.InstallError, match="unrecognized state"):
        install.uninstall_server(root=root, unit_dir=unit_dir, systemctl=runner)

    assert_owned_enablement_restored(link, target)
    assert root.is_dir() and unit.is_file()
```

- [ ] **Step 3: Run the new tests and confirm RED**

Run:

```bash
python3 -m pytest tests/test_install.py -q -k 'noninactive_state or waits_from_deactivating or resets_failed_state or reset_failure or compensation_retains or preserves_absent_enablement or unknown_activity'
```

Expected: FAIL against baseline because transitional/failed states are accepted as
stopped, there is no bounded wait/reset behavior, and wants links removed before a
failed stop are not restored. Tests must fail on assertions or missing API, not a
broken double.

- [ ] **Step 4: Implement exact state query and bounded wait**

Add:

```python
_UNIT_ACTIVITY_STATES = frozenset(
    {
        "active",
        "activating",
        "deactivating",
        "failed",
        "inactive",
        "maintenance",
        "reloading",
        "unknown",
    }
)


def _query_unit_active_state(runner: Callable[..., object]) -> str:
    args = ["is-active", UNIT_NAME]
    try:
        result = runner(args, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError(
            f"systemctl --user is-active {UNIT_NAME} could not run: {exc}"
        ) from exc
    state = str(getattr(result, "stdout", "") or "").strip()
    if state not in _UNIT_ACTIVITY_STATES or state == "unknown":
        detail = str(getattr(result, "stderr", "") or state or "no diagnostic output")
        raise InstallError(
            f"systemctl --user is-active {UNIT_NAME} returned unrecognized state: "
            f"{detail.strip()}"
        )
    return state


def _wait_for_unit_inactive(
    runner: Callable[..., object],
    *,
    attempts: int,
    delay: float,
    sleeper: Callable[[float], None],
) -> tuple[str, ...]:
    history = []
    reset_attempted = False
    for attempt in range(max(1, attempts)):
        state = _query_unit_active_state(runner)
        history.append(state)
        if state == "inactive":
            return tuple(history)
        if state == "failed" and not reset_attempted:
            _run_systemctl(runner, ["reset-failed", UNIT_NAME])
            reset_attempted = True
        if attempt + 1 < max(1, attempts):
            sleeper(delay)
    raise InstallError(
        f"{UNIT_NAME} did not become inactive; observed: {', '.join(history)}"
    )
```

Do not change `_query_unit_state`; install rollback and status still consume its
existing boolean contract.

- [ ] **Step 5: Implement enablement compensation in uninstall**

Add:

```python
def _restore_enablement_after_failed_disable(
    path: pathlib.Path,
    unit_path: pathlib.Path,
    snapshot: _PathSnapshot,
) -> None:
    # Absence is safe. Any present path must still be our validated link;
    # foreign replacements are retained and reported by validation.
    _validate_enablement_link(path, unit_path)
    _restore_path(path, snapshot)
```

In `uninstall_server`, add injected stop controls to the signature. Immediately
after strict enablement validation, capture `enablement_snapshot =
_snapshot_path(enablement)`. Run `disable --now`, then call
`_wait_for_unit_inactive` before any link/unit/root removal.

Resolve the production sleeper at call time so tests can monkeypatch the clock
without passing a keyword unknown to baseline production code:

```python
stop_sleeper = time.sleep if sleeper is None else sleeper
```

Catch every inactivity-query/reset/timeout exception. Attempt enablement
compensation and aggregate errors:

```python
try:
    history = _wait_for_unit_inactive(...)
except BaseException as stop_error:
    compensation = None
    try:
        _restore_enablement_after_failed_disable(
            enablement, unit_path, enablement_snapshot
        )
    except BaseException as exc:
        compensation = exc
    detail = f"server uninstall failed:\n- {stop_error}"
    if disable_failure is not None:
        detail += f"\n- {disable_failure}"
    if compensation is not None:
        detail += f"\n- could not restore service enablement {enablement}: {compensation}"
    else:
        detail += "\n- service enablement restored for retry"
    raise InstallError(detail) from stop_error
```

Only after exact inactivity succeeds should existing post-disable link
revalidation/removal continue. Preserve nonzero-disable tolerance only after
inactive plus link-absent postconditions are proven. Do not remove root/unit in
any stop-failure branch.

Update existing uninstall tests only where the new bounded query adds expected
calls; never weaken filesystem or retry assertions.

- [ ] **Step 6: Run scoped tests and confirm GREEN**

Run:

```bash
python3 -m pytest tests/test_install.py -q
.venv/bin/python -m pytest tests/test_server.py -q
```

Expected: PASS with no failures (118 installer and 95 server tests at baseline;
totals increase).

- [ ] **Step 7: Prove F-16 regressions against baseline production code**

Run:

```bash
BASE=b1fa605a37041128e0b48604800bdcaa34dd537c
HEAD_BEFORE=$(git rev-parse HEAD)
STATUS_BEFORE=$(git status --porcelain=v1)
TMP=$(mktemp -d)
git archive HEAD | tar -x -C "$TMP"
cp tests/test_install.py "$TMP/tests/test_install.py"
git show "$BASE:install.py" > "$TMP/install.py"
(cd "$TMP" && python3 -m pytest tests/test_install.py -q -k 'noninactive_state or waits_from_deactivating or resets_failed_state or reset_failure or compensation_retains or preserves_absent_enablement or unknown_activity')
STATUS=$?
rm -rf "$TMP"
test "$STATUS" -ne 0
test "$(git rev-parse HEAD)" = "$HEAD_BEFORE"
test "$(git status --porcelain=v1)" = "$STATUS_BEFORE"
```

Expected: selected tests FAIL against baseline for F-16's intended transitional
state and missing-compensation mechanisms. Temporary files are removed and active
HEAD/status are unchanged.

- [ ] **Step 8: Run full verification**

Run:

```bash
.venv/bin/python -m pytest -q
python3 -m py_compile install.py server.py tests/test_install.py tests/test_server.py
python3 install.py --help
git diff --check
```

Expected: full suite PASS with no failures (591 at baseline), help exits 0, and no
diff errors.

- [ ] **Step 9: Commit**

```bash
git add install.py tests/test_install.py
git commit -m "fix(install): require terminal inactive state on uninstall"
```
