# Server Installer Inactive-State Remediation Design

Date: 2026-08-10
Status: Approved

## Purpose

The server-installer residual-remediation run ended legally in `FINAL_BLOCKED`
at revision 36 after its sole final-fix wave. That wave fixed install-time
systemd enablement ownership (F-15), but terminal Frontier re-review found that
the uninstall inactivity postcondition remained incomplete. The immutable
severity successor is F-16.

This remediation closes only F-16 in a fresh deterministic run. Neither terminal
run is reopened or edited.

## Scope

F-16 has two coupled failure modes:

1. `activating`, `reloading`, and `deactivating` are currently treated as stopped
   because `_query_unit_state(..., "is-active", "active")` returns false for every
   recognized non-`active` value.
2. `disable --now` may remove the owned wants symlink before inactivity is proven.
   If uninstall then fails, the prior enabled state is not restored.

No install transaction, endpoint, health identity, desktop integration,
documentation, dependency, or live-service migration change is in scope.
Commit `b1fa605a37041128e0b48604800bdcaa34dd537c` is the implementation baseline.

## Global Constraints

- Python 3.11 remains the minimum supported interpreter.
- `install.py` and `tests/test_install.py` remain standard-library-only; no
  dependency or `requirements.txt` change is permitted.
- Tests never invoke real systemctl, real virtualenv creation, or real network
  calls. Systemctl state sequences, sleeps, and filesystem side effects remain
  injected and deterministic.
- `desktop/install.py`, `tests/test_extension_split_sentences.py`, and
  `tests/test_media_session.py` are not modified.
- Ownership is established before mutation. A failed uninstall leaves the exact
  prior owned enablement state, root, and unit, or names every incomplete
  compensation without overwriting foreign data.
- Existing config-authoritative service operation, endpoint/PID/InvocationID
  verification, install rollback, wants-path ownership, retryable root deletion,
  CLI behavior, and desktop delegation remain unchanged.
- The third deterministic run's final Frontier review covers the original merge
  base through the new HEAD and reconciles every finding from all prior runs.
- The feature branch is not merged and the live service is not migrated during
  this remediation.

## Architecture

### Exact Activity State

Uninstall gains an exact activity-state query rather than using the existing
boolean `_query_unit_state` helper. The new helper returns a validated systemd
state token from `systemctl --user is-active free-tts.service` and preserves the
command's diagnostic when output is missing or unrecognized.

The bounded wait accepts only `inactive` as proof that filesystem cleanup may
begin:

- `active`, `activating`, `reloading`, `deactivating`, and `maintenance` remain
  unproven and are retried;
- `failed` triggers one `systemctl --user reset-failed free-tts.service`, after
  which the unit must report `inactive`;
- `unknown`, malformed output, execution failure, reset failure, or exhaustion of
  the attempt bound fails uninstall.

`uninstall_server` accepts injected `stop_attempts`, `stop_delay`, and `sleeper`
parameters. Production defaults provide a short bounded wait; tests use zero
delay and deterministic state sequences. No test sleeps for correctness.

### Enablement Snapshot And Compensation

After strict manifest, unit, and wants-link validation but before `disable --now`,
uninstall snapshots the exact enablement state with `_snapshot_path`.

If exact inactivity is not proven, uninstall restores that snapshot before
returning failure:

- if the prior link existed and disable removed it, the exact symlink target is
  recreated atomically;
- if the prior path was absent and a new owned link appeared, that owned link is
  removed;
- if the current path is still the owned link, restoring the same prior state is
  harmless;
- if a foreign file, directory, or symlink appeared, it is retained and the
  compensation failure is reported instead of overwritten or deleted;
- `FileNotFoundError` races are handled by rechecking and restoring from the
  snapshot, not by claiming success.

Root and unit removal remains strictly after the inactivity gate. Therefore a
failed stop leaves both untouched. The final error includes the disable result,
observed activity history, and any enablement compensation error.

### Successful Cleanup

Once exact `inactive` is observed, uninstall continues the existing sequence:

1. revalidate and remove a still-present owned wants link;
2. evaluate whether a nonzero disable result is tolerable from the proven
   inactive/link-absent postconditions;
3. remove the unit fragment;
4. run `daemon-reload`;
5. delete the root through the existing recoverable rename/manifest transaction.

Normal systemd behavior where disable removes the link remains successful.
Missing-fragment dangling cleanup and post-disable foreign replacement protection
remain unchanged.

## Error Handling

- No transitional state is accepted as stopped.
- A sequence that eventually reaches `inactive` succeeds within the bounded wait.
- A failed unit is reset and must then become inactive before cleanup.
- Timeout, query failure, or reset failure restores the prior enablement snapshot
  and retains root/unit.
- Compensation never overwrites a foreign replacement. Its path and diagnostic are
  included in the raised `InstallError`.
- A failed uninstall remains immediately retryable whenever compensation succeeds.
- CLI component aggregation remains unchanged: server failure returns exit 1 while
  `uninstall all` may continue to desktop cleanup.

## Testing Strategy

The remediation is one independently reviewed TDD task.

### F-16 Regression Matrix

- A filesystem-backed disable removes the owned wants link while the unit remains
  `active`; uninstall fails and restores the exact link, root, and unit.
- Parametrized `activating`, `reloading`, and `deactivating` sequences never count
  as inactive; exhaustion restores the exact prior state.
- `deactivating` followed by `inactive` succeeds and performs normal cleanup.
- `failed` invokes `reset-failed`; `inactive` afterward succeeds.
- Failed `reset-failed` restores enablement and retains root/unit.
- A foreign replacement appearing before compensation is retained, compensation
  is reported incomplete, and root/unit remain.
- A prior absent enablement path remains absent after failed inactivity proof.
- Existing normal disable-removes-link, ENOENT race, missing-fragment, foreign
  replacement, daemon-reload, root-deletion compensation, install rollback,
  configured endpoint, and stable service identity tests remain green.

New tests are demonstrated failing against baseline `b1fa605` using a temporary
`git archive` with current tests and baseline production code. The active worktree
and HEAD are never moved. The task runs installer tests, server tests, the complete
suite, pycompile, CLI help, and diff checks before its scoped commit.

## Delivery

A fresh deterministic SDD run executes one Advanced task with an independent
Capable task review and Frontier whole-branch review. The two earlier runs remain
terminal. Integration and live migration occur only after the new run reaches
`COMPLETE`, all tests pass from a clean worktree, and Frontier records
`SPEC: PASS` plus `QUALITY: APPROVED`.

Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
