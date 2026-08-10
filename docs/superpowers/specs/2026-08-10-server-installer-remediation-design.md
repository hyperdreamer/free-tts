# Server Installer Residual Remediation Design

Date: 2026-08-10
Status: Approved

## Purpose

The original server-installer run ended legally in `FINAL_BLOCKED` at revision
75 after its single final-fix wave. That wave corrected the original ownership,
transaction, port-occupancy, systemctl-error, config-publication, and unit-path
findings, but a fresh Frontier re-review found four load-bearing residuals:
F-10 through F-13.

This remediation closes those residuals in a new deterministic run without
reopening or mutating the terminal run. The user-visible goal is unchanged: a
per-user installer must safely install, verify, report, and remove a self-contained
free-tts systemd user service on this machine or another Linux machine.

## Scope

This remediation covers only:

1. Configured service endpoint resolution during fresh install and reinstall
   (F-10).
2. Proof that post-restart health belongs to the systemd unit process that was
   just started (F-11).
3. Cleanup of an owned dangling systemd enablement link when the unit fragment is
   already absent (F-12).
4. Retryable server-root cleanup after partial filesystem deletion (F-13).

Commit `562c1ee8ce713690a16c2d3ffe2a48026b0d00db` is the remediation baseline.
Earlier fixed findings remain fixed, and earlier parked Minor findings remain out
of scope unless one of these corrections naturally supersedes them. No unrelated
refactor, dependency change, desktop-installer change, or live-machine migration
is in scope.

## Global Constraints

- Python 3.11 remains the minimum supported interpreter.
- `install.py` and `tests/test_install.py` remain standard-library-only; no
  dependency is added to `requirements.txt`.
- Tests never invoke real `systemctl`, real virtualenv creation, or real network
  calls. Systemctl, endpoint fetch, socket occupancy, sleep, and filesystem
  failures remain injectable.
- `desktop/install.py`, `tests/test_extension_split_sentences.py`, and
  `tests/test_media_session.py` are not modified.
- The server root remains `~/.local/share/free-tts-server`; the desktop root
  remains independent at `~/.local/share/free-tts`.
- The systemd-managed server reads `config.json` beside `server.py`. The unit does
  not set `TTS_CONFIG`; it enables a dedicated config-only mode in which every
  per-setting `TTS_*` environment override is ignored, making the installed config
  authoritative for all server settings.
- Ownership must be established before mutation, writes remain atomic, and every
  failed operation leaves either the exact previous installation or a state that
  can be retried without manual ownership repair.
- A fresh deterministic SDD run starts from the current feature branch. Its final
  Frontier review covers the original merge base through the remediated HEAD and
  reconciles the complete prior finding ledger.

## Architecture

### 1. Authoritative Service Endpoint

`install.py` introduces an immutable service-endpoint value containing the bind
host, the host used for local verification, and the port. Endpoint resolution is
read-only and happens before any target mutation:

- on fresh install, it reads the checkout's `config.example.json`, which becomes
  the initial installed config;
- on reinstall, it reads the existing manifest-owned `config.json` when present;
- if an owned config is missing, it falls back to the checkout example that will
  be bootstrapped into the staged tree.

The config must be a regular non-symlink JSON object. Missing `host` and `port`
keys use `127.0.0.1` and `5000`. The host must be a non-empty string. The port may
be an integer or an integer-like string, but booleans and values outside
`1..65535` are rejected with `PreflightError`. Malformed configuration is rejected
before mutation instead of silently probing a different endpoint from the server.

For a wildcard bind, verification uses the matching loopback address:
`0.0.0.0` maps to `127.0.0.1`, while `::` maps to `::1`. A concrete host remains
the probe host. URL rendering brackets IPv6 addresses. Port occupancy and health
checks consume the same endpoint object, so preflight, startup verification, and
the server configuration cannot drift.

The generated unit includes:

```ini
Environment=FREE_TTS_CONFIG_ONLY=1
UnsetEnvironment=FLASK_DEBUG
```

`server.py` resolves config-only mode before choosing its config path. In that
mode, the config path is always `config.json` beside `server.py`, and the generic
configuration helpers ignore all per-setting environment overrides. The process
environment is not cleared, so systemd's `INVOCATION_ID` remains available for
identity verification. Desktop-started and manually launched servers do not set
the mode and retain the existing environment-over-config precedence. The unit also
removes inherited Flask debug mode so the persistent service always uses its
configured production server.

### 2. Systemd Process Identity

The health response keeps its existing fields and adds:

- `pid`: the integer result of `os.getpid()`;
- `invocation_id`: the process's systemd `INVOCATION_ID`, or null outside a
  systemd service invocation.

This is a backward-compatible extension to API version 1. Desktop-started and
manually launched servers may report a null invocation ID; only systemd installer
verification requires it.

After `restart`, the installer queries `systemctl --user show` for the unit's
`MainPID` and `InvocationID`. A verification attempt succeeds only when:

1. the unit is active and both systemd identity values are valid;
2. the configured health endpoint identifies `free-tts` and returns the same PID
   and invocation ID;
3. a second systemd query after the HTTP response still reports the unit active
   with the same PID and invocation ID.

Starting, empty, changed, or malformed identity values cause the bounded retry to
continue. A foreign responder on a forced occupied port cannot satisfy the check,
and a unit that exits or restarts during verification cannot commit the install
transaction. Failure follows the existing exact fresh cleanup or upgrade rollback
path.

### 3. Owned Enablement Cleanup

The expected per-user enablement path is derived, not added to the existing
manifest schema:

```text
{unit_dir}/default.target.wants/free-tts.service
```

Before stopping anything, uninstall inspects that path. Absence is safe. A symlink
is owned only when its resolved target is the validated manifest-owned unit path.
A regular file, directory, or symlink to another target is foreign and aborts the
operation before mutation.

After ownership validation, uninstall invokes `disable --now` even when the unit
fragment is already missing. It then requires the unit to be inactive and removes
the still-present validated owned enablement symlink directly if systemctl did not
remove it. A nonzero disable result is tolerated only when these postconditions
prove the intended cleanup occurred; otherwise it remains an actionable
`InstallError`. The unit fragment is removed when present, followed by
`daemon-reload`.

### 4. Retryable Root Deletion

Once service, enablement, unit, and daemon-reload cleanup succeeds, uninstall
atomically renames the validated server root to a reserved sibling deletion path.
It then removes that staged tree.

If deletion succeeds, uninstall reports the original root as removed. If deletion
raises but the staged tree no longer exists, deletion is complete and no ownership
state needs restoration. If a partial tree remains, the installer atomically
rewrites the already validated manifest into that tree and renames it back to the
canonical root before raising `InstallError`. The first invocation therefore
reports failure, while a retry can prove ownership and finish deleting the
remaining contents.

If manifest restoration or the rename-back compensation itself fails, the error
names the retained deletion path and all compensation failures. It never reports
successful uninstall while silently abandoning unowned residual data.

## Data Flow

### Install Or Reinstall

1. Check Python and the systemd user session.
2. Strictly inspect existing ownership and select the config that will govern the
   resulting installation.
3. Resolve and validate the service endpoint without mutation.
4. Run endpoint occupancy preflight against that endpoint.
5. Execute the existing staged runtime, config, manifest, unit, and systemd
   transaction.
6. Verify endpoint identity against stable systemd PID and invocation values.
7. Commit or roll back the transaction exactly as before.

### Uninstall

1. Strictly load the ownership manifest.
2. Validate the unit fragment and expected enablement path before mutation.
3. Disable/stop, prove inactivity, remove the owned enablement link and unit, and
   reload systemd.
4. Rename the root to deletion staging and remove it.
5. On partial deletion, restore a valid manifest-bearing canonical root and return
   failure so a retry can continue.

## Error Handling And Compatibility

- `--force` continues to bypass only endpoint occupancy conflicts. It never
  bypasses configuration validation, ownership, process-identity verification, or
  uninstall safety.
- `config.json` is authoritative for every setting in the systemd-managed server;
  desktop and manual launches retain their existing environment overrides.
- A custom configured port is supported on first install and every reinstall.
- Invalid endpoint configuration fails before target mutation with a path-specific
  message.
- Existing health clients remain compatible because response fields are only
  added.
- A free-tts-shaped response from another process is treated as foreign after
  restart, even when `--force` allowed installation to proceed.
- Foreign enablement entries are left byte-for-byte unchanged.
- A missing unit fragment no longer prevents cleanup of its validated dangling
  enablement link.
- Partial root deletion may remove some files because the user requested
  uninstall, but it cannot erase the ownership receipt and make the remainder
  non-retryable.
- Desktop installation, desktop server startup, and Speech Dispatcher reload
  behavior are unchanged.

## Testing Strategy

### Task 1: Configured endpoint and process identity (F-10/F-11)

- A URL-sensitive fetch double rejects any request using the wrong host or port.
- Fresh install and reinstall succeed with port `6123`; preflight and post-restart
  verification use that port.
- Missing endpoint keys use defaults; integer-like ports are accepted; malformed,
  boolean, out-of-range, non-object, symlinked, and unreadable configs fail before
  mutation.
- Config-only mode ignores an external `TTS_CONFIG` and representative per-setting
  `TTS_*` overrides, while an ordinary desktop/manual launch retains the existing
  override precedence.
- Wildcard IPv4/IPv6 binds and IPv6 URL rendering use the correct probe host.
- `/health` returns the current PID and nullable invocation ID without changing
  existing fields.
- A stateful systemctl double proves that a foreign free-tts responder cannot
  satisfy verification after forced installation.
- A race in which the unit changes PID, invocation ID, or activity between the
  first identity query, health response, and final query fails and rolls back.
- Read-only regression probes demonstrate that the endpoint and identity tests
  fail against baseline commit `562c1ee8` for the intended reason.

### Task 2: Retryable uninstall (F-12/F-13)

- An inactive, missing unit fragment with a validated dangling enablement symlink
  is fully cleaned even when `systemctl disable` reports `not-found`.
- Active-unit, disable-failure, and daemon-reload failures retain the existing
  retry guarantees and CLI exit 1 behavior.
- A foreign symlink, regular file, or directory at the enablement path aborts
  before any service or filesystem mutation.
- An injected staged-tree deletion removes the manifest and then raises; uninstall
  restores a strict-loadable canonical root, reports failure, and succeeds on
  retry.
- Compensation failure names the retained deletion path and does not claim
  success.
- Read-only regression probes demonstrate that the dangling-link and partial
  deletion tests fail against baseline commit `562c1ee8` for the intended reason.

Each task runs its focused tests and the full repository suite. The new run's final
Frontier reviewer covers the original branch range, reproduces F-10 through F-13,
and reconciles all earlier fixed and parked findings.

## Delivery

The remediation is implemented by a fresh deterministic SDD run with two tasks and
an independent review gate after each task. The original run remains untouched in
`FINAL_BLOCKED`. The feature branch is not merged and the live service is not
migrated until the remediation run reaches `COMPLETE`, the full suite passes from
a clean worktree, and Frontier records `SPEC: PASS` and `QUALITY: APPROVED`.

Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
