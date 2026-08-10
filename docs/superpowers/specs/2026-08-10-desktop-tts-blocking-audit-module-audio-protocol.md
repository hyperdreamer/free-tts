## Summary

Audit target: branch `feature/desktop-tts-engine`, HEAD
`40bc8454b2787900e23ff653302e4290e2f20980`. The strict latency figures below
are wall-clock bounds, not the values merely passed as `timeout=` arguments.
`+inf s` means that the code imposes no finite end-to-end deadline. Where useful,
I also give a nominal total that would apply if each lower-level timeout were a
hard whole-call deadline.

The worst offenders, ordered by cancellation impact, are:

1. **Blocked stdout while holding both locks: `+inf s`.**
   `SpeechEngine._emit()` holds the engine lock while calling `ProtocolIO`, and
   `ProtocolIO` holds its lock through both `stdout.write()` and `flush()`.
   A blocked audio/event write therefore prevents STOP and PAUSE from even
   recording intent; a blocked terminal write prevents delivery and worker exit.
2. **No-timeout futures and executor shutdown: `+inf s`.**
   `future.result()` has no timeout, and `ThreadPoolExecutor.__exit__()` calls
   `shutdown(wait=True)`. Cancelling a running lookahead future does nothing.
   The terminal event is deliberately emitted only after executor shutdown.
3. **Synthesis/recovery calls: `+inf s` strict, about `549.25 s` nominal for one
   current `_fetch`.** A `SynthClient.synthesize()` can make two nominal 120 s
   POSTs around a 5 s Retry-After wait (`245 s`). `_fetch` can then perform
   backend readiness (nominally less than `39.25 s` absent lock/spawn delay), a
   20 s voice request, and another two-POST synthesis (`245 s`):
   `245 + 39.25 + 20 + 245 = 549.25 s`. `urllib` timeouts are socket-operation
   timeouts, not total-response deadlines, so a trickling peer makes the strict
   total unbounded.
4. **Decoder process/I/O ownership: `+inf s`.** PAUSE never sets the decoder's
   cancellation Event, `communicate()` has no timeout, stream `close()` has no
   deadline, and cancellation ends with an unconditional
   `pump_thread.join()`. The two process waits total only 1.0 s, but the second
   timeout is suppressed, so that sequence also does not guarantee reaping.
5. **Command-loop blockers: `+inf s`.** A LIST VOICES or second SPEAK received
   while an utterance is active runs readiness and voice I/O synchronously before
   the loop can read a queued STOP/PAUSE. Synchronous stderr logging and malformed
   unterminated data blocks are also unbounded.
6. **Large but nominally bounded gates:** worker reclaim is `10.0 s`; the
   acceptance `_registered` gate is `5.0 s`; `_go` is `5.0 s`; each `wait_idle`
   join is `5.0 s`, while the QUIT loop repeats it without a total deadline.
   Synchronous worker cleanup can serially spend nominally less than `12.10 s`
   on two generation-owned DELETEs. The asynchronous STOP cancellation thread can
   process up to three IDs, nominally less than `18.15 s`, although it is not
   joined by the speech worker.

The shipped `idle_timeout=300` is not a module wait bound. It is passed to a
backend process as its self-managed inactivity lifetime. It limits none of the
operations in this report.

There is no intrinsic lock-order cycle. The observed orders are engine ->
generation and engine -> protocol -> stdout. Generation and protocol code never
acquire the engine lock, and all worker `join()` calls occur after releasing it.
Thus there is no lock-ordering deadlock path. There are indefinite lock convoys:
a thread can hold the engine/protocol lock forever in output I/O while STOP or
PAUSE waits forever, but that is not a cyclic deadlock.

A deterministic probe at `/tmp/free_tts_blocking_audit_probe.py` used only
`threading.Event` barriers, not sleep ordering. It observed:

- `_registered.wait(5.0)`: `5.001 s` when registration was withheld;
- `_go.wait(5.0)`: `5.002 s` when `_go` was withheld;
- an in-flight one-chunk future: STOP had returned from cancellation delivery,
  but `703 STOP` was absent and the speech worker was alive until the fetch was
  explicitly released;
- a running lookahead: executor `shutdown()` had been entered, but `704 PAUSE`
  was absent and the speech worker was alive until the lookahead was released;
- decoder cleanup: all three process streams were closed, but decode was still
  alive until the fake `communicate()` was released; `_stop_process()` passed
  `[0.5, 0.5]` to its two waits;
- blocked stdout `write()` and, separately, blocked `flush()`: STOP had attempted
  the engine lock and a second protocol writer had attempted the protocol lock,
  but neither returned until stdout was released;
- reclaim and idle joins received exactly `10.0 s` and `5.0 s`.

The requested read-only suite passed: `.venv/bin/python -m pytest -q` reported
`451 passed in 15.21s`.

An absolute `PAUSE < 2 s` guarantee is impossible while also requiring an honest
future index mark or next chunk boundary: PAUSE cannot truthfully report that
boundary before current-chunk synthesis, decode, and transmission reach it. The
recommendations can keep **cancellation overhead after STOP, and after PAUSE has
reached its honest boundary**, under about two seconds. A hard two-second PAUSE
from command receipt additionally requires smaller/precomputed chunks or a change
to what constitutes an honest boundary.

## Findings

### F-1 - Speech worker start after acceptance - `desktop/module.py:237`

- **Call and effective bound:** `worker.start()`. CPython `Thread.start()` ends in
  `_started.wait()` with no timeout. Effective bound: unbounded.
- **Reachable path:** `handle_speak()` sends `200 OK SPEAKING` at line 228, stores
  the worker, then starts it. The utterance has already been accepted.
- **Worst added latency:** STOP `+inf s`; PAUSE `+inf s`. The command loop cannot
  return from `handle_speak()`, and no speech worker exists to emit a terminal
  event. Failure to create a thread can instead raise after acceptance.
- **Existing test:** No. Ordinary tests start threads successfully; none gates or
  fails `Thread.start()` after `200 OK SPEAKING`.
- **Enforcement:** none.

### F-2 - Cancellation-thread start - `desktop/module.py:259`

- **Call and effective bound:** `threading.Thread(...).start()` at line 259;
  unbounded by the same `Thread.start()` rule.
- **Reachable path:** accepted utterance -> `run()` reads STOP -> `handle_stop()`
  records `generation.cancelled` under the engine lock -> snapshots IDs -> starts
  asynchronous DELETE delivery.
- **Worst added latency:** terminal event `0 s` of code-imposed dependency, because
  cancellation intent is recorded before this call and the speech worker does not
  join this thread; STOP-handler return itself is `+inf s`. Runtime starvation can
  still make the practical terminal latency unbounded.
- **Existing test:** Partial. cancellation tests prove normal asynchronous delivery,
  not a stalled `start()` and not prompt handler return.
- **Enforcement:** none.

### F-3 - Acceptance registration gate - `desktop/module.py:241`

- **Call and effective bound:** `self._registered.wait(5.0)`; at most nominally
  `5.0 s`.
- **Reachable path:** `200 OK SPEAKING` -> worker start -> the command-loop thread
  waits here before it can read the next STOP or PAUSE.
- **Worst added latency:** STOP `5.0 s`; PAUSE `5.0 s`. The probe measured
  `5.001 s`.
- **Existing test:** No latency contract. Existing SPEAK tests happen to register
  immediately.
- **Enforcement:** literal constant `5.0` at the call site.

### F-4 - Worker go gate - `desktop/module.py:353`

- **Call and effective bound:** `self._go.wait(5.0)`; at most nominally `5.0 s`.
- **Reachable path:** accepted utterance -> worker registers its first request ID,
  sets `_registered`, then parks before the first pool submission.
- **Worst added latency:** STOP `5.0 s`; PAUSE `5.0 s` if `_go` is not set. The
  isolated probe measured `5.002 s`. On the normal `handle_speak()` path this is
  **not additive** with F-3: the command thread sets `_go` immediately after its
  registration wait returns or times out, before it can read STOP/PAUSE.
- **Existing test:** No direct timeout test.
- **Enforcement:** literal constant `5.0` at the call site.

### F-5 - First executor submission - `desktop/module.py:354`

- **Call and effective bound:** `pool.submit(self._fetch, ...)`. Queue insertion is
  short, but the first submit may create the executor thread via an unbounded
  `Thread.start()`; there is no application deadline.
- **Reachable path:** accepted worker -> `_go` gate -> submit current chunk.
- **Worst added latency:** STOP `+inf s`; PAUSE `+inf s` if executor thread startup
  stalls before `submit()` returns.
- **Existing test:** No thread-start failure/stall test.
- **Enforcement:** none.

### F-6 - Lookahead executor submission - `desktop/module.py:367`

- **Call and effective bound:** `pool.submit(self._fetch, ...)`; no direct timeout.
  With `max_workers=1` the worker normally already exists, so this is a queue/lock
  operation, but the API supplies no wall-clock guarantee.
- **Reachable path:** accepted worker -> current chunk submitted -> register and
  submit the next chunk before waiting for the current result.
- **Worst added latency:** `0 s` of deliberate waiting in the normal one-worker
  state; strict API/runtime bound `+inf s`.
- **Existing test:** Lookahead behavior is covered, but submit latency is not.
- **Enforcement:** none.

### F-7 - No-timeout current future - `desktop/module.py:376`

- **Call and effective bound:** `future.result()` defaults to `timeout=None` and is
  explicitly unbounded. The default `_fetch` has no strict bound either. Nominally,
  one `_fetch` can consume about `549.25 s` as summarized above.
- **Reachable path:** accepted worker -> current or promoted lookahead future ->
  wait before decode/audio/boundary/terminal event.
- **Worst added latency:** strict STOP `+inf s`; strict PAUSE `+inf s`. If all HTTP
  timeout arguments were hard total deadlines and STOP reached `handle_stop()`, an
  in-flight synthesis POST adds at most nominally `120 s` because abort is checked
  between attempts. PAUSE before the honest current boundary can incur the full
  nominal `_fetch` chain, about `549.25 s`. F-3 can precede a just-started POST,
  yielding a nominal `5 + 120 = 125 s` STOP path.
- **Existing test:** Partial only. `test_stop_emits_stop_event_once`, pause fallback
  tests, and cancellation ownership tests release their fake synthesis calls.
  None leaves a future uncooperative and asserts a production deadline. The probe
  demonstrated that no terminal event appears before release.
- **Enforcement:** no direct bound. POST arguments derive from configured
  `request_timeout=120`; retry delay and voices timeout are constants; startup
  window is configured.

### F-8 - Executor context exit waits for submitted work - `desktop/module.py:341`

- **Call and effective bound:** implicit `ThreadPoolExecutor.__exit__()` after the
  suite ending at line 442. CPython implements it as `shutdown(wait=True)` with no
  timeout. `pending.cancel()` at line 441 cannot cancel a running lookahead.
- **Reachable path:** accepted worker -> one-chunk lookahead begins while current
  audio is decoded/sent -> STOP or PAUSE chooses a terminal outcome -> `finally`
  requests cancellation -> context exit waits -> only then lines 451-463 emit the
  terminal event.
- **Worst added latency:** STOP `+inf s`; PAUSE `+inf s`. Granting hard HTTP
  timeouts, a lookahead already in a POST can still add nominally up to `120 s`
  after cancellation/boundary. The probe reached `shutdown()` with PAUSE selected
  and proved `704 PAUSE` remained absent until the lookahead was released.
- **Existing test:** Partial. Tests at `tests/test_desktop_module.py:533`,
  `tests/test_desktop_module.py:620`, `tests/test_desktop_module.py:705`, and
  `tests/test_desktop_module.py:802` require prompt PAUSE for cooperative
  retry/DELETE fakes. They catch those specific abort regressions, not an
  uncooperative running future or unbounded `shutdown(wait=True)`.
- **Enforcement:** none in `module.py`; only incidental callee timeouts.

### F-9 - `wait_idle` join - `desktop/module.py:275`

- **Call and effective bound:** `worker.join(timeout)`, with method default
  `timeout=5.0` at line 269. One default call waits nominally at most `5.0 s`.
- **Reachable path:** after acceptance, EOF calls `close()`; tests/callers also use
  this method, and QUIT uses it repeatedly.
- **Worst added latency:** one call `5.0 s`. It does not hold the engine lock, so a
  concurrently received control that has another command-reading thread is not
  lock-blocked; in production the sole command loop is the caller.
- **Existing test:** Many tests use explicit 1/3/5 s joins as test harness limits,
  but no test pins the shipped default or requires cancellation cleanup below it.
- **Enforcement:** function-default constant `5.0`, caller-overridable; not config.

### F-10 - Previous-worker reclaim join - `desktop/module.py:304`

- **Call and effective bound:** `worker.join(_WORKER_RECLAIM_SECONDS)` with
  `_WORKER_RECLAIM_SECONDS = 10.0` at line 62.
- **Reachable path:** an utterance is active -> command loop receives a second
  SPEAK -> synchronous catalog refresh completes -> `_reclaim_worker()` calls
  `handle_stop()` on the old generation -> waits here before refusing or accepting
  the new message. A queued explicit STOP/PAUSE cannot be read during the join.
- **Worst added latency:** command recognition `10.0 s`. Cancellation of the old
  utterance is already recorded before the join, so this join need not add 10 s to
  its terminal event if the worker unwinds sooner. If it does not, new speech is
  refused and the old worker remains the admission blocker.
- **Existing test:** `test_stale_worker_cannot_cancel_a_newer_generation` at
  `tests/test_desktop_module.py:909` monkeypatches this constant to `0.0` and checks
  refusal/ownership. It does not catch a regression in the shipped 10 s latency.
- **Enforcement:** module-level constant, not config.

### F-11 - Repeated QUIT idle wait - `desktop/module.py:677`

- **Call and effective bound:** `while not engine.wait_idle(): pass`; each nested
  join is 5 s, but the loop has no total deadline.
- **Reachable path:** accepted utterance -> bare QUIT. By documented behavior, bare
  QUIT intentionally lets current speech finish.
- **Worst added latency:** a STOP or PAUSE queued after QUIT is never dispatched:
  `+inf s`. For a prior STOP followed by QUIT, terminal emission can remain blocked
  by the worker for `+inf s`, and QUIT waits with it.
- **Existing test:** End-to-end QUIT tests have an outer 60 s subprocess timeout,
  not a module cancellation deadline.
- **Enforcement:** per-iteration constant only; no aggregate bound.

### F-12 - Backend readiness on the command thread - `desktop/module.py:285`

- **Call and effective bound:** `self._controller.ensure_ready()`. Strict bound:
  unbounded. Its backend startup lock is unbounded, process spawn has no deadline,
  and health HTTP timeouts are socket-operation timeouts. Ignoring those facts,
  the nominal call can approach `3 + 3 + 33.25 = 39.25 s`: two health probes before
  the startup window, then a configured 30 s readiness window whose final 3 s
  probe and 0.25 s sleep can overshoot.
- **Reachable path:** after one accepted utterance, the command loop can receive
  LIST VOICES or a second SPEAK; both call `_ensure_catalog()` synchronously before
  the loop can read a later STOP/PAUSE. The second SPEAK then can add F-10.
- **Worst added latency:** STOP `+inf s`; PAUSE `+inf s`. Nominal command-loop delay
  from this call alone is less than about `39.25 s` before the control is read.
- **Existing test:** Backend and module tests verify restart/revalidation behavior,
  not cancellation responsiveness while this call runs.
- **Enforcement:** `startup_timeout=30` is config but bounds only the inner readiness
  loop; 3 s probe and 0.25 s sleep are constants in `backend.py`; lock/spawn have no
  bound.

### F-13 - Voice request on the command thread - `desktop/module.py:289`

- **Call and effective bound:** `self._client.voices()`. It passes the constant
  `_VOICES_TIMEOUT=20.0` to urllib, but that is not a total deadline; strict bound
  unbounded, nominal `20.0 s`.
- **Reachable path:** F-12 succeeds for LIST VOICES or a second SPEAK while an older
  accepted utterance lives -> this GET runs synchronously -> queued STOP/PAUSE is
  unread. Combined nominal catalog delay is about `39.25 + 20 = 59.25 s`; a second
  SPEAK can then add the 10 s reclaim gate for about `69.25 s` of command-thread
  occupancy.
- **Worst added latency:** STOP `+inf s`; PAUSE `+inf s`; nominal direct addition
  `20.0 s`.
- **Existing test:** Voice tests check payload/error behavior. No slow/trickling GET
  or active-utterance command-loop deadline test exists.
- **Enforcement:** 20 s callee constant, not config, and not end-to-end.

### F-14 - Initial synthesis call in `_fetch` - `desktop/module.py:527`

- **Call and effective bound:** `self._client.synthesize(...)`. Strict bound
  unbounded. Nominally it allows two configured 120 s POST attempts plus a constant
  Retry-After cap of 5 s: `245 s`.
- **Reachable path:** accepted worker -> pool future -> `_fetch` -> backend POST.
- **Worst added latency:** after STOP intent is recorded, nominal remaining delay is
  at most one in-flight `120 s` POST because callbacks are checked before a retry;
  strict STOP `+inf s`. PAUSE does not abort current synthesis before its honest
  boundary, so nominal `245 s` here and strict PAUSE `+inf s`.
- **Existing test:** `tests/test_desktop_synth.py` tests retry count, 5 s cap, and
  abort during Retry-After. It does not test aborting a blocked transport or a hard
  total timeout. Module tests use cooperative fakes.
- **Enforcement:** per-attempt `request_timeout=120` from config; retry cap constant;
  no whole-call deadline.

### F-15 - Backend recovery readiness in the fetch thread - `desktop/module.py:544`

- **Call and effective bound:** `_controller.ensure_ready()`; same unbounded strict
  and about 39.25 s nominal analysis as F-12.
- **Reachable path:** accepted worker -> F-14 raises `SynthError` -> cancellation
  check loses the race -> recovery enters readiness -> STOP or PAUSE arrives.
- **Worst added latency:** STOP `+inf s`; PAUSE `+inf s`; nominal direct addition
  less than about `39.25 s`.
- **Existing test:** Recovery tests prove restart and retry, not cancellation during
  readiness.
- **Enforcement:** partially config (`startup_timeout=30`), partially constants,
  with no overall call bound.
- **Scope note:** this is one of the two defects supplied as already known. It is
  included for completeness, not presented as a newly discovered defect.

### F-16 - Recovery voice refresh - `desktop/module.py:545` via `desktop/module.py:289`

- **Call and effective bound:** `self._refresh_catalog()` ->
  `self._client.voices()`; strict unbounded, nominal `20.0 s`.
- **Reachable path:** accepted `_fetch` loses the cancellation race into recovery ->
  readiness returns -> voice GET runs without a cancellation callback -> only then
  line 546 rechecks cancellation.
- **Worst added latency:** STOP `+inf s`; PAUSE `+inf s`; nominal `20.0 s`, added
  after F-15 for a conceptual `30 + 20 = 50 s` configured/constant sum and a more
  complete nominal call-site sum approaching `59.25 s` including health probes.
- **Existing test:** No cancellation-timing test around recovery voice fetch.
- **Enforcement:** constant in `synth.py`, not config, and not end-to-end.
- **Scope note:** this is the other half of the already-known recovery defect.

### F-17 - Recovery synthesis retry - `desktop/module.py:552`

- **Call and effective bound:** second `self._client.synthesize(...)`; strict
  unbounded, nominal `245 s` for two POSTs and Retry-After.
- **Reachable path:** accepted `_fetch` -> first synthesis error -> recovery and
  catalog refresh -> cancellation check passes -> fresh request ID -> retry call.
  STOP/PAUSE can race immediately after that check and after the transport's own
  pre-request callback.
- **Worst added latency:** STOP `+inf s`, nominally one in-flight `120 s` POST after
  intent; PAUSE `+inf s`, nominally up to `245 s` before current boundary.
- **Existing test:** Fresh-ID and retry-admission tests cover ownership/races, not a
  blocked retry transport deadline.
- **Enforcement:** config per POST plus constant retry delay; no whole-call bound.

### F-18 - Serial DELETE cleanup - `desktop/module.py:582`

- **Call and effective bound:** `self._client.cancel(request_id, ...)`, serial for
  every ID. Each DELETE uses a 5 s socket timeout and a 1 s 404-handoff window. If
  those were hard call bounds, a DELETE can start just after the 1 s handoff
  deadline was crossed by the 0.05 s sleep and then consume 5 s, giving a supremum
  below `6.05 s` per ID. Strict bound is unbounded.
- **Reachable path:** STOP starts an asynchronous snapshot canceler; the speech
  worker also calls `_cancel_outstanding()` at line 442 before executor exit and
  terminal emission. PAUSE uses the synchronous worker path. With one running and
  one queued lookahead, the worker can own at most two IDs at cleanup, for nominal
  `< 12.10 s` serial delay. The earlier asynchronous STOP snapshot can contain a
  current original ID, its recovery retry ID, and a queued lookahead ID, for
  nominal `< 18.15 s`, but that thread is not joined.
- **Worst added latency:** worker-gating STOP `+inf s`; PAUSE `+inf s`; nominal
  synchronous addition `< 12.10 s`. It overlaps a running fetch, so it is not
  always additive to F-8, but it is fully additive when the future was cancellable
  before starting.
- **Existing test:** DELETE handoff and generation-ownership tests catch retry and
  ownership regressions with immediate/event-gated transports. No test uses a slow
  DELETE, verifies the 5 s argument, or pins a shared multi-ID cleanup deadline.
- **Enforcement:** 5 s, 1 s, and 0.05 s constants in `synth.py`; not config; no
  aggregate list deadline.

### F-19 - Decoder call as seen by the speech worker - `desktop/module.py:381`

- **Call and effective bound:** `self._decode(...)`; the default `decode_mp3()` is
  unbounded for the reasons in F-30 through F-36 below. An injected decoder is
  also allowed and has no interface deadline.
- **Reachable path:** accepted future returns MP3 -> worker decodes before BEGIN,
  audio, mark, and terminal event.
- **Worst added latency:** STOP `+inf s`; PAUSE `+inf s`. STOP sets the Event passed
  to the decoder; PAUSE does not, so PAUSE cannot even trigger decoder teardown.
- **Existing test:** `test_stop_leaves_no_decode_thread_running` verifies Event
  propagation for a cooperative injected decoder. Audio tests cover cooperative
  process teardown. No test enforces a deadline against an uncooperative decoder.
- **Enforcement:** none at this call site.

### F-20 - Gain conversion over unbounded PCM - `desktop/module.py:380`, `desktop/audio.py:168`

- **Call and effective bound:** `apply_gain(...)`, an uncancelled O(number of PCM
  samples) loop/allocation when gain is not 1.0. There is no decoded-output size or
  CPU deadline. Default volume 100 maps to gain 1.0 and takes the fast path, but a
  valid SET can select any other gain.
- **Reachable path:** accepted decode returns arbitrarily large PCM -> gain scaling
  runs before the next cancellation check at module line 389.
- **Worst added latency:** STOP `+inf s`; PAUSE `+inf s` for unbounded input. This is
  CPU work rather than a wait primitive, but it is cancellation-blind wall time.
- **Existing test:** gain arithmetic tests do not test cancellation or a size cap.
- **Enforcement:** none; no PCM size limit.

### F-21 - Protocol emissions while holding the engine lock - `desktop/module.py:321`; call sites `desktop/module.py:395`, `desktop/module.py:408`, `desktop/module.py:418`, `desktop/module.py:447`, `desktop/module.py:456`, `desktop/module.py:461`

- **Call and effective bound:** `_emit()` acquires the engine lock and executes the
  supplied `event_begin`, `send_audio`, `index_mark`, `event_stop`, `event_pause`,
  or `event_end` action before releasing it. Each action can block in protocol
  output for `+inf s`.
- **Reachable path:** every accepted worker emission. In particular, STOP and PAUSE
  both need the same engine lock at lines 246 and 263.
- **Worst added latency:** STOP `+inf s`; PAUSE `+inf s`. If BEGIN/audio/mark holds
  the lock, control intent is not recorded. If the terminal action blocks, intent
  may be recorded but the required `703`/`704` is not delivered and the worker is
  not released.
- **Existing test:** No. State-machine tests use nonblocking `_FakeIO`; protocol
  tests use `BytesIO`; the small end-to-end tone does not fill its stdout pipe.
  The probe deterministically blocked both `write()` and `flush()` and observed
  STOP waiting on this lock.
- **Enforcement:** none at the engine layer; audio is chunked by a 10,000-byte
  constant, but write duration has no bound.

### F-22 - Synchronous logging after acceptance - `desktop/module.py:189`, `desktop/module.py:203`, `desktop/module.py:306`, `desktop/module.py:434`, `desktop/module.py:437`; `desktop/protocol.py:62`

- **Call and effective bound:** `logger.error`, `logger.exception`, and
  `logger.debug`. With `_configure_logging()`, handlers write/flush stderr
  synchronously under logging locks; there is no timeout.
- **Reachable path:** active utterance plus LIST VOICES/second SPEAK failure; worker
  synthesis/decode/unexpected failure before terminal cleanup; malformed SET data
  when debug logging is enabled.
- **Worst added latency:** STOP `+inf s`; PAUSE `+inf s`. Command-thread logging can
  keep the loop from reading control; worker logging can keep it from cleanup and
  terminal emission.
- **Existing test:** No blocked stderr or logging-handler concurrency test.
- **Enforcement:** none.

### F-23 - Post-acceptance command/data reads - `desktop/module.py:630`, `desktop/module.py:636`, `desktop/module.py:639`, `desktop/module.py:642`, `desktop/module.py:652`, `desktop/module.py:658`, `desktop/module.py:666` via `desktop/protocol.py:79`, `desktop/protocol.py:88`, `desktop/protocol.py:95`

- **Call and effective bound:** binary `stdin.readline()` is unbounded; repeated
  reads for a dot-terminated message/settings block have no aggregate deadline.
- **Reachable path:** after acceptance, line 630 normally waits for STOP/PAUSE. A
  previously received SPEAK/CHAR/KEY/SOUND_ICON/SET/AUDIO/LOGLEVEL starts a data
  block and can leave the command loop waiting for its terminating dot.
- **Worst added latency:** correctly framed STOP/PAUSE behind the top-level read:
  `0 s` added by the wait, because the arriving line satisfies it. If the client
  first opens an unterminated data block, STOP/PAUSE is either consumed as data or
  remains unread: `+inf s`.
- **Existing test:** Protocol tests cover complete `BytesIO` blocks and EOF, not a
  live partial block with queued control.
- **Enforcement:** none. This is primarily a malformed/protocol-serialization path.

### F-24 - Every generation-token lock region - `desktop/module.py:76`, `desktop/module.py:80`, `desktop/module.py:84`, `desktop/module.py:89`, `desktop/module.py:95`

- **Call and effective bound:** each `with self._lock` is an un-timed lock acquire.
  The protected bodies are fixed-size set add/discard/membership, or sorting and
  clearing the small request-ID set; none holds the lock across I/O, a wait, a
  callback, or another lock.
- **Reachable path:** request registration/discard in `_fetch`, STOP snapshot, and
  worker cleanup after acceptance.
- **Worst added latency:** `0 s` of intentional blocking in the owner bodies;
  scheduler-level strict lock-acquire bound `+inf s`. At production lookahead depth,
  the set is at most three fixed-length IDs when STOP snapshots it.
- **Existing test:** ownership tests cover contents and generation isolation, not a
  lock deadline or future insertion of blocking work.
- **Enforcement:** no timeout; bounded only structurally by short bodies.

### F-25 - Short engine-lock regions - `desktop/module.py:220`, `desktop/module.py:235`, `desktop/module.py:246`, `desktop/module.py:263`, `desktop/module.py:271`, `desktop/module.py:298`, `desktop/module.py:308`, `desktop/module.py:466`, `desktop/module.py:473`, `desktop/module.py:482`, `desktop/module.py:572`

- **Call and effective bound:** each is an un-timed engine-lock acquire. Once
  acquired, these regions only copy references, inspect identity/Events,
  call `Thread.is_alive()`, or set an Event. They contain no blocking I/O or wait.
- **Reachable path:** generation/worker publication, STOP/PAUSE, idle/reclaim
  snapshots, abort checks, pause-boundary publication, and DELETE's
  `still_wanted` callback.
- **Worst added latency:** owner-body contribution `0 s`; acquisition at STOP line
  246 or PAUSE line 263 is `+inf s` because F-21 may own this same lock across
  blocked output.
- **Existing test:** Race tests cover atomic event/state outcomes, but no test
  blocks the current lock owner in real I/O.
- **Enforcement:** no lock timeout; safety depends on short owners except F-21.

### F-26 - Nested engine-to-generation lock - `desktop/module.py:497` -> `desktop/module.py:84`

- **Call and effective bound:** `_reserve_retry()` holds the engine lock while
  `generation.owns_request()` acquires the generation lock. No timeout.
- **Reachable path:** accepted synthesis receives 503 -> immediately before its
  second POST, the fetch thread reserves retry admission against STOP/PAUSE.
- **Worst added latency:** `0 s` code-imposed with current short generation owners;
  strict scheduler bound `+inf s`. There is no reverse generation -> engine path,
  so this does not create a lock-order deadlock.
- **Existing test:** `test_pause_boundary_wins_before_retry_reservation` and
  `test_retry_reservation_wins_then_pause_cancels_it` cover race semantics, not a
  lock-duration regression.
- **Enforcement:** structural only; no timeout.

### F-27 - Protocol lock regions - `desktop/protocol.py:99`, `desktop/protocol.py:105`, `desktop/protocol.py:132`

- **Call and effective bound:** each un-timed `with self._lock` is held through
  `_write()`. At line 132, escaping and payload construction also occur while the
  lock is held. `_write()` can block without limit.
- **Reachable path:** accepted worker emits BEGIN/audio/mark/terminal while the main
  loop can emit command replies or voice rows on the same stdout stream.
- **Worst added latency:** STOP `+inf s`; PAUSE `+inf s`. A writer can wait forever
  for another writer that is stuck in stdout; when called through `_emit`, the
  worker also holds the engine lock. The probe showed a second protocol writer and
  STOP both blocked behind one audio write/flush.
- **Existing test:** Byte-conformance and frame-splitting tests are single-threaded
  and use `BytesIO`; no backpressure/lock test exists.
- **Enforcement:** no lock or I/O deadline. `MAX_AUDIO_CHUNK_BYTES=10000` bounds
  payload size per module call, not duration.

### F-28 - stdout write - `desktop/protocol.py:158`

- **Call and effective bound:** `self._stdout.write(payload)`; unbounded. Pipes and
  buffered streams expose no deadline here, and a slow/non-reading Speech
  Dispatcher can fill the pipe.
- **Reachable path:** every accepted response/event/audio frame, under F-27 and,
  for worker events/audio, under F-21.
- **Worst added latency:** STOP `+inf s`; PAUSE `+inf s`. This can delay intent,
  terminal delivery, and worker release.
- **Existing test:** No blocked/partial stdout test. Existing tests only verify
  exact bytes and small real-pipe transcripts.
- **Enforcement:** none.

### F-29 - stdout flush - `desktop/protocol.py:161`

- **Call and effective bound:** `flush()` when present; unbounded and still inside
  the protocol lock and any caller's engine lock.
- **Reachable path:** every protocol write after acceptance.
- **Worst added latency:** STOP `+inf s`; PAUSE `+inf s`. The deterministic flush
  probe reproduced the same lock convoy as a blocked write.
- **Existing test:** `test_send_appends_newline_and_flushes` proves flush occurs,
  which preserves the risk; it does not bound it.
- **Enforcement:** none.

### F-30 - Decoder process creation - `desktop/audio.py:97`

- **Call and effective bound:** `popen_factory(...)`, default
  `subprocess.Popen`; no timeout for process creation/exec setup.
- **Reachable path:** accepted future returns MP3 -> default decoder starts ffmpeg
  before its first cancellation check at line 106.
- **Worst added latency:** STOP `+inf s`; PAUSE `+inf s` if process creation stalls.
- **Existing test:** Missing-ffmpeg and command-shape tests cover immediate return or
  exception, not stalled process creation.
- **Enforcement:** none.

### F-31 - Decoder I/O-thread start - `desktop/audio.py:126`

- **Call and effective bound:** `pump_thread.start()`; CPython waits on the new
  thread's `_started` Event without a timeout.
- **Reachable path:** accepted decode creates ffmpeg, checks STOP once, then starts
  the owned I/O thread.
- **Worst added latency:** STOP `+inf s`; PAUSE `+inf s`. The cancellation polling
  loop is not reached until this call returns.
- **Existing test:** No stalled thread-start test.
- **Enforcement:** none.

### F-32 - ffmpeg `communicate()` - `desktop/audio.py:118`

- **Call and effective bound:** `process.communicate(input=data)` with no timeout,
  on the owned non-daemon pump thread.
- **Reachable path:** accepted decode -> pump thread writes all MP3 and reads all
  PCM/stderr while the speech worker polls the thread.
- **Worst added latency:** PAUSE `+inf s` directly, because PAUSE does not set the
  `cancel` Event. STOP reaches teardown within nominally 0.05 s, but strict STOP
  remains `+inf s` because close/join need not unblock this call.
- **Existing test:** Real ffmpeg and fake-process tests cover successful and
  cooperative teardown. No fake ignores terminate, kill, and pipe close while
  keeping `communicate()` blocked.
- **Enforcement:** none.

### F-33 - Decoder polling join - `desktop/audio.py:130`

- **Call and effective bound:** `pump_thread.join(_POLL_INTERVAL)` with constant
  `_POLL_INTERVAL=0.05`; each join is nominally bounded by `0.05 s`, repeated until
  completion or STOP cancellation.
- **Reachable path:** accepted decode while communicate remains alive.
- **Worst added latency:** STOP detection nominally `0.05 s`; PAUSE `+inf s`
  because its Event is not observed and the loop has no total deadline.
- **Existing test:** Mid-decode cancellation tests require completion within 2/5 s
  for cooperative fakes and would catch a gross polling regression, but do not pin
  0.05 s or a hard total.
- **Enforcement:** module constant.

### F-34 - Decoder terminate/kill waits - `desktop/audio.py:45`, `desktop/audio.py:52`

- **Call and effective bound:** two `process.wait(timeout=_TERMINATE_GRACE)` calls,
  each with constant `0.5 s`; explicit wait budget `0.5 + 0.5 = 1.0 s`. The first
  exception triggers kill. Every exception from the second wait is suppressed.
- **Reachable path:** STOP already set before decode, STOP during communicate, or
  any decode exception while process/pump remains live.
- **Worst added latency:** nominal `1.0 s` plus F-33 polling, stream close, and
  F-36 join; strict STOP `+inf s`. Importantly, the bounded sequence can return
  after the second timeout without reaping the child.
- **Existing test:** Terminate and kill escalation tests use fakes whose second wait
  succeeds immediately. They do not test a second timeout or assert successful
  reap after it.
- **Enforcement:** constant `_TERMINATE_GRACE=0.5`; no aggregate deadline object and
  no enforced postcondition.

### F-35 - Concurrent process-stream close - `desktop/audio.py:61`

- **Call and effective bound:** `stream.close()` for stdin/stdout/stderr, with
  exceptions suppressed and no timeout. These buffered objects may be in use by
  the communicate thread.
- **Reachable path:** every decode exit; on STOP, after terminate/kill waits and
  before the final pump join.
- **Worst added latency:** STOP `+inf s`; PAUSE reaches it only after communicate
  ends, also `+inf s`. A close may itself wait on an internal stream lock, and even
  successful close is not guaranteed to wake arbitrary/injected I/O.
- **Existing test:** `test_cancellation_closes_pipes_and_joins_blocked_decode_io`
  uses custom pipes whose close immediately releases communicate. It does not test
  a blocking close or a close that fails to wake I/O.
- **Enforcement:** none; exceptions are suppressed.

### F-36 - Unconditional decoder I/O join - `desktop/audio.py:156`

- **Call and effective bound:** `pump_thread.join()` with no timeout.
- **Reachable path:** STOP/exception -> process stop attempt -> stream close -> if
  communicate thread is still alive, wait here before propagating
  `DecodeCancelled`, before worker cleanup, and before `703 STOP`.
- **Worst added latency:** STOP `+inf s`; PAUSE `+inf s`. The probe proved decode
  remained blocked here after all three streams had been closed until communicate
  was explicitly released. Because the pump is non-daemon, it can also prevent
  process exit.
- **Existing test:** Existing lifetime tests assert no I/O thread remains only for
  fakes that release. They do not catch this unbounded join case.
- **Enforcement:** none. This contradicts the comment's unconditional ownership
  claim for an uncooperative I/O operation.

## Recommended deadlines

A single control-relative monotonic deadline should be threaded through existing
operations. Recomputing a fresh timeout for every serial step recreates the large
sums above. These recommendations preserve exact SSIP events, generation-local
request IDs, PAUSE/END exclusivity, honest boundaries, and decoder ownership.

1. **Acceptance and thread startup (F-1, F-3 through F-6, F-31):** do not send
   `200 OK SPEAKING` until the speech thread has successfully started. Use the
   existing `_registered` Event with a `0.25 s` acceptance-startup budget; on
   failure, send `301 ERROR CANT SPEAK`. Set the existing `_go` Event in a
   `finally`, and cap its defensive worker wait at `0.25 s`. A deadline cannot
   interrupt CPython `Thread.start()` itself; moving acceptance after successful
   start is required, or a prestarted worker must be used.
2. **Synthesis and Future waiting (F-7, F-14, F-17):** keep the normal generation
   allowance separate if 120 s synthesis is intentional, but once STOP is set,
   enforce one hard end-to-end cancellation deadline of `now + 1.25 s` across the
   active HTTP transport and `future.result()` polling. Poll Future completion at
   `0.05 s`; forbid any POST from starting after cancellation/boundary admission
   fails. `urllib`'s socket timeout is insufficient: the transport must close or
   otherwise abort the in-flight response at the shared deadline.
3. **Executor shutdown (F-8):** stop using context-manager exit as the source of a
   bound. Explicitly cancel queued work and require all running `_fetch` calls to
   consume the same `1.25 s` control deadline before `shutdown(wait=True)`. With
   hard-bounded fetches, shutdown is then bounded without another synchronization
   primitive. Do not emit `703`/`704` and leave an unowned executor thread behind;
   ownership requires fixing the fetch bound, not merely using `wait=False`.
4. **Recovery and catalog I/O (F-12, F-13, F-15, F-16):** after STOP, or after an
   honest PAUSE boundary is published, give the entire readiness-plus-voice chain
   at most `1.0 s`, not 30 s plus 20 s. Suggested slices are health probe `0.25 s`,
   startup-lock acquisition `0.25 s`, and voices `0.50 s`, all capped by the same
   absolute deadline. Check the existing generation Events before and after every
   probe, lock acquisition, spawn, sleep, and voice read. For command-thread LIST
   VOICES/second SPEAK while speech is active, either refuse/defer synchronously or
   apply a `1.0 s` command budget so queued controls are read promptly.
5. **DELETE cleanup (F-18):** use a shared `0.75 s` cancellation-handoff deadline
   across all IDs. Give each DELETE no more than `0.25 s` end-to-end and cap each
   retry sleep by the remaining shared time. This prevents two IDs from turning a
   per-request budget into 12.10 s. Preserve generation-local ownership and the
   404 registration handoff.
6. **Decoder (F-19, F-30 through F-36):** retain the `0.05 s` cancellation poll,
   but impose one `0.90 s` STOP teardown deadline: `0.25 s` terminate grace and the
   remaining `0.65 s` for kill plus successful reap. Every process wait and I/O
   join must consume the remaining deadline; no bare `join()` is acceptable.
   Because Python cannot kill a stuck thread while preserving ownership, prefer
   driving timeout-capable `Popen.communicate()`/pipe I/O in the speech worker
   itself rather than creating the pump thread. Do not return until the child is
   reaped; a second wait timeout must be an explicit fatal decoder/session failure,
   not suppressed success.
7. **Gain and decoded size (F-20):** cap decoded PCM per chunk to a documented
   duration/byte count and scale it in at most 10,000-byte slabs, checking the
   existing STOP Event between slabs. Give each slab a target budget below
   `0.05 s`. This adds no synchronization primitive and retains exact samples.
8. **Protocol output and locks (F-21, F-27 through F-29):** retain serialization and
   the engine-lock atomicity needed to prevent post-STOP audio, but make each
   status/frame write plus flush a hard `0.25 s` operation using nonblocking fd I/O
   and `poll`/`select` against one absolute deadline. Give terminal delivery a
   `1.0 s` total. If the peer does not read, delivery is physically impossible;
   fail the disconnected session and release ownership rather than block forever.
   Do not solve this by moving unbounded output outside `_emit`, which would reopen
   the post-STOP audio race.
9. **Worker joins (F-9 through F-11):** set `_WORKER_RECLAIM_SECONDS` and the
   cancellation/close `wait_idle` default to `1.5 s`, backed by the lower-level
   deadlines above. Bare QUIT is intentionally a finish-not-cancel operation and
   may continue waiting to preserve behavior, but it should use a non-busy loop and
   must not be described as a cancellation deadline.
10. **Logging (F-22):** worker/control-path logging should have a `0.05 s` best-
    effort budget and drop after it; stderr backpressure must never own the speech
    lifecycle. A nonblocking stream handler is preferable to another joined worker.
11. **Input framing (F-23):** no deadline is needed for the top-level command read.
    If hostile/malformed clients are in scope, cap an already-open data block at
    `1.0 s` or a finite byte/line budget and terminate the malformed session.
    There is no way to reinterpret a STOP line as control while the parser is
    legitimately inside an SSIP data block without changing protocol framing.
12. **Locks (F-24 through F-26):** keep the short regions as they are. Do not add
    lock timeouts as a substitute for bounding the only external calls under lock.
    Once protocol output is capped at 0.25 s, STOP/PAUSE acquisition of the engine
    lock inherits that finite bound; the current acyclic order should remain.

For PAUSE, all `1.25 s` cancellation/shutdown budgets begin when the real index mark
or fallback chunk boundary is reached. Guaranteeing receipt-to-`704` in about two
seconds requires ensuring the next honest boundary itself is always available in
roughly `0.5-0.75 s`, for example through smaller chunks or completed lookahead; a
mere deadline cannot manufacture a truthful index mark.

## Explicitly not a problem

- `desktop/module.py:104` (`check_ffmpeg`'s `subprocess.run`) can be unbounded, but
  it runs during INIT before any SPEAK can be accepted, so it is outside the stated
  post-acceptance scope.
- The first utterance's `_ensure_catalog()` at `desktop/module.py:201` occurs before
  `200 OK SPEAKING`; it delays acceptance, not cancellation of an accepted
  utterance. The same method becomes in-scope only when LIST VOICES or another
  SPEAK is handled while an older generation remains alive, as covered in F-12/F-13.
- `desktop/module.py:228` can block writing `200 OK SPEAKING`, but the utterance is
  not accepted from the client's perspective until that line is delivered. Later
  stdout writes remain in scope and are unbounded.
- `desktop/protocol.py:79` at the top-level command wait is supposed to block. A
  correctly framed arriving STOP/PAUSE satisfies that read immediately; only the
  partial-data-block path in F-23 is problematic.
- `desktop/module.py:441` (`pending.cancel()`) does not wait for running work. Its
  failure to stop running work matters at executor exit (F-8), but `cancel()`
  itself is not the blocking call.
- `Thread.is_alive()`, Event `set()/clear()/is_set()`, reference assignments, and
  fixed-length request-ID set operations do not contain blocking I/O. Their locks
  are accounted for in F-24/F-25.
- `desktop/audio.py:43` `terminate()` and `desktop/audio.py:50` `kill()` only send
  signals; the explicit waits at `desktop/audio.py:45` and `desktop/audio.py:52`
  are the blocking process operations and are covered in F-34.
- Audio framing CPU work at `desktop/protocol.py:121`, `desktop/protocol.py:122`,
  and `desktop/protocol.py:134` is bounded to one 10,000-byte module frame, and the
  engine releases its lock between frames. It does not create an additional
  configured wait. Its enclosing output call is still unbounded.
- `_POLL_INTERVAL=0.05` is a reasonable STOP observation interval by itself. The
  defect is the absent total decoder deadline and the final no-timeout join, not
  the poll value.
- `_WORKER_RECLAIM_SECONDS` and `wait_idle` joins are performed outside the engine
  lock, so they do not form a join-while-holding-lock deadlock.
- `idle_timeout=300` only controls backend self-shutdown after inactivity. It is
  not a request, worker, STOP, PAUSE, or decoder timeout.
- The two supplied known defects remain real but were not counted as discoveries:
  internal error exits do not sustain DELETE-404 cancellation intent, and recovery
  readiness/voice fetching is cancellation-unaware. F-15/F-16 record their timing
  only because the requested inventory includes every reachable blocking call.
