// free-tts background service worker
// Sentence-by-sentence TTS with pre-caching and highlight bar.

const DEFAULT_SERVER = "http://localhost:5000";
const PRELOAD_AHEAD = 2;  // pre-fetch this many sentences ahead

// Pipeline state
let activePlayback = { tabId: null };
let playbackTimeout = null;
let sentencePipeline = null;  // { sentences, currentIdx, cache, tabId, voice, serverUrl }

function clearPlayback() {
  activePlayback = { tabId: null };
  if (playbackTimeout) { clearTimeout(playbackTimeout); playbackTimeout = null; }
  sentencePipeline = null;
  updateStopMenu();
}

function updateStopMenu() {
  chrome.contextMenus.update("stop-speaking", {
    visible: !!activePlayback.tabId,
  });
}

// --- Context menu ----------------------------------------------------------
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "speak-selection",
    title: "Speak this",
    contexts: ["selection"],
  });
  chrome.contextMenus.create({
    id: "stop-speaking",
    title: "Stop speaking",
    contexts: ["page"],
    visible: false,
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === "speak-selection" && info.selectionText) {
    await startSentenceTTS(tab.id, info.selectionText);
  } else if (info.menuItemId === "stop-speaking") {
    await stopPlayback();
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
    } catch {
      // scripting permission may not be available on all URLs
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
        func: () => { const s = window.__freeTtsAudio; if (s) { s.pause(); s.src = ""; } clearHighlight(); },
      });
    } catch {}
  }
  clearPlayback();
}

// --- Sentence splitting ----------------------------------------------------
function splitSentences(text) {
  // Split on sentence-ending punctuation followed by space
  const sentences = text.match(/[^.!?\n]+[.!?]+(\s|$)|[^.!?\n]+$/g) || [text];
  return sentences.map(s => s.trim()).filter(s => s.length > 0);
}

// --- Highlight bar ---------------------------------------------------------
async function showHighlight(tabId, sentence) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (s) => {
        let bar = document.getElementById("free-tts-highlight");
        if (!bar) {
          bar = document.createElement("div");
          bar.id = "free-tts-highlight";
          bar.style.cssText = "position:fixed;bottom:0;left:0;right:0;background:#fff3cd;color:#333;padding:16px 24px;font-size:18px;line-height:1.5;z-index:999999;box-shadow:0 -2px 12px rgba(0,0,0,0.15);text-align:center;font-family:-apple-system,BlinkMacSystemFont,sans-serif;";
          document.body.appendChild(bar);
        }
        bar.textContent = s;
        if (typeof clearHighlight === "undefined") {
          window.clearHighlight = () => { const b = document.getElementById("free-tts-highlight"); if (b) b.remove(); };
        }
      },
      args: [sentence],
    });
  } catch {}
}

async function removeHighlight(tabId) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: () => { const b = document.getElementById("free-tts-highlight"); if (b) b.remove(); },
    });
  } catch {}
}

// --- Fetch audio for one sentence ------------------------------------------
async function fetchSentenceAudio(serverUrl, voice, sentence) {
  const ssml = buildSSML(sentence, voice);
  const resp = await fetch(`${serverUrl}/generate-and-download-tts`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ssml }),
  });
  if (!resp.ok) throw new Error(`Server returned ${resp.status}`);
  const blob = await resp.blob();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });
}

// --- Main sentence TTS pipeline --------------------------------------------
async function startSentenceTTS(tabId, text) {
  const { serverUrl, voice } = await chrome.storage.sync.get({
    serverUrl: DEFAULT_SERVER,
    voice: "en-US-AvaMultilingualNeural",
  });

  const sentences = splitSentences(text);
  if (sentences.length === 0) return;

  await stopPlayback();
  const cache = new Map();  // idx → dataUrl
  sentencePipeline = { sentences, currentIdx: 0, cache, tabId, voice, serverUrl };
  activePlayback = { tabId };
  updateStopMenu();

  // Show first sentence immediately
  await showHighlight(tabId, sentences[0]);

  // Pre-fetch first few sentences
  const preloadCount = Math.min(PRELOAD_AHEAD, sentences.length);
  for (let i = 0; i < preloadCount; i++) {
    fetchSentenceAudio(serverUrl, voice, sentences[i])
      .then(url => cache.set(i, url))
      .catch(() => {});
  }

  // Wait a bit for first sentence to cache, then play
  await playNextSentence();
}

async function playNextSentence() {
  if (!sentencePipeline) return;
  const { sentences, currentIdx, cache, tabId, voice, serverUrl } = sentencePipeline;
  if (currentIdx >= sentences.length) {
    await removeHighlight(tabId);
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
    try { dataUrl = await fetchSentenceAudio(serverUrl, voice, sentences[currentIdx]); }
    catch { clearPlayback(); return; }
  }

  // Pre-fetch upcoming sentences
  for (let i = currentIdx + 1; i < currentIdx + 1 + PRELOAD_AHEAD && i < sentences.length; i++) {
    if (!cache.has(i)) {
      fetchSentenceAudio(serverUrl, voice, sentences[i])
        .then(url => cache.set(i, url))
        .catch(() => {});
    }
  }

  // Show highlight
  await showHighlight(tabId, sentences[currentIdx]);

  // Play
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (url, callback) => {
        if (window.__freeTtsAudio) { window.__freeTtsAudio.pause(); }
        const a = new Audio(url);
        a.play().catch(() => {});
        a.onended = () => { if (typeof callback === "function") callback(); };
        window.__freeTtsAudio = a;
      },
      args: [dataUrl, true],  // flag to indicate completion
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

    // Move to next sentence
    if (sentencePipeline) {
      sentencePipeline.currentIdx++;
      await playNextSentence();
    }
  } catch {
    clearPlayback();
  }
}

// --- XML helpers -----------------------------------------------------------
function escapeXML(str) {
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function buildSSML(text, voice) {
  return `<speak xmlns="http://www.w3.org/2001/10/synthesis" xmlns:mstts="http://www.w3.org/2001/mstts" xmlns:emo="http://www.w3.org/2009/10/emotionml" version="1.0" xml:lang="en-US">
    <voice name="${escapeXML(voice)}">
        <prosody rate="100%" pitch="0%">
${escapeXML(text)}
        </prosody>
    </voice>
</speak>`;
}
