// free-tts popup
const DEFAULT_SERVER = "http://localhost:5000";
const DEFAULT_VOICE = "en-US-AvaMultilingualNeural";

// DOM refs
const voiceSelect  = document.getElementById("voiceSelect");
const textInput    = document.getElementById("textInput");
const speedSlider  = document.getElementById("speedSlider");
const speedVal     = document.getElementById("speedVal");
const speakBtn     = document.getElementById("speakBtn");
const stopBtn      = document.getElementById("stopBtn");
const statusDot    = document.getElementById("statusDot");
const audioPlayer  = document.getElementById("audioPlayer");
const optionsLink  = document.getElementById("optionsLink");

let serverUrl = DEFAULT_SERVER;
let voices = [];
let activeAudio = null;
let activeDataUrl = null;
let defaultVoice = DEFAULT_VOICE;

// --- Init ------------------------------------------------------------------
async function init() {
  const { serverUrl: stored } = await chrome.storage.sync.get({ serverUrl: DEFAULT_SERVER });
  serverUrl = stored;

  // Load saved default voice
  const { voice: savedVoice } = await chrome.storage.sync.get({ voice: DEFAULT_VOICE });
  defaultVoice = savedVoice;

  // Auto-fill with selected text from the active tab
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab?.id) {
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => window.getSelection()?.toString() || "",
      });
      const sel = results?.[0]?.result?.trim();
      if (sel) textInput.value = sel;
    }
  } catch {
    // scripting permission not available everywhere
  }

  await checkServer();
  await loadVoices();

  // Save voice selection on change
  voiceSelect.addEventListener("change", () => {
    const v = voiceSelect.value;
    if (v && v !== defaultVoice) {
      defaultVoice = v;
      chrome.storage.sync.set({ voice: v });
    }
  });

  speedSlider.addEventListener("input", () => {
    speedVal.textContent = speedSlider.value + "%";
  });
  speakBtn.addEventListener("click", () => speak());
  stopBtn.addEventListener("click", () => stop());
  optionsLink.addEventListener("click", () => chrome.runtime.openOptionsPage());
}

// --- Server check ---------------------------------------------------------
async function checkServer() {
  try {
    const resp = await fetch(`${serverUrl}/health`, { signal: AbortSignal.timeout(3000) });
    if (resp.ok) {
      statusDot.className = "status-dot online";
      statusDot.title = "Server online";
      return true;
    }
  } catch {}
  statusDot.className = "status-dot offline";
  statusDot.title = "Server offline";
  return false;
}

// --- Load voices ----------------------------------------------------------
async function loadVoices() {
  try {
    const resp = await fetch(`${serverUrl}/voices`, { signal: AbortSignal.timeout(3000) });
    if (!resp.ok) throw new Error("Failed");
    const data = await resp.json();
    voices = data.voices || [];
    // Use the server's configured default voice if available
    if (data.default_voice && defaultVoice === "en-US-AvaMultilingualNeural") {
      defaultVoice = data.default_voice;
      chrome.storage.sync.set({ voice: defaultVoice });
    }
    renderVoiceSelect();
  } catch {
    if (voices.length === 0) renderVoiceSelect();
  }
}

function renderVoiceSelect() {
  voiceSelect.innerHTML = "";
  if (voices.length === 0) {
    voiceSelect.innerHTML = '<option value="">No voices (server offline?)</option>';
    return;
  }

  // Group by locale
  const grouped = {};
  for (const v of voices) {
    const lang = v.LanguageName || v.Locale;
    if (!grouped[lang]) grouped[lang] = [];
    grouped[lang].push(v);
  }

  for (const [lang, group] of Object.entries(grouped).sort()) {
    const optgroup = document.createElement("optgroup");
    optgroup.label = lang;
    for (const v of group) {
      const opt = document.createElement("option");
      opt.value = v.ShortName;
      opt.textContent = `${v.ShortName} (${v.Gender})`;
      if (v.ShortName === defaultVoice) opt.selected = true;
      optgroup.appendChild(opt);
    }
    voiceSelect.appendChild(optgroup);
  }
}

// --- Speak -----------------------------------------------------------------
async function speak() {
  const text = textInput.value.trim();
  if (!text) return;

  const voice = voiceSelect.value || defaultVoice;
  // Persist as default voice for context-menu "Speak this"
  if (voice !== defaultVoice) {
    defaultVoice = voice;
    chrome.storage.sync.set({ voice });
  }
  const speed = parseInt(speedSlider.value);
  const rateAttr = (100 + speed) + "%";

  const ssml = `<speak xmlns="http://www.w3.org/2001/10/synthesis" xmlns:mstts="http://www.w3.org/2001/mstts" xmlns:emo="http://www.w3.org/2009/10/emotionml" version="1.0" xml:lang="en-US">
    <voice name="${escapeXML(voice)}">
        <prosody rate="${rateAttr}" pitch="0%">
${escapeXML(text)}
        </prosody>
    </voice>
</speak>`;

  speakBtn.disabled = true;
  speakBtn.textContent = "Loading...";

  try {
    const resp = await fetch(`${serverUrl}/generate-and-download-tts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ssml }),
    });
    if (!resp.ok) throw new Error(`Server error ${resp.status}`);

    const blob = await resp.blob();
    const url = await blobToDataUrl(blob);

    if (activeAudio) {
      activeAudio.pause();
    }

    activeDataUrl = url;
    audioPlayer.src = url;
    audioPlayer.play();
    activeAudio = audioPlayer;

    stopBtn.disabled = false;

    audioPlayer.onended = () => {
      stopBtn.disabled = true;
      activeAudio = null;
      activeDataUrl = null;
    };
  } catch (err) {
    console.error("free-tts:", err);
  } finally {
    speakBtn.disabled = false;
    speakBtn.textContent = "▶ Speak";
  }
}

function stop() {
  if (activeAudio) {
    activeAudio.pause();
    activeAudio.currentTime = 0;
    activeAudio = null;
  }
  activeDataUrl = null;
  audioPlayer.src = "";
  stopBtn.disabled = true;
}

function blobToDataUrl(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });
}

// --- XML escape -----------------------------------------------------------
function escapeXML(str) {
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

// --- Start ----------------------------------------------------------------
init();
