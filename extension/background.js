// free-tts background service worker
// Sentence-by-sentence TTS with pre-caching and highlight bar.

const DEFAULT_SERVER = "http://localhost:5000";
const PRELOAD_AHEAD = 2;  // pre-fetch this many sentences ahead
const FETCH_TIMEOUT_MS = 120000;

// Pipeline state. Revisions are scoped to this worker; opaque IDs are identities.
const workerId = crypto.randomUUID();
let lifecycleRevision = 0;
let viewRevision = 0;
let lastError = null;
let startRequestSerial = 0;
let pendingStart = null;
const pageLeases = new Map();
let activePlayback = { tabId: null };
let sentencePipeline = null;

function ownsPipeline(pipeline, attempt = null) {
  return !!pipeline && sentencePipeline === pipeline && !pipeline.cancelled &&
    activePlayback.tabId === pipeline.tabId && activePlayback.leaseId === pipeline.leaseId &&
    activePlayback.sessionId === pipeline.sessionId && activePlayback.lifecycleRevision === pipeline.lifecycleRevision &&
    (!attempt || (pipeline.attempt === attempt && !attempt.retired && pipeline.currentIdx === attempt.idx));
}

function ownsAttempt(pipeline, attempt) {
  return !!attempt && ownsPipeline(pipeline, attempt);
}

function buildPlaybackSnapshot(pipeline = sentencePipeline) {
  if (pipeline?.cancelled) pipeline = null;
  const attempt = pipeline?.attempt;
  return {
    workerId, state: pipeline ? (pipeline.isPaused ? "paused" : "playing") : "idle",
    sessionId: pipeline?.sessionId ?? null, viewRevision,
    intentRevision: pipeline?.intentRevision ?? null, mediaRevision: pipeline?.mediaRevision ?? null,
    phase: attempt?.phase ?? "idle", audioStatus: attempt?.audioStatus ?? "none", error: lastError,
  };
}

function failOwnedPlayback(pipeline, code, error, attempt = null) {
  if (!ownsPipeline(pipeline, attempt)) return;
  pipeline.failure = { code, message: error.message || String(error) };
  lastError = pipeline.failure;
  logError(code, error);
  return stopPlayback(pipeline, false);
}

function commandResponse(accepted, receipt = null, commandError = null) {
  return { ...buildPlaybackSnapshot(), ok: !commandError, accepted,
    disposition: commandError ? "failed" : !accepted ? "ignored" : receipt?.disposition === "stale" || receipt?.disposition === "superseded-by-media" ? "superseded" : "applied",
    pageApplied: !!receipt && ["applied", "idempotent"].includes(receipt.disposition), commandError };
}

function workerIntent(pipeline) {
  return { leaseId: pipeline.leaseId, sessionId: pipeline.sessionId, lifecycleRevision: pipeline.lifecycleRevision,
    attemptId: pipeline.attempt?.attemptId ?? null, intentRevision: pipeline.intentRevision,
    mediaBase: pipeline.mediaRevision, paused: pipeline.isPaused };
}

function pageOwner(pipeline) {
  return { tabId: pipeline.tabId, leaseId: pipeline.leaseId, sessionId: pipeline.sessionId, lifecycleRevision: pipeline.lifecycleRevision };
}

function startTicket() {
  if (pendingStart) pendingStart.cancelled = true;
  return pendingStart = { requestId: crypto.randomUUID(), requestSerial: ++startRequestSerial, cancelled: false };
}

function playbackModes(pipeline) {
  return { loopEnabled: pipeline.loopEnabled, repeatCurrentEnabled: pipeline.repeatCurrentEnabled };
}

function setAttemptPhase(pipeline, attempt, phase) {
  if (!ownsPipeline(pipeline, attempt) || (attempt.completionObserved && phase !== "ended")) return;
  if (attempt.phase !== phase) { attempt.phase = phase; viewRevision++; }
}

function logError(context, error) {
  console.error(`free-tts background: ${context}`, error);
}

function normalizeServerUrl(value) {
  try {
    const url = new URL(value || DEFAULT_SERVER);
    if (!["http:", "https:"].includes(url.protocol)) return DEFAULT_SERVER;
    if (!["localhost", "127.0.0.1", "::1"].includes(url.hostname)) return DEFAULT_SERVER;
    url.pathname = url.pathname.replace(/\/+$/, "");
    url.search = "";
    url.hash = "";
    return url.toString().replace(/\/$/, "");
  } catch {
    console.warn("free-tts: invalid server URL, falling back to default:", value);
    return DEFAULT_SERVER;
  }
}

function normalizeColor(value) {
  return /^#[0-9a-fA-F]{6}$/.test(value) ? value : "#fff3cd";
}

function normalizeSpeed(value) {
  const speed = Number.parseInt(value, 10);
  if (!Number.isFinite(speed)) return 0;
  return Math.min(200, Math.max(-50, speed));
}

function sendAsyncResponse(sendResponse, promise) {
  promise
    .then((result) => sendResponse({ ...result, ...buildPlaybackSnapshot() }))
    .catch((error) => {
      logError("handling runtime message", error);
      sendResponse(commandResponse(false, null, { code: "page-operation-failure", message: error.message || "Operation failed" }));
    });
  return true;
}

function clearPlayback() {
  activePlayback = { tabId: null };
  sentencePipeline = null;
}

function clearOwnedPlayback(pipeline) {
  if (sentencePipeline === pipeline) { clearPlayback(); viewRevision++; }
}

// --- Messages from popup, bound bar, and isolated media relay ----------------
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (["speakSentences", "getPlaybackState", "pausePlayback", "resumePlayback", "togglePlayback", "stopPlayback",
    "mediaPause", "mediaResume", "audioPlaybackEvent", "prevSentence", "nextSentence", "jumpToSentence",
    "setLoopState", "setRepeatCurrentState"].includes(msg.action)) {
    return sendAsyncResponse(sendResponse, acceptRuntimeCommand(msg, sender));
  }
});

function resolveCommandPipeline(msg, sender) {
  const pipeline = sentencePipeline;
  if (!ownsPipeline(pipeline)) return null;
  if (!sender.tab && !["bar", "media"].includes(msg.source)) return pipeline;
  return ["bar", "media"].includes(msg.source) && sender.tab?.id === pipeline.tabId &&
    msg.leaseId === pipeline.leaseId && msg.sessionId === pipeline.sessionId && msg.lifecycleRevision === pipeline.lifecycleRevision
    ? pipeline : null;
}

function admitMediaAction(pipeline, msg) {
  if (!pipeline || !Number.isSafeInteger(msg.intentRevision) || msg.intentRevision < pipeline.localIntentFloor ||
      msg.intentRevision > pipeline.intentRevision || !Number.isSafeInteger(msg.mediaSequence) ||
      msg.mediaSequence <= pipeline.lastMediaSequence || !Number.isSafeInteger(msg.mediaRevision) || msg.mediaRevision < 0 ||
      typeof msg.paused !== "boolean") return false;
  pipeline.lastMediaSequence = msg.mediaSequence;
  return true;
}

chrome.tabs.onRemoved.addListener((tabId) => {
  const lease = pageLeases.get(tabId);
  if (lease) lease.closed = true;
  pageLeases.delete(tabId);
  if (sentencePipeline?.tabId === tabId) void stopPlayback(sentencePipeline, false);
});

// --- Keyboard shortcut -----------------------------------------------------
chrome.commands.onCommand.addListener(async (command) => {
  if (command === "speak-selection") {
    // Toggle: stop playback if active, otherwise speak selection
    if (ownsPipeline(sentencePipeline) || pendingStart) {
      await stopPlayback();
      return;
    }
    const ticket = startTicket();
    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab?.id || ticket.cancelled || pendingStart !== ticket) return;
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => window.getSelection()?.toString() || document.body.innerText || "",
      });
      const text = results?.[0]?.result?.trim();
      if (text && !ticket.cancelled && pendingStart === ticket) await startSentenceTTS(tab.id, text, ticket);
    } catch (error) {
      // scripting permission may not be available on all URLs
      if (!ticket.cancelled && pendingStart === ticket) logError("handling keyboard shortcut", error);
    } finally {
      if (pendingStart === ticket) pendingStart = null;
    }
    return;
  }
});

// --- Stop playback ---------------------------------------------------------
async function stopPlayback(pipeline = sentencePipeline, cancelStarts = true) {
  if (cancelStarts && pendingStart) { pendingStart.cancelled = true; pendingStart = null; }
  if (!pipeline || sentencePipeline !== pipeline) return commandResponse(false);
  if (pipeline.retirementPromise) return pipeline.retirementPromise;
  if (!ownsPipeline(pipeline)) return commandResponse(false);
  // Cancellation and the retirement floor precede every page/cleanup await.
  // Replacement allocates its successor lifecycle only after this step.
  const owner = pageOwner(pipeline), retirementRevision = ++lifecycleRevision;
  pipeline.cancelled = true;
  const attempt = pipeline.attempt;
  if (attempt) {
    attempt.retired = true;
    clearTimeout(attempt.pollTimer); attempt.pollTimer = null;
    clearTimeout(attempt.cacheTimer); attempt.cacheTimer = null;
    attempt.cacheWake?.(); attempt.cacheWake = null;
    attempt.observationNeeded = false;
    attempt.pollInFlight = false;
  }
  viewRevision++;
  pipeline.retirementPromise = (async () => {
    const failures = [];
    let retirementReceipt = null;
    // Each cleanup is independently attempted, using the captured owner even
    // when a successor publishes during an earlier injection.
    for (const cleanup of [cleanupMediaSession, cleanupPageHighlighting, hideControlBar]) {
      try {
        const receipt = await cleanup(owner, retirementRevision);
        if (cleanup === cleanupMediaSession) {
          retirementReceipt = receipt;
          if (receipt.errors.length) throw new Error(receipt.errors.join("; "));
        }
      } catch (error) { failures.push(error); logError("retiring playback in page", error); }
    }
    clearOwnedPlayback(pipeline);
    return commandResponse(true, retirementReceipt, failures.length
      ? { code: "cleanup-failure", message: failures.map(error => error.message).join("; ") } : null);
  })();
  return pipeline.retirementPromise;
}

// --- Pause / Resume ---------------------------------------------------------
async function acceptRuntimeCommand(msg, sender) {
  if (msg.action === "getPlaybackState") return buildPlaybackSnapshot();
  if (msg.action === "speakSentences") {
    if (typeof msg.text !== "string" || !msg.text.trim()) return commandResponse(false, null, { code: "invalid-argument", message: "No text" });
    const ticket = startTicket();
    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (ticket.cancelled || pendingStart !== ticket) return commandResponse(false);
      if (!tab?.id) return commandResponse(false, null, { code: "invalid-argument", message: "No active tab" });
      return await startSentenceTTS(tab.id, msg.text, ticket);
    } catch (error) {
      if (ticket.cancelled || (pendingStart && pendingStart !== ticket)) return commandResponse(false);
      return commandResponse(false, null, { code: "page-operation-failure", message: error.message || String(error) });
    } finally {
      if (pendingStart === ticket) pendingStart = null;
    }
  }
  const pipeline = resolveCommandPipeline(msg, sender);
  const bound = !!sender.tab || ["bar", "media"].includes(msg.source);
  if (msg.action === "stopPlayback" && !bound) return stopPlayback(sentencePipeline, true);
  if (!pipeline) return commandResponse(false);
  if (msg.action === "audioPlaybackEvent") return msg.source === "media" ? handleAudioOutcome(pipeline, msg) : commandResponse(false);
  if (msg.source === "media" && !admitMediaAction(pipeline, msg)) return commandResponse(false);
  if (["mediaPause", "mediaResume"].includes(msg.action)) {
    return msg.source === "media" ? syncPausedState(pipeline, msg) : commandResponse(false);
  }
  if (msg.action === "stopPlayback") return stopPlayback(pipeline, false);
  const owner = pageOwner(pipeline);
  const media = msg.source === "media" ? msg : null;
  if (msg.action === "prevSentence") return prevSentence(owner, media);
  if (msg.action === "nextSentence") return nextSentence(owner, media);
  if (msg.action === "jumpToSentence") return jumpToSentence(owner, msg.idx);
  if (["setLoopState", "setRepeatCurrentState"].includes(msg.action)) {
    if (typeof msg.enabled !== "boolean") return commandResponse(false, null, { code: "invalid-argument", message: "Mode must be a boolean" });
    const key = msg.action === "setLoopState" ? "loopEnabled" : "repeatCurrentEnabled";
    const other = key === "loopEnabled" ? "repeatCurrentEnabled" : "loopEnabled";
    if (pipeline[key] !== msg.enabled || (msg.enabled && pipeline[other])) {
      pipeline[key] = msg.enabled;
      if (msg.enabled) pipeline[other] = false;
      viewRevision++;
    }
    await updateControlBar(pipeline, buildPlaybackSnapshot(pipeline), playbackModes(pipeline));
    return commandResponse(true, null, pipeline.failure || null);
  }
  return msg.action === "pausePlayback" || (msg.action === "togglePlayback" && !pipeline.isPaused) ? pausePlayback(pipeline) : resumePlayback(pipeline);
}

async function pausePlayback(pipeline = sentencePipeline) {
  return changePausedState(pipeline, true);
}

async function resumePlayback(pipeline = sentencePipeline) {
  return changePausedState(pipeline, false);
}

async function changePausedState(pipeline, paused) {
  if (!ownsPipeline(pipeline)) return commandResponse(false);
  if (pipeline.isPaused !== paused) {
    pipeline.isPaused = paused;
    pipeline.localIntentFloor = ++pipeline.intentRevision;
    viewRevision++;
  }

  let attempt = pipeline.attempt;
  let replayCompletedAttempt = false;
  if (!paused && attempt.completionObservedWhilePaused && !attempt.completionConsumed) {
    attempt.completionConsumed = true;
    attempt.retired = true;
    clearTimeout(attempt.pollTimer);
    clearTimeout(attempt.cacheTimer);
    attempt.cacheWake?.();
    pipeline.currentIdx = attempt.idx;
    attempt = createAttempt(pipeline, "loading");
    replayCompletedAttempt = true;
    viewRevision++;
  }

  const acceptedSnapshot = buildPlaybackSnapshot(pipeline);
  const acceptedRender = updateControlBar(pipeline, acceptedSnapshot, playbackModes(pipeline));
  if (replayCompletedAttempt) {
    await selectAttempt(pipeline, attempt);
    if (!ownsPipeline(pipeline, attempt)) return commandResponse(false);
  }
  const receipt = await applyWorkerIntent(pipeline, workerIntent(pipeline));
  let currentRender = Promise.resolve();
  if (ownsPipeline(pipeline, attempt)) {
    if (!pipeline.isPaused) void playNextSentence(pipeline, attempt);
    const currentSnapshot = buildPlaybackSnapshot(pipeline);
    if (currentSnapshot.viewRevision !== acceptedSnapshot.viewRevision || currentSnapshot.state !== acceptedSnapshot.state) {
      currentRender = updateControlBar(pipeline, currentSnapshot, playbackModes(pipeline));
    }
  }
  await Promise.all([acceptedRender, currentRender]);
  return commandResponse(true, receipt || { disposition: "stale" }, pipeline.failure || null);
}

function applyWorkerIntent(pipeline, capability) {
  const previous = pipeline.intentApplication;
  if (previous && previous.capability.intentRevision === capability.intentRevision && previous.capability.mediaBase === capability.mediaBase &&
      previous.capability.attemptId === capability.attemptId && previous.capability.paused === capability.paused) return previous.promise;
  // Retain only the latest application; redundant commands share its boundary.
  const application = pipeline.intentApplication = { capability, receipt: null, promise: null };
  const attempt = pipeline.attempt;
  application.promise = (async () => {
    await pipeline.installPromise;
    if (!ownsPipeline(pipeline, attempt)) return null;
    const [result] = await chrome.scripting.executeScript({ target: { tabId: pipeline.tabId }, world: "MAIN", func: injectWorkerIntent, args: [capability] });
    application.receipt = result?.result;
    if (ownsPipeline(pipeline, attempt)) {
      if (!validPageReceipt(pipeline, application.receipt) && pipeline.intentRevision === capability.intentRevision) {
        throw new Error("MAIN intent returned no valid owned receipt");
      }
      adoptPageReceipt(pipeline, attempt, application.receipt);
      if (attempt.audioId) scheduleCompletionPoll(pipeline, attempt);
      if (pipeline.intentApplication === application) requestActivation(pipeline, attempt);
    }
    return application.receipt;
  })().catch(error => {
    if (pipeline.intentApplication === application && pipeline.intentRevision === capability.intentRevision) {
      failOwnedPlayback(pipeline, "page-operation-failure", error, attempt);
    }
    return null;
  });
  return application.promise;
}

function validPageReceipt(pipeline, receipt) {
  return !!receipt && receipt.leaseId === pipeline.leaseId && receipt.sessionId === pipeline.sessionId &&
    receipt.lifecycleRevision === pipeline.lifecycleRevision && Number.isSafeInteger(receipt.intentRevision) &&
    Number.isSafeInteger(receipt.mediaRevision) && receipt.mediaRevision >= 0 && typeof receipt.paused === "boolean" &&
    Number.isSafeInteger(receipt.audioRevision) && receipt.audioRevision >= 0 &&
    ["none", "starting", "playing", "paused", "ended", "error"].includes(receipt.audioStatus) &&
    (receipt.audioId === null || (typeof receipt.audioId === "string" && Number.isSafeInteger(receipt.startSerial)));
}

function adoptPageReceipt(pipeline, attempt, receipt) {
  if (!ownsPipeline(pipeline, attempt) || !validPageReceipt(pipeline, receipt) ||
      receipt.intentRevision !== pipeline.intentRevision || receipt.mediaRevision < pipeline.mediaRevision) return;
  const learnedMedia = receipt.mediaRevision > pipeline.mediaRevision;
  if (pipeline.isPaused !== receipt.paused || learnedMedia) {
    pipeline.isPaused = receipt.paused;
    pipeline.mediaRevision = receipt.mediaRevision;
    viewRevision++;
  }
  if (receipt.attemptId !== attempt.attemptId) {
    if (learnedMedia) requestPageObservation(pipeline, attempt);
    return;
  }
  attempt.pageReceipt = receipt;
  let learnedInvocation = false;
  if (receipt.audioRevision > attempt.audioRevision && (!attempt.audioId || receipt.audioId === attempt.audioId) &&
      (attempt.startSerial === null || receipt.startSerial >= attempt.startSerial)) {
    learnedInvocation = !!receipt.audioId && (!attempt.audioId || receipt.startSerial > attempt.startSerial);
    attempt.audioId = receipt.audioId;
    attempt.audioRevision = receipt.audioRevision;
    attempt.startSerial = receipt.startSerial;
    attempt.audioStatus = receipt.audioStatus;
    if (receipt.audioId && ["starting", "playing", "paused"].includes(receipt.audioStatus)) setAttemptPhase(pipeline, attempt, receipt.audioStatus);
    viewRevision++;
    if (receipt.audioStatus === "error") {
      failOwnedPlayback(pipeline, "play-rejection", new Error(receipt.audioError?.message || "Audio play rejected"), attempt);
      return;
    }
  }
  if (learnedMedia || learnedInvocation) requestPageObservation(pipeline, attempt);
}

// One no-effect read and one follow-up bit per live attempt. Reads never admit
// media actions and cannot grant a stale command permission to play again.
function requestPageObservation(pipeline, attempt) {
  if (!ownsPipeline(pipeline, attempt)) return;
  if (attempt.observationPromise) { attempt.observationNeeded = true; return; }
  let intentRevision = pipeline.intentRevision;
  const observation = (async () => {
    await pipeline.installPromise;
    // A worker attempt can be published before its MAIN selection executes.
    // Only sample once that selection has an owned receipt; the preceding
    // attempt's Audio is not evidence that the new attempt is missing Audio.
    await selectAttempt(pipeline, attempt);
    do {
      if (!ownsPipeline(pipeline, attempt)) return;
      attempt.observationNeeded = false;
      intentRevision = pipeline.intentRevision;
      const [result] = await chrome.scripting.executeScript({ target: { tabId: pipeline.tabId }, world: "MAIN",
        func: injectPlaybackObservation, args: [pageOwner(pipeline), attempt.attemptId] });
      if (!ownsPipeline(pipeline, attempt)) return;
      const receipt = result?.result;
      if (intentRevision !== pipeline.intentRevision) continue;
      if (!validPageReceipt(pipeline, receipt)) throw new Error("MAIN observation returned no valid owned receipt");
      if (!receipt.owned || (attempt.audioId && !receipt.audioId)) {
        if (receipt.intentRevision === pipeline.intentRevision && receipt.attemptId === attempt.attemptId) {
          failOwnedPlayback(pipeline, "missing-audio", new Error("Owned Audio is missing"), attempt);
        }
        return;
      }
      adoptPageReceipt(pipeline, attempt, receipt);
      if (ownsPipeline(pipeline, attempt)) await updateControlBar(pipeline, buildPlaybackSnapshot(pipeline), playbackModes(pipeline));
    } while (attempt.observationNeeded);
  })().catch(error => {
    if (intentRevision === pipeline.intentRevision) failOwnedPlayback(pipeline, "page-operation-failure", error, attempt);
  }).finally(() => {
    if (attempt.observationPromise !== observation) return;
    attempt.observationPromise = null;
    if (!ownsPipeline(pipeline, attempt)) return;
    if (attempt.observationNeeded) requestPageObservation(pipeline, attempt);
    else continueObservedPlayback(pipeline, attempt);
  });
  attempt.observationPromise = observation;
}

function injectPlaybackObservation(owner, attemptId) {
  const record = window.__freeTtsPlayback;
  if (!record?.receipt || record.retired || record.leaseId !== owner.leaseId || record.sessionId !== owner.sessionId ||
      record.lifecycleRevision !== owner.lifecycleRevision || record.attemptId !== attemptId) return null;
  return { ...record.receipt("applied"), owned: !record.audioId || !!record.audio, ended: !!record.audio?.ended };
}

function continueObservedPlayback(pipeline, attempt) {
  if (!ownsPipeline(pipeline, attempt) || pipeline.isPaused || attempt.observationPromise || attempt.observationNeeded) return;
  if (attempt.completionObservedWhilePaused && !attempt.completionConsumed) {
    attempt.completionConsumed = true;
    const replay = createAttempt(pipeline, pipeline.initialized ? "loading" : "initializing");
    pipeline.intentRevision++; viewRevision++;
    void selectAttempt(pipeline, replay);
    void applyWorkerIntent(pipeline, workerIntent(pipeline));
    void playNextSentence(pipeline, replay);
    return;
  }
  if (attempt.audioId) scheduleCompletionPoll(pipeline, attempt);
  void playNextSentence(pipeline, attempt);
  requestActivation(pipeline, attempt);
}

async function handleAudioOutcome(pipeline, message) {
  const attempt = pipeline.attempt;
  if (!validPageReceipt(pipeline, message) || message.attemptId !== attempt.attemptId ||
      (attempt.audioId && message.audioId !== attempt.audioId) || message.mediaRevision < pipeline.mediaRevision ||
      message.audioRevision <= attempt.audioRevision || (attempt.startSerial !== null && message.startSerial < attempt.startSerial)) {
    return commandResponse(false);
  }
  if (message.intentRevision === pipeline.intentRevision && message.mediaRevision === pipeline.mediaRevision) {
    adoptPageReceipt(pipeline, attempt, message);
  } else requestPageObservation(pipeline, attempt);
  if (ownsPipeline(pipeline, attempt)) await updateControlBar(pipeline, buildPlaybackSnapshot(pipeline), playbackModes(pipeline));
  return commandResponse(true, null, pipeline.failure || null);
}

// Claims serialize only the short per-tab establishment, never data or play.
async function establishPageLease(pipeline) {
  const entry = pageLeases.get(pipeline.tabId);
  if (!entry || entry.closed || entry.leaseId !== pipeline.leaseId) return false;
  while (ownsPipeline(pipeline) && !entry.closed) {
    // A document navigation discards world records while retaining this tab.
    // Revalidate the lease for each new session before installing its owner.
    if (entry.establishedSessionId === pipeline.sessionId) return true;
    if (!entry.promise) {
      const promise = claimPageLease(pipeline, entry);
      entry.promise = promise;
      entry.claimant = pipeline;
      // Compare the shared promise before releasing its slot. A retired
      // claimant's inert partial claim can then be completed by the live owner.
      void promise.finally(() => {
        if (entry.promise === promise) { entry.promise = null; entry.claimant = null; }
      }).catch(() => {});
    }
    const claimant = entry.claimant;
    try { await entry.promise; }
    catch (error) {
      if (!ownsPipeline(pipeline) || entry.closed) return false;
      if (claimant === pipeline) throw error;
      // A live successor sharing a retired claim retries its own bounded
      // establishment; it never inherits the retired claimant's fatal error.
    }
    if (!ownsPipeline(pipeline)) return false;
  }
  return false;
}

async function claimPageLease(pipeline, entry) {
  const { tabId, leaseId } = pipeline;
  for (let retry = 0; retry < 3; retry++) {
    if (!ownsPipeline(pipeline) || entry.closed) return false;
    const observed = {};
    for (const world of ["MAIN", "ISOLATED"]) {
      const [result] = await chrome.scripting.executeScript({ target: { tabId }, world,
        func: injectPageLease, args: [world, "read"] });
      if (!ownsPipeline(pipeline) || entry.closed) return false;
      observed[world] = result?.result;
      if (!observed[world]) throw new Error("Page lease read returned no receipt");
    }
    let claimed = true;
    for (const world of ["MAIN", "ISOLATED"]) {
      if (!ownsPipeline(pipeline) || entry.closed) return false;
      const [result] = await chrome.scripting.executeScript({ target: { tabId }, world,
        func: injectPageLease, args: [world, "claim", observed[world], leaseId, workerId] });
      if (!ownsPipeline(pipeline) || entry.closed) return false;
      if (!result?.result?.claimed) { claimed = false; break; }
    }
    if (claimed) { entry.establishedSessionId = pipeline.sessionId; return true; }
  }
  throw new Error("Page lease claim contention");
}

// One explicit CAS per world. Ordinary install/effect/cleanup functions never
// adopt a different lease. A same-token claim cannot reset an advanced floor.
function injectPageLease(world, operation, observed, leaseId, workerToken) {
  const key = world === "MAIN" ? "__freeTtsPlayback" : "__freeTtsView";
  const previous = window[key];
  const current = { leaseId: previous?.leaseId ?? null, lifecycleFloor: previous?.lifecycleFloor ?? 0 };
  if (operation === "read") return current;
  if (operation !== "claim" || current.leaseId !== observed.leaseId || current.lifecycleFloor !== observed.lifecycleFloor) return { ...current, claimed: false };
  if (current.leaseId === leaseId) return { ...current, claimed: true };
  if (world === "MAIN") {
    if (window.__freeTtsKeepalive) { clearInterval(window.__freeTtsKeepalive); window.__freeTtsKeepalive = null; }
    if ("mediaSession" in navigator) {
      for (const action of ["play", "pause", "previoustrack", "nexttrack", "stop"]) {
        try { navigator.mediaSession.setActionHandler(action, null); } catch {}
      }
      try { navigator.mediaSession.metadata = null; } catch {}
      try { navigator.mediaSession.playbackState = "none"; } catch {}
    }
    if (previous) { previous.retired = true; previous.desiredPaused = true; previous.startSerial++; }
    const audio = previous?.audio || window.__freeTtsAudio;
    try { audio?.pause(); } catch {}
    try { if (audio) audio.src = ""; } catch {}
    window.__freeTtsAudio = null;
  } else {
    if (previous) previous.retired = true;
    document.querySelectorAll(".free-tts-highlight-overlay").forEach(element => element.remove());
    delete window.__freeTtsSentences; delete window.__freeTtsColor;
    window.__freeTtsCleanupControlBar?.();
  }
  window[key] = { leaseId, workerId: workerToken, lifecycleFloor: 0, sessionId: null, lifecycleRevision: null, retired: true };
  return { leaseId, lifecycleFloor: 0, claimed: true };
}

// The short install barrier contains no storage, fetch, highlight, or play wait.
async function installPlayback(pipeline) {
  const owner = pageOwner(pipeline);
  if (!await establishPageLease(pipeline) || !ownsPipeline(pipeline)) return;
  const [main] = await chrome.scripting.executeScript({ target: { tabId: owner.tabId }, world: "MAIN", func: injectPlaybackSession, args: [owner, workerId] });
  if (!ownsPipeline(pipeline)) return;
  if (!main?.result?.installed) throw new Error("MAIN session installation rejected");
  const [view] = await chrome.scripting.executeScript({ target: { tabId: owner.tabId }, func: injectViewSession, args: [owner] });
  if (!ownsPipeline(pipeline)) return;
  if (!view?.result?.installed) throw new Error("Isolated session installation rejected");
}

function injectViewSession(owner) {
  const previous = window.__freeTtsView;
  if (!previous || previous.leaseId !== owner.leaseId || owner.lifecycleRevision < previous.lifecycleFloor) return { installed: false };
  if (owner.lifecycleRevision === previous.lifecycleFloor) return { installed: !previous.retired && previous.sessionId === owner.sessionId };
  previous.retired = true;
  document.querySelectorAll(".free-tts-highlight-overlay").forEach(element => element.remove());
  delete window.__freeTtsSentences; delete window.__freeTtsColor;
  window.__freeTtsCleanupControlBar?.();
  window.__freeTtsView = { ...owner, lifecycleFloor: owner.lifecycleRevision, retired: false, viewRevision: 0, barCreated: false, pendingRender: null };
  return { installed: true };
}

function injectPlaybackSession(owner, workerToken) {
  const previous = window.__freeTtsPlayback;
  if (!previous || previous.leaseId !== owner.leaseId || owner.lifecycleRevision < previous.lifecycleFloor) return { installed: false };
  if (owner.lifecycleRevision === previous.lifecycleFloor) return { installed: !previous.retired && previous.sessionId === owner.sessionId };
  previous.retired = true;
  if (window.__freeTtsKeepalive) { clearInterval(window.__freeTtsKeepalive); window.__freeTtsKeepalive = null; }
  if ("mediaSession" in navigator) {
    for (const action of ["play", "pause", "previoustrack", "nexttrack", "stop"]) {
      try { navigator.mediaSession.setActionHandler(action, null); } catch {}
    }
    try { navigator.mediaSession.metadata = null; } catch {}
    try { navigator.mediaSession.playbackState = "none"; } catch {}
  }
  try { previous.audio?.pause(); } catch {}
  try { if (previous.audio) previous.audio.src = ""; } catch {}
  const record = window.__freeTtsPlayback = { ...owner, workerId: workerToken, lifecycleFloor: owner.lifecycleRevision, retired: false,
    workerIntentRevision: 0, mediaRevision: 0, mediaSequence: 0, desiredPaused: true, attemptFloor: 0, attemptId: null,
    audio: null, audioId: null, audioStatus: "none", startSerial: 0, audioRevision: 0, audioError: null };
  window.__freeTtsAudio = null;
  record.receipt = (disposition) => ({ leaseId: record.leaseId, sessionId: record.sessionId, lifecycleRevision: record.lifecycleRevision,
    attemptId: record.attemptId, intentRevision: record.workerIntentRevision, mediaRevision: record.mediaRevision, paused: record.desiredPaused,
    mediaSequence: record.mediaSequence, audioError: record.audioError,
    audioId: record.audioId, audioStatus: record.audioStatus, startSerial: record.audioId ? record.startSerial : null, audioRevision: record.audioRevision, disposition });
  record.setMediaState = state => {
    try { if ("mediaSession" in navigator) navigator.mediaSession.playbackState = state; }
    catch (error) { console.warn("free-tts: Media Session state unavailable", error); }
  };
  record.pause = () => {
    record.startSerial++; record.audioError = null;
    if (record.audio) { record.audio.pause(); record.audioStatus = "paused"; record.audioRevision++; }
    record.setMediaState("paused");
  };
  record.play = () => {
    if (record !== window.__freeTtsPlayback || record.retired || record.desiredPaused || !record.audio || record.audio.ended || ["starting", "playing"].includes(record.audioStatus)) return;
    const audio = record.audio, serial = ++record.startSerial, attemptId = record.attemptId;
    const audioId = record.audioId;
    record.audioStatus = "starting"; record.audioError = null; record.audioRevision++;
    record.setMediaState("paused");
    // Newer running intents may retain this invocation. Reconcile its outcome
    // with the current running snapshot; an applied pause invalidates its serial.
    const live = () => record === window.__freeTtsPlayback && !record.retired && record.audio === audio && record.audioId === audioId &&
      record.attemptId === attemptId && record.startSerial === serial && !record.desiredPaused;
    const settled = (error) => {
      if (!live()) return;
      record.audioStatus = error ? "error" : "playing";
      record.audioError = error ? { code: "play-rejection", message: error.message || String(error) } : null;
      record.audioRevision++;
      if (!error) record.setMediaState("playing");
      window.postMessage({ __freeTtsMedia: true, action: "audioPlaybackEvent", ...record.receipt("applied"), audioError: record.audioError }, "*");
    };
    try { Promise.resolve(audio.play()).then(() => settled(null), settled); } catch (error) { settled(error); }
  };
  return { installed: true };
}

function selectAttempt(pipeline, attempt) {
  if (!ownsPipeline(pipeline, attempt)) return Promise.resolve(null);
  if (!attempt.selectionPromise) {
    const capability = workerIntent(pipeline);
    attempt.selectionPromise = (async () => {
      await pipeline.installPromise;
      if (!ownsPipeline(pipeline, attempt)) return null;
      const [result] = await chrome.scripting.executeScript({ target: { tabId: pipeline.tabId }, world: "MAIN",
        func: injectSelectAttempt, args: [capability] });
      if (!ownsPipeline(pipeline, attempt)) return null;
      attempt.selectionReceipt = result?.result;
      if (!validPageReceipt(pipeline, attempt.selectionReceipt)) throw new Error("MAIN selection returned no valid owned receipt");
      adoptPageReceipt(pipeline, attempt, attempt.selectionReceipt);
      requestActivation(pipeline, attempt);
      return attempt.selectionReceipt;
    })().catch(error => {
      failOwnedPlayback(pipeline, "page-operation-failure", error, attempt);
      return null;
    });
  }
  return attempt.selectionPromise;
}

function injectSelectAttempt(capability) {
  const record = window.__freeTtsPlayback;
  if (!record?.receipt) return null;
  if (record.retired || record.leaseId !== capability.leaseId || record.sessionId !== capability.sessionId || record.lifecycleRevision !== capability.lifecycleRevision || capability.attemptId < record.attemptFloor) return record.receipt("stale");
  if (capability.attemptId === record.attemptId) return record.receipt("idempotent");
  record.startSerial++;
  if (record.audio) { record.audio.pause(); record.audio.src = ""; record.audioRevision++; }
  record.attemptId = record.attemptFloor = capability.attemptId;
  record.audio = window.__freeTtsAudio = null;
  record.audioId = null; record.audioStatus = "none"; record.audioError = null;
  return record.receipt("applied");
}

function injectWorkerIntent(capability) {
  const record = window.__freeTtsPlayback;
  if (!record?.receipt) return null;
  if (record.retired || record.leaseId !== capability.leaseId || record.sessionId !== capability.sessionId || record.lifecycleRevision !== capability.lifecycleRevision || capability.intentRevision < record.workerIntentRevision) return record.receipt("stale");
  if (capability.intentRevision === record.workerIntentRevision) return record.receipt("idempotent");
  record.workerIntentRevision = capability.intentRevision;
  if (capability.mediaBase !== record.mediaRevision) return record.receipt("superseded-by-media");
  record.desiredPaused = capability.paused;
  if (capability.paused) record.pause();
  else if (record.attemptId === capability.attemptId) record.play();
  return record.receipt("applied");
}

function injectActivation(capability) {
  const record = window.__freeTtsPlayback;
  if (!record?.receipt) return null;
  if (record.retired || record.leaseId !== capability.leaseId || record.sessionId !== capability.sessionId || record.lifecycleRevision !== capability.lifecycleRevision ||
      record.attemptId !== capability.attemptId || record.workerIntentRevision !== capability.intentRevision || record.mediaRevision !== capability.mediaRevision || record.desiredPaused || record.audioStatus === "ended") return record.receipt("no-effect");
  if (!record.audio) {
    const audio = new Audio(capability.dataUrl);
    record.audio = window.__freeTtsAudio = audio;
    record.audioId = `${record.sessionId}:${record.attemptId}`; record.audioStatus = "none"; record.audioRevision++;
  }
  record.play();
  return record.receipt("applied");
}

// Sync worker state after a media key already paused/resumed the Audio element
// in the page. Updates the pipeline flag, context menu, and control bar without
// re-issuing audio control (the page already did it, with lower latency).
async function syncPausedState(pipeline, notification) {
  const attempt = pipeline.attempt;
  if (notification.mediaRevision >= pipeline.mediaRevision &&
      (pipeline.isPaused !== notification.paused || pipeline.mediaRevision !== notification.mediaRevision)) {
    pipeline.isPaused = notification.paused;
    pipeline.mediaRevision = notification.mediaRevision;
    viewRevision++;
  }
  requestPageObservation(pipeline, attempt);
  await updateControlBar(pipeline, buildPlaybackSnapshot(pipeline), playbackModes(pipeline));
  return commandResponse(true, notification, pipeline.failure || null);
}

// --- Media Session (system media keys) --------------------------------------
// Media key handling MUST run in the page's MAIN world: action handlers set in
// the extension's isolated world are never wired to the hardware media keys,
// and the Audio element they control lives in the MAIN world too. Since MAIN
// world cannot use chrome.* APIs, prev/next/stop are bridged out via
// window.postMessage to an isolated-world relay that calls chrome.runtime.
//
// play/pause are handled entirely in the page (direct Audio control), so they
// work even when the service worker is asleep. prev/next/stop need the worker;
// incoming runtime messages from the relay wake it, and a 25s keepalive ping
// keeps it alive across pauses so the in-memory pipeline state survives.

// Relay installed in the ISOLATED world: forwards MAIN-world postMessages to
// the service worker. Idempotent so repeated setup calls don't stack listeners.
function injectMediaRelay(owner) {
  const view = window.__freeTtsView;
  if (!view || view.retired || view.leaseId !== owner.leaseId || view.sessionId !== owner.sessionId || view.lifecycleRevision !== owner.lifecycleRevision) return;
  if (window.__freeTtsRelayInstalled) return;
  window.__freeTtsRelayInstalled = true;
  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    const data = event.data;
    if (!data || data.__freeTtsMedia !== true || typeof data.action !== "string") return;
    try { chrome.runtime.sendMessage({ ...data, source: "media" }).catch(() => {}); } catch {}
  });
}

// Media session set up in the MAIN world.
function injectMediaSession(owner) {
  const record = window.__freeTtsPlayback;
  if (!record || record.retired || record.leaseId !== owner.leaseId || record.sessionId !== owner.sessionId || record.lifecycleRevision !== owner.lifecycleRevision) return;
  if (!("mediaSession" in navigator)) { console.warn("free-tts: Media Session is unavailable"); return; }
  // Registration and metadata are optional browser capabilities. Isolate each
  // operation so one unsupported action cannot disable the remaining controls.
  const optionalSetup = effect => { try { effect(); } catch (error) { console.warn("free-tts: optional Media Session setup", error); } };
  const live = () => record === window.__freeTtsPlayback && !record.retired;
  const send = (action) => {
    if (!live()) return;
    try { window.postMessage({ __freeTtsMedia: true, action, ...record.receipt("applied") }, "*"); } catch {}
  };

  // Play/pause: directly control the Audio element for latency (no service
  // worker round-trip), then notify the worker so it syncs sentencePipeline
  // state and the floating control bar / context menu.
  optionalSetup(() => navigator.mediaSession.setActionHandler("play", () => {
    if (!live()) return;
    record.mediaSequence++; record.mediaRevision++;
    record.desiredPaused = false;
    record.play();
    send("mediaResume");
  }));
  optionalSetup(() => navigator.mediaSession.setActionHandler("pause", () => {
    if (!live()) return;
    record.mediaSequence++; record.mediaRevision++;
    record.desiredPaused = true;
    record.pause();
    send("mediaPause");
  }));
  // Prev/next/stop: bridge to the service worker via the isolated-world relay.
  const request = action => { if (live()) { record.mediaSequence++; send(action); } };
  optionalSetup(() => navigator.mediaSession.setActionHandler("previoustrack", () => request("prevSentence")));
  optionalSetup(() => navigator.mediaSession.setActionHandler("nexttrack", () => request("nextSentence")));
  optionalSetup(() => navigator.mediaSession.setActionHandler("stop", () => request("stopPlayback")));

  optionalSetup(() => { navigator.mediaSession.metadata = new MediaMetadata({
    title: "free-tts",
    artist: "Text-to-Speech",
    album: document.title || "Web Page",
  }); });
  optionalSetup(() => { navigator.mediaSession.playbackState = "paused"; });

  // Keepalive: ping the worker so prev/next/stop stay responsive while paused.
  if (window.__freeTtsKeepalive) clearInterval(window.__freeTtsKeepalive);
  window.__freeTtsKeepalive = setInterval(() => send("ping"), 25000);
}

function clearMediaSession(owner, retirementRevision) {
  const receipt = { ...owner, retirementRevision, disposition: "stale", errors: [] };
  const record = window.__freeTtsPlayback;
  if (!record || record.leaseId !== owner.leaseId || record.lifecycleFloor > retirementRevision ||
      record.lifecycleRevision > owner.lifecycleRevision ||
      (record.lifecycleRevision === owner.lifecycleRevision && record.sessionId !== owner.sessionId)) return receipt;
  record.lifecycleFloor = retirementRevision;
  record.retired = true; record.desiredPaused = true; record.startSerial++;
  const errors = receipt.errors, attempt = effect => { try { effect(); } catch (error) { errors.push(error.message || String(error)); } };
  // Retain this tombstone even when installation has not yet completed.
  attempt(() => { if (window.__freeTtsKeepalive) clearInterval(window.__freeTtsKeepalive); });
  window.__freeTtsKeepalive = null;
  if ("mediaSession" in navigator) {
    for (const action of ["play", "pause", "previoustrack", "nexttrack", "stop"]) attempt(() => navigator.mediaSession.setActionHandler(action, null));
    attempt(() => { navigator.mediaSession.metadata = null; });
    attempt(() => { navigator.mediaSession.playbackState = "none"; });
  }
  const audio = record.audio;
  attempt(() => audio?.pause());
  attempt(() => { if (audio) audio.src = ""; });
  record.audio = null; record.audioId = null; record.audioStatus = "none"; record.audioError = null; record.audioRevision++;
  window.__freeTtsAudio = null;
  receipt.disposition = "applied";
  return receipt;
}

async function setupMediaSession(pipeline) {
  const owner = pageOwner(pipeline);
  await pipeline.installPromise;
  if (!ownsPipeline(pipeline)) return;
  try {
    await chrome.scripting.executeScript({ target: { tabId: owner.tabId }, func: injectMediaRelay, args: [owner] });
    if (!ownsPipeline(pipeline)) return;
    await chrome.scripting.executeScript({ target: { tabId: owner.tabId }, world: "MAIN", func: injectMediaSession, args: [owner] });
  } catch (error) {
    failOwnedPlayback(pipeline, "page-operation-failure", error);
  }
}

async function cleanupMediaSession(owner, retirementRevision) {
  const [result] = await chrome.scripting.executeScript({ target: { tabId: owner.tabId }, world: "MAIN",
    func: clearMediaSession, args: [owner, retirementRevision] });
  const receipt = result?.result;
  if (!receipt || receipt.tabId !== owner.tabId || receipt.leaseId !== owner.leaseId || receipt.sessionId !== owner.sessionId ||
      receipt.lifecycleRevision !== owner.lifecycleRevision || receipt.retirementRevision !== retirementRevision ||
      !["applied", "stale"].includes(receipt.disposition) || !Array.isArray(receipt.errors)) {
    throw new Error("MAIN retirement returned no valid owned receipt");
  }
  return receipt;
}

// --- Control bar -----------------------------------------------------------
async function getLoopState(tabId) {
  if (sentencePipeline?.tabId === tabId) return sentencePipeline.loopEnabled !== false;
  try {
    const [result] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => document.getElementById("free-tts-loop")?.checked ?? window.__freeTtsLoop ?? true,
    });
    return result?.result ?? true;
  } catch {
    return true;  // default to loop on
  }
}

async function getRepeatCurrentState(tabId) {
  if (sentencePipeline?.tabId === tabId) return sentencePipeline.repeatCurrentEnabled === true;
  try {
    const [result] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => document.getElementById("free-tts-repeat-current")?.checked ?? false,
    });
    return result?.result ?? false;
  } catch {
    return false;
  }
}

async function showControlBar(owner, snapshot, modes) {
  await owner.installPromise;
  if (!ownsPipeline(owner)) return "deferred";
  try {
    const [result] = await chrome.scripting.executeScript({
      target: { tabId: owner.tabId },
      func: (binding, supplied, suppliedModes) => {
        const view = window.__freeTtsView;
        if (!view || view.retired || view.leaseId !== binding.leaseId || view.sessionId !== binding.sessionId || view.lifecycleRevision !== binding.lifecycleRevision) return "deferred";
        if (view.barCreated) {
          if (!document.getElementById("free-tts-bar")) throw new Error("Owned control bar is missing");
          return "rendered";
        }
        const pending = view.pendingRender;
        const latest = pending && pending.snapshot.viewRevision > supplied.viewRevision ? pending : { snapshot: supplied, modes: suppliedModes };
        let alive = true;
        const selectionTimers = new Set();
        const live = () => alive && view === window.__freeTtsView && !view.retired;
        const sendBarMessage = message => live() && chrome.runtime.sendMessage({ ...message, source: "bar", leaseId: binding.leaseId,
          sessionId: binding.sessionId, lifecycleRevision: binding.lifecycleRevision });
        const paused = latest.snapshot.state === "paused";
        const initialLoopEnabled = latest.modes.loopEnabled, initialRepeatCurrent = latest.modes.repeatCurrentEnabled;
        view.viewRevision = latest.snapshot.viewRevision;
        view.pendingRender = null;
        // Remove existing bar
        if (typeof window.__freeTtsCleanupControlBar === "function") {
          window.__freeTtsCleanupControlBar();
        }
        document.getElementById("free-tts-bar")?.remove();
        const bar = document.createElement("div");
        bar.id = "free-tts-bar";
        [
          ["free-tts-prev", "Previous", "⏮"],
          ["free-tts-toggle", paused ? "Resume" : "Pause", paused ? "▶" : "⏸"],
          ["free-tts-next", "Next", "⏭"],
          ["free-tts-close", "Stop", "✕"],
        ].forEach(([id, title, text]) => {
          const button = document.createElement("button");
          button.id = id;
          button.title = title;
          button.setAttribute("aria-label", title);
          button.textContent = text;
          bar.appendChild(button);
        });
        // Repeat All (loop) checkbox — checked by default
        const loopLabel = document.createElement("label");
        loopLabel.title = "Repeat All";
        loopLabel.style.cssText = "display:flex;align-items:center;gap:2px;font-size:12px;color:#555;padding:2px 4px;cursor:pointer;border-left:1px solid #ddd;margin-left:2px;";
        const loopCheck = document.createElement("input");
        loopCheck.type = "checkbox";
        loopCheck.id = "free-tts-loop";
        loopCheck.checked = initialLoopEnabled !== false;
        loopCheck.style.cssText = "margin:0;cursor:pointer;";
        loopCheck.addEventListener("change", () => {
          if (!live()) return;
          if (loopCheck.checked) {
            const repeatCurrent = document.getElementById("free-tts-repeat-current");
            if (repeatCurrent) repeatCurrent.checked = false;
          }
          window.__freeTtsLoop = loopCheck.checked;
          sendBarMessage({ action: "setLoopState", enabled: loopCheck.checked });
        });
        loopLabel.appendChild(loopCheck);
        loopLabel.appendChild(document.createTextNode("↻"));
        bar.appendChild(loopLabel);
        window.__freeTtsLoop = initialLoopEnabled !== false;

        // Repeat Current Sentence checkbox — unchecked by default
        const repeatCurrentLabel = document.createElement("label");
        repeatCurrentLabel.title = "Repeat Current Sentence";
        repeatCurrentLabel.style.cssText = "display:flex;align-items:center;gap:2px;font-size:12px;color:#555;padding:2px 4px;cursor:pointer;";
        const repeatCurrentCheck = document.createElement("input");
        repeatCurrentCheck.type = "checkbox";
        repeatCurrentCheck.id = "free-tts-repeat-current";
        repeatCurrentCheck.checked = initialRepeatCurrent === true;
        repeatCurrentCheck.style.cssText = "margin:0;cursor:pointer;";
        repeatCurrentCheck.addEventListener("change", () => {
          if (!live()) return;
          if (repeatCurrentCheck.checked) {
            const loop = document.getElementById("free-tts-loop");
            if (loop) loop.checked = false;
          }
          sendBarMessage({ action: "setRepeatCurrentState", enabled: repeatCurrentCheck.checked });
        });
        repeatCurrentLabel.appendChild(repeatCurrentCheck);
        repeatCurrentLabel.appendChild(document.createTextNode("↻1"));
        bar.appendChild(repeatCurrentLabel);
        bar.style.cssText = "position:fixed;top:56px;right:16px;background:rgba(255,255,255,0.85);backdrop-filter:blur(8px);border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,0.06);padding:2px 4px;z-index:999999;display:flex;gap:0;font-family:-apple-system,BlinkMacSystemFont,sans-serif;cursor:move;user-select:none;";
        bar.querySelectorAll("button").forEach(b => {
          b.style.cssText = "border:none;background:none;font-size:14px;cursor:pointer;padding:2px 4px;border-radius:4px;color:#555;transition:background 0.15s;line-height:1;";
          b.addEventListener("mouseenter", () => b.style.background = "#f0f0f0");
          b.addEventListener("mouseleave", () => b.style.background = "none");
        });
        document.body.appendChild(bar);
        view.barCreated = true;

        // --- Drag to move ---
        let drag = false, startX, startY, startLeft, startTop;
        bar.addEventListener("mousedown", (e) => {
          if (!live() || e.target.tagName === "BUTTON") return;  // don't drag when clicking buttons
          drag = true;
          startX = e.clientX; startY = e.clientY;
          const rect = bar.getBoundingClientRect();
          startLeft = rect.left; startTop = rect.top;
          e.preventDefault();
        });
        const onMouseMove = (e) => {
          if (!live() || !drag) return;
          bar.style.transform = "none";
          bar.style.left = (startLeft + e.clientX - startX) + "px";
          bar.style.top = (startTop + e.clientY - startY) + "px";
        };
        const onDragMouseUp = () => { drag = false; };
        document.addEventListener("mousemove", onMouseMove);
        document.addEventListener("mouseup", onDragMouseUp);

        // --- Button handlers ---
        bar.querySelector("#free-tts-prev").addEventListener("click", () => sendBarMessage({ action: "prevSentence" }));
        bar.querySelector("#free-tts-next").addEventListener("click", () => sendBarMessage({ action: "nextSentence" }));
        bar.querySelector("#free-tts-toggle").addEventListener("click", () => {
          sendBarMessage({ action: "togglePlayback" });
        });
        bar.querySelector("#free-tts-close").addEventListener("click", () => sendBarMessage({ action: "stopPlayback" }));

        // --- ESC key stops playback ---
        const onKeyDown = (e) => {
          if (e.key === "Escape") {
            sendBarMessage({ action: "stopPlayback" });
          }
        };
        document.addEventListener("keydown", onKeyDown);

        // --- Double-click to jump to sentence ---
        const onDoubleClick = () => {
          if (!live()) return;
          const sel = window.getSelection();
          if (!sel || !sel.toString().trim()) return;
          const word = sel.toString().trim();
          const sents = window.__freeTtsSentences;
          if (!sents) return;
          for (let i = 0; i < sents.length; i++) {
            if (sents[i].includes(word)) {
              sendBarMessage({ action: "jumpToSentence", idx: i });
              return;
            }
          }
        };
        document.addEventListener("dblclick", onDoubleClick);

        // --- Drag-select to jump to sentence ---
        const onSelectionMouseUp = () => {
          if (!live()) return;
          const timer = setTimeout(() => {
            selectionTimers.delete(timer);
            if (!live()) return;
            const sel = window.getSelection();
            if (!sel || sel.isCollapsed) return;
            const text = sel.toString().trim();
            if (!text) return;
            const sents = window.__freeTtsSentences;
            if (!sents) return;
            for (let i = 0; i < sents.length; i++) {
              if (sents[i].includes(text)) {
                sendBarMessage({ action: "jumpToSentence", idx: i });
                return;
              }
            }
          }, 100);
          selectionTimers.add(timer);
        };
        document.addEventListener("mouseup", onSelectionMouseUp);

        const cleanup = () => {
          if (!alive) return;
          alive = false; drag = false;
          for (const timer of selectionTimers) clearTimeout(timer);
          selectionTimers.clear();
          document.removeEventListener("mousemove", onMouseMove);
          document.removeEventListener("mouseup", onDragMouseUp);
          document.removeEventListener("dblclick", onDoubleClick);
          document.removeEventListener("mouseup", onSelectionMouseUp);
          document.removeEventListener("keydown", onKeyDown);
          bar.remove();
          if (window.__freeTtsCleanupControlBar === cleanup) delete window.__freeTtsCleanupControlBar;
        };
        window.__freeTtsCleanupControlBar = cleanup;
        return "rendered";
      },
      args: [pageOwner(owner), snapshot, modes],
    });
    if (!["rendered", "deferred"].includes(result?.result)) throw new Error("Control bar creation returned no receipt");
    return result.result;
  } catch (error) {
    if (snapshot.viewRevision === viewRevision) failOwnedPlayback(owner, "render-failure", error);
    return "deferred";
  }
}

async function updateControlBar(owner, snapshot, modes) {
  // Existing navigation callers still supply a tab ID; render canonical state.
  if (typeof owner === "number") { owner = sentencePipeline; snapshot = buildPlaybackSnapshot(owner); modes = owner && playbackModes(owner); }
  const pipeline = sentencePipeline;
  if (!ownsPipeline(pipeline) || !owner || owner.leaseId !== pipeline.leaseId || owner.sessionId !== pipeline.sessionId || owner.lifecycleRevision !== pipeline.lifecycleRevision) return "deferred";
  try {
    const [result] = await chrome.scripting.executeScript({
      target: { tabId: owner.tabId },
      func: (binding, current, currentModes) => {
        const view = window.__freeTtsView;
        if (!view || view.retired || view.leaseId !== binding.leaseId || view.sessionId !== binding.sessionId || view.lifecycleRevision !== binding.lifecycleRevision) return "deferred";
        if (current.viewRevision < view.viewRevision) return "rendered";
        if (!view.barCreated) {
          if (!view.pendingRender || current.viewRevision >= view.pendingRender.snapshot.viewRevision) view.pendingRender = { snapshot: current, modes: currentModes };
          return "deferred";
        }
        const btn = document.getElementById("free-tts-toggle");
        if (!btn) throw new Error("Control bar is missing");
        const paused = current.state === "paused", title = paused ? "Resume" : "Pause";
        btn.textContent = paused ? "▶" : "⏸";
        btn.title = title;
        btn.setAttribute("aria-label", title);
        document.getElementById("free-tts-loop").checked = currentModes.loopEnabled;
        document.getElementById("free-tts-repeat-current").checked = currentModes.repeatCurrentEnabled;
        view.viewRevision = current.viewRevision;
        return "rendered";
      },
      args: [pageOwner(owner), snapshot, modes],
    });
    if (!["rendered", "deferred"].includes(result?.result)) throw new Error("Control bar update returned no receipt");
    return result.result;
  } catch (error) {
    if (snapshot.viewRevision === viewRevision) failOwnedPlayback(pipeline, "render-failure", error);
    return "deferred";
  }
}

async function hideControlBar(owner, retirementRevision) {
  await chrome.scripting.executeScript({
    target: { tabId: owner.tabId },
    func: (binding, floor) => {
      const view = window.__freeTtsView;
      if (!view || view.leaseId !== binding.leaseId || view.lifecycleFloor > floor ||
          view.lifecycleRevision > binding.lifecycleRevision ||
          (view.lifecycleRevision === binding.lifecycleRevision && view.sessionId !== binding.sessionId)) return;
      view.lifecycleFloor = floor; view.retired = true;
      view.pendingRender = null; view.barCreated = false;
      if (typeof window.__freeTtsCleanupControlBar === "function") window.__freeTtsCleanupControlBar();
      else document.getElementById("free-tts-bar")?.remove();
    }, args: [owner, retirementRevision],
  });
}

// --- Prev/next/jump sentence -----------------------------------------------
function navigationPipeline(owner) {
  const pipeline = sentencePipeline;
  return ownsPipeline(pipeline) && owner && owner.tabId === pipeline.tabId && owner.leaseId === pipeline.leaseId &&
    owner.sessionId === pipeline.sessionId && owner.lifecycleRevision === pipeline.lifecycleRevision ? pipeline : null;
}

async function navigateToSentence(pipeline, idx, media = null) {
  // Selection and accepted running intent are synchronous worker decisions.
  // MAIN selection only displaces the old Audio; it cannot undo a newer pause.
  pipeline.currentIdx = idx;
  const attempt = createAttempt(pipeline, pipeline.initialized ? "loading" : "initializing");
  if (!media || media.mediaRevision >= pipeline.mediaRevision) pipeline.isPaused = false;
  pipeline.intentRevision++;
  if (!media) pipeline.localIntentFloor = pipeline.intentRevision;
  viewRevision++;
  const capability = workerIntent(pipeline);
  if (media) capability.mediaBase = media.mediaRevision;
  const render = updateControlBar(pipeline, buildPlaybackSnapshot(pipeline), playbackModes(pipeline));
  const selection = selectAttempt(pipeline, attempt);
  const application = applyWorkerIntent(pipeline, capability);
  void playNextSentence(pipeline, attempt);
  const [, receipt] = await Promise.all([selection, application, render]);
  return commandResponse(true, receipt || { disposition: "stale" }, pipeline.failure || null);
}

async function prevSentence(owner, media = null) {
  const pipeline = navigationPipeline(owner);
  if (!pipeline || pipeline.currentIdx === 0) return commandResponse(false);
  return navigateToSentence(pipeline, pipeline.currentIdx - 1, media);
}

async function nextSentence(owner, media = null) {
  const pipeline = navigationPipeline(owner);
  if (!pipeline) return commandResponse(false);
  let idx = pipeline.currentIdx + 1;
  if (idx >= pipeline.sentences.length) {
    if (pipeline.repeatCurrentEnabled || pipeline.loopEnabled) idx = 0;
    else return stopPlayback(pipeline, false);
  }
  return navigateToSentence(pipeline, idx, media);
}

async function jumpToSentence(owner, idx) {
  const pipeline = navigationPipeline(owner);
  if (!pipeline) return commandResponse(false);
  if (!Number.isInteger(idx) || idx < 0 || idx >= pipeline.sentences.length) {
    return commandResponse(false, null, { code: "invalid-argument", message: "Invalid sentence index" });
  }
  return navigateToSentence(pipeline, idx);
}

// --- Sentence splitting ----------------------------------------------------
function splitSentences(text) {
  // Split on sentence-ending punctuation and newline-delimited fragments.
  // Selected page text often contains headings/list items without punctuation;
  // keep those fragments instead of dropping everything before the final line.
  const sentences = text.match(/[^.!?\n。！？．｡]+(?:[.!?。！？．｡]+["'”’»）)\]}」』】》]*|(?=\n|$))/g) || [text];
  return sentences.map(s => s.trim()).filter(s => s.length > 0);
}

// --- In-page sentence wrapping + highlight + scroll -----------------------
async function initPageHighlighting(tabId, sentences, pipeline = sentencePipeline) {
  try {
    if (!ownsPipeline(pipeline)) return;
    const { highlightColor } = await chrome.storage.sync.get({ highlightColor: "#fff3cd" });
    if (!ownsPipeline(pipeline)) return;
    await pipeline.installPromise;
    if (!ownsPipeline(pipeline)) return;
    const safeHighlightColor = normalizeColor(highlightColor);
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (sents, color, owner) => {
        const view = window.__freeTtsView;
        if (!view || view.retired || view.leaseId !== owner.leaseId || view.sessionId !== owner.sessionId || view.lifecycleRevision !== owner.lifecycleRevision) return;
        window.__freeTtsSentences = sents;
        window.__freeTtsColor = color;
      },
      args: [sentences, safeHighlightColor, pageOwner(pipeline)],
    });
  } catch (error) {
    if (ownsPipeline(pipeline)) logError("initializing page highlighting", error);
  }
}

async function highlightCurrentSentence(tabId, idx, pipeline = sentencePipeline, attempt = pipeline?.attempt) {
  if (!ownsPipeline(pipeline, attempt)) return;
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (idx, owner, attemptId) => {
        const view = window.__freeTtsView;
        if (!view || view.retired || view.leaseId !== owner.leaseId || view.sessionId !== owner.sessionId || view.lifecycleRevision !== owner.lifecycleRevision || attemptId < (view.attemptFloor || 0)) return;
        view.attemptFloor = attemptId;
        const color = window.__freeTtsColor || "#fff3cd";
        const sentence = window.__freeTtsSentences[idx];
        if (!sentence) return;

        // Remove previous highlight overlay
        document.querySelectorAll(".free-tts-highlight-overlay").forEach(el => el.remove());

        const allRects = [];
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
        let node;
        while ((node = walker.nextNode())) {
          let searchFrom = 0;
          while (true) {
            const pos = node.textContent.indexOf(sentence, searchFrom);
            if (pos < 0) break;

            try {
              const range = document.createRange();
              range.setStart(node, pos);
              range.setEnd(node, pos + sentence.length);
              for (const rect of range.getClientRects()) {
                allRects.push(rect);
              }
            } catch (e) { /* invalid range, skip */ }

            searchFrom = pos + 1;
          }
        }

        // Create highlight overlays for all found rects
        for (const rect of allRects) {
          const overlay = document.createElement("div");
          overlay.className = "free-tts-highlight-overlay";
          overlay.style.cssText = `position:absolute;left:${rect.left + window.scrollX}px;top:${rect.top + window.scrollY}px;width:${rect.width}px;height:${rect.height}px;background:${color};opacity:0.5;pointer-events:none;z-index:999998;transition:background 0.3s;`;
          document.body.appendChild(overlay);
        }
        // Auto-scroll to the first highlighted sentence
        if (allRects.length > 0) {
          const r = allRects[0];
          window.scrollTo({ left: 0, top: r.top + window.scrollY - window.innerHeight / 3, behavior: "smooth" });
        }
      },
      args: [idx, pageOwner(pipeline), attempt.attemptId],
    });
  } catch (error) {
    if (ownsPipeline(pipeline, attempt)) logError("highlighting current sentence", error);
  }
}

async function cleanupPageHighlighting(owner, retirementRevision) {
  await chrome.scripting.executeScript({
    target: { tabId: owner.tabId },
    func: (binding, floor) => {
      const view = window.__freeTtsView;
      if (!view || view.leaseId !== binding.leaseId || view.lifecycleFloor > floor ||
          view.lifecycleRevision > binding.lifecycleRevision ||
          (view.lifecycleRevision === binding.lifecycleRevision && view.sessionId !== binding.sessionId)) return;
      view.lifecycleFloor = floor; view.retired = true; view.pendingRender = null;
      document.querySelectorAll(".free-tts-highlight-overlay").forEach(el => el.remove());
      delete window.__freeTtsSentences;
      delete window.__freeTtsColor;
    }, args: [owner, retirementRevision],
  });
}

// --- Fetch audio for one sentence ------------------------------------------
function arrayBufferToDataUrl(buffer, mimeType = "audio/mpeg") {
  let binary = "";
  const bytes = new Uint8Array(buffer);
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
  }
  return `data:${mimeType};base64,${btoa(binary)}`;
}

async function fetchSentenceAudio(serverUrl, voice, speed, sentence) {
  const ssml = buildSSML(sentence, voice, speed);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const resp = await fetch(`${normalizeServerUrl(serverUrl)}/generate-and-download-tts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ssml }),
      signal: controller.signal,
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ error: `Server returned ${resp.status}` }));
      throw new Error(err.error || `Server returned ${resp.status}`);
    }
    const buffer = await resp.arrayBuffer();
    const contentType = resp.headers.get("content-type") || "audio/mpeg";
    return arrayBufferToDataUrl(buffer, contentType);
  } catch (error) {
    if (controller.signal.aborted) error.code = "load-timeout";
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

// --- Main sentence TTS pipeline --------------------------------------------
async function startSentenceTTS(tabId, text, ticket = null) {
  if (!tabId || typeof text !== "string" || !text.trim()) return commandResponse(false);
  const sentences = splitSentences(text);
  if (!sentences.length) return commandResponse(false);
  ticket ||= startTicket();
  if (ticket.cancelled || ticket !== pendingStart) return commandResponse(false);
  if (ownsPipeline(sentencePipeline)) void stopPlayback(sentencePipeline, false);
  pendingStart = null;
  if (!pageLeases.has(tabId)) pageLeases.set(tabId, { leaseId: crypto.randomUUID(), establishedSessionId: null, promise: null, closed: false });
  const pipeline = sentencePipeline = {
    tabId, leaseId: pageLeases.get(tabId).leaseId, sessionId: crypto.randomUUID(), lifecycleRevision: ++lifecycleRevision,
    sentences, currentIdx: 0, cache: new Map(), voice: null, serverUrl: null, speed: 0, isPaused: false,
    loopEnabled: true, repeatCurrentEnabled: false, intentRevision: 1, localIntentFloor: 1,
    mediaRevision: 0, lastMediaSequence: 0, nextAttemptId: 0, attempt: null, cancelled: false, initialized: false,
  };
  activePlayback = pageOwner(pipeline);
  lastError = null;
  viewRevision++;
  createAttempt(pipeline, "initializing");
  const capability = workerIntent(pipeline);
  pipeline.installPromise = installPlayback(pipeline).catch(error => {
    failOwnedPlayback(pipeline, "page-operation-failure", error);
    return null;
  });
  // Selection follows the session barrier rather than participating in it;
  // navigation during installation must never make that barrier await itself.
  void selectAttempt(pipeline, pipeline.attempt);
  pipeline.initializationPromise = initializePlayback(pipeline).catch(error => {
    failOwnedPlayback(pipeline, "settings-failure", error);
  });
  void pipeline.installPromise.then(() => {
    if (ownsPipeline(pipeline)) return showControlBar(pipeline, buildPlaybackSnapshot(pipeline), playbackModes(pipeline));
  }).catch(error => { if (ownsPipeline(pipeline)) logError("installing playback", error); });
  const receipt = await applyWorkerIntent(pipeline, capability);
  if (ownsPipeline(pipeline)) await updateControlBar(pipeline, buildPlaybackSnapshot(pipeline), playbackModes(pipeline));
  return commandResponse(true, receipt, pipeline.failure || null);
}

function createAttempt(pipeline, phase = "loading") {
  const previous = pipeline.attempt;
  if (previous) {
    previous.retired = true;
    clearTimeout(previous.pollTimer);
    clearTimeout(previous.cacheTimer);
    previous.pollTimer = null;
    previous.cacheTimer = null;
    previous.cacheWake?.();
    previous.cacheWake = null;
  }
  return pipeline.attempt = { attemptId: ++pipeline.nextAttemptId, idx: pipeline.currentIdx, phase, retired: false,
    dataUrl: null, loadPromise: null, selectionPromise: null, selectionReceipt: null,
    activationRequest: null, pageReceipt: null, observationPromise: null, observationNeeded: false,
    audioId: null, audioStatus: "none", audioRevision: -1,
    startSerial: null, pollTimer: null, pollInFlight: false, completionObserved: false,
    completionObservedWhilePaused: false, completionConsumed: false };
}

async function initializePlayback(pipeline) {
  const settings = await chrome.storage.sync.get({ serverUrl: DEFAULT_SERVER, voice: "en-US-AvaMultilingualNeural", speed: 0 });
  if (!ownsPipeline(pipeline)) return;
  pipeline.serverUrl = normalizeServerUrl(settings.serverUrl); pipeline.voice = settings.voice; pipeline.speed = normalizeSpeed(settings.speed);
  await pipeline.installPromise;
  if (!ownsPipeline(pipeline)) return;
  await setupMediaSession(pipeline);
  if (!ownsPipeline(pipeline)) return;
  await initPageHighlighting(pipeline.tabId, pipeline.sentences, pipeline);
  if (!ownsPipeline(pipeline)) return;
  pipeline.initialized = true;
  setAttemptPhase(pipeline, pipeline.attempt, "loading");
  for (let idx = 0; idx < Math.min(PRELOAD_AHEAD, pipeline.sentences.length); idx++) preloadSentence(pipeline, idx);
  await playNextSentence(pipeline, pipeline.attempt);
}

function preloadSentence(pipeline, idx) {
  fetchSentenceAudio(pipeline.serverUrl, pipeline.voice, pipeline.speed, pipeline.sentences[idx])
    .then(url => { if (ownsPipeline(pipeline)) pipeline.cache.set(idx, url); })
    .catch(error => { if (ownsPipeline(pipeline)) logError(`preloading sentence ${idx}`, error); });
}

async function playNextSentence(pipeline = sentencePipeline, attempt = pipeline?.attempt) {
  if (!ownsAttempt(pipeline, attempt) || !pipeline.initialized) return;
  if (attempt.completionConsumed) {
    // Terminal completion delegates to the same captured-owner retirement.
    await stopPlayback(pipeline, false);
    return;
  }
  const { sentences, cache, tabId, voice, serverUrl, speed } = pipeline;
  const idx = attempt.idx;
  if (!attempt.loadPromise) attempt.loadPromise = (async () => {
    let dataUrl = cache.get(idx);
    const start = Date.now();
    while (!dataUrl && Date.now() - start < 10000) {
      await new Promise(resolve => { attempt.cacheWake = resolve; attempt.cacheTimer = setTimeout(resolve, 200); });
      if (!ownsPipeline(pipeline, attempt)) return;
      dataUrl = cache.get(idx);
    }
    if (!dataUrl) {
      dataUrl = await fetchSentenceAudio(serverUrl, voice, speed, sentences[idx]);
      if (!ownsPipeline(pipeline, attempt)) return;
      cache.set(idx, dataUrl);
    }
    if (!ownsPipeline(pipeline, attempt)) return;
    for (let nextIdx = idx + 1; nextIdx < idx + 1 + PRELOAD_AHEAD && nextIdx < sentences.length; nextIdx++) {
      if (!cache.has(nextIdx)) preloadSentence(pipeline, nextIdx);
    }
    await highlightCurrentSentence(tabId, idx, pipeline, attempt);
    if (!ownsPipeline(pipeline, attempt)) return;
    attempt.dataUrl = dataUrl;
    setAttemptPhase(pipeline, attempt, "ready");
  })();
  try {
    await attempt.loadPromise;
    if (ownsPipeline(pipeline, attempt) && !pipeline.isPaused) requestActivation(pipeline, attempt);
  } catch (error) {
    failOwnedPlayback(pipeline, error.code === "load-timeout" ? "load-timeout" : "load-failure", error, attempt);
  }
}

function requestActivation(pipeline, attempt) {
  if (!ownsPipeline(pipeline, attempt) || pipeline.isPaused || attempt.observationPromise || attempt.observationNeeded || !attempt.dataUrl || attempt.audioStatus === "ended" ||
      ["starting", "playing"].includes(attempt.audioStatus) || attempt.completionObserved) return;
  const selection = attempt.selectionReceipt;
  if (!selection || selection.leaseId !== pipeline.leaseId || selection.sessionId !== pipeline.sessionId ||
      selection.lifecycleRevision !== pipeline.lifecycleRevision || selection.attemptId !== attempt.attemptId ||
      !["applied", "idempotent"].includes(selection.disposition)) return;
  // Loading, selection, and intent application race independently. Whichever
  // finishes last continues the ready attempt after both page boundaries apply.
  const receipt = attempt.pageReceipt;
  if (!receipt || receipt.attemptId !== attempt.attemptId || receipt.leaseId !== pipeline.leaseId ||
      receipt.sessionId !== pipeline.sessionId || receipt.lifecycleRevision !== pipeline.lifecycleRevision ||
      receipt.intentRevision !== pipeline.intentRevision || receipt.mediaRevision !== pipeline.mediaRevision || receipt.paused ||
      !["applied", "idempotent"].includes(receipt.disposition)) return;
  const capability = { leaseId: pipeline.leaseId, sessionId: pipeline.sessionId, lifecycleRevision: pipeline.lifecycleRevision,
    attemptId: attempt.attemptId, intentRevision: pipeline.intentRevision, mediaRevision: pipeline.mediaRevision, dataUrl: attempt.dataUrl };
  const previous = attempt.activationRequest;
  if (previous?.intentRevision === capability.intentRevision && previous.mediaRevision === capability.mediaRevision) return;
  const request = attempt.activationRequest = { intentRevision: capability.intentRevision, mediaRevision: capability.mediaRevision, promise: null };
  request.promise = chrome.scripting.executeScript({ target: { tabId: pipeline.tabId }, world: "MAIN", func: injectActivation, args: [capability] })
    .then(([result]) => {
      if (!ownsPipeline(pipeline, attempt) || pipeline.intentRevision !== capability.intentRevision || pipeline.mediaRevision !== capability.mediaRevision) return;
      if (!validPageReceipt(pipeline, result?.result)) throw new Error("MAIN activation returned no valid owned receipt");
      adoptPageReceipt(pipeline, attempt, result?.result);
      if (attempt.audioId) scheduleCompletionPoll(pipeline, attempt);
    }).catch(error => {
      if (pipeline.intentRevision === capability.intentRevision && pipeline.mediaRevision === capability.mediaRevision) {
        failOwnedPlayback(pipeline, "page-operation-failure", error, attempt);
      }
    }).finally(() => { if (ownsPipeline(pipeline, attempt) && attempt.activationRequest === request) attempt.activationRequest = null; });
}

function scheduleCompletionPoll(pipeline, attempt) {
  if (!ownsPipeline(pipeline, attempt) || attempt.pollTimer || attempt.pollInFlight || attempt.completionObserved || attempt.phase === "ended") return;
  attempt.pollTimer = setTimeout(() => { attempt.pollTimer = null; void pollCompletion(pipeline, attempt); }, 500);
}

async function pollCompletion(pipeline, attempt) {
  if (!ownsPipeline(pipeline, attempt) || attempt.pollInFlight || attempt.completionObserved || !attempt.audioId) return;
  attempt.pollInFlight = true;
  const intentRevision = pipeline.intentRevision;
  try {
    const [result] = await chrome.scripting.executeScript({ target: { tabId: pipeline.tabId }, world: "MAIN", func: (owner, attemptId, audioId) => {
      const record = window.__freeTtsPlayback;
      if (!record || record.retired || record.leaseId !== owner.leaseId || record.sessionId !== owner.sessionId ||
          record.lifecycleRevision !== owner.lifecycleRevision || record.attemptId !== attemptId ||
          record.audioId !== audioId) return { owned: false, status: "stale" };
      if (!record.audio) return { owned: false, status: "missing-audio" };
      const ended = !!record.audio.ended;
      if (ended && record.audioStatus !== "ended") { record.audioStatus = "ended"; record.audioRevision++; }
      return { ...record.receipt("applied"), owned: true, ended, audioStatus: record.audioStatus,
        audioRevision: record.audioRevision };
    }, args: [pageOwner(pipeline), attempt.attemptId, attempt.audioId] });
    if (!ownsPipeline(pipeline, attempt)) return;
    const sample = result?.result;
    if (!sample?.owned || sample.attemptId !== attempt.attemptId || sample.audioId !== attempt.audioId) {
      if (pipeline.intentRevision === intentRevision && sample?.status === "missing-audio") {
        failOwnedPlayback(pipeline, "missing-audio", new Error("Owned Audio is missing"), attempt);
      } else if (!sample && pipeline.intentRevision === intentRevision) {
        throw new Error("Completion poll returned no receipt");
      }
      return;
    }
    if (sample.audioStatus === "error" && sample.intentRevision === pipeline.intentRevision && sample.mediaRevision === pipeline.mediaRevision) {
      adoptPageReceipt(pipeline, attempt, sample);
      return;
    }
    if (sample.audioRevision > attempt.audioRevision) {
      attempt.audioRevision = sample.audioRevision;
      attempt.audioStatus = sample.audioStatus;
      attempt.startSerial = sample.startSerial;
      if (["starting", "playing", "paused"].includes(sample.audioStatus)) setAttemptPhase(pipeline, attempt, sample.audioStatus);
    }
    if (!sample.ended || attempt.completionObserved) return;

    attempt.audioStatus = "ended";
    attempt.completionObserved = true;
    setAttemptPhase(pipeline, attempt, "ended");
    if (pipeline.isPaused) {
      attempt.completionObservedWhilePaused = true;
      return;
    }

    attempt.completionConsumed = true;
    let nextIdx = pipeline.repeatCurrentEnabled ? attempt.idx : attempt.idx + 1;
    if (nextIdx >= pipeline.sentences.length) {
      if (pipeline.loopEnabled) nextIdx = 0;
      else { void playNextSentence(pipeline, attempt); return; }
    }
    pipeline.currentIdx = nextIdx;
    const nextAttempt = createAttempt(pipeline);
    pipeline.intentRevision++; viewRevision++;
    void selectAttempt(pipeline, nextAttempt);
    void applyWorkerIntent(pipeline, workerIntent(pipeline));
    void playNextSentence(pipeline, nextAttempt);
  } catch (error) {
    if (ownsPipeline(pipeline, attempt) && pipeline.intentRevision === intentRevision) failOwnedPlayback(pipeline, "poll-failure", error, attempt);
  } finally {
    if (ownsPipeline(pipeline, attempt)) {
      attempt.pollInFlight = false;
      scheduleCompletionPoll(pipeline, attempt);
    }
  }
}

// --- XML helpers -----------------------------------------------------------
function escapeXML(str) {
  return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function escapeXMLAttribute(str) {
  return escapeXML(str).replace(/"/g, "&quot;").replace(/'/g, "&apos;");
}

function buildSSML(text, voice, speed = 0) {
  const rateAttr = (100 + speed) + "%";
  return `<speak xmlns="http://www.w3.org/2001/10/synthesis" xmlns:mstts="http://www.w3.org/2001/mstts" xmlns:emo="http://www.w3.org/2009/10/emotionml" version="1.0" xml:lang="en-US">
    <voice name="${escapeXMLAttribute(voice)}">
        <prosody rate="${rateAttr}" pitch="0%">
${escapeXML(text)}
        </prosody>
    </voice>
</speak>`;
}

