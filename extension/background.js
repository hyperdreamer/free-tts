// free-tts background service worker
// Handles context menu clicks and keyboard shortcut.
// Audio playback is injected into the active tab (service workers can't play audio).

const DEFAULT_SERVER = "http://localhost:5000";

// --- Context menu ----------------------------------------------------------
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "speak-selection",
    title: "Speak this",
    contexts: ["selection"],
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === "speak-selection" && info.selectionText) {
    await speakInTab(tab.id, info.selectionText);
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

// --- Fetch audio and inject playback into the tab --------------------------
async function speakInTab(tabId, text) {
  const { serverUrl } = await chrome.storage.sync.get({ serverUrl: DEFAULT_SERVER });
  const ssml = buildSSML(text);

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

    // Inject audio playback into the tab (service workers can't play audio)
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (url) => {
        const a = new Audio(url);
        a.play();
      },
      args: [dataUrl],
    });
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

function buildSSML(text) {
  return `<speak xmlns="http://www.w3.org/2001/10/synthesis" xmlns:mstts="http://www.w3.org/2001/mstts" xmlns:emo="http://www.w3.org/2009/10/emotionml" version="1.0" xml:lang="en-US">
    <voice name="en-US-AvaMultilingualNeural">
        <prosody rate="100%" pitch="0%">
${escapeXML(text)}
        </prosody>
    </voice>
</speak>`;
}
