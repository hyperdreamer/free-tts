// free-tts background service worker
// Sentence-by-sentence TTS with pre-caching and highlight bar.

const DEFAULT_SERVER = "http://localhost:5000";
const PRELOAD_AHEAD = 2;  // pre-fetch this many sentences ahead

// Pipeline state
let activePlayback = { tabId: null };
let playbackTimeout = null;
let sentencePipeline = null;  // { sentences, currentIdx, cache, tabId, voice, serverUrl, speed, isPaused }

function logError(context, error) {
  console.error(`free-tts background: ${context}`, error);
}

function callContextMenuApi(method, ...args) {
  return new Promise((resolve, reject) => {
    try {
      chrome.contextMenus[method](...args, () => {
        const error = chrome.runtime.lastError;
        if (error) reject(new Error(error.message));
        else resolve();
      });
    } catch (error) {
      reject(error);
    }
  });
}

function clearPlayback() {
  activePlayback = { tabId: null };
  if (playbackTimeout) { clearTimeout(playbackTimeout); playbackTimeout = null; }
  sentencePipeline = null;
  updateStopMenu();
}

function updateStopMenu() {
  const visible = !!activePlayback.tabId;
  const paused = sentencePipeline?.isPaused || false;
  callContextMenuApi("update", "stop-speaking", { visible }).catch(() => {});
  callContextMenuApi("update", "pause-reading", { visible: visible && !paused }).catch(() => {});
  callContextMenuApi("update", "resume-reading", { visible: visible && paused }).catch(() => {});
}

async function createContextMenus() {
  await callContextMenuApi("removeAll");
  await callContextMenuApi("create", {
    id: "speak-selection",
    title: "Speak this",
    contexts: ["selection"],
  });
  await callContextMenuApi("create", {
    id: "stop-speaking",
    title: "Stop speaking",
    contexts: ["page"],
    visible: false,
  });
  await callContextMenuApi("create", {
    id: "pause-reading",
    title: "Pause reading",
    contexts: ["page"],
    visible: false,
  });
  await callContextMenuApi("create", {
    id: "resume-reading",
    title: "Resume reading",
    contexts: ["page"],
    visible: false,
  });
}

// --- Context menu ----------------------------------------------------------
chrome.runtime.onInstalled.addListener(() => {
  createContextMenus().catch((error) => logError("creating context menus on install", error));
});

chrome.runtime.onStartup.addListener(() => {
  createContextMenus().catch((error) => logError("creating context menus on startup", error));
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  try {
    if (info.menuItemId === "speak-selection") {
      const text = info.selectionText?.trim();
      if (!tab?.id || !text) return;
      await startSentenceTTS(tab.id, text);
      return;
    }

    if (info.menuItemId === "stop-speaking") {
      await stopPlayback();
    }
    if (info.menuItemId === "pause-reading") {
      await pausePlayback();
    }
    if (info.menuItemId === "resume-reading") {
      await resumePlayback();
    }
  } catch (error) {
    logError("handling context menu click", error);
  }
});

// --- Messages from popup ---------------------------------------------------
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === "speakSentences") {
    chrome.tabs.query({ active: true, currentWindow: true }).then(([tab]) => {
      if (tab?.id) {
        startSentenceTTS(tab.id, msg.text).then(() => sendResponse({ ok: true }));
      } else {
        sendResponse({ ok: false, error: "No active tab" });
      }
    });
    return true;  // async response
  }
  if (msg.action === "stopPlayback") {
    stopPlayback().then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg.action === "getPlaybackState") {
    if (sentencePipeline?.isPaused) sendResponse({ state: "paused" });
    else if (sentencePipeline) sendResponse({ state: "playing" });
    else sendResponse({ state: "idle" });
    return false;
  }
  if (msg.action === "pausePlayback") {
    pausePlayback().then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg.action === "resumePlayback") {
    resumePlayback().then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg.action === "prevSentence") {
    prevSentence().then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg.action === "nextSentence") {
    nextSentence().then(() => sendResponse({ ok: true }));
    return true;
  }
});

// --- Keyboard shortcut -----------------------------------------------------
chrome.commands.onCommand.addListener(async (command) => {
  if (command === "speak-selection") {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id) return;
    try {
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => window.getSelection()?.toString() || "",
      });
      const text = results?.[0]?.result;
      if (text) await startSentenceTTS(tab.id, text);
    } catch (error) {
      // scripting permission may not be available on all URLs
      logError("handling keyboard shortcut", error);
    }
  }
});

// --- Stop playback ---------------------------------------------------------
async function stopPlayback() {
  sentencePipeline = null;
  if (activePlayback.tabId) {
    try {
      await chrome.scripting.executeScript({
        target: { tabId: activePlayback.tabId },
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
      func: () => { const s = window.__freeTtsAudio; if (s) { s.pause(); } },
    });
  } catch {}
  updateStopMenu();
  await updateControlBar(sentencePipeline.tabId, true);
}

async function resumePlayback() {
  if (!sentencePipeline || !sentencePipeline.isPaused) return;
  sentencePipeline.isPaused = false;
  updateStopMenu();
  await updateControlBar(sentencePipeline.tabId, false);
  await playNextSentence();
}

// --- Control bar -----------------------------------------------------------
async function showControlBar(tabId, isPaused) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (paused) => {
        // Remove existing bar
        document.getElementById("free-tts-bar")?.remove();
        const bar = document.createElement("div");
        bar.id = "free-tts-bar";
        bar.innerHTML = `
          <button id="free-tts-prev" title="Previous">⏮</button>
          <button id="free-tts-toggle" title="${paused ? "Resume" : "Pause"}">${paused ? "▶" : "⏸"}</button>
          <button id="free-tts-next" title="Next">⏭</button>
          <button id="free-tts-close" title="Stop">✕</button>
        `;
        bar.style.cssText = "position:fixed;top:16px;right:16px;background:#fff;border-radius:10px;box-shadow:0 4px 16px rgba(0,0,0,0.15);padding:6px 10px;z-index:999999;display:flex;gap:2px;font-family:-apple-system,BlinkMacSystemFont,sans-serif;";
        bar.querySelectorAll("button").forEach(b => {
          b.style.cssText = "border:none;background:none;font-size:18px;cursor:pointer;padding:6px 8px;border-radius:6px;color:#333;transition:background 0.15s;";
          b.addEventListener("mouseenter", () => b.style.background = "#f0f0f0");
          b.addEventListener("mouseleave", () => b.style.background = "none");
        });
        document.body.appendChild(bar);

        bar.querySelector("#free-tts-prev").addEventListener("click", () => chrome.runtime.sendMessage({ action: "prevSentence" }));
        bar.querySelector("#free-tts-next").addEventListener("click", () => chrome.runtime.sendMessage({ action: "nextSentence" }));
        bar.querySelector("#free-tts-toggle").addEventListener("click", () => {
          const btn = document.getElementById("free-tts-toggle");
          if (btn.textContent === "⏸") chrome.runtime.sendMessage({ action: "pausePlayback" });
          else chrome.runtime.sendMessage({ action: "resumePlayback" });
        });
        bar.querySelector("#free-tts-close").addEventListener("click", () => chrome.runtime.sendMessage({ action: "stopPlayback" }));
      },
      args: [isPaused],
    });
  } catch {}
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
  } catch {}
}

async function hideControlBar(tabId) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: () => { const b = document.getElementById("free-tts-bar"); if (b) b.remove(); },
    });
  } catch {}
}

// --- Prev/next sentence ----------------------------------------------------
async function prevSentence() {
  if (!sentencePipeline || sentencePipeline.currentIdx <= 0) return;
  sentencePipeline.currentIdx -= 2;  // -2 because playNextSentence increments
  if (sentencePipeline.currentIdx < 0) sentencePipeline.currentIdx = 0;
  sentencePipeline.isPaused = false;
  // Stop current audio
  try {
    await chrome.scripting.executeScript({
      target: { tabId: sentencePipeline.tabId },
      func: () => { const s = window.__freeTtsAudio; if (s) { s.pause(); s.src = ""; } },
    });
  } catch {}
  updateStopMenu();
  updateControlBar(sentencePipeline.tabId, false);
  await playNextSentence();
}

async function nextSentence() {
  if (!sentencePipeline) return;
  sentencePipeline.isPaused = false;
  try {
    await chrome.scripting.executeScript({
      target: { tabId: sentencePipeline.tabId },
      func: () => { const s = window.__freeTtsAudio; if (s) { s.pause(); s.src = ""; } },
    });
  } catch {}
  updateStopMenu();
  updateControlBar(sentencePipeline.tabId, false);
  await playNextSentence();
}

// --- Sentence splitting ----------------------------------------------------
function splitSentences(text) {
  // Split on sentence-ending punctuation: Latin (.!?) and CJK (。！？．｡)
  const sentences = text.match(/[^.!?\n。！？．｡]+[.!?。！？．｡]+(\s|$)|[^.!?\n。！？．｡]+$/g) || [text];
  return sentences.map(s => s.trim()).filter(s => s.length > 0);
}

// --- In-page sentence wrapping + highlight + scroll -----------------------
async function initPageHighlighting(tabId, sentences) {
  const { highlightColor } = await chrome.storage.sync.get({ highlightColor: "#fff3cd" });
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (sents, color) => {
        window.__freeTtsSentences = sents;
        window.__freeTtsColor = color;
      },
      args: [sentences, highlightColor],
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
  const resp = await fetch(`${serverUrl}/generate-and-download-tts`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ssml }),
  });
  if (!resp.ok) throw new Error(`Server returned ${resp.status}`);
  const buffer = await resp.arrayBuffer();
  const contentType = resp.headers.get("content-type") || "audio/mpeg";
  return arrayBufferToDataUrl(buffer, contentType);
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
  sentencePipeline = { sentences, currentIdx: 0, cache, tabId, voice, serverUrl, speed, isPaused: false };
  activePlayback = { tabId };
  updateStopMenu();
  await showControlBar(tabId, false);

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
  if (currentIdx >= sentences.length) {
    await cleanupPageHighlighting(tabId);
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
      if (!sentencePipeline) return;  // cancelled
    }
  }
  if (!dataUrl) {
    // Fallback: fetch directly
    try {
      dataUrl = await fetchSentenceAudio(serverUrl, voice, speed, sentences[currentIdx]);
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

  // Play
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (url) => {
        if (window.__freeTtsAudio) { window.__freeTtsAudio.pause(); }
        const a = new Audio(url);
        a.play().catch(() => {});
        window.__freeTtsAudio = a;
      },
      args: [dataUrl],
    });

    // Wait for audio to end (polling approach)
    await new Promise((resolve) => {
      const check = setInterval(async () => {
        if (!sentencePipeline) { clearInterval(check); resolve(); return; }
        try {
          const [result] = await chrome.scripting.executeScript({
            target: { tabId },
            func: () => window.__freeTtsAudio?.ended ?? true,
          });
          if (result?.result) { clearInterval(check); resolve(); }
        } catch { clearInterval(check); resolve(); }
      }, 500);
    });

    // Move to next sentence or pause
    if (sentencePipeline) {
      if (sentencePipeline.isPaused) {
        // Don't advance — just clean up audio, keep state
        await chrome.scripting.executeScript({
          target: { tabId },
          func: () => { const s = window.__freeTtsAudio; if (s) { s.src = ""; } },
        });
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
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function buildSSML(text, voice, speed = 0) {
  const rateAttr = (100 + speed) + "%";
  return `<speak xmlns="http://www.w3.org/2001/10/synthesis" xmlns:mstts="http://www.w3.org/2001/mstts" xmlns:emo="http://www.w3.org/2009/10/emotionml" version="1.0" xml:lang="en-US">
    <voice name="${escapeXML(voice)}">
        <prosody rate="${rateAttr}" pitch="0%">
${escapeXML(text)}
        </prosody>
    </voice>
</speak>`;
}
