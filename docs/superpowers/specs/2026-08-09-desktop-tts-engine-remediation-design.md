# Desktop TTS Engine Residual Remediation Design

## Purpose

The original desktop TTS implementation run ended legally in `FINAL_BLOCKED`
after its single final-fix wave. That wave fixed findings F-2 through F-10, but a
fresh Frontier re-review found six load-bearing residuals, F-11 through F-16.
This remediation closes those residuals in a new deterministic run without
reopening or mutating the terminal run.

The user-visible goal is unchanged: Okular and other Speech Dispatcher clients
must be able to speak, stop, and pause reliably through free-tts, including after
backend failures and with malformed user configuration handled as protocol errors
rather than process crashes.

## Scope

This remediation covers only:

1. End-to-end cancellation registration and per-generation request ownership
   (F-11 and F-12).
2. PAUSE fallback for speech without usable index marks (F-13).
3. Duplicate live request-ID safety in the Flask cancellation registry (F-14).
4. Ownership and termination of ffmpeg decoder processes (F-15).
5. Backend URL validation and protocol-safe error mapping (F-16).

The fixes from commit `e0bf71c` remain the baseline. The four parked Minor
findings from the original run remain out of this remediation unless a required
change naturally supersedes one. No unrelated refactor, frontend work, or new
runtime dependency is in scope.

## Global Constraints

- `desktop/` remains Python-standard-library-only and must not import `server.py`
  or Flask.
- Standard output remains exclusively Speech Dispatcher protocol traffic.
- `tests/test_extension_split_sentences.py` and `tests/test_media_session.py` are
  not modified.
- Existing tests are not weakened, skipped, deleted, or made timing-dependent.
- Every concurrency regression uses deterministic barriers rather than sleeps as
  proof of ordering.
- A fresh deterministic SDD run starts from the current feature branch and its
  final Frontier review covers the original merge base through the new HEAD.

## Architecture

### 1. Cancellation Identity And Server Registration

The server cancellation registry owns live request IDs. Registering an ID that is
already live is rejected with HTTP 409 instead of overwriting the existing token.
Releasing a request removes the registry entry only when the stored token is the
same token being released. Completed IDs may be reused because no live ownership
remains.

The adapter uses a fresh request ID whenever it retries synthesis after an
ambiguous delivery or backend-recovery failure. It never overlaps two POSTs under
one ID.

STOP has an unavoidable transport interval between the adapter's final
`should_abort` check and server-side POST registration. Cancellation delivery
therefore treats DELETE 404 as "not registered yet," not final failure. While the
cancelled generation still owns the request, the adapter retries DELETE for a
tightly bounded one-second window with short interruptible intervals. STOP itself
continues to return promptly; cancellation delivery runs independently, while the
speech worker waits only for the bounded request cancellation or normal request
timeout.

This gives the cancellation lifecycle three explicit states: locally owned,
server-registered, and released. A request can move forward through those states,
and a stale generation cannot affect another generation's state.

### 2. Per-Generation Request Ownership

Each `_GenerationToken` owns its own active request-ID set. Registration,
unregistration, and cancellation operate on that token rather than a global
engine-wide set. A worker's `finally` block cancels only IDs owned by that worker's
generation.

Starting a new SPEAK first joins the prior worker for the existing bounded join
period. If the old worker is still alive, the engine refuses the new message with
`301 ERR CANT SPEAK` and retains the old worker reference; it does not discard the
reference or begin overlapping generations. Once the old worker exits, a later
SPEAK may proceed normally.

The design keeps stale-emission suppression from F-2: generation invalidation and
protocol writes remain serialized under the same lock, so STOP cannot be followed
by audio, marks, or a conflicting terminal event.

### 3. PAUSE Without Index Marks

PAUSE still prefers a usable Speech Dispatcher index mark. If a future marked
chunk exists, the worker finishes through that mark, reports it, then emits exactly
one `704 PAUSE`.

If no usable mark remains, the next completed internal chunk is the fallback pause
boundary. The worker emits `704 PAUSE` after that chunk and does not report a
fabricated index mark. This applies to entirely unmarked speech and to chunks
created only by `max_chunk_chars` hard splitting. Resume remains a new SPEAK from
the client's last known position; the adapter keeps no hidden resume buffer.

The worker precomputes whether a marked boundary remains after each chunk, making
the choice explicit and avoiding special cases based only on `chunk.mark`.

### 4. Owned Cancellable Decoder Processes

`decode_mp3` owns an ffmpeg `subprocess.Popen` object rather than delegating to
uncancellable `subprocess.run`. It accepts an optional generation cancellation
event. The decoder communicates with ffmpeg in bounded polling intervals:

- normal completion returns native-endian 24 kHz mono s16 PCM as before;
- cancellation sends `terminate`, waits briefly, escalates to `kill` if needed,
  waits again, and always reaps the process;
- missing ffmpeg, nonzero exit, empty output, and malformed output remain
  `DecodeError` paths;
- cancellation raises a distinct decoder-cancelled exception consumed by the
  speech worker as the generation's STOP/PAUSE outcome.

The speech worker performs decoding directly with the generation event. It does
not detach a daemon decoder thread. When STOP, EOF, or broken-pipe shutdown finishes,
no decoder thread or ffmpeg child remains alive.

Test injection uses a small decoder-process abstraction or injected `Popen`
factory, so terminate, kill, wait, and reap behavior is deterministic and does not
depend on a real hung ffmpeg process.

### 5. Backend URL Validation And Error Mapping

`backend_url` is validated with `urllib.parse.urlsplit` during configuration
resolution. Accepted values use `http` or `https`, contain a hostname and valid
port, have no credentials, query, or fragment, and use either an empty path or `/`.
The stored value has no trailing slash.

Invalid configured URLs raise a dedicated configuration error. The module catches
that error during INIT and returns a `399 ERR CANT INIT MODULE` diagnostic on the
protocol channel instead of crashing.

`BackendController.probe` also treats URL-construction, parsing, and response
decoding exceptions as an unavailable backend with an actionable detail. This
defensive boundary protects direct construction in tests or future callers that
bypass `load_config`. LIST VOICES and SPEAK consequently return `304` and `301`
respectively for malformed backend addresses, never an uncaught exception.

## Error Handling And Compatibility

- Duplicate live request IDs receive HTTP 409; ordinary malformed IDs retain the
  existing HTTP 400 behavior.
- DELETE 404 remains the response for a truly unknown request. Only the adapter's
  bounded cancellation handoff interprets it as potentially not-yet-registered.
- A timed-out prior worker causes a new SPEAK refusal, not concurrent generations.
- PAUSE emits one and only one terminal PAUSE event at the selected boundary.
- Decoder cancellation is resource cleanup, not a decode failure shown to users;
  normal decoder failures retain the existing STOP behavior and diagnostics.
- Invalid backend URLs fail predictably at INIT or at the LIST/SPEAK boundary,
  depending on where the invalid configuration enters the system.
- Existing health revalidation, owned-backend restart, installer transaction, and
  single-process server contracts remain unchanged.

## Testing Strategy

### Task 1: Server request-ID ownership (F-14)

- Concurrent duplicate registration returns 409 without replacing the first
  token.
- Releasing an older token cannot remove a newer registry entry.
- Completed IDs can be reused only after identity-safe release.
- Existing malformed-ID, DELETE, mid-await cancellation, and semaphore-release
  tests continue to pass.

### Task 2: Cancellation handoff and generation isolation (F-11/F-12)

- A barrier forces DELETE to arrive before POST registration; bounded retries
  eventually cancel the registered request and release the worker promptly.
- A stale worker cannot cancel a newer generation's request.
- A new SPEAK is refused while an old worker remains alive after the join timeout.
- Recovery POSTs use fresh request IDs.
- Active STOP emits exactly one STOP event and no later audio or marks.

### Task 3: PAUSE fallback (F-13)

- Unmarked speech pauses at the next internal chunk boundary.
- Hard-split speech without marks pauses after the current split chunk.
- Speech with a future mark still pauses at that mark and reports it first.
- Unit and subprocess transcripts assert exactly one PAUSE event and no END event.

### Task 4: Decoder ownership (F-15)

- Cancellation terminates, escalates when necessary, waits, and reaps ffmpeg.
- No decoder thread/process remains after STOP, EOF, or broken-pipe cleanup.
- Successful decode command shape, PCM format, odd-byte trimming, and gain tests
  remain unchanged.
- A real ffmpeg integration test still runs when ffmpeg is available.

### Task 5: Backend URL validation (F-16)

- Empty, relative, credential-bearing, query-bearing, fragment-bearing,
  unsupported-scheme, missing-host, and invalid-port values are rejected.
- Valid localhost HTTP and HTTPS URLs normalize correctly.
- INIT reports 399 for invalid loaded configuration.
- Direct malformed configs produce 304 for LIST VOICES and 301 for SPEAK without
  crashing the command loop.

Every task runs focused tests and the full repository suite. The new run's final
Frontier reviewer reconciles F-11 through F-16 as absent and rechecks all earlier
fixed and parked findings across the original branch range.

## Delivery

The remediation is implemented by a fresh deterministic SDD run with five tasks
and an independent review gate after each task. The original run remains untouched
in `FINAL_BLOCKED`. The feature branch is not merged until the remediation run
reaches `COMPLETE`, the full suite passes from a clean worktree, and the final
Frontier report records `SPEC: PASS` and `QUALITY: APPROVED`.
