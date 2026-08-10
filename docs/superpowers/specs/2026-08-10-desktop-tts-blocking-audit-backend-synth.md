## Summary

Scope: `desktop/backend.py` and `desktop/synth.py` at `40bc8454b2787900e23ff653302e4290e2f20980`, using shipped defaults `startup_timeout=30`, `request_timeout=120`, and `idle_timeout=300`. The accepted-message route was traced through `SpeechEngine._fetch()` and worker cleanup only to establish reachability; calls outside the two requested files are not audited as findings.

Latency below means time added between receipt of STOP (or the point at which PAUSE is requested/reaches its boundary, where distinguished) and the worker's terminal `703 STOP`/`704 PAUSE`. `SpeechEngine.handle_stop()` does not emit the event itself; the speech worker emits it only after the blocked fetch and synchronous executor cleanup unwind. A live worker also causes a later SPEAK to be refused.

There are two different kinds of bound in this code:

* **Strict wall-clock bound:** what the implementation actually guarantees against an adverse local HTTP peer, lock holder, filesystem, or child launch. `urllib`'s `timeout=` is a socket inactivity timeout, not an absolute request/body deadline. Therefore every HTTP operation that reads a trickling response has **no finite wall-clock maximum**.
* **Declared-timeout arithmetic:** useful for comparing constants if one temporarily treats each supplied HTTP timeout as a whole-call deadline. These numbers are not strict guarantees, but they expose large sums of individually finite values.

Worst offenders, ordered by strict risk and then declared-timeout latency:

| Rank | Path | Strict worst case | Declared-timeout arithmetic |
|---|---|---:|---:|
| 1 | `backend.py:95` blocking startup `flock(LOCK_EX)` during accepted-message recovery | Unbounded | Unbounded |
| 2 | `backend.py:108`, `backend.py:109`, `synth.py:69`, `synth.py:70`, and `synth.py:72` HTTP open/body reads | Unbounded | Route-dependent: health 3s, voices 20s, POST 120s, DELETE 5s per socket operation |
| 3 | Synchronous startup filesystem work and `Popen` at `backend.py:92`, `backend.py:93`, `backend.py:120`-`backend.py:124`, and `backend.py:252` | Unbounded at the application layer | Normally short on the shipped local paths; no enforced number |
| 4 | PAUSE before the current chunk boundary, with first synthesis exhausting a 503 retry, successful backend restart, catalog refresh, and recovered synthesis exhausting another 503 retry | Unbounded | Just under **549.0s**: 245 + (<39 startup recovery) + 20 + 245 |
| 5 | One in-flight POST after STOP or a PAUSE boundary when DELETE does not release it | Unbounded | **120s** for one configured POST; one `synthesize()` can total **245s** (120 + 5 + 120) |
| 6 | STOP arriving just after `_fetch()`'s pre-recovery abort check | Unbounded | Just under **59.0s** for successful startup recovery plus voices; **39.25s** if readiness times out before voices |
| 7 | DELETE registration handoff | Unbounded | Just under **6.0s per request ID**, despite the nominal 1s handoff deadline |
| 8 | Sequential cancellation cleanup sums | Unbounded | Up to about **12.0s** for PAUSE with two recovery IDs; about **24.0s** for STOP with three snapshotted IDs plus duplicate queued-ID cleanup |
| 9 | `_wait_until_ready()` itself | Unbounded because each health read is unbounded | Failure can approach **33.25s**, not 30s; whole `ensure_ready()` can approach **39.25s** before filesystem/lock costs |
| 10 | 503 Retry-After wait | Scheduler-dependent | STOP/lookahead-at-boundary: **0.05s** added; PAUSE on the current pre-boundary chunk: full **5.0s** |

The approximately 549s successful-recovery sum is:

1. First `SynthClient.synthesize()`: 120s POST + 5s Retry-After + 120s POST = 245s.
2. `ensure_ready()`: 3s pre-lock probe + 3s under-lock probe + a latest-success readiness loop approaching 33s = just under 39s. Lock, filesystem, and spawn time are additional and unbounded.
3. Voice refresh: 20s.
4. Recovery `SynthClient.synthesize()`: another 245s.

A STOP normally prevents later steps when it is observed between calls. The known recovery defect is the race in which STOP/PAUSE arrives after the check before recovery: `ensure_ready()` and voice refresh have no cancellation input, so the approximately 59s recovery suffix remains reachable. A PAUSE requested before the current chunk boundary is not included in `_should_abort_fetch()`, so it can inherit the complete approximately 549s chain. These are path-accounting facts, not new reports of the two defects excluded in the request.

Cancellation handoff also has a large sum. `cancel()` computes a 1s deadline, but checks it only after a DELETE and then sleeps before looping. A DELETE may therefore start just before the deadline and consume its full 5s timeout, approaching 6s per ID. During one-chunk lookahead, the generation can own the current original ID, its fresh recovery ID, and one queued lookahead ID. STOP snapshots all three without removing them; after the active request unwinds, worker cleanup can cancel the queued ID again. Four sequential `cancel()` invocations can therefore approach 24s. At a PAUSE boundary, an active lookahead in recovery can own two IDs, yielding about 12s of synchronous cleanup.

No loop-attempt cap exists independently of elapsed time in either the readiness loop (`backend.py:277`) or cancellation handoff loop (`synth.py:188`). The retry-wait loop (`synth.py:163`) is different: its arithmetic remaining value and constants bound it to 101 sleep iterations for the maximum 5s delay (the extra iteration is a floating-point remainder), independent of clock time. The synthesis retry loop has exactly two attempts.

### Probe evidence

Scratch probe: `/tmp/backend_synth_blocking_probe.py`. It uses `threading.Event` for concurrent ordering and writes only under `/tmp`.

* `backend._fetch_json(..., timeout=0.1)` read a 17-byte JSON body delivered every 0.025s in **0.441s**, 4.41 times the supplied timeout, without timing out.
* `synth._http_transport(..., timeout=0.1)` read a successful 16-byte body in **0.405s**, 4.05 times the supplied timeout.
* The `HTTPError.read()` route read a trickled 404 body in **0.406s**, 4.06 times the supplied timeout.
* A logical-clock DELETE probe admitted its twentieth DELETE at `t=0.95`; that call consumed the 5s DELETE timeout and returned at `t=5.95`, despite the 1s handoff deadline.
* A logical-clock readiness probe arranged the last health probe just before the deadline. `_wait_until_ready()` admitted 120 probes and failed at **33.249s** under `startup_timeout=30` and `_PROBE_TIMEOUT=3`.
* A real two-thread `flock` probe showed that a second `file_lock()` remained blocked for the entire 0.20s observation and acquired only after an Event released the holder. There is no code deadline that would change that result for a longer hold.
* Read-only baseline: `PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' .venv/bin/python -m pytest -q` completed with **451 passed in 15.45s**.

## Findings

### F01 - Startup lock directory creation

* **Exact location:** `desktop/backend.py:92`.
* **Call and bound:** `path.parent.mkdir(parents=True, exist_ok=True)`. Synchronous filesystem metadata/write operation; no application timeout. Strict bound: unbounded.
* **Accepted path:** accepted SPEAK -> worker `_fetch()` -> `SynthError` -> abort check loses the race -> `BackendController.ensure_ready()` -> `with self._lock_factory()` -> `file_lock()`.
* **Worst added STOP/PAUSE latency:** no finite number. On the shipped `/run/user/...` or `/tmp`-style local path it should normally be milliseconds, but the code enforces no number.
* **Regression coverage:** `TestPaths.test_real_file_lock_round_trips` executes this on an uncontended pytest temporary directory. It has no blocker, elapsed-time, or cancellation assertion and would not catch a latency regression.
* **Bound source:** none.

### F02 - Startup lock file open

* **Exact location:** `desktop/backend.py:93`.
* **Call and bound:** `os.open(path, os.O_CREAT | os.O_RDWR, 0o600)`. Synchronous filesystem open/create; no application timeout. Strict bound: unbounded.
* **Accepted path:** same accepted recovery path as F01, immediately before lock acquisition.
* **Worst added STOP/PAUSE latency:** no finite number; normally milliseconds on the default local runtime directory.
* **Regression coverage:** the uncontended real-lock round-trip exercises the call but has no deadline assertion.
* **Bound source:** none.

### F03 - Exclusive startup flock

* **Exact location:** `desktop/backend.py:95`.
* **Call and bound:** `fcntl.flock(handle, fcntl.LOCK_EX)`. Blocking acquisition with neither `LOCK_NB` nor a timeout. Strict and nominal bound: unbounded.
* **Accepted path:** accepted SPEAK -> synthesis transport error -> `_fetch()` recovery -> `ensure_ready()` sees a non-ready backend -> enters `file_lock()` while another process may be starting or wedged.
* **Worst added STOP/PAUSE latency:** no finite number. STOP/PAUSE cannot reach the worker's terminal emission while the recovery thread is blocked here.
* **Regression coverage:** `test_real_file_lock_round_trips` is uncontended. `test_recheck_under_lock_adopts_backend_started_by_racer` replaces the lock with `_noop_lock`. No test holds the lock while an accepted utterance is cancelled.
* **Bound source:** none.

### F04 - Health HTTP open

* **Exact locations:** call boundary `desktop/backend.py:167`; primitive `desktop/backend.py:108`.
* **Call and bound:** `self._fetch(url, _PROBE_TIMEOUT)` -> `urllib.request.urlopen(request, timeout=timeout)`, with `_PROBE_TIMEOUT = 3.0` at `desktop/backend.py:36`. The 3s value is a socket-operation inactivity timeout, not an absolute call deadline. Strict bound: unbounded.
* **Accepted path:** accepted `_fetch()` recovery calls `ensure_ready()`, which probes once before the lock, once after the lock, and repeatedly in `_wait_until_ready()`.
* **Worst added STOP/PAUSE latency:** no finite number. Under declared-timeout arithmetic, one socket operation adds 3.0s; two pre-loop probes add 6.0s and the loop can add almost 33.25s.
* **Regression coverage:** all `TestProbe` and `TestEnsureReady` timing paths inject a nonblocking `fetch`. No test invokes real `urlopen` against a slow health endpoint or asserts that `3.0` is passed.
* **Bound source:** intended inactivity bound is a module constant; strict wall-clock bound is not enforced.

### F05 - Health response body read

* **Exact location:** `desktop/backend.py:109`.
* **Call and bound:** `response.read()` with no byte limit and no absolute deadline. It inherits the socket inactivity timeout set by `urlopen`, which resets as data arrives. Strict bound: unbounded.
* **Accepted path:** every recovery health probe that receives a 2xx response, including every readiness-loop iteration.
* **Worst added STOP/PAUSE latency:** no finite number. The probe observed 0.441s under a 0.1s timeout; an indefinitely trickling body can continue indefinitely.
* **Regression coverage:** none. Backend tests bypass `_fetch_json()` with injected payloads.
* **Bound source:** no absolute bound; only the constant 3.0s socket inactivity timeout and remote connection close/content length.

### F06 - Backend log directory creation

* **Exact location:** `desktop/backend.py:120`.
* **Call and bound:** `log_path.parent.mkdir(parents=True, exist_ok=True)`. Synchronous filesystem operation, no timeout. Strict bound: unbounded.
* **Accepted path:** accepted recovery -> backend absent -> `_start_and_wait()` -> injected/default `_spawn_backend()`.
* **Worst added STOP/PAUSE latency:** no finite number; normally milliseconds on the default cache path.
* **Regression coverage:** `TestEnsureReady` injects `spawn`, so it does not execute this call. No elapsed-time test exists.
* **Bound source:** none.

### F07 - Backend log existence check

* **Exact location:** `desktop/backend.py:121` (`log_path.exists()`).
* **Call and bound:** synchronous `stat`-style metadata lookup; no timeout. Strict bound: unbounded.
* **Accepted path:** same spawn recovery path as F06.
* **Worst added STOP/PAUSE latency:** no finite number; normally milliseconds locally.
* **Regression coverage:** none on the accepted recovery path.
* **Bound source:** none.

### F08 - Backend log size stat

* **Exact location:** `desktop/backend.py:121` (`log_path.stat()`).
* **Call and bound:** synchronous filesystem metadata lookup; no timeout. Strict bound: unbounded.
* **Accepted path:** spawn recovery when the log exists.
* **Worst added STOP/PAUSE latency:** no finite number; normally milliseconds locally.
* **Regression coverage:** none.
* **Bound source:** none.

### F09 - Backend log truncation write

* **Exact location:** `desktop/backend.py:122`.
* **Call and bound:** `log_path.write_bytes(b"")`, which opens, truncates/writes, and closes synchronously. No timeout. Strict bound: unbounded.
* **Accepted path:** spawn recovery when the existing backend log exceeds 1 MiB.
* **Worst added STOP/PAUSE latency:** no finite number; normally milliseconds locally.
* **Regression coverage:** none.
* **Bound source:** no time bound. `_MAX_LOG_BYTES` is a size trigger, not a latency bound.

### F10 - Backend log append open

* **Exact location:** `desktop/backend.py:123`.
* **Call and bound:** `log_path.open("ab", buffering=0)`. Synchronous filesystem open; no timeout. Strict bound: unbounded.
* **Accepted path:** every default backend spawn during accepted recovery.
* **Worst added STOP/PAUSE latency:** no finite number; normally milliseconds locally.
* **Regression coverage:** none; controller tests inject `spawn`.
* **Bound source:** none.

### F11 - Backend process launch

* **Exact locations:** call boundary `desktop/backend.py:269`; primitive begins at `desktop/backend.py:124`.
* **Call and bound:** `self._spawn(...)` -> `subprocess.Popen(...)`. `Popen` synchronously performs process creation and waits for exec setup/error reporting; there is no launch timeout. Strict bound: unbounded.
* **Accepted path:** accepted recovery -> backend absent -> `_start_and_wait()` after taking the startup lock.
* **Worst added STOP/PAUSE latency:** no finite number. The later 30s readiness deadline is created only after `Popen` returns, so it does not include launch time.
* **Regression coverage:** `test_starts_backend_when_unreachable`, `test_spawn_passes_idle_timeout`, and the recovery test inject an immediate fake spawn. They verify behavior and environment, not launch latency or cancellation.
* **Bound source:** none. `idle_timeout=300` is passed to the child and does not bound `Popen` or recovery.

### F12 - Installed-venv existence check

* **Exact location:** `desktop/backend.py:252`.
* **Call and bound:** `venv_python.exists()`, a synchronous filesystem metadata lookup with no timeout. Strict bound: unbounded.
* **Accepted path:** accepted recovery -> backend spawn -> `_server_command()`.
* **Worst added STOP/PAUSE latency:** no finite number; normally milliseconds on the local install path.
* **Regression coverage:** no timing or cancellation test.
* **Bound source:** none.

### F13 - Readiness polling loop

* **Exact locations:** deadline creation `desktop/backend.py:276`; loop admission `desktop/backend.py:277`; health probe `desktop/backend.py:283`; sleep `desktop/backend.py:292`.
* **Call and bound:** `deadline = clock() + config.startup_timeout`, then `while clock() < deadline`. The deadline is checked only before a probe. A last probe may consume 3s and the unconditional following sleep another 0.25s after the 30s deadline. Strict bound: unbounded because the probe read is unbounded. Declared-timeout failure supremum: 33.25s; latest success: just under 33.0s.
* **Accepted path:** accepted recovery when the backend is the expected service but not ready, or after this adapter starts it.
* **Worst added STOP/PAUSE latency:** no finite number strictly. Treating probe timeout as a hard call limit gives 33.25s for this loop, 39.25s including the two preceding 3s probes, and just under 59s when a latest-success loop is followed by the 20s catalog refresh.
* **Regression coverage:** `test_startup_timeout_reports_log_path` checks only that an exception mentions the log. It uses a no-op sleep and a fake clock that advances 1s per call. It does not enforce elapsed time, post-deadline probe admission, probe count, or cancellation.
* **Bound source:** intended aggregate bound is config (`startup_timeout=30`); overshoot comes from constants (`_PROBE_TIMEOUT=3`, sleep 0.25); no strict bound.
* **Loop count:** no independent attempt limit. The `/tmp` probe admitted 120 iterations by making probes fast until the final one. Production progress depends only on monotonic time and sleep/probe duration.

### F14 - Readiness poll sleep

* **Exact location:** `desktop/backend.py:292`.
* **Call and bound:** `self._sleep(0.25)`, defaulting to `time.sleep`. Requested duration is 0.25s; no cancellation predicate is consulted before or during it.
* **Accepted path:** every unsuccessful readiness iteration during accepted recovery.
* **Worst added STOP/PAUSE latency:** 0.25s for the individual sleep, excluding scheduler stalls. In the loop it contributes to the 33.25s/39.25s sums above.
* **Regression coverage:** backend tests replace sleep with a no-op. No test would catch changing 0.25 to a materially larger sub-timeout value as long as fake-clock behavior still terminates.
* **Bound source:** literal constant.

### F15 - Shared synthesis HTTP open

* **Exact location:** `desktop/synth.py:69`.
* **Call and bound:** `urllib.request.urlopen(request, timeout=timeout)`. Effective supplied inactivity values are 20.0s for voices (`desktop/synth.py:98`), configured 120s for POST (`desktop/synth.py:134` and `desktop/settings.py:39`), and 5.0s for DELETE (`desktop/synth.py:190`). None is an absolute wall-clock deadline. Strict bound: unbounded.
* **Accepted paths:** recovery catalog refresh uses GET `/voices`; current and lookahead fetches use POST; STOP's asynchronous canceller and worker cleanup use DELETE.
* **Worst added STOP/PAUSE latency:** no finite number. Under declared-timeout arithmetic: voices 20s; one POST 120s; one DELETE 5s. Multiple socket phases may each consume the inactivity timeout even without a trickle.
* **Regression coverage:** `test_desktop_synth.py` injects `_Transport`; it records timeout arguments but tests do not assert their values. End-to-end tests use prompt local handlers and a broad 60s harness timeout, not a control-latency deadline.
* **Bound source:** voices and DELETE use module constants; POST uses config; strict bound is not enforced.

### F16 - Successful HTTP response body read

* **Exact location:** `desktop/synth.py:70`.
* **Call and bound:** `response.read()` with no maximum body size and no absolute deadline. Strict bound: unbounded for 2xx voices, audio, and DELETE responses.
* **Accepted paths:** recovery voices GET, every successful synthesis POST, and successful cancellation DELETE.
* **Worst added STOP/PAUSE latency:** no finite number. The probe completed a 16-byte body in 0.405s under a 0.1s timeout; indefinite sub-timeout trickling remains possible.
* **Regression coverage:** none against real slow bodies. Scripted transports return complete bytes immediately.
* **Bound source:** no absolute bound; only the route's constant/configured socket inactivity value.

### F17 - HTTP error response body read

* **Exact location:** `desktop/synth.py:72`.
* **Call and bound:** `exc.read()` for every `HTTPError`, again with no byte or absolute time limit. This includes cancellation 404, synthesis 499, and retryable 503 bodies. Strict bound: unbounded.
* **Accepted paths:** STOP/PAUSE DELETE handoff receiving 404; POST cancellation receiving 499; POST busy response receiving 503; any other HTTP error after acceptance.
* **Worst added STOP/PAUSE latency:** no finite number. The probe read a 404 body in 0.406s under a 0.1s timeout.
* **Regression coverage:** scripted tests cover status interpretation only; no slow/error-body transport test exists.
* **Bound source:** no absolute bound; inherits route-specific socket inactivity timeout.

### F18 - Voice catalog transport call

* **Exact location:** `desktop/synth.py:98`.
* **Call and bound:** `self._transport("GET", ..., _VOICES_TIMEOUT)` with `_VOICES_TIMEOUT=20.0` at `desktop/synth.py:22`. Default transport reaches F15-F17. Strict bound: unbounded; declared inactivity value: 20s.
* **Accepted path:** only the mid-utterance recovery refresh is post-acceptance. Normal `_ensure_catalog()` voices loading occurs before `200 OK SPEAKING` and is excluded.
* **Worst added STOP/PAUSE latency:** no finite number strictly; 20s under declared-timeout arithmetic. When STOP loses the pre-recovery check, this follows up to about 39s of startup recovery, producing the approximately 59s sum.
* **Regression coverage:** `TestVoices` verifies parsing/status with an immediate fake transport. `test_health_to_post_race_restarts_refreshes_and_retries_once` verifies that refresh happens, but its fake `voices()` is immediate and it issues no STOP/PAUSE.
* **Bound source:** module constant for inactivity; no strict deadline.

### F19 - Synthesis POST transport call

* **Exact location:** `desktop/synth.py:134` (arguments continue at `desktop/synth.py:135`).
* **Call and bound:** `self._transport("POST", ..., float(config.request_timeout))`, default 120s. Strict bound: unbounded; declared socket inactivity value: 120s per POST.
* **Accepted path:** every current/lookahead chunk; a 503 permits a second POST after retry wait; `_fetch()` recovery can invoke a fresh `synthesize()`, again with up to two POSTs.
* **Worst added STOP/PAUSE latency:** no finite number strictly. Declared arithmetic is 120s for one in-flight POST, 245s for one `synthesize()`, and just under 549s for the full successful failure/recovery/retry chain. STOP observed between calls prevents later attempts, but a STOP that cannot deliver DELETE can wait the active 120s POST. A pre-boundary PAUSE does not abort the current fetch and can inherit all steps.
* **Regression coverage:** tests verify timeout-independent status/retry behavior with immediate transports. `test_health_to_post_race_restarts_refreshes_and_retries_once` pins the successful one-time recovery behavior but no duration or cancellation deadline. Active STOP/PAUSE end-to-end tests release POST promptly when DELETE arrives.
* **Bound source:** config (`request_timeout=120`) as socket inactivity only; retry count is code-constant two; strict call deadline absent.

### F20 - Direct Retry-After sleep without abort callback

* **Exact location:** `desktop/synth.py:159`.
* **Call and bound:** `self._sleep(delay)` where `_retry_delay()` clamps to `_MAX_RETRY_DELAY=5.0`. Requested maximum: 5s.
* **Accepted path:** **none in the shipped `SpeechEngine`**. Both initial and recovered worker calls pass `should_abort`, so accepted speech uses F21 instead. This fallback remains reachable to other direct `SynthClient.synthesize()` callers.
* **Worst added STOP/PAUSE latency for the requested accepted path:** 0s because this branch is unreachable. API-only maximum is 5s, excluding scheduler stalls.
* **Regression coverage:** `test_503_retry_delay_is_capped` asserts the injected sleep receives exactly 5.0 for a huge Retry-After and would catch removal/increase of the cap.
* **Bound source:** module constant after remote Retry-After parsing.

### F21 - Abort-aware Retry-After sleep

* **Exact locations:** loop `desktop/synth.py:163`; sleep `desktop/synth.py:167`; abort checks `desktop/synth.py:164`, `desktop/synth.py:168`, and `desktop/synth.py:171`.
* **Call and bound:** sleeps `min(0.05, remaining)` until a delay clamped to 5s is consumed. One sleep requests at most 0.05s; maximum requested aggregate is 5s; maximum loop count is 101 due floating-point remainder.
* **Accepted path:** first POST returns 503 during current or lookahead synthesis.
* **Worst added STOP/PAUSE latency:** STOP adds at most 0.05s before `Cancelled`, excluding scheduler stalls. A lookahead whose PAUSE boundary is already recorded also adds at most 0.05s. PAUSE on the current pre-boundary chunk is not an abort condition and can incur the full 5.0s, followed by another POST.
* **Regression coverage:** `test_stop_interrupts_retry_after_wait` and `test_pause_interrupts_lookahead_retry_after_at_boundary` catch loss of abort polling and unwanted second POSTs. They use Event-driven injected sleeps and do not measure the real 0.05s wall bound; the cap test catches the 5s maximum.
* **Bound source:** module constants `_RETRY_ABORT_POLL_INTERVAL=0.05` and `_MAX_RETRY_DELAY=5.0`.

### F22 - Cancellation DELETE transport call

* **Exact location:** `desktop/synth.py:190` (arguments continue at `desktop/synth.py:191`).
* **Call and bound:** `self._transport("DELETE", ..., _CANCEL_TIMEOUT)` with `_CANCEL_TIMEOUT=5.0`. Default transport reaches F15-F17. Strict bound: unbounded; declared socket inactivity value: 5s for each DELETE.
* **Accepted path:** STOP snapshots outstanding IDs and starts asynchronous cancellation; PAUSE and all worker exits synchronously cancel outstanding lookahead in worker cleanup.
* **Worst added STOP/PAUSE latency:** no finite number strictly. One admitted call can add 5s under declared arithmetic. Because the handoff deadline is checked after this call, the per-ID cancellation operation approaches 6s. Sequential ID handling can approach 12s or 24s as described in Summary.
* **Regression coverage:** `TestCancel`, `TestCancelHandoff`, and module ownership/race tests verify DELETE occurrence, 404 retry semantics, and eventual 200 with immediate fake transports. None asserts timeout values, per-generation aggregate time, ID ordering, or slow DELETE behavior.
* **Bound source:** module constant as socket inactivity only; no strict deadline.

### F23 - Cancellation handoff loop

* **Exact locations:** deadline `desktop/synth.py:187`; loop `desktop/synth.py:188`; post-call deadline check `desktop/synth.py:202`; retry sleep `desktop/synth.py:207`.
* **Call and bound:** `deadline = monotonic() + 1.0`, but no deadline check occurs at the top of the loop before the next DELETE. A request admitted near 1s may use the full 5s DELETE timeout. Strict bound: unbounded because DELETE is unbounded; declared per-ID supremum: 6.0s.
* **Accepted path:** DELETE receives 404 before POST registration while `still_wanted()` remains true for the generation.
* **Worst added STOP/PAUSE latency:** no finite number strictly; just under 6s per ID under declared arithmetic. PAUSE can synchronously process two recovery IDs (about 12s). STOP can process three snapshotted IDs and then one queued ID again in worker cleanup (about 24s).
* **Regression coverage:** `test_404_is_retried_while_the_generation_still_wants_it`, `test_delete_before_registration_is_retried_until_it_lands`, and the PAUSE handoff tests prove functional retry. Their sleeps/transports are immediate or Event-released. There is no expiry, late-admission, multiple-slow-ID, or aggregate-deadline test.
* **Bound source:** constants `_CANCEL_HANDOFF_SECONDS=1.0` and `_CANCEL_TIMEOUT=5.0`; strict and aggregate generation bounds absent.
* **Loop count:** no independent attempt cap. Iterations are bounded only by monotonic elapsed time, and a blocking transport can hold one iteration indefinitely.

### F24 - Cancellation handoff retry sleep

* **Exact location:** `desktop/synth.py:207`.
* **Call and bound:** `self._sleep(_CANCEL_RETRY_INTERVAL)` with `_CANCEL_RETRY_INTERVAL=0.05`. No cancellation Event interrupts the sleep, though `still_wanted()` is checked before it.
* **Accepted path:** each still-wanted DELETE 404 before handoff expiry, in asynchronous STOP cancellation or synchronous worker cleanup.
* **Worst added STOP/PAUSE latency:** 0.05s for one sleep, excluding scheduler stalls; approximately 1s aggregate before the late DELETE overrun. Per-ID and multiple-ID sums are F23.
* **Regression coverage:** handoff tests replace sleep with a no-op and do not enforce 0.05s or aggregate elapsed time.
* **Bound source:** module constant.

## Recommended deadlines

A strict approximately 2s control guarantee cannot be obtained merely by reducing the existing `timeout=` numbers: `urlopen` does not implement an absolute deadline, `flock` has no deadline, and local startup syscalls have no application timeout. Use one explicit **1.5s control-completion deadline** measured from STOP receipt or PAUSE receipt/boundary, leaving roughly 0.5s for terminal emission and scheduling. Normal synthesis may retain its 120s request timeout and normal no-cancellation startup may retain 30s; the short deadline applies when accepted work is recovering or cancellation is pending.

The proposals below are single absolute call/control deadlines. They do **not** slice an HTTP request into retry chunks, and they do not make `flock` interruptible.

### R01 - F01-F03 and F06-F12: startup filesystem, flock, and Popen

* Keep blocking `flock(LOCK_EX)` unchanged; do not add polling, signals, `LOCK_NB`, or an interruptible-lock scheme.
* Do not claim a syscall-level timeout for `mkdir`, `open`, `stat`, truncation, or `Popen`; Python provides none here.
* Keep normal recovery capable of waiting the configured 30s and completing the existing health-to-POST recovery test, but remove startup ownership work from the speech worker's terminal-event critical path. The speech worker may wait for the recovery result while speech remains wanted; once STOP or PAUSE wins, its wait must end by the shared **1.5s control deadline**, even if the independent startup owner remains blocked in `flock`/filesystem/launch work.
* This preserves mid-utterance restart-and-retry when no control command occurs, without proposing that `flock` itself become interruptible. It is the only way to state a finite worker deadline while retaining a blocking flock.

### R02 - F04-F05: recovery health HTTP

* Retain the 3s probe for pre-accept LIST/SPEAK readiness if desired, but use a **0.25s absolute health-call deadline** on the accepted recovery path.
* Cap a health body at **64 KiB** and include open, headers, and the complete body in that one deadline. A trickle must not reset it.
* After STOP/PAUSE, admit no new health call. An already-running recovery health call must be abandoned/closed by 0.25s, within the 1.5s control budget.

### R03 - F13-F14: readiness loop

* Preserve `startup_timeout=30` for wanted recovery so `test_health_to_post_race_restarts_refreshes_and_retries_once` continues to restart, refresh, and retry once.
* Check the startup deadline before and after every probe; do not sleep after the deadline. Pass `min(0.25s, remaining)` as the accepted-recovery health deadline.
* Reduce readiness polling sleep from 0.25s to **0.10s**, and abort it when STOP/PAUSE wins. Set a separate **1.5s cancellation grace**, not a replacement 1.5s normal startup timeout.
* Add an explicit iteration safety cap derived once from the configured normal budget, for example **300 probes at 0.10s cadence**, so injected clocks/sleeps cannot make the loop infinite. The monotonic deadline remains authoritative in production.

### R04 - F15-F18: voice and shared HTTP open/read

* Implement one absolute deadline around open, headers, and full body; a successful byte must not reset the wall-clock budget.
* Use **0.50s** for `/voices` specifically during accepted recovery, with a **1 MiB** catalog body cap. The pre-accept catalog path may keep 20s if startup compatibility requires it.
* Stop reading DELETE response bodies entirely, or cap them at **4 KiB**; cap health/error detail bodies at **64 KiB**. These bodies do not justify delaying control completion.
* Preserve route semantics and the single recovery refresh. These changes alter deadlines, not the recovery decision tested by `test_health_to_post_race_restarts_refreshes_and_retries_once`.

### R05 - F19: synthesis POST

* Keep the normal configured `request_timeout=120` for wanted synthesis; shortening every synthesis request to less than 2s would reject legitimate generation and is not a defensible control fix.
* Give the in-flight transport a **1.5s post-control completion deadline**: once STOP or PAUSE makes that request unwanted, close/abort that one transport and require the worker-facing call to return by the shared deadline. This is one cancellation deadline, not repeated short HTTP retries.
* Once PAUSE is requested, do not enter 503 retry or backend recovery for the current chunk. Use the nearest already-available chunk/index boundary and emit `704 PAUSE`; no-control recovery remains unchanged and the pinned recovery test still passes.
* Enforce a normal absolute POST deadline as well if `120` is intended to mean 120 wall-clock seconds. Otherwise document it explicitly as inactivity-only; it cannot be used in a worst-case latency claim.

### R06 - F20-F21: Retry-After waits

* Keep the 5s server-busy cap for speech that remains wanted and keep the **0.05s** abort polling interval.
* Treat both STOP and any pending PAUSE as cancellation of retry admission. Required control latency at this site: **0.05s maximum requested sleep**, with no attempt 2 after the control state wins.
* Keep the atomic `reserve_retry()` gate. The existing health-to-POST recovery test is unaffected because it issues neither STOP nor PAUSE.

### R07 - F22-F24: DELETE handoff

* Reduce a single DELETE to a **0.25s absolute call deadline**, including any response body.
* Use one **1.0s generation-wide handoff deadline**, shared by all request IDs and both asynchronous and worker cleanup, rather than restarting a 1s budget for every ID. This leaves 0.5s of the proposed 1.5s control budget for worker/executor unwind.
* Check the deadline at the top of the loop before every DELETE and after every 0.05s wait. Never admit a DELETE whose absolute deadline extends beyond the generation deadline.
* Visit every owned ID once before retrying 404 IDs, so a stale original ID cannot consume the budget ahead of the active recovery ID. Add an explicit aggregate attempt cap, for example **20 DELETE attempts per generation**; the existing 404-then-200 race needs only a few attempts and remains covered.
* Deduplicate the STOP snapshot and worker-finally cancellation accounting against the same generation-wide budget. This need not introduce parallel DELETEs or new synchronization primitives; it only prevents four independent per-ID budgets from summing to approximately 24s.

### Required deadline regressions

The current tests protect behavior but not time. Add deterministic Event/clock/transport tests that assert:

* an accepted recovery STOP/PAUSE returns the worker within 1.5s while startup ownership is still blocked, without trying to interrupt `flock`;
* health and voices trickle bodies cannot outlive their absolute recovery deadlines;
* a POST can remain valid for normal synthesis but returns within 1.5s after cancellation;
* DELETE cannot start after its handoff deadline, all IDs share one aggregate deadline, and stale IDs cannot starve the active ID;
* readiness does not admit a post-deadline probe or sleep and has an independent attempt cap;
* `test_health_to_post_race_restarts_refreshes_and_retries_once` still passes unchanged.

## Explicitly not a problem

### `desktop/backend.py:99` - `flock(LOCK_UN)`

Unlock does not wait for another owner; it releases this descriptor's lock. It is not the acquisition delay and should not be made interruptible. `os.close()` at `desktop/backend.py:101` similarly does not wait for lock ownership transfer.

### `desktop/backend.py:279` - `process.poll()`

`Popen.poll()` uses a nonblocking child-status check. It does not wait for backend exit and adds no configured startup delay. The process launch at F11, not `poll()`, is the blocking concern.

### `desktop/backend.py:276` and `desktop/backend.py:277` - monotonic clock reads

The default `time.monotonic()` calls are nonblocking. The problem is deadline placement around blocking probes and sleeps, not clock acquisition.

### `desktop/synth.py:159` on the shipped accepted path

The uninterruptible direct Retry-After sleep is not selected by `SpeechEngine`: both worker synthesis calls pass `should_abort`. It remains an API fallback and is included as F20 because every sleep was checked.

### Retry and loop cardinality that is actually fixed

The `for attempt in (1, 2)` synthesis loop has exactly two attempts. The abort-aware retry wait reduces a maximum 5s arithmetic remainder and therefore has at most 101 sleeps. Neither can iterate indefinitely by count, although the HTTP calls and scheduler can still make wall time unbounded. By contrast, readiness and cancellation handoff have no attempt cap independent of time.

### Pre-accept health and catalog work

`SpeechEngine.handle_speak()` performs ordinary readiness/catalog loading before sending `200 OK SPEAKING`; STOP/PAUSE latency after acceptance does not include those invocations. The same functions are nevertheless in Findings because `_fetch()` invokes them again during mid-utterance recovery, after acceptance.

### `idle_timeout=300`

At `desktop/backend.py:258` this value is only copied to child environment variable `TTS_IDLE_TIMEOUT`. It is not a wait in either audited file, does not cap startup or HTTP calls, and does not add 300s to STOP/PAUSE latency.

### Numeric default backend host

The shipped URL is `http://127.0.0.1:5000`, so DNS resolution is not an extra default-path blocker. A user-configured hostname would add resolver behavior that `urlopen(timeout=...)` does not reliably bound, but that is outside the requested shipped-default calculation.

### Abort/reservation callbacks

Calls at `desktop/synth.py:129`, `desktop/synth.py:131`, `desktop/synth.py:164`, `desktop/synth.py:168`, `desktop/synth.py:171`, and `desktop/synth.py:200` acquire the engine's short state lock in production. Those critical sections contain only identity/Event/set checks and no I/O or waits. No lock-order cycle or meaningful application-level delay was found there; the blocking risks occur in the transport, startup lock, sleeps, and cleanup sequencing documented above.
