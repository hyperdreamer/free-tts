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
    activePlayback.sessionId === pipeline.sessionId && activePlayback.lifecycleRevision === pipeline.lifecycleRevision &&
    (!attempt || (pipeline.attempt === attempt && !attempt.retired && pipeline.currentIdx === attempt.idx));
}

function buildPlaybackSnapshot(pipeline = sentencePipeline) {
  const attempt = pipeline?.attempt;
  return {
    workerId, state: pipeline ? (pipeline.isPaused ? "paused" : "playing") : "idle",
    sessionId: pipeline?.sessionId ?? null, viewRevision,
    intentRevision: pipeline?.intentRevision ?? null, mediaRevision: pipeline?.mediaRevision ?? null,
    phase: attempt?.phase ?? "idle", audioStatus: attempt?.audioStatus ?? "none", error: lastError,
  };
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
  if (ownsPipeline(pipeline, attempt) && attempt.phase !== phase) { attempt.phase = phase; viewRevision++; }
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
    .then((result = { ok: true }) => sendResponse(result))
    .catch((error) => {
      logError("handling runtime message", error);
      sendResponse({ ok: false, error: error.message || "Operation failed" });
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

// --- Messages from popup ---------------------------------------------------
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (["speakSentences", "getPlaybackState", "pausePlayback", "resumePlayback", "togglePlayback"].includes(msg.action)) {
    return sendAsyncResponse(sendResponse, acceptRuntimeCommand(msg, sender));
  }
  if (msg.action === "stopPlayback") {
    return sendAsyncResponse(sendResponse, stopPlayback().then(() => ({ ok: true })));
  }

  // Media keys already paused/played the Audio element in the page; only sync
  // worker state (pipeline + control bar + context menu) without re-issuing
  // audio control.
  if (msg.action === "mediaPause") {
    return sendAsyncResponse(sendResponse, syncPausedState(true).then(() => ({ ok: true })));
  }
  if (msg.action === "mediaResume") {
    return sendAsyncResponse(sendResponse, syncPausedState(false).then(() => ({ ok: true })));
  }
  if (msg.action === "prevSentence") {
    return sendAsyncResponse(sendResponse, prevSentence().then(() => ({ ok: true })));
  }
  if (msg.action === "nextSentence") {
    return sendAsyncResponse(sendResponse, nextSentence().then(() => ({ ok: true })));
  }
  if (msg.action === "jumpToSentence") {
    return sendAsyncResponse(sendResponse, jumpToSentence(msg.idx).then(() => ({ ok: true })));
  }
  if (msg.action === "setLoopState") {
    if (sentencePipeline && sender.tab?.id === sentencePipeline.tabId) {
      sentencePipeline.loopEnabled = msg.enabled !== false;
      if (msg.enabled) sentencePipeline.repeatCurrentEnabled = false;
    }
    sendResponse({ ok: true });
    return false;
  }
  if (msg.action === "setRepeatCurrentState") {
    if (sentencePipeline && sender.tab?.id === sentencePipeline.tabId) {
      sentencePipeline.repeatCurrentEnabled = msg.enabled !== false;
      if (msg.enabled) sentencePipeline.loopEnabled = false;
    }
    sendResponse({ ok: true });
    return false;
  }
});

// --- Keyboard shortcut -----------------------------------------------------
chrome.commands.onCommand.addListener(async (command) => {
  if (command === "speak-selection") {
    // Toggle: stop playback if active, otherwise speak selection
    if (sentencePipeline || activePlayback.tabId) {
      await stopPlayback();
      return;
    }
    const ticket = startTicket();
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id || ticket.cancelled) return;
    try {
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => window.getSelection()?.toString() || document.body.innerText || "",
      });
      const text = results?.[0]?.result?.trim();
      if (text && !ticket.cancelled) await startSentenceTTS(tab.id, text, ticket);
    } catch (error) {
      // scripting permission may not be available on all URLs
      logError("handling keyboard shortcut", error);
    }
    return;
  }
});

// --- Stop playback ---------------------------------------------------------
async function stopPlayback(pipeline = sentencePipeline, cancelStarts = true) {
  if (cancelStarts && pendingStart) { pendingStart.cancelled = true; pendingStart = null; }
  const tabId = pipeline?.tabId ?? activePlayback.tabId;
  if (pipeline) {
    pipeline.cancelled = true;
    pipeline.attempt.retired = true;
    clearTimeout(pipeline.attempt.pollTimer);
    clearTimeout(pipeline.attempt.cacheTimer);
    pipeline.attempt.cacheWake?.();
    lifecycleRevision++;
    viewRevision++;
  }
  if (tabId) {
    try {
      await cleanupMediaSession(tabId);
      await chrome.scripting.executeScript({
        target: { tabId }, world: "MAIN",
        func: (owner) => {
          const record = window.__freeTtsPlayback;
          if (owner && (record?.sessionId !== owner.sessionId || record.lifecycleRevision !== owner.lifecycleRevision)) return;
          if (record) { record.desiredPaused = true; record.startSerial++; }
          const audio = window.__freeTtsAudio;
          if (audio) { audio.pause(); audio.src = ""; }
          if (record) { record.audio = null; record.audioId = null; record.audioStatus = "none"; record.audioRevision++; }
          window.__freeTtsAudio = null;
        }, args: [pipeline && { sessionId: pipeline.sessionId, lifecycleRevision: pipeline.lifecycleRevision }],
      });
      await cleanupPageHighlighting(tabId);
      await hideControlBar(tabId);
    } catch (error) { logError("stopping playback in page", error); }
  }
  clearOwnedPlayback(pipeline);
}

// --- Pause / Resume ---------------------------------------------------------
async function acceptRuntimeCommand(msg, sender) {
  if (msg.action === "getPlaybackState") return buildPlaybackSnapshot();
  if (msg.action === "speakSentences") {
    if (typeof msg.text !== "string" || !msg.text.trim()) return commandResponse(false, null, { code: "invalid-argument", message: "No text" });
    const ticket = startTicket();
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (ticket.cancelled) return commandResponse(false);
    if (!tab?.id) return commandResponse(false, null, { code: "invalid-argument", message: "No active tab" });
    return startSentenceTTS(tab.id, msg.text, ticket);
  }
  const pipeline = sentencePipeline;
  if (!pipeline || (sender.tab
    ? msg.source !== "bar" || sender.tab.id !== pipeline.tabId || msg.leaseId !== pipeline.leaseId || msg.sessionId !== pipeline.sessionId || msg.lifecycleRevision !== pipeline.lifecycleRevision
    : msg.source === "bar")) return commandResponse(false);
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
  const acceptedSnapshot = buildPlaybackSnapshot(pipeline);
  const acceptedRender = updateControlBar(pipeline, acceptedSnapshot, playbackModes(pipeline));
  const capability = workerIntent(pipeline);
  const receipt = await applyWorkerIntent(pipeline, capability);
  let currentRender = Promise.resolve();
  if (ownsPipeline(pipeline)) {
    if (!pipeline.isPaused) {
      if (pipeline._pausedAtEnd) { pipeline._pausedAtEnd = false; pipeline.attempt.retired = true; }
      void playNextSentence(pipeline);
    }
    const currentSnapshot = buildPlaybackSnapshot(pipeline);
    if (currentSnapshot.viewRevision !== acceptedSnapshot.viewRevision || currentSnapshot.state !== acceptedSnapshot.state) {
      currentRender = updateControlBar(pipeline, currentSnapshot, playbackModes(pipeline));
    }
  }
  await Promise.all([acceptedRender, currentRender]);
  return commandResponse(true, receipt || { disposition: "stale" });
}

function applyWorkerIntent(pipeline, capability) {
  const previous = pipeline.intentApplication;
  if (previous && previous.capability.intentRevision === capability.intentRevision && previous.capability.mediaBase === capability.mediaBase &&
      previous.capability.attemptId === capability.attemptId && previous.capability.paused === capability.paused) return previous.promise;
  // Retain only the latest application; redundant commands share its boundary.
  const application = pipeline.intentApplication = { capability, receipt: null, promise: null };
  application.promise = (async () => {
    await pipeline.installPromise;
    if (!ownsPipeline(pipeline)) return null;
    const [result] = await chrome.scripting.executeScript({ target: { tabId: pipeline.tabId }, world: "MAIN", func: injectWorkerIntent, args: [capability] });
    application.receipt = result?.result;
    if (ownsPipeline(pipeline)) {
      adoptPageReceipt(pipeline, pipeline.attempt, application.receipt);
      if (pipeline.attempt.audioId) scheduleCompletionPoll(pipeline, pipeline.attempt);
      if (pipeline.intentApplication === application) requestActivation(pipeline, pipeline.attempt);
    }
    return application.receipt;
  })();
  return application.promise;
}

function adoptPageReceipt(pipeline, attempt, receipt) {
  if (!ownsPipeline(pipeline, attempt) || !receipt || receipt.sessionId !== pipeline.sessionId || receipt.lifecycleRevision !== pipeline.lifecycleRevision ||
      receipt.intentRevision !== pipeline.intentRevision || receipt.mediaRevision < pipeline.mediaRevision) return;
  pipeline.isPaused = receipt.paused;
  pipeline.mediaRevision = receipt.mediaRevision;
  if (receipt.attemptId !== attempt.attemptId || receipt.audioRevision <= attempt.audioRevision) return;
  attempt.audioId = receipt.audioId;
  attempt.audioRevision = receipt.audioRevision;
  attempt.startSerial = receipt.startSerial;
  attempt.audioStatus = receipt.audioStatus;
  if (receipt.audioId && receipt.audioStatus !== "error") setAttemptPhase(pipeline, attempt, receipt.audioStatus);
  viewRevision++;
}

// The short install barrier contains no storage, fetch, highlight, or play wait.
async function installPlayback(pipeline) {
  const owner = pageOwner(pipeline);
  await chrome.scripting.executeScript({ target: { tabId: owner.tabId }, world: "MAIN", func: injectPlaybackSession, args: [owner, workerId] });
  if (!ownsPipeline(pipeline)) return;
  await chrome.scripting.executeScript({ target: { tabId: owner.tabId }, func: (binding) => {
    const previous = window.__freeTtsView;
    if (previous?.leaseId === binding.leaseId && previous.lifecycleRevision >= binding.lifecycleRevision) return;
    window.__freeTtsView = { ...binding, viewRevision: 0, barCreated: false, pendingRender: null };
  }, args: [owner] });
  if (!ownsPipeline(pipeline)) return;
  await chrome.scripting.executeScript({ target: { tabId: owner.tabId }, world: "MAIN", func: injectSelectAttempt, args: [workerIntent(pipeline)] });
}

function injectPlaybackSession(owner, workerToken) {
  const previous = window.__freeTtsPlayback;
  if (previous?.leaseId === owner.leaseId && previous.lifecycleFloor >= owner.lifecycleRevision) return;
  if (previous?.audio) { previous.audio.pause(); previous.audio.src = ""; }
  const record = window.__freeTtsPlayback = { ...owner, workerId: workerToken, lifecycleFloor: owner.lifecycleRevision,
    workerIntentRevision: 0, mediaRevision: 0, mediaSequence: 0, desiredPaused: true, attemptFloor: 0, attemptId: null,
    audio: null, audioId: null, audioStatus: "none", startSerial: 0, audioRevision: 0, audioError: null };
  window.__freeTtsAudio = null;
  record.receipt = (disposition) => ({ leaseId: record.leaseId, sessionId: record.sessionId, lifecycleRevision: record.lifecycleRevision,
    attemptId: record.attemptId, intentRevision: record.workerIntentRevision, mediaRevision: record.mediaRevision, paused: record.desiredPaused,
    audioId: record.audioId, audioStatus: record.audioStatus, startSerial: record.audioId ? record.startSerial : null, audioRevision: record.audioRevision, disposition });
  record.pause = () => {
    record.startSerial++; record.audioError = null;
    if (record.audio) { record.audio.pause(); record.audioStatus = "paused"; record.audioRevision++; }
    if ("mediaSession" in navigator) navigator.mediaSession.playbackState = "paused";
  };
  record.play = () => {
    if (record !== window.__freeTtsPlayback || record.desiredPaused || !record.audio || record.audio.ended || ["starting", "playing"].includes(record.audioStatus)) return;
    const audio = record.audio, serial = ++record.startSerial, attemptId = record.attemptId;
    const intentRevision = record.workerIntentRevision, audioId = record.audioId;
    record.audioStatus = "starting"; record.audioError = null; record.audioRevision++;
    if ("mediaSession" in navigator) navigator.mediaSession.playbackState = "paused";
    const live = () => record === window.__freeTtsPlayback && record.audio === audio && record.audioId === audioId &&
      record.attemptId === attemptId && record.workerIntentRevision === intentRevision && record.startSerial === serial && !record.desiredPaused;
    const settled = (error) => {
      if (!live()) return;
      record.audioStatus = error ? "error" : "playing";
      record.audioError = error ? { code: "play-rejection", message: error.message || String(error) } : null;
      record.audioRevision++;
      if (!error && "mediaSession" in navigator) navigator.mediaSession.playbackState = "playing";
      window.postMessage({ __freeTtsMedia: true, action: "audioPlaybackEvent", ...record.receipt("applied"), audioError: record.audioError }, "*");
    };
    try { Promise.resolve(audio.play()).then(() => settled(null), settled); } catch (error) { settled(error); }
  };
}

function injectSelectAttempt(capability) {
  const record = window.__freeTtsPlayback;
  if (!record || record.leaseId !== capability.leaseId || record.sessionId !== capability.sessionId || record.lifecycleRevision !== capability.lifecycleRevision || capability.attemptId <= record.attemptFloor) return;
  record.startSerial++;
  if (record.audio) { record.audio.pause(); record.audio.src = ""; record.audioRevision++; }
  record.attemptId = record.attemptFloor = capability.attemptId;
  record.audio = window.__freeTtsAudio = null;
  record.audioId = null; record.audioStatus = "none";
}

function injectWorkerIntent(capability) {
  const record = window.__freeTtsPlayback;
  if (!record) return null;
  if (record.leaseId !== capability.leaseId || record.sessionId !== capability.sessionId || record.lifecycleRevision !== capability.lifecycleRevision || capability.intentRevision < record.workerIntentRevision) return record.receipt("stale");
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
  if (!record) return null;
  if (record.leaseId !== capability.leaseId || record.sessionId !== capability.sessionId || record.lifecycleRevision !== capability.lifecycleRevision ||
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
async function syncPausedState(paused) {
  if (!sentencePipeline || sentencePipeline.isPaused === paused) return;
  // If resuming from a paused-at-end state, re-fetch the sentence
  if (!paused && sentencePipeline._pausedAtEnd) {
    sentencePipeline._pausedAtEnd = false;
    sentencePipeline.isPaused = false;
    await updateControlBar(sentencePipeline.tabId, false);
    await playNextSentence();
    return;
  }
  sentencePipeline.isPaused = paused;
  await updateControlBar(sentencePipeline.tabId, paused);
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
function injectMediaRelay() {
  if (window.__freeTtsRelayInstalled) return;
  window.__freeTtsRelayInstalled = true;
  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    const data = event.data;
    if (!data || data.__freeTtsMedia !== true || typeof data.action !== "string") return;
    try { chrome.runtime.sendMessage({ action: data.action }).catch(() => {}); } catch {}
  });
}

// Media session set up in the MAIN world.
function injectMediaSession() {
  if (!("mediaSession" in navigator)) return;
  const record = window.__freeTtsPlayback;
  const send = (action) => {
    try { window.postMessage({ __freeTtsMedia: true, action }, "*"); } catch {}
  };

  // Play/pause: directly control the Audio element for latency (no service
  // worker round-trip), then notify the worker so it syncs sentencePipeline
  // state and the floating control bar / context menu.
  navigator.mediaSession.setActionHandler("play", () => {
    if (!record || record !== window.__freeTtsPlayback) return;
    record.desiredPaused = false;
    record.play();
    send("mediaResume");
  });
  navigator.mediaSession.setActionHandler("pause", () => {
    if (!record || record !== window.__freeTtsPlayback) return;
    record.desiredPaused = true;
    record.pause();
    send("mediaPause");
  });
  // Prev/next/stop: bridge to the service worker via the isolated-world relay.
  navigator.mediaSession.setActionHandler("previoustrack", () => send("prevSentence"));
  navigator.mediaSession.setActionHandler("nexttrack", () => send("nextSentence"));
  navigator.mediaSession.setActionHandler("stop", () => send("stopPlayback"));

  navigator.mediaSession.metadata = new MediaMetadata({
    title: "free-tts",
    artist: "Text-to-Speech",
    album: document.title || "Web Page",
  });
  navigator.mediaSession.playbackState = "paused";

  // Keepalive: ping the worker so prev/next/stop stay responsive while paused.
  if (window.__freeTtsKeepalive) clearInterval(window.__freeTtsKeepalive);
  window.__freeTtsKeepalive = setInterval(() => send("ping"), 25000);
}

function clearMediaSession() {
  if (window.__freeTtsKeepalive) {
    clearInterval(window.__freeTtsKeepalive);
    window.__freeTtsKeepalive = null;
  }
  if (!("mediaSession" in navigator)) return;
  for (const action of ["play", "pause", "previoustrack", "nexttrack", "stop"]) {
    try { navigator.mediaSession.setActionHandler(action, null); } catch {}
  }
  navigator.mediaSession.metadata = null;
  try { navigator.mediaSession.playbackState = "none"; } catch {}
}

async function setupMediaSession(tabId) {
  try {
    // Relay first (isolated world), then handlers (main world).
    await chrome.scripting.executeScript({ target: { tabId }, func: injectMediaRelay });
    await chrome.scripting.executeScript({ target: { tabId }, world: "MAIN", func: injectMediaSession });
  } catch (error) {
    logError("setting up media session", error);
  }
}

async function cleanupMediaSession(tabId) {
  try {
    await chrome.scripting.executeScript({ target: { tabId }, world: "MAIN", func: clearMediaSession });
  } catch (error) {
    logError("cleaning up media session", error);
  }
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
  try {
    const [result] = await chrome.scripting.executeScript({
      target: { tabId: owner.tabId },
      func: (binding, supplied, suppliedModes) => {
        const view = window.__freeTtsView;
        if (!view || view.leaseId !== binding.leaseId || view.sessionId !== binding.sessionId || view.lifecycleRevision !== binding.lifecycleRevision) return "deferred";
        if (view.barCreated) return "rendered";
        const pending = view.pendingRender;
        const latest = pending && pending.snapshot.viewRevision > supplied.viewRevision ? pending : { snapshot: supplied, modes: suppliedModes };
        const sendBarMessage = message => chrome.runtime.sendMessage({ ...message, source: "bar", leaseId: binding.leaseId,
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
          if (loopCheck.checked) {
            const repeatCurrent = document.getElementById("free-tts-repeat-current");
            if (repeatCurrent) repeatCurrent.checked = false;
          }
          window.__freeTtsLoop = loopCheck.checked;
          chrome.runtime.sendMessage({ action: "setLoopState", enabled: loopCheck.checked });
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
          if (repeatCurrentCheck.checked) {
            const loop = document.getElementById("free-tts-loop");
            if (loop) loop.checked = false;
          }
          chrome.runtime.sendMessage({ action: "setRepeatCurrentState", enabled: repeatCurrentCheck.checked });
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
          if (e.target.tagName === "BUTTON") return;  // don't drag when clicking buttons
          drag = true;
          startX = e.clientX; startY = e.clientY;
          const rect = bar.getBoundingClientRect();
          startLeft = rect.left; startTop = rect.top;
          e.preventDefault();
        });
        const onMouseMove = (e) => {
          if (!drag) return;
          bar.style.transform = "none";
          bar.style.left = (startLeft + e.clientX - startX) + "px";
          bar.style.top = (startTop + e.clientY - startY) + "px";
        };
        const onDragMouseUp = () => { drag = false; };
        document.addEventListener("mousemove", onMouseMove);
        document.addEventListener("mouseup", onDragMouseUp);

        // --- Button handlers ---
        bar.querySelector("#free-tts-prev").addEventListener("click", () => chrome.runtime.sendMessage({ action: "prevSentence" }));
        bar.querySelector("#free-tts-next").addEventListener("click", () => chrome.runtime.sendMessage({ action: "nextSentence" }));
        bar.querySelector("#free-tts-toggle").addEventListener("click", () => {
          sendBarMessage({ action: "togglePlayback" });
        });
        bar.querySelector("#free-tts-close").addEventListener("click", () => chrome.runtime.sendMessage({ action: "stopPlayback" }));

        // --- ESC key stops playback ---
        const onKeyDown = (e) => {
          if (e.key === "Escape") {
            chrome.runtime.sendMessage({ action: "stopPlayback" });
          }
        };
        document.addEventListener("keydown", onKeyDown);

        // --- Double-click to jump to sentence ---
        const onDoubleClick = () => {
          const sel = window.getSelection();
          if (!sel || !sel.toString().trim()) return;
          const word = sel.toString().trim();
          const sents = window.__freeTtsSentences;
          if (!sents) return;
          for (let i = 0; i < sents.length; i++) {
            if (sents[i].includes(word)) {
              chrome.runtime.sendMessage({ action: "jumpToSentence", idx: i });
              return;
            }
          }
        };
        document.addEventListener("dblclick", onDoubleClick);

        // --- Drag-select to jump to sentence ---
        const onSelectionMouseUp = () => {
          setTimeout(() => {
            const sel = window.getSelection();
            if (!sel || sel.isCollapsed) return;
            const text = sel.toString().trim();
            if (!text) return;
            const sents = window.__freeTtsSentences;
            if (!sents) return;
            for (let i = 0; i < sents.length; i++) {
              if (sents[i].includes(text)) {
                chrome.runtime.sendMessage({ action: "jumpToSentence", idx: i });
                return;
              }
            }
          }, 100);
        };
        document.addEventListener("mouseup", onSelectionMouseUp);

        window.__freeTtsCleanupControlBar = () => {
          document.removeEventListener("mousemove", onMouseMove);
          document.removeEventListener("mouseup", onDragMouseUp);
          document.removeEventListener("dblclick", onDoubleClick);
          document.removeEventListener("mouseup", onSelectionMouseUp);
          document.removeEventListener("keydown", onKeyDown);
          document.getElementById("free-tts-bar")?.remove();
          delete window.__freeTtsCleanupControlBar;
        };
        return "rendered";
      },
      args: [pageOwner(owner), snapshot, modes],
    });
    return result?.result;
  } catch (error) {
    logError("showing control bar", error);
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
        if (!view || view.leaseId !== binding.leaseId || view.sessionId !== binding.sessionId || view.lifecycleRevision !== binding.lifecycleRevision) return "deferred";
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
    return result?.result;
  } catch (error) {
    logError("updating control bar", error);
    return "deferred";
  }
}

async function hideControlBar(tabId) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        if (typeof window.__freeTtsCleanupControlBar === "function") {
          window.__freeTtsCleanupControlBar();
        } else {
          document.getElementById("free-tts-bar")?.remove();
        }
      },
    });
  } catch (error) {
    logError("hiding control bar", error);
  }
}

// --- Prev/next sentence ----------------------------------------------------
async function prevSentence() {
  if (!sentencePipeline || sentencePipeline.currentIdx <= 0) return;
  // currentIdx is the sentence currently playing (increment happens only after
  // playback ends), so go back one to reach the previous sentence.
  sentencePipeline.currentIdx -= 1;
  if (sentencePipeline.currentIdx < 0) sentencePipeline.currentIdx = 0;
  sentencePipeline.isPaused = false;
  // Stop current audio
  try {
    await chrome.scripting.executeScript({
      target: { tabId: sentencePipeline.tabId },
      world: "MAIN",
      func: () => { const s = window.__freeTtsAudio; if (s) { s.pause(); s.src = ""; } },
    });
  } catch (error) {
    logError("stopping current audio before previous sentence", error);
  }
  updateControlBar(sentencePipeline.tabId, false);
  await playNextSentence();
}

async function nextSentence() {
  if (!sentencePipeline) return;
  sentencePipeline.isPaused = false;
  sentencePipeline.currentIdx++;  // skip to next
  try {
    await chrome.scripting.executeScript({
      target: { tabId: sentencePipeline.tabId },
      world: "MAIN",
      func: () => { const s = window.__freeTtsAudio; if (s) { s.pause(); s.src = ""; } },
    });
  } catch (error) {
    logError("stopping current audio before next sentence", error);
  }
  updateControlBar(sentencePipeline.tabId, false);
  await playNextSentence();
}

// --- Jump to sentence -------------------------------------------------------
async function jumpToSentence(idx) {
  if (!sentencePipeline) return;
  if (idx < 0 || idx >= sentencePipeline.sentences.length) return;
  sentencePipeline.currentIdx = idx;
  sentencePipeline.isPaused = false;
  try {
    await chrome.scripting.executeScript({
      target: { tabId: sentencePipeline.tabId },
      world: "MAIN",
      func: () => { const s = window.__freeTtsAudio; if (s) { s.pause(); s.src = ""; } },
    });
  } catch (error) {
    logError("stopping current audio before sentence jump", error);
  }
  updateControlBar(sentencePipeline.tabId, false);
  await playNextSentence();
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
  const { highlightColor } = await chrome.storage.sync.get({ highlightColor: "#fff3cd" });
  if (!ownsPipeline(pipeline)) return;
  const safeHighlightColor = normalizeColor(highlightColor);
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (sents, color, owner) => {
        const view = window.__freeTtsView;
        if (!view || view.leaseId !== owner.leaseId || view.sessionId !== owner.sessionId || view.lifecycleRevision !== owner.lifecycleRevision) return;
        window.__freeTtsSentences = sents;
        window.__freeTtsColor = color;
      },
      args: [sentences, safeHighlightColor, pageOwner(pipeline)],
    });
  } catch (error) {
    logError("initializing page highlighting", error);
  }
}

async function highlightCurrentSentence(tabId, idx, pipeline = sentencePipeline, attempt = pipeline?.attempt) {
  if (!ownsPipeline(pipeline, attempt)) return;
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (idx, owner, attemptId) => {
        const view = window.__freeTtsView;
        if (!view || view.leaseId !== owner.leaseId || view.sessionId !== owner.sessionId || view.lifecycleRevision !== owner.lifecycleRevision || attemptId < (view.attemptFloor || 0)) return;
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
    logError("highlighting current sentence", error);
  }
}

async function cleanupPageHighlighting(tabId) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        document.querySelectorAll(".free-tts-highlight-overlay").forEach(el => el.remove());
        delete window.__freeTtsSentences;
        delete window.__freeTtsColor;
      },
    });
  } catch (error) {
    logError("cleaning page highlighting", error);
  }
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
  if (sentencePipeline) void stopPlayback(sentencePipeline, false);
  if (!pageLeases.has(tabId)) pageLeases.set(tabId, crypto.randomUUID());
  const pipeline = sentencePipeline = {
    tabId, leaseId: pageLeases.get(tabId), sessionId: crypto.randomUUID(), lifecycleRevision: ++lifecycleRevision,
    sentences, currentIdx: 0, cache: new Map(), voice: null, serverUrl: null, speed: 0, isPaused: false,
    loopEnabled: true, repeatCurrentEnabled: false, intentRevision: 1, localIntentFloor: 1,
    mediaRevision: 0, lastMediaSequence: 0, nextAttemptId: 0, attempt: null, cancelled: false, initialized: false,
  };
  activePlayback = pageOwner(pipeline);
  lastError = null;
  viewRevision++;
  createAttempt(pipeline, "initializing");
  const capability = workerIntent(pipeline);
  pipeline.installPromise = installPlayback(pipeline);
  pipeline.initializationPromise = initializePlayback(pipeline).catch(error => {
    if (ownsPipeline(pipeline)) { logError("initializing playback", error); lastError = { code: "settings-failure", message: error.message }; void stopPlayback(pipeline, false); }
  });
  pipeline.installPromise.then(() => {
    if (ownsPipeline(pipeline)) void showControlBar(pipeline, buildPlaybackSnapshot(pipeline), playbackModes(pipeline));
  }).catch(error => logError("installing playback", error));
  const receipt = await applyWorkerIntent(pipeline, capability);
  if (ownsPipeline(pipeline)) await updateControlBar(pipeline, buildPlaybackSnapshot(pipeline), playbackModes(pipeline));
  return commandResponse(true, receipt);
}

function createAttempt(pipeline, phase = "loading") {
  if (pipeline.attempt) { pipeline.attempt.retired = true; clearTimeout(pipeline.attempt.pollTimer); }
  return pipeline.attempt = { attemptId: ++pipeline.nextAttemptId, idx: pipeline.currentIdx, phase, retired: false,
    dataUrl: null, loadPromise: null, activationRequest: null, audioId: null, audioStatus: "none", audioRevision: -1,
    startSerial: null, pollTimer: null, pollInFlight: false };
}

async function initializePlayback(pipeline) {
  const settings = await chrome.storage.sync.get({ serverUrl: DEFAULT_SERVER, voice: "en-US-AvaMultilingualNeural", speed: 0 });
  if (!ownsPipeline(pipeline)) return;
  pipeline.serverUrl = normalizeServerUrl(settings.serverUrl); pipeline.voice = settings.voice; pipeline.speed = normalizeSpeed(settings.speed);
  await pipeline.installPromise;
  if (!ownsPipeline(pipeline)) return;
  await setupMediaSession(pipeline.tabId);
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
    .catch(error => logError(`preloading sentence ${idx}`, error));
}

async function playNextSentence(pipeline = sentencePipeline, attempt = null) {
  if (!ownsPipeline(pipeline) || !pipeline.initialized) return;
  const { sentences, currentIdx, cache, tabId, voice, serverUrl, speed } = pipeline;
  const startIdx = currentIdx;
  if (currentIdx >= sentences.length) {
    if (pipeline.repeatCurrentEnabled || pipeline.loopEnabled) { pipeline.currentIdx = 0; return playNextSentence(pipeline); }
    // Keep finalization bound to this owner across the existing cleanup awaits.
    const clearPlayback = () => clearOwnedPlayback(pipeline);
    await cleanupMediaSession(tabId);
    await cleanupPageHighlighting(tabId);
    await hideControlBar(tabId);
    clearPlayback();
    return;
  }
  if (!attempt) {
    attempt = pipeline.attempt;
    if (arguments.length === 0 || attempt.retired || attempt.idx !== currentIdx) {
      attempt = createAttempt(pipeline);
      pipeline.intentRevision++; pipeline.localIntentFloor = pipeline.intentRevision; viewRevision++;
      await chrome.scripting.executeScript({ target: { tabId }, world: "MAIN", func: injectSelectAttempt, args: [workerIntent(pipeline)] });
      if (!ownsPipeline(pipeline, attempt)) return;
      await applyWorkerIntent(pipeline, workerIntent(pipeline));
    }
  }
  if (!ownsPipeline(pipeline, attempt)) return;
  if (!attempt.loadPromise) attempt.loadPromise = (async () => {
    let dataUrl = cache.get(startIdx);
    const start = Date.now();
    while (!dataUrl && Date.now() - start < 10000) {
      await new Promise(resolve => { attempt.cacheWake = resolve; attempt.cacheTimer = setTimeout(resolve, 200); });
      if (!ownsPipeline(pipeline, attempt)) return;
      dataUrl = cache.get(startIdx);
    }
    if (!dataUrl) {
      dataUrl = await fetchSentenceAudio(serverUrl, voice, speed, sentences[startIdx]);
      if (!ownsPipeline(pipeline, attempt)) return;
      cache.set(startIdx, dataUrl);
    }
    if (!ownsPipeline(pipeline, attempt)) return;
    for (let idx = startIdx + 1; idx < startIdx + 1 + PRELOAD_AHEAD && idx < sentences.length; idx++) {
      if (!cache.has(idx)) preloadSentence(pipeline, idx);
    }
    await highlightCurrentSentence(tabId, startIdx, pipeline, attempt);
    if (!ownsPipeline(pipeline, attempt)) return;
    attempt.dataUrl = dataUrl;
    setAttemptPhase(pipeline, attempt, "ready");
  })();
  try {
    await attempt.loadPromise;
    if (ownsPipeline(pipeline, attempt) && !pipeline.isPaused) requestActivation(pipeline, attempt);
  } catch (error) {
    if (ownsPipeline(pipeline, attempt)) { logError(`fetching sentence ${startIdx}`, error); lastError = { code: "load-failure", message: error.message }; void stopPlayback(pipeline, false); }
  }
}

function requestActivation(pipeline, attempt) {
  if (!ownsPipeline(pipeline, attempt) || pipeline.isPaused || !attempt.dataUrl || ["starting", "playing"].includes(attempt.audioStatus)) return;
  // Loading and intent application race independently. Whichever finishes last
  // continues the ready attempt, but never ahead of its confirmed running intent.
  const application = pipeline.intentApplication, receipt = application?.receipt;
  if (!receipt || application.capability.attemptId !== attempt.attemptId || receipt.leaseId !== pipeline.leaseId ||
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
      adoptPageReceipt(pipeline, attempt, result?.result);
      if (attempt.audioId) scheduleCompletionPoll(pipeline, attempt);
    }).catch(error => {
      if (ownsPipeline(pipeline, attempt) && pipeline.intentRevision === capability.intentRevision) { logError("activating audio", error); lastError = { code: "page-operation-failure", message: error.message }; void stopPlayback(pipeline, false); }
    }).finally(() => { if (ownsPipeline(pipeline, attempt) && attempt.activationRequest === request) attempt.activationRequest = null; });
}

function scheduleCompletionPoll(pipeline, attempt) {
  if (!ownsPipeline(pipeline, attempt) || attempt.pollTimer || attempt.pollInFlight || attempt.phase === "ended") return;
  attempt.pollTimer = setTimeout(() => { attempt.pollTimer = null; void pollCompletion(pipeline, attempt); }, 500);
}

async function pollCompletion(pipeline, attempt) {
  if (!ownsPipeline(pipeline, attempt)) return;
  attempt.pollInFlight = true;
  try {
    const [result] = await chrome.scripting.executeScript({ target: { tabId: pipeline.tabId }, world: "MAIN", func: (owner, attemptId, audioId) => {
      const record = window.__freeTtsPlayback;
      if (!record || record.leaseId !== owner.leaseId || record.sessionId !== owner.sessionId || record.lifecycleRevision !== owner.lifecycleRevision || record.attemptId !== attemptId || record.audioId !== audioId || !record.audio) return { owned: false };
      return { ...record.receipt("applied"), owned: true, ended: record.audio.ended };
    }, args: [pageOwner(pipeline), attempt.attemptId, attempt.audioId] });
    if (!ownsPipeline(pipeline, attempt)) return;
    if (result?.result?.owned && result.result.ended) {
      setAttemptPhase(pipeline, attempt, "ended");
      if (pipeline.isPaused) pipeline._pausedAtEnd = true;
      else { attempt.retired = true; if (!pipeline.repeatCurrentEnabled) pipeline.currentIdx++; void playNextSentence(pipeline); }
    }
  } catch (error) {
    if (ownsPipeline(pipeline, attempt)) logError("polling playback", error);
  } finally {
    if (ownsPipeline(pipeline, attempt)) { attempt.pollInFlight = false; scheduleCompletionPoll(pipeline, attempt); }
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

