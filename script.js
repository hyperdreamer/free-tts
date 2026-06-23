/**
 * SSML TTS Generator — Frontend Logic
 *
 * Two modes:
 *   1. SSML tab — raw SSML editing with the default template.
 *   2. Text Input tab — Language/Gender/Voice picker, text input,
 *      speed/pitch sliders, SSML preview, results history.
 */

// ---------------------------------------------------------------------------
// SSML Template
// ---------------------------------------------------------------------------
const SSML_TEMPLATE = `<speak xmlns="http://www.w3.org/2001/10/synthesis" xmlns:mstts="http://www.w3.org/2001/mstts" xmlns:emo="http://www.w3.org/2009/10/emotionml" version="1.0" xml:lang="en-US">
    <voice name="VOICE_NAME">
        <prosody rate="RATE%" pitch="PITCH%">
TEXT_CONTENT
        </prosody>
    </voice>
</speak>`;

const DEFAULT_VOICE_FALLBACK = "en-US-AvaMultilingualNeural";
let configuredDefaultVoice = DEFAULT_VOICE_FALLBACK;
const DEFAULT_TEXT =
  "Gaming is not just play anymore. It has become a nation. A culture. A career. A community.";
const SAMPLE_TEXT = "Hello, this is a sample of my voice.";

// ---------------------------------------------------------------------------
// Backend URL
// - file:// frontend: talk to the local daemon directly
// - http(s) frontend: same-origin/proxy path by default
// ---------------------------------------------------------------------------
const BACKEND_URL = window.location.protocol === "file:" ? "http://localhost:5000" : "";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let allVoices = [];           // raw from server
let languages = [];           // [{locale, name}]
let selectedVoice = DEFAULT_VOICE_FALLBACK;
let results = [];             // { text, voice, rate, pitch, blobUrl }
let activeGender = "all";     // "all" | "Male" | "Female"

// ---------------------------------------------------------------------------
// DOM refs
// ---------------------------------------------------------------------------
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// Tabs
const ssmlInput       = $("#ssmlInput");
const downloadSsmlBtn  = $("#downloadSsmlBtn");

// Text Input tab
const languageSelect  = $("#languageSelect");
const voiceList       = $("#voiceList");
const voiceSearch     = $("#voiceSearch");
const voiceSelectedName = $("#voiceSelectedName");
const voiceSelectedMeta = $("#voiceSelectedMeta");
const voicePreviewBtn = $("#voicePreviewBtn");
const textInputArea   = $("#textInputArea");
const charCount       = $("#charCount");
const speedSlider     = $("#speedSlider");
const speedValue      = $("#speedValue");
const pitchSlider     = $("#pitchSlider");
const pitchValue      = $("#pitchValue");
const previewBtn      = $("#previewBtn");
const stopBtn         = $("#stopBtn");
const downloadTextBtn  = $("#downloadTextBtn");
const ssmlPreview = $("#ssmlPreview");
const resultsList     = $("#resultsList");
const audioPlayer     = $("#audioPlayer");
const errorMsg        = $("#errorMsg");

// ---------------------------------------------------------------------------
// XML escaping
// ---------------------------------------------------------------------------
function escapeXML(str) {
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

// ---------------------------------------------------------------------------
// SSML construction
// ---------------------------------------------------------------------------
function buildSSML(voice, text, rateSliderVal, pitchSliderVal) {
  const rateAttr = (100 + rateSliderVal) + "%";
  const pitchAttr = pitchSliderVal === 0
    ? "0%"
    : (pitchSliderVal > 0 ? "+" : "") + pitchSliderVal + "Hz";

  return SSML_TEMPLATE
    .replace("VOICE_NAME", escapeXML(voice))
    .replace("RATE%", rateAttr)
    .replace("PITCH%", pitchAttr)
    .replace("TEXT_CONTENT", escapeXML(text));
}

function fillDefaultSSML() {
  ssmlInput.value = buildSSML(configuredDefaultVoice, DEFAULT_TEXT, 0, 0);
  textInputArea.value = DEFAULT_TEXT;
}

// ---------------------------------------------------------------------------
// Tab switching
// ---------------------------------------------------------------------------
$$(".tab").forEach(tab => {
  tab.addEventListener("click", () => {
    const target = tab.dataset.tab;
    $$(".tab").forEach(t => t.classList.remove("active-tab"));
    tab.classList.add("active-tab");
    $$(".tab-content").forEach(tc => tc.classList.remove("active"));
    $(`#tab-${target}`).classList.add("active");
  });
});

// Panel tabs
$$(".panel-tab").forEach(tab => {
  tab.addEventListener("click", () => {
    const target = tab.dataset.panel;
    $$(".panel-tab").forEach(t => t.classList.remove("active"));
    tab.classList.add("active");
    $$(".panel-content").forEach(pc => pc.classList.remove("active"));
    $(`#${target}`).classList.add("active");
  });
});

// ---------------------------------------------------------------------------
// Safe DOM helpers (avoid innerHTML for XSS prevention)
// ---------------------------------------------------------------------------
function el(tag, attrs = {}, children = []) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "className") e.className = v;
    else if (k === "textContent") e.textContent = v;
    else if (k === "dataset") Object.assign(e.dataset, v);
    else if (k.startsWith("on")) e.addEventListener(k.slice(2).toLowerCase(), v);
    else e.setAttribute(k, v);
  }
  for (const c of children) {
    e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return e;
}

function clearChildren(parent) {
  while (parent.firstChild) parent.removeChild(parent.firstChild);
}

// ---------------------------------------------------------------------------
// Voice data loading
// ---------------------------------------------------------------------------
async function loadVoices() {
  try {
    // Remove any stale cached voice data from older builds.
    localStorage.removeItem("freeTtsVoices");

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    const resp = await fetch(`${BACKEND_URL}/voices`, { signal: controller.signal });
    clearTimeout(timeout);
    if (!resp.ok) throw new Error(`Failed to load voices (${resp.status})`);
    const data = await resp.json();
    allVoices = data.voices || [];
    languages = data.languages || [];
    // Respect the server's configured default voice
    if (data.default_voice) configuredDefaultVoice = data.default_voice;
    if (selectedVoice === DEFAULT_VOICE_FALLBACK) selectedVoice = configuredDefaultVoice;
    populateLanguageDropdown();
  } catch (err) {
    console.error("Voice load error:", err);
    clearChildren(languageSelect);
    languageSelect.appendChild(el("option", { value: "", textContent: "Server offline / cannot load voices" }));
    clearChildren(voiceList);
    voiceList.appendChild(el("div", { className: "voice-loading", textContent: "Failed to load voices. Is python server.py running?" }));
  }
}

function populateLanguageDropdown() {
  clearChildren(languageSelect);
  languageSelect.appendChild(el("option", { value: "", textContent: "All Languages" }));
  for (const l of languages) {
    languageSelect.appendChild(el("option", { value: l.locale, textContent: l.name }));
  }

  // Select the language containing the configured default voice.
  const defaultVoiceInfo = allVoices.find(v => v.ShortName === selectedVoice);
  if (defaultVoiceInfo) {
    languageSelect.value = defaultVoiceInfo.Locale;
    voiceSelectedName.textContent = defaultVoiceInfo.ShortName;
    const langName = languages.find(l => l.locale === defaultVoiceInfo.Locale);
    voiceSelectedMeta.textContent =
      `${langName ? langName.name : defaultVoiceInfo.Locale} · ${defaultVoiceInfo.Gender}`;
  } else {
    const enOption = languageSelect.querySelector('option[value="en-US"]');
    if (enOption) enOption.selected = true;
  }

  renderVoiceList();
  updateSSMLPreview();
}

// ---------------------------------------------------------------------------
// Filtering & rendering voice list
// ---------------------------------------------------------------------------
function getFilteredVoices() {
  const langFilter = languageSelect.value;
  const searchFilter = voiceSearch.value.toLowerCase().trim();

  return allVoices.filter(v => {
    if (langFilter && v.Locale !== langFilter) return false;
    if (activeGender !== "all" && v.Gender !== activeGender) return false;
    if (searchFilter && !v.ShortName.toLowerCase().includes(searchFilter)
        && !v.LanguageName.toLowerCase().includes(searchFilter)) return false;
    return true;
  });
}

function renderVoiceList() {
  const filtered = getFilteredVoices();
  clearChildren(voiceList);

  if (filtered.length === 0) {
    voiceList.appendChild(el("div", { className: "voice-loading", textContent: "No voices match" }));
    return;
  }

  for (const v of filtered) {
    const sel = v.ShortName === selectedVoice ? " voice-item selected" : "voice-item";
    const localePart = v.Locale.split("-")[1] || v.Locale;
    const item = el("div", {
      className: sel,
      dataset: { voice: v.ShortName, locale: v.Locale, gender: v.Gender },
    }, [
      el("span", { className: "voice-item-name", textContent: v.ShortName }),
      el("span", { className: "voice-item-locale", textContent: localePart }),
      el("span", { className: "voice-item-preview", dataset: { action: "preview" }, textContent: "▶", title: "Preview voice" }),
    ]);
    item.addEventListener("click", (e) => {
      if (e.target.dataset.action === "preview") {
        e.stopPropagation();
        previewVoice(v.ShortName);
        return;
      }
      selectVoice(v.ShortName, v.Locale, v.Gender);
    });
    voiceList.appendChild(item);
  }

  const selectedItem = voiceList.querySelector(".voice-item.selected");
  if (selectedItem) {
    selectedItem.scrollIntoView({ block: "nearest" });
  }
}

function selectVoice(shortName, locale, gender) {
  selectedVoice = shortName;
  voiceSelectedName.textContent = shortName;
  const langName = languages.find(l => l.locale === locale);
  voiceSelectedMeta.textContent =
    `${langName ? langName.name : locale} · ${gender}`;
  updateSSMLPreview();
  renderVoiceList();
}

async function previewVoice(shortName) {
  // Generate a short sample using this voice
  const ssml = buildSSML(shortName, SAMPLE_TEXT, 0, 0);
  try {
    const blob = await callTTS(ssml);
    playBlob(blob);
  } catch (err) {
    console.error("Voice preview failed:", err);
  }
}

// ---------------------------------------------------------------------------
// Filter events
// ---------------------------------------------------------------------------
languageSelect.addEventListener("change", renderVoiceList);

$$(".gender-chip").forEach(chip => {
  chip.addEventListener("click", () => {
    $$(".gender-chip").forEach(c => c.classList.remove("active"));
    chip.classList.add("active");
    activeGender = chip.dataset.gender;
    renderVoiceList();
  });
});

voiceSearch.addEventListener("input", renderVoiceList);

// ---------------------------------------------------------------------------
// Sliders → live value + SSML preview
// ---------------------------------------------------------------------------
function updateSliderDisplay() {
  speedValue.textContent = speedSlider.value + "%";
  pitchValue.textContent = pitchSlider.value + " Hz";
}

function updateSSMLPreview() {
  const text = textInputArea.value || "[enter text]";
  const ssml = buildSSML(selectedVoice, text, parseInt(speedSlider.value), parseInt(pitchSlider.value));
  ssmlPreview.textContent = ssml;
  charCount.textContent = textInputArea.value.length + " characters";
}

speedSlider.addEventListener("input", () => { updateSliderDisplay(); updateSSMLPreview(); });
pitchSlider.addEventListener("input", () => { updateSliderDisplay(); updateSSMLPreview(); });
textInputArea.addEventListener("input", updateSSMLPreview);

// ---------------------------------------------------------------------------
// TTS API call
// ---------------------------------------------------------------------------
async function callTTS(ssml) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30000);
  try {
    const resp = await fetch(`${BACKEND_URL}/generate-and-download-tts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ssml }),
      signal: controller.signal,
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ error: resp.statusText }));
      throw new Error(err.error || "TTS request failed");
    }

    return await resp.blob();
  } finally {
    clearTimeout(timeout);
  }
}

function playBlob(blob) {
  // Revoke previous blob URL to prevent memory leaks
  if (audioPlayer.src && audioPlayer.src.startsWith("blob:")) {
    URL.revokeObjectURL(audioPlayer.src);
  }
  const url = URL.createObjectURL(blob);
  audioPlayer.src = url;
  audioPlayer.play();
  showStopButton();
  audioPlayer.onended = () => { hideStopButton(); URL.revokeObjectURL(url); };
}

function stopAudio() {
  audioPlayer.pause();
  audioPlayer.currentTime = 0;
  if (audioPlayer.src && audioPlayer.src.startsWith("blob:")) {
    URL.revokeObjectURL(audioPlayer.src);
  }
  audioPlayer.src = "";
  hideStopButton();
}

function downloadBlob(blob, filename = "tts-output.mp3") {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function showStopButton() {
  previewBtn.style.display = "none";
  stopBtn.style.display = "inline-flex";
}

function hideStopButton() {
  previewBtn.style.display = "inline-flex";
  stopBtn.style.display = "none";
}

// ---------------------------------------------------------------------------
// SSML tab: download button
// ---------------------------------------------------------------------------
downloadSsmlBtn.addEventListener("click", async () => {
  const ssml = ssmlInput.value.trim();
  if (!ssml) return alert("Please enter SSML before downloading.");

  downloadSsmlBtn.textContent = "Generating...";
  downloadSsmlBtn.disabled = true;

  try {
    const blob = await callTTS(ssml);
    downloadBlob(blob);
  } catch (err) {
    alert("Error: " + err.message);
  } finally {
    downloadSsmlBtn.textContent = "Download MP3";
    downloadSsmlBtn.disabled = false;
  }
});

// ---------------------------------------------------------------------------
// Text Input: preview & download
// ---------------------------------------------------------------------------
async function handleTextGenerate(preview = false) {
  const text = textInputArea.value.trim();
  if (!text) return alert("Please enter text to synthesize.");

  const rate = parseInt(speedSlider.value);
  const pitch = parseInt(pitchSlider.value);
  const ssml = buildSSML(selectedVoice, text, rate, pitch);

  previewBtn.disabled = true;
  downloadTextBtn.disabled = true;
  previewBtn.textContent = "Generating...";
  downloadTextBtn.textContent = "Generating...";

  try {
    const blob = await callTTS(ssml);

    // Add to results
    const blobUrl = URL.createObjectURL(blob);
    results.unshift({ text, voice: selectedVoice, rate, pitch, blobUrl });
    if (results.length > 20) results.pop();
    renderResults();

    if (preview) {
      playBlob(blob);
    } else {
      downloadBlob(blob);
    }
  } catch (err) {
    alert("Error: " + err.message);
  } finally {
    previewBtn.disabled = false;
    downloadTextBtn.disabled = false;
    previewBtn.textContent = "▶ Preview Audio";
    downloadTextBtn.textContent = "⬇ Download MP3";
  }
}

previewBtn.addEventListener("click", () => handleTextGenerate(true));
stopBtn.addEventListener("click", stopAudio);
downloadTextBtn.addEventListener("click", () => handleTextGenerate(false));

// Selected voice preview button
voicePreviewBtn.addEventListener("click", () => previewVoice(selectedVoice));

// ---------------------------------------------------------------------------
// Results list
// ---------------------------------------------------------------------------
function renderResults() {
  clearChildren(resultsList);

  if (results.length === 0) {
    resultsList.appendChild(el("div", { className: "results-empty", textContent: "No generated items yet" }));
    return;
  }

  for (let i = 0; i < results.length; i++) {
    const r = results[i];
    const shortText = r.text.length > 40 ? r.text.slice(0, 40) + "\u2026" : r.text;
    const voiceLabel = r.voice.replace("Neural", "").replace("Multilingual", "");

    const item = el("div", { className: "result-item", dataset: { idx: String(i) } }, [
      el("div", { className: "result-info" }, [
        el("span", { className: "result-text", textContent: shortText }),
        el("span", { className: "result-voice", textContent: `${voiceLabel} \u00b7 ${r.rate}% speed \u00b7 ${r.pitch}% pitch` }),
      ]),
      el("span", { className: "result-play", dataset: { idx: String(i) }, textContent: "\u25b6" }),
    ]);

    const playBtn = item.querySelector(".result-play");
    playBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      audioPlayer.src = r.blobUrl;
      audioPlayer.play();
    });

    item.addEventListener("click", () => {
      textInputArea.value = r.text;
      const voiceInfo = allVoices.find(v => v.ShortName === r.voice);
      if (voiceInfo) {
        selectVoice(voiceInfo.ShortName, voiceInfo.Locale, voiceInfo.Gender);
        languageSelect.value = voiceInfo.Locale;
      } else {
        selectedVoice = r.voice;
        voiceSelectedName.textContent = r.voice;
        voiceSelectedMeta.textContent = "\u2014";
      }
      speedSlider.value = r.rate;
      pitchSlider.value = r.pitch;
      updateSliderDisplay();
      updateSSMLPreview();
      renderVoiceList();
      $$(".tab").forEach(t => t.classList.remove("active-tab"));
      document.querySelector('[data-tab="text"]').classList.add("active-tab");
      $$(".tab-content").forEach(tc => tc.classList.remove("active"));
      $("#tab-text").classList.add("active");
    });

    resultsList.appendChild(item);
  }
}

// ---------------------------------------------------------------------------
// Keyboard shortcut
// ---------------------------------------------------------------------------
ssmlInput.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
    e.preventDefault();
    downloadSsmlBtn.click();
  }
});

// ---------------------------------------------------------------------------
// Initialisation
// ---------------------------------------------------------------------------
async function init() {
  fillDefaultSSML();
  updateSliderDisplay();
  await loadVoices();
  updateSSMLPreview();
}

init();
