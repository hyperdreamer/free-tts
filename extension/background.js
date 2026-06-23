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
        func: () => { const s = window.__freeTtsAudio; if (s) { s.pause(); s.src = ""; } },
      });
      await cleanupPageHighlighting(activePlayback.tabId);
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
  } catch {}
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

        // Find text in page using TreeWalker, then create a Range overlay
        const norm = (s) => s.replace(/\s+/g, " ").trim();
        const target = norm(sentence);
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
        const textNodes = [];
        while (walker.nextNode()) textNodes.push(walker.currentNode);

        for (const node of textNodes) {
          const nodeText = norm(node.textContent);
          const idx = nodeText.indexOf(target);
          if (idx < 0) continue;

          // Find exact positions in original text
          const origText = node.textContent;
          let origStart = 0, normPos = 0;
          while (normPos < idx && origStart < origText.length) {
            if (origText[origStart] && origText[origStart].match(/\s/)) {
              while (origStart < origText.length && origText[origStart].match(/\s/)) origStart++;
            } else {
              origStart++;
            }
            normPos++;
          }
          // Find end by consuming characters up to the sentence length in normalized form
          let origEnd = origStart;
          let consumed = 0;
          while (consumed < sentence.length && origEnd < origText.length) {
            if (origText[origEnd].match(/\s/)) {
              origEnd++;
            } else {
              origEnd++;
              consumed++;
            }
          }

          const range = document.createRange();
          range.setStart(node, origStart);
          range.setEnd(node, origEnd);
          const rects = range.getClientRects();

          // Create highlight overlays for each line
          for (const rect of rects) {
            const overlay = document.createElement("div");
            overlay.className = "free-tts-highlight-overlay";
            overlay.style.cssText = `position:absolute;left:${rect.left + window.scrollX}px;top:${rect.top + window.scrollY}px;width:${rect.width}px;height:${rect.height}px;background:${color};opacity:0.5;pointer-events:none;z-index:999998;transition:background 0.3s;`;
            document.body.appendChild(overlay);
          }
          break;
        }
      },
      args: [idx],
    });
  } catch {}
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
  await initPageHighlighting(tabId, sentences);

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

  // Highlight current and scroll
  await highlightCurrentSentence(tabId, currentIdx);

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
