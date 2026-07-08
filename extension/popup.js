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
const optionsLink     = document.getElementById("optionsLink");
const statusDot       = document.getElementById("statusDot");
const hostInput       = document.getElementById("hostInput");
const portInput       = document.getElementById("portInput");
const serverConfig    = document.getElementById("serverConfig");
const moreOptionsLink = document.getElementById("moreOptionsLink");

let serverUrl = DEFAULT_SERVER;
let voices = [];
let defaultVoice = DEFAULT_VOICE;
let popupState = "idle";  // idle | playing | paused

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

function normalizeSpeed(value) {
  const speed = Number.parseInt(value, 10);
  if (!Number.isFinite(speed)) return 0;
  return Math.min(200, Math.max(-50, speed));
}

function parseUrl(serverUrl) {
  try {
    const url = new URL(serverUrl || DEFAULT_SERVER);
    return { host: url.hostname, port: url.port };
  } catch {
    return { host: "localhost", port: "5000" };
  }
}

async function applyServerConfig() {
  const host = (hostInput.value || "").trim() || "localhost";
  const port = Number.parseInt(portInput.value, 10);
  if (!Number.isFinite(port) || port < 1 || port > 65535) {
    portInput.classList.add("invalid");
    return;
  }
  const url = `http://${host}:${port}`;
  const normalized = normalizeServerUrl(url);
  if (normalized === serverUrl) {
    portInput.classList.remove("invalid");
    return;
  }
  serverUrl = normalized;
  portInput.classList.remove("invalid");
  await chrome.storage.sync.set({ serverUrl: normalized }).catch(() => {});
  await checkServer();
  await loadVoices();
}

// --- Init ------------------------------------------------------------------
async function init() {
  const { serverUrl: stored } = await chrome.storage.sync.get({ serverUrl: DEFAULT_SERVER });
  serverUrl = normalizeServerUrl(stored);
  const parsed = parseUrl(serverUrl);
  hostInput.value = parsed.host;
  portInput.value = parsed.port;

  // Load saved default voice
  const { voice: savedVoice } = await chrome.storage.sync.get({ voice: DEFAULT_VOICE });
  defaultVoice = savedVoice;

  // Check current playback state from background
  const { state } = await chrome.runtime.sendMessage({ action: "getPlaybackState" });
  popupState = state || "idle";
  updateButtons();

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
      chrome.storage.sync.set({ voice: v }).catch(() => {});
    }
  });

  // Load saved speed
  const { speed } = await chrome.storage.sync.get({ speed: 0 });
  const safeSpeed = normalizeSpeed(speed);
  speedSlider.value = safeSpeed;
  speedVal.textContent = safeSpeed + "%";

  // Save speed on change
  speedSlider.addEventListener("input", () => {
    const s = speedSlider.value;
    speedVal.textContent = s + "%";
    chrome.storage.sync.set({ speed: normalizeSpeed(s) }).catch(() => {});
  });
  speakBtn.addEventListener("click", () => speak().catch(() => {
    popupState = "idle";
    updateButtons();
  }));
  stopBtn.addEventListener("click", () => stop());
  optionsLink.addEventListener("click", (event) => {
    event.preventDefault();
    serverConfig.classList.toggle("show");
  });
  moreOptionsLink.addEventListener("click", () => {
    chrome.runtime.openOptionsPage();
  });
  hostInput.addEventListener("blur", () => applyServerConfig());
  portInput.addEventListener("blur", () => applyServerConfig());
  portInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") applyServerConfig();
  });
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
  } catch (error) {
    console.warn("free-tts popup: server health check failed", error);
  }
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
      chrome.storage.sync.set({ voice: defaultVoice }).catch(() => {});
    }
    renderVoiceSelect();
  } catch (error) {
    console.warn("free-tts popup: failed to load voices", error);
    if (voices.length === 0) renderVoiceSelect();
  }
}

function renderVoiceSelect() {
  voiceSelect.replaceChildren();
  if (voices.length === 0) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "No voices (server offline?)";
    voiceSelect.appendChild(opt);
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

// --- Speak / Pause / Resume -------------------------------------------------
function updateButtons() {
  if (popupState === "playing") {
    speakBtn.textContent = "⏸ Pause";
    speakBtn.disabled = false;
    stopBtn.disabled = false;
  } else if (popupState === "paused") {
    speakBtn.textContent = "▶ Resume";
    speakBtn.disabled = false;
    stopBtn.disabled = false;
  } else {
    speakBtn.textContent = "▶ Speak";
    speakBtn.disabled = false;
    stopBtn.disabled = true;
  }
}

async function speak() {
  if (popupState === "playing") {
    // Pause
    const response = await chrome.runtime.sendMessage({ action: "pausePlayback" });
    if (!response?.ok) throw new Error(response?.error || "Failed to pause playback");
    popupState = "paused";
    updateButtons();
    return;
  }
  if (popupState === "paused") {
    // Resume
    const response = await chrome.runtime.sendMessage({ action: "resumePlayback" });
    if (!response?.ok) throw new Error(response?.error || "Failed to resume playback");
    popupState = "playing";
    updateButtons();
    return;
  }

  // Start new playback
  const text = textInput.value.trim();
  if (!text) return;

  // Persist voice as default
  const voice = voiceSelect.value || defaultVoice;
  if (voice !== defaultVoice) {
    defaultVoice = voice;
    chrome.storage.sync.set({ voice }).catch(() => {});
  }

  const response = await chrome.runtime.sendMessage({ action: "speakSentences", text });
  if (!response?.ok) return;
  popupState = "playing";
  updateButtons();
}

async function stop() {
  await chrome.runtime.sendMessage({ action: "stopPlayback" }).catch(() => {});
  popupState = "idle";
  updateButtons();
}

// --- Start ----------------------------------------------------------------
init();
