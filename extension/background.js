// free-tts background service worker
// Sentence-by-sentence TTS with pre-caching and highlight bar.

const DEFAULT_SERVER = "http://localhost:5000";
const PRELOAD_AHEAD = 2;  // pre-fetch this many sentences ahead
const FETCH_TIMEOUT_MS = 120000;

// Pipeline state
let activePlayback = { tabId: null };
let sentencePipeline = null;  // { sentences, currentIdx, cache, tabId, voice, serverUrl, speed, isPaused, loopEnabled, _pausedAtEnd }

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

// --- Messages from popup ---------------------------------------------------
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === "speakSentences") {
    return sendAsyncResponse(sendResponse, chrome.tabs.query({ active: true, currentWindow: true }).then(([tab]) => {
      if (tab?.id) {
        return startSentenceTTS(tab.id, msg.text).then(() => ({ ok: true }));
      }
      return { ok: false, error: "No active tab" };
    }));
  }
  if (msg.action === "stopPlayback") {
    return sendAsyncResponse(sendResponse, stopPlayback().then(() => ({ ok: true })));
  }
  if (msg.action === "getPlaybackState") {
    if (sentencePipeline?.isPaused) sendResponse({ state: "paused" });
    else if (sentencePipeline) sendResponse({ state: "playing" });
    else sendResponse({ state: "idle" });
    return false;
  }
  if (msg.action === "pausePlayback") {
    return sendAsyncResponse(sendResponse, pausePlayback().then(() => ({ ok: true })));
  }
  if (msg.action === "resumePlayback") {
    return sendAsyncResponse(sendResponse, resumePlayback().then(() => ({ ok: true })));
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
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id) return;
    try {
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => window.getSelection()?.toString() || document.body.innerText || "",
      });
      const text = results?.[0]?.result?.trim();
      if (text) await startSentenceTTS(tab.id, text);
    } catch (error) {
      // scripting permission may not be available on all URLs
      logError("handling keyboard shortcut", error);
    }
    return;
  }
});

// --- Stop playback ---------------------------------------------------------
async function stopPlayback() {
  sentencePipeline = null;
  if (activePlayback.tabId) {
    try {
      await cleanupMediaSession(activePlayback.tabId);
      await chrome.scripting.executeScript({
        target: { tabId: activePlayback.tabId },
        world: "MAIN",
        func: () => { const s = window.__freeTtsAudio; if (s) { s.pause(); s.src = ""; } },
      });
      await cleanupPageHighlighting(activePlayback.tabId);
      await hideControlBar(activePlayback.tabId);
    } catch (error) {
      logError("stopping playback in page", error);
    }
  }
  clearPlayback();
}

// --- Pause / Resume ---------------------------------------------------------
async function pausePlayback() {
  if (!sentencePipeline) return;
  sentencePipeline.isPaused = true;
  // Stop audio in the tab
  try {
    await chrome.scripting.executeScript({
      target: { tabId: sentencePipeline.tabId },
      world: "MAIN",
      func: () => { const s = window.__freeTtsAudio; if (s) { s.pause(); } },
    });
  } catch (error) {
    logError("pausing playback in page", error);
  }
  await updateControlBar(sentencePipeline.tabId, true);
}

async function resumePlayback() {
  if (!sentencePipeline || !sentencePipeline.isPaused) return;
  sentencePipeline.isPaused = false;
  await updateControlBar(sentencePipeline.tabId, false);
  // If we paused after the current sentence finished, the page's audio element is
  // already terminated — re-fetch and play the sentence instead of trying to play a dead element.
  if (sentencePipeline._pausedAtEnd) {
    sentencePipeline._pausedAtEnd = false;
    await playNextSentence();
    return;
  }
  try {
    await chrome.scripting.executeScript({
      target: { tabId: sentencePipeline.tabId },
      world: "MAIN",
      func: () => {
        const audio = window.__freeTtsAudio;
        if (audio && !audio.ended) audio.play().catch(() => {});
      },
    });
  } catch (error) {
    logError("resuming playback in page", error);
  }
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
  const send = (action) => {
    try { window.postMessage({ __freeTtsMedia: true, action }, "*"); } catch {}
  };

  // Play/pause: directly control the Audio element for latency (no service
  // worker round-trip), then notify the worker so it syncs sentencePipeline
  // state and the floating control bar / context menu.
  navigator.mediaSession.setActionHandler("play", () => {
    const a = window.__freeTtsAudio;
    if (a && !a.ended) {
      a.play().catch(() => {});
      navigator.mediaSession.playbackState = "playing";
    }
    send("mediaResume");
  });
  navigator.mediaSession.setActionHandler("pause", () => {
    const a = window.__freeTtsAudio;
    if (a) {
      a.pause();
      navigator.mediaSession.playbackState = "paused";
    }
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
  navigator.mediaSession.playbackState = "playing";

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

async function showControlBar(tabId, isPaused, loopEnabled = true) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (paused, initialLoopEnabled) => {
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
          button.textContent = text;
          bar.appendChild(button);
        });
        // Loop checkbox — checked by default
        const loopLabel = document.createElement("label");
        loopLabel.style.cssText = "display:flex;align-items:center;gap:2px;font-size:12px;color:#555;padding:2px 4px;cursor:pointer;border-left:1px solid #ddd;margin-left:2px;";
        const loopCheck = document.createElement("input");
        loopCheck.type = "checkbox";
        loopCheck.id = "free-tts-loop";
        loopCheck.checked = initialLoopEnabled !== false;
        loopCheck.style.cssText = "margin:0;cursor:pointer;";
        loopCheck.addEventListener("change", () => {
          window.__freeTtsLoop = loopCheck.checked;
          chrome.runtime.sendMessage({ action: "setLoopState", enabled: loopCheck.checked });
        });
        loopLabel.appendChild(loopCheck);
        loopLabel.appendChild(document.createTextNode("↻"));
        bar.appendChild(loopLabel);
        window.__freeTtsLoop = initialLoopEnabled !== false;
        bar.style.cssText = "position:fixed;top:56px;right:16px;background:rgba(255,255,255,0.85);backdrop-filter:blur(8px);border-radius:6px;box-shadow:0 1px 4px rgba(0,0,0,0.06);padding:2px 4px;z-index:999999;display:flex;gap:0;font-family:-apple-system,BlinkMacSystemFont,sans-serif;cursor:move;user-select:none;";
        bar.querySelectorAll("button").forEach(b => {
          b.style.cssText = "border:none;background:none;font-size:14px;cursor:pointer;padding:2px 4px;border-radius:4px;color:#555;transition:background 0.15s;line-height:1;";
          b.addEventListener("mouseenter", () => b.style.background = "#f0f0f0");
          b.addEventListener("mouseleave", () => b.style.background = "none");
        });
        document.body.appendChild(bar);

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
          const btn = document.getElementById("free-tts-toggle");
          if (btn.textContent === "⏸") chrome.runtime.sendMessage({ action: "pausePlayback" });
          else chrome.runtime.sendMessage({ action: "resumePlayback" });
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
      },
      args: [isPaused, loopEnabled],
    });
  } catch (error) {
    logError("showing control bar", error);
  }
}

async function updateControlBar(tabId, isPaused) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (paused) => {
        const btn = document.getElementById("free-tts-toggle");
        if (btn) btn.textContent = paused ? "▶" : "⏸";
      },
      args: [isPaused],
    });
  } catch (error) {
    logError("updating control bar", error);
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
async function initPageHighlighting(tabId, sentences) {
  const { highlightColor } = await chrome.storage.sync.get({ highlightColor: "#fff3cd" });
  const safeHighlightColor = normalizeColor(highlightColor);
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (sents, color) => {
        window.__freeTtsSentences = sents;
        window.__freeTtsColor = color;
      },
      args: [sentences, safeHighlightColor],
    });
  } catch (error) {
    logError("initializing page highlighting", error);
  }
}

async function highlightCurrentSentence(tabId, idx) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (idx) => {
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
      args: [idx],
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
async function startSentenceTTS(tabId, text) {
  if (!tabId || !text?.trim()) return;

  const { serverUrl, voice, speed } = await chrome.storage.sync.get({
    serverUrl: DEFAULT_SERVER,
    voice: "en-US-AvaMultilingualNeural",
    speed: 0,
  });

  const sentences = splitSentences(text);
  if (sentences.length === 0) return;

  await stopPlayback();
  const cache = new Map();  // idx → dataUrl
  sentencePipeline = {
    sentences,
    currentIdx: 0,
    cache,
    tabId,
    voice,
    serverUrl: normalizeServerUrl(serverUrl),
    speed: normalizeSpeed(speed),
    isPaused: false,
    loopEnabled: true,
  };
  activePlayback = { tabId };
  await showControlBar(tabId, false, sentencePipeline.loopEnabled);

  // Set up Media Session for system media keys (play/pause/prev/next)
  await setupMediaSession(tabId);

  // Show first sentence immediately
  await initPageHighlighting(tabId, sentences);

  // Pre-fetch first few sentences
  const preloadCount = Math.min(PRELOAD_AHEAD, sentences.length);
  for (let i = 0; i < preloadCount; i++) {
    fetchSentenceAudio(serverUrl, voice, speed, sentences[i])
      .then(url => cache.set(i, url))
      .catch((error) => logError(`preloading sentence ${i}`, error));
  }

  // Wait a bit for first sentence to cache, then play
  await playNextSentence();
}

async function playNextSentence() {
  if (!sentencePipeline) return;
  const { sentences, currentIdx, cache, tabId, voice, serverUrl, speed } = sentencePipeline;
  const startIdx = currentIdx;  // guard: exit if external action changed index
  if (currentIdx >= sentences.length) {
    // Check loop checkbox state
    const loopEnabled = await getLoopState(tabId);
    if (loopEnabled) {
      sentencePipeline.currentIdx = 0;
      await playNextSentence();
      return;
    }
    await cleanupPageHighlighting(tabId);
    await hideControlBar(tabId);
    clearPlayback();
    return;
  }

  // Wait for current sentence to be cached (with timeout)
  let dataUrl = cache.get(currentIdx);
  if (!dataUrl) {
    const start = Date.now();
    while (!dataUrl && Date.now() - start < 10000) {
      await new Promise(r => setTimeout(r, 200));
      dataUrl = cache.get(currentIdx);
      if (!sentencePipeline || sentencePipeline.currentIdx !== startIdx) return;  // cancelled or index changed
    }
  }
  if (!dataUrl) {
    // Fallback: fetch directly
    try {
      dataUrl = await fetchSentenceAudio(serverUrl, voice, speed, sentences[currentIdx]);
      if (!sentencePipeline || sentencePipeline.currentIdx !== startIdx) return;  // index changed during fetch
      cache.set(currentIdx, dataUrl);
    } catch (error) {
      logError(`fetching sentence ${currentIdx}`, error);
      clearPlayback();
      return;
    }
  }

  // Pre-fetch upcoming sentences
  for (let i = currentIdx + 1; i < currentIdx + 1 + PRELOAD_AHEAD && i < sentences.length; i++) {
    if (!cache.has(i)) {
      fetchSentenceAudio(serverUrl, voice, speed, sentences[i])
        .then(url => cache.set(i, url))
        .catch((error) => logError(`preloading sentence ${i}`, error));
    }
  }

  // Highlight current and scroll
  await highlightCurrentSentence(tabId, currentIdx);
  if (!sentencePipeline || sentencePipeline.currentIdx !== startIdx) return;  // index changed during highlight

  // Play
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: (url) => {
        if (window.__freeTtsAudio) { window.__freeTtsAudio.pause(); }
        const a = new Audio(url);
        a.play().catch(() => {});
        window.__freeTtsAudio = a;
        if ("mediaSession" in navigator) navigator.mediaSession.playbackState = "playing";
      },
      args: [dataUrl],
    });

    // Wait for audio to end (polling approach)
    await new Promise((resolve) => {
      const check = setInterval(async () => {
        if (!sentencePipeline || sentencePipeline.currentIdx !== startIdx) { clearInterval(check); resolve(); return; }
        try {
          const [result] = await chrome.scripting.executeScript({
            target: { tabId },
            world: "MAIN",
            func: () => window.__freeTtsAudio?.ended ?? true,
          });
          if (result?.result) { clearInterval(check); resolve(); }
        } catch { clearInterval(check); resolve(); }
      }, 500);
    });

    // Move to next sentence or pause — only if index hasn't been changed externally
    if (sentencePipeline && sentencePipeline.currentIdx === startIdx) {
      if (sentencePipeline.isPaused) {
        // Don't advance — mark that we paused at end-of-sentence so resume can re-fetch
        sentencePipeline._pausedAtEnd = true;
      } else {
        sentencePipeline.currentIdx++;
        await playNextSentence();
      }
    }
  } catch (error) {
    logError(`playing sentence ${currentIdx}`, error);
    clearPlayback();
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

