// free-tts background service worker
// Handles context menu clicks, keyboard shortcut, and dynamic stop menu.
// Audio playback is injected into the active tab (service workers can't play audio).

const DEFAULT_SERVER = "http://localhost:5000";

// Track injected audio
let activePlayback = { tabId: null };
let playbackTimeout = null;

function clearPlayback() {
  activePlayback = { tabId: null };
  if (playbackTimeout) { clearTimeout(playbackTimeout); playbackTimeout = null; }
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
    await speakInTab(tab.id, info.selectionText);
  } else if (info.menuItemId === "stop-speaking") {
    await stopPlaybackInTab();
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
      if (text) await speakInTab(tab.id, text);
    } catch {
      // scripting permission may not be available on all URLs
    }
  }
});

// --- Stop playback in tab --------------------------------------------------
async function stopPlaybackInTab() {
  if (!activePlayback.tabId) return;
  const tabId = activePlayback.tabId;
  clearPlayback();
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const a = window.__freeTtsAudio;
        if (a) { a.pause(); a.currentTime = 0; a.src = ""; delete window.__freeTtsAudio; }
      },
    });
  } catch {
    // tab may have closed
  }
}

// --- Fetch audio and inject playback into the tab --------------------------
async function speakInTab(tabId, text) {
  const { serverUrl, voice } = await chrome.storage.sync.get({
    serverUrl: DEFAULT_SERVER,
    voice: "en-US-AvaMultilingualNeural",
  });
  const ssml = buildSSML(text, voice);

  try {
    const resp = await fetch(`${serverUrl}/generate-and-download-tts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ssml }),
    });
    if (!resp.ok) throw new Error(`Server returned ${resp.status}`);

    const blob = await resp.blob();
    const reader = new FileReader();
    const dataUrl = await new Promise((resolve, reject) => {
      reader.onload = () => resolve(reader.result);
      reader.onerror = reject;
      reader.readAsDataURL(blob);
    });

    // Stop any previous playback
    await stopPlaybackInTab();

    // Inject audio playback into the tab with a known window-level reference
    const audioId = Date.now();
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (url, id) => {
        if (window.__freeTtsAudio) {
          window.__freeTtsAudio.pause();
          window.__freeTtsAudio.src = "";
        }
        const a = new Audio(url);
        a.id = `free-tts-${id}`;
        a.play().catch(() => {});
        a.onended = () => { delete window.__freeTtsAudio; };
        window.__freeTtsAudio = a;
      },
      args: [dataUrl, audioId],
    });

    activePlayback = { tabId };
    updateStopMenu();
    // Auto-clear after 30 seconds
    playbackTimeout = setTimeout(clearPlayback, 30000);
  } catch (err) {
    console.error("free-tts:", err);
  }
}

function escapeXML(str) {
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
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
