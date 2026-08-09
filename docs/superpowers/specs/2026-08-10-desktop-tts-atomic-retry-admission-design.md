# Desktop TTS Atomic Retry Admission Design

## Context

The branch's third deterministic SDD run closed `F-18`, `F-19`, and the decoder
half of `F-15`, but ended legally in `FINAL_BLOCKED` at revision 30 because `F-17`
remains load-bearing. Commit `2861207` made Retry-After waits interruptible and
made PAUSE boundary state generation-local, but it still passes a plain boolean
across the final check-to-transport interval:

1. `SynthClient` calls `should_abort()`.
2. `SpeechEngine._should_abort_fetch()` checks under the engine lock and returns
   `False`.
3. PAUSE acquires that lock and records that cleanup has begun.
4. `SynthClient` starts the retry POST using the now-stale `False` result.

A deterministic controller probe and a fresh Frontier re-review both reproduced
that ordering. Another unlocked check would only move the race.

## Decision

Add an explicit request-scoped retry-admission seam. `SynthClient.synthesize`
accepts an optional `reserve_retry: Callable[[], bool]` alongside
`should_abort`. Retry-After waiting remains interruptible through `should_abort`,
but immediately before attempt 2, `SynthClient` calls `reserve_retry` instead of
using another plain abort check.

`SpeechEngine` supplies a callback bound to the generation and request ID:

```python
reserve_retry=lambda: self._reserve_retry(generation, request_id)
```

`_reserve_retry` acquires the same `SpeechEngine._lock` used by STOP and
`_reach_pause_boundary`, then admits the retry only when all of these remain true:

- the generation is still current;
- STOP has not cancelled it;
- PAUSE has not reached its honest boundary;
- the generation still owns this exact request ID.

The successful reservation is the retry-start linearization point. The engine lock
is released before HTTP I/O, so STOP and PAUSE remain prompt.

## Ordering Contract

There are two legal orderings:

**PAUSE/STOP wins.** It records cancellation or the PAUSE boundary under the engine
lock before retry reservation. `_reserve_retry` returns `False`, `SynthClient`
raises `Cancelled`, and the retry transport is never entered.

**Retry wins.** `_reserve_retry` returns `True` while the request ID is still in the
generation-owned request set. PAUSE/STOP then records cleanup. Cleanup sees that ID
as outstanding and delivers DELETE, including the existing bounded 404 registration
handoff if transport registration has not landed yet.

The request ID matters. A generation-only callback would admit work that cleanup
has already removed after another failure path. `_GenerationToken` therefore adds
a lock-protected `owns_request(request_id) -> bool` query. Lock order is always
engine lock then generation request lock; no existing path holds the request lock
while acquiring the engine lock.

## Alternatives Rejected

### Add another `should_abort()` check

Rejected because the result can become stale immediately after the check. It does
not establish ordering with PAUSE cleanup.

### Hold the engine lock across `_transport()`

Rejected because an HTTP call can block for the configured 120-second timeout.
STOP and PAUSE would be unable to acquire the lock and initiate cancellation.

### Move HTTP retry policy into `SpeechEngine`

Rejected because it leaks transport policy out of `SynthClient`, duplicates error
handling, and makes both modules shallower. Retry admission is the smaller seam:
`SynthClient` keeps retry mechanics while `SpeechEngine` owns lifecycle ordering.

## Interface Changes

`desktop/synth.py`:

```python
def synthesize(
    self,
    text: str,
    voice_name: str,
    rate: str,
    pitch: str,
    request_id: str,
    should_abort: Callable[[], bool] | None = None,
    reserve_retry: Callable[[], bool] | None = None,
) -> bytes:
```

Attempt 1 and Retry-After slices use `should_abort`. At the top of attempt 2:

```python
if reserve_retry is not None:
    if not reserve_retry():
        raise Cancelled("retry no longer admitted")
elif should_abort is not None and should_abort():
    raise Cancelled("aborted before request")
```

The fallback preserves direct callers that do not need engine-level atomicity.

`desktop/module.py`:

```python
def _reserve_retry(
    self,
    generation: _GenerationToken,
    request_id: str,
) -> bool:
    with self._lock:
        return (
            generation is self._generation
            and not generation.cancelled.is_set()
            and not generation.pause_boundary_reached.is_set()
            and generation.owns_request(request_id)
        )
```

Both the original synthesis call and the fresh-ID backend-recovery call pass their
own bound retry reservation callback.

## Testing

All concurrency tests use `threading.Event`; no sleep orders a race.

1. A direct `SynthClient` regression proves a denied retry reservation raises
   `Cancelled` and makes exactly one POST.
2. An engine-level PAUSE regression blocks immediately before `_reserve_retry`
   acquires the engine lock, lets the current chunk finish and PAUSE record its
   honest boundary, then releases reservation. It proves one `704 PAUSE`, no
   `702 END`, no retry POST, and worker exit without releasing a retry transport.
3. A complementary engine-level regression lets retry reservation win, then
   requests PAUSE while the retry POST is in pre-registration handoff. It proves
   cleanup observes the owned ID, DELETE retries 404 to 200, the POST is cancelled,
   and `704 PAUSE` remains prompt.
4. A read-only mutant that removes the `reserve_retry` callback must fail the
   PAUSE-wins regression, proving the test pins the atomic gate rather than only
   interruptible sleep.
5. The focused audio/synth/module suites and full repository suite must pass, and
   the whole branch remains free of timing-based ordering and forbidden test edits.

## Scope

This remediation changes only `desktop/synth.py`, `desktop/module.py`, and focused
synth/module tests. It does not alter SSIP output, PAUSE boundary semantics,
Retry-After duration, backend request IDs, cancellation handoff bounds, or installer
behavior.
