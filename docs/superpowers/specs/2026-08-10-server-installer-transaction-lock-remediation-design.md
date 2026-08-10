# Server Installer Transaction Lock Remediation Design

## Context

The server installer implementation is functionally complete and its inactive-state behavior is covered, but the third deterministic run ended in `FINAL_BLOCKED` after its sole final-fix wave. Terminal Frontier re-review found four load-bearing residuals:

- F-17: a same-target enablement symlink replacement between creation and first observation can be misidentified as installer-created and deleted during rollback.
- F-18: quarantine and original-path entries can change after observation, so a later pathname unlink can delete a replacement or miss a late collision.
- F-20: unit-fragment install, rollback, and uninstall use equivalent inspect-then-replace or inspect-then-unlink sequences.
- F-21: `json.loads` can raise `ValueError` for an oversized unquoted integer before string-port validation runs.

F-17, F-18, and F-20 are not independent defects. They expose an unsupported concurrency assumption: pathname inspection cannot authorize a later mutation against an arbitrary same-user process. Moving a path into quarantine does not solve that limitation because the quarantine pathname can itself be replaced after observation.

This remediation replaces the impossible adversarial-path guarantee with an explicit serialized installer contract. It also concentrates unit and enablement ownership behavior in one internal deep module instead of distributing it across install, rollback, and uninstall.

## Goals

1. Serialize every server install and uninstall transaction with a secure per-user process lock.
2. Support concurrent installer invocations deterministically: one transaction proceeds and every contender fails before target inspection or mutation.
3. Preserve foreign entries observed at transaction entry or at a no-replace publication point.
4. Give the unit fragment and enablement link one ownership interface for capture, publication, verification, rollback, and removal.
5. Preserve exact-inactive shutdown, failed-stop enablement compensation, configured endpoint verification, service identity checks, strict manifests, retryable root removal, desktop delegation, and CLI aggregation.
6. Convert every JSON-decoding and port-conversion limit failure to a path-specific `PreflightError`.
7. Keep installer implementation and tests standard-library-only and hermetic.

## Non-Goals

- Protect against an independent process that directly edits managed paths or runs mutating `systemctl enable`, `disable`, or `reenable` while the installer lock is held.
- Provide hostile same-UID process isolation. A process with the same effective UID can modify user-owned paths and lock files outside the supported protocol.
- Add a native syscall extension, dependency, daemon, privilege boundary, or helper executable.
- Change desktop installation behavior or protected desktop/media tests.
- Merge the feature branch or migrate the live service.

## Scope

Implementation may modify only `install.py`, `tests/test_install.py`, and `README.md`. The design document and subsequent implementation plan are the only additional files. No dependency or packaging metadata changes are permitted.

Never modify `desktop/install.py`, `tests/test_extension_split_sentences.py`, or `tests/test_media_session.py`.

The verified starting baseline is 141 installer tests, 95 server tests, and 614 complete-suite tests at `42e973dff7b3eb729b4384765cef6ddee6dad51a`.

## Supported Concurrency Contract

All calls to `install_server()` and `uninstall_server()` acquire the same exclusive lock before preflight, manifest loading, systemd-artifact inspection, or target mutation. The lock remains held through service verification, successful cleanup, or complete rollback, and is released in `finally` behavior on every exit path.

Concurrent installer invocations are supported. A contender fails immediately with a path-specific `PreflightError`; it does not run preflight hooks, systemctl, network probes, virtualenv construction, ownership reads, or target mutations.

Manual filesystem mutation and independent mutating systemctl operations against free-tts-owned paths while the installer transaction is active are unsupported. Foreign state present before lock acquisition, or encountered by an atomic no-replace publication, remains protected. Best-effort validation still aborts on a changed artifact observed before mutation, but the installer does not claim atomic compare-and-delete semantics that the standard filesystem interface does not provide.

Read-only `status` remains unlocked. It may report a transient state while a transaction is active and must remain tolerant of missing or changing files.

## Transaction Lock

A private context manager, `_server_transaction_lock`, provides the lock seam.

The production lock path is `$XDG_RUNTIME_DIR/free-tts-installer.lock`. `XDG_RUNTIME_DIR` must be absolute and name an existing, non-symlink directory owned by the effective user. Tests may inject or monkeypatch the resolved lock path without changing install/uninstall behavior.

The lock file is opened with `O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW` and mode `0600`. `fstat` must prove that the opened object is a regular file owned by the effective user and has no group or other permission bits. An existing valid unlocked file is reusable; stale file contents do not indicate lock ownership. Invalid runtime directories, symlink lock paths, non-regular files, foreign ownership, and insecure permissions produce `PreflightError`.

`fcntl.flock(fd, LOCK_EX | LOCK_NB)` provides fail-fast exclusion. `BlockingIOError` becomes an actionable error naming the lock and stating that another server installer operation is active. The descriptor remains open for the full transaction and is always closed after unlocking.

The lock file may persist after the operation. Its existence is not installed-component state; only the kernel lock determines activity.

## Systemd Artifact Module

A private `_SystemdArtifacts` module owns the unit fragment and the sole `default.target.wants/free-tts.service` enablement link. It is implemented inside `install.py` to preserve the unified self-contained installer and introduce no import or packaging dependency.

Its interface provides the minimum operations needed by orchestration:

- `capture_for_install(unit_path, enablement_path, upgrading)`
- `capture_for_uninstall(unit_path, enablement_path)`
- `publish_unit(unit_text)`
- `ensure_enablement()`
- `verify_enablement()`
- `restore()`
- `remove_for_uninstall()`

The module hides raw snapshots, expected-current state, no-replace publication, rollback ordering, and ownership diagnostics. `install_server` and `uninstall_server` do not call `unlink`, `rename`, `replace`, generic path restoration, or enablement helpers directly for these artifacts.

### Capture

Capture occurs under the server lock.

For a fresh install, both artifacts must be missing. For an upgrade or uninstall, the unit may be missing or a regular non-symlink file, and enablement may be missing or a symlink resolving to the canonical owned unit. Any regular file, directory, special file, or foreign symlink at the enablement path is foreign. Any symlink, directory, or special file at the unit path is foreign.

Snapshots preserve exact unit bytes and mode plus the enablement link's raw target. Internal expected-current state records which mutations the active transaction has performed. The module validates that state immediately before each destructive mutation. Under the supported lock protocol, that validation is stable with respect to every other installer invocation.

### Unit Publication

A unit is staged as a regular sibling file with complete contents and mode before publication.

When the captured unit is missing, publication uses a no-replace operation so an existing entry is never overwritten. A collision is observed, retained, and reported. When the captured unit is an owned regular file, the staged file atomically replaces it after expected-current validation under the lock.

The module records whether the transaction created or replaced the unit so rollback can restore the captured missing or exact-file state.

### Enablement Publication

The installer does not invoke mutating `systemctl enable` or `reenable`. The controlled unit has one known `WantedBy=default.target` link.

When enablement is missing, `os.symlink` creates the known relative target and fails without replacement on collision. When an exact owned link already exists, it remains byte-for-byte unchanged. Verification confirms the link still resolves to the owned unit before service health is accepted.

Rollback uses transaction history under the lock: a link created from a missing snapshot is removed after expected-current validation; a captured raw target is recreated only into an absent path or accepted when already exact. Any observed mismatch aborts compensation and is retained with an explicit diagnostic.

### Uninstall Removal

After systemd reaches exact `inactive`, `remove_for_uninstall()` reconciles the expected effect of `disable --now`: an already missing enablement path is accepted, while a still-present exact owned link is removed after expected-current validation. A foreign entry aborts before unit or root removal.

The unit is then removed only when it is still the captured owned regular file. Missing is accepted for retryable partial states. Any changed kind or content identity observed before mutation aborts and retains the root.

The prior quarantine-and-observe protocol is removed. Under the supported lock contract it adds complexity without providing stronger guarantees, and outside that contract it cannot make pathname deletion adversary-safe.

## Install Flow

`install_server` performs the following while holding the lock:

1. Run Python/systemd preflight and strict root ownership validation.
2. Capture `_SystemdArtifacts`.
3. Parse the authoritative config endpoint and check occupancy before target mutation.
4. Stage and transactionally publish the runtime root.
5. Build the virtualenv when needed.
6. Publish the unit through `_SystemdArtifacts`.
7. Run `daemon-reload`.
8. Ensure enablement through `_SystemdArtifacts`.
9. Restart the unit.
10. Verify enablement, stable MainPID and InvocationID, and the configured health endpoint.
11. Remove superseded rollback data and release the lock.

On failure, service state, artifacts, runtime root, and temporary directories are restored in reverse dependency order. Every rollback error is aggregated. Failed runtime data remains at a named sibling whenever exact restoration is incomplete.

## Uninstall Flow

`uninstall_server` performs the following while holding the same lock:

1. Strictly load the owned manifest and capture `_SystemdArtifacts`.
2. Snapshot enablement and run `disable --now`.
3. Poll bounded systemd state until exact `inactive`, using one `reset-failed` transition when required.
4. Restore the captured enablement through `_SystemdArtifacts` if inactivity cannot be proven.
5. Remove enablement and unit artifacts through `_SystemdArtifacts`.
6. Run `daemon-reload`.
7. Remove the root through the existing retryable sibling transaction.
8. Release the lock.

No artifact or root cleanup occurs before exact inactivity. Disable failure remains tolerable only when exact inactivity and expected enablement postconditions are established.

## Endpoint Parse Boundary

`_load_service_endpoint` treats JSON decoding as a total preflight boundary. The `json.loads` block catches `OSError`, `json.JSONDecodeError`, and `ValueError`, including Python's integer-string conversion limit for an oversized unquoted numeric token. All become path-specific `PreflightError`.

String port conversion remains inside its own guarded boundary. Booleans and unsupported types are rejected; ASCII integer-like strings and integer values remain accepted only in the range `1..65535`; missing values retain existing defaults.

## Error Semantics

- Lock setup or contention: `PreflightError`, no managed-target inspection or mutation.
- Foreign artifact at capture: `OwnershipError`, no server target mutation.
- No-replace collision: `OwnershipError` or `InstallError` naming the retained path, followed by transaction rollback where mutation had already begun.
- Service stop or inactivity failure: restore enablement before any filesystem cleanup and report every compensation failure.
- Artifact rollback mismatch: retain the observed entry and failed runtime, then report incomplete rollback.
- Root deletion failure: restore strict manifest ownership or name the retained deletion sibling for retry.
- Endpoint decode or conversion failure: path-specific `PreflightError`, aggregated by CLI without traceback.

## Testing Strategy

Tests remain in `tests/test_install.py` and use only standard-library filesystem/process primitives plus the existing test framework. They perform no real systemctl, network, virtualenv, or live-service operations.

### Lock regressions

- Two independently opened descriptors prove real `flock` contention.
- A held lock causes install and uninstall to fail before preflight callbacks, systemctl calls, network probes, or managed-target mutation.
- The lock is released after success, preflight failure, implementation failure, and incomplete rollback.
- A stale valid unlocked lock file is reusable.
- Symlink, directory, foreign-owner simulation, and insecure-mode lock cases fail cleanly.
- Install and uninstall resolve the same default lock path.

### Artifact regressions

- Fresh install publishes a regular unit and exact relative enablement link without calling `systemctl enable`.
- Upgrade preserves or repairs missing owned artifacts and restores exact snapshots on each failure boundary.
- No-replace unit and enablement collisions retain the competing entries and make rollback explicit.
- Uninstall accepts systemd removing the link, handles missing-unit retry state, and rejects foreign state before unit/root mutation.
- Exact-inactive, reset-failed, configured endpoint, identity, root retry, and desktop delegation regressions remain green.

Tests whose sole premise is arbitrary path replacement after lock-protected validation are replaced by lock-boundary tests. Pre-entry foreign-state tests and publication-collision tests remain. The suite must not claim support for a concurrency model excluded by this design.

### Endpoint regressions

- Oversized quoted string ports remain path-specific `PreflightError`.
- Oversized unquoted JSON numeric ports become path-specific `PreflightError`.
- Direct endpoint parsing and CLI aggregation return cleanly without traceback.
- Valid integer/string ports, booleans, range limits, and defaults remain covered.

New regressions must fail against `42e973d` for the intended missing lock, distributed artifact ownership, or unhandled JSON `ValueError` mechanism. Read-only baseline probes must leave active HEAD and status unchanged.

Required verification is the focused lock/artifact/endpoint selection, all installer tests, all server tests, the complete suite, pycompile, CLI help, current/range `git diff --check`, protected-file checks, pinned-HEAD checks, and clean status.

## Documentation

README installation documentation will state the serialized concurrency contract and that manual mutation of managed paths during an active transaction is unsupported. It will also replace fixed-port wording with the authoritative configured endpoint and describe config-only systemd operation.

Historical specs, plans, and terminal run state remain immutable evidence. This design supersedes their implicit arbitrary-concurrency interpretation without rewriting them.

## Finding Resolution Map

- F-17: resolved by installer serialization, explicit unsupported manual mutation, no-replace enablement publication, and lock-bound rollback expectations.
- F-18: resolved by removing pseudo-adversarial quarantine logic and performing validated cleanup under the supported lock protocol.
- F-20: resolved by placing unit publication, rollback, and removal in the same lock-bound artifact module.
- F-21: resolved by catching `ValueError` at the JSON decoding boundary and adding unquoted oversized-number regressions.

## Alternatives Rejected

### Minimal Lock Wrapper

Wrapping the existing implementation would serialize supported callers but retain duplicated and misleading quarantine/identity machinery. It would leave ownership behavior difficult to audit and likely to drift again.

### Linux-Specific Syscall Layer

`renameat2`, `linkat`, or native wrappers can strengthen publication but do not provide a general atomic unlink-if-inode operation. They add complexity without removing the need for the approved concurrency contract.

### Absolute Same-UID Adversarial Safety

A same-UID process can replace user-owned pathnames and lock files outside the protocol. Guaranteeing cleanup while forbidding deletion of every possible replacement is not implementable with pathname-based standard-library operations. This is explicitly outside the supported model rather than represented as a partially implemented guarantee.
