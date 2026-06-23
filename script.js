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

const DEFAULT_VOICE = "en-US-AvaMultilingualNeural";
const DEFAULT_TEXT =
  "Gaming is not just play anymore. It has become a nation. A culture. A career. A community.";
const SAMPLE_TEXT = "Hello, this is a sample of my voice.";

// ---------------------------------------------------------------------------
// Backend URL
// ---------------------------------------------------------------------------
const BACKEND_URL = "http://localhost:5000";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let allVoices = [];           // raw from server
let languages = [];           // [{locale, name}]
let selectedVoice = DEFAULT_VOICE;
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
const downloadTextBtn  = $("#downloadTextBtn");
const ssmlPreview     = $("#ssmlPreview");
const resultsList     = $("#resultsList");
const audioPlayer     = $("#audioPlayer");

// ---------------------------------------------------------------------------
// SSML construction
// ---------------------------------------------------------------------------
function buildSSML(voice, text, rateSliderVal, pitchSliderVal) {
  const rateAttr = (100 + rateSliderVal) + "%";
  const pitchAttr = pitchSliderVal === 0
    ? "0%"
    : (pitchSliderVal > 0 ? "+" : "") + pitchSliderVal + "Hz";

  return SSML_TEMPLATE
    .replace("VOICE_NAME", voice)
    .replace("RATE%", rateAttr)
    .replace("PITCH%", pitchAttr)
    .replace("TEXT_CONTENT", text);
}

function fillDefaultSSML() {
  ssmlInput.value = buildSSML(DEFAULT_VOICE, DEFAULT_TEXT, 0, 0);
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
// Voice data loading
// ---------------------------------------------------------------------------
async function loadVoices() {
  try {
    const resp = await fetch(`${BACKEND_URL}/voices`);
    if (!resp.ok) throw new Error("Failed to load voices");
    const data = await resp.json();
    allVoices = data.voices || [];
    languages = data.languages || [];
    populateLanguageDropdown();
  } catch (err) {
    console.error("Voice load error:", err);
    voiceList.innerHTML = '<div class="voice-loading">Failed to load voices</div>';
  }
}

function populateLanguageDropdown() {
  languageSelect.innerHTML = '<option value="">All Languages</option>' +
    languages.map(l => `<option value="${l.locale}">${l.name}</option>`).join("");

  // Default to English
  const enOption = languageSelect.querySelector('option[value="en-US"]');
  if (enOption) enOption.selected = true;

  renderVoiceList();
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

  if (filtered.length === 0) {
    voiceList.innerHTML = '<div class="voice-loading">No voices match</div>';
    return;
  }

  voiceList.innerHTML = filtered.map(v => {
    const sel = v.ShortName === selectedVoice ? " selected" : "";
    const localePart = v.Locale.split("-")[1] || v.Locale;
    return `<div class="voice-item${sel}" data-voice="${v.ShortName}" data-locale="${v.Locale}" data-gender="${v.Gender}">
      <span class="voice-item-name">${v.ShortName}</span>
      <span class="voice-item-locale">${localePart}</span>
      <span class="voice-item-preview" data-action="preview" title="Preview voice">▶</span>
    </div>`;
  }).join("");

  // Click handlers
  voiceList.querySelectorAll(".voice-item").forEach(item => {
    item.addEventListener("click", (e) => {
      // If clicked on the preview button, preview the voice
      if (e.target.dataset.action === "preview") {
        e.stopPropagation();
        previewVoice(item.dataset.voice);
        return;
      }
      selectVoice(item.dataset.voice, item.dataset.locale, item.dataset.gender);
    });
  });
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
  pitchValue.textContent = pitchSlider.value + "%";
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
  const resp = await fetch(`${BACKEND_URL}/generate-and-download-tts`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ssml }),
  });

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ error: resp.statusText }));
    throw new Error(err.error || "TTS request failed");
  }

  return await resp.blob();
}

function playBlob(blob) {
  const url = URL.createObjectURL(blob);
  audioPlayer.src = url;
  audioPlayer.play();
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
downloadTextBtn.addEventListener("click", () => handleTextGenerate(false));

// Selected voice preview button
voicePreviewBtn.addEventListener("click", () => previewVoice(selectedVoice));

// ---------------------------------------------------------------------------
// Results list
// ---------------------------------------------------------------------------
function renderResults() {
  if (results.length === 0) {
    resultsList.innerHTML = '<div class="results-empty">No generated items yet</div>';
    return;
  }

  resultsList.innerHTML = results.map((r, i) => {
    const shortText = r.text.length > 40 ? r.text.slice(0, 40) + "…" : r.text;
    const voiceLabel = r.voice.replace("Neural", "").replace("Multilingual", "");
    return `<div class="result-item" data-idx="${i}">
      <div class="result-info">
        <span class="result-text">${shortText}</span>
        <span class="result-voice">${voiceLabel} · ${r.rate}% speed · ${r.pitch}% pitch</span>
      </div>
      <span class="result-play" data-idx="${i}">▶</span>
    </div>`;
  }).join("");

  resultsList.querySelectorAll(".result-play").forEach(el => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      audioPlayer.src = results[parseInt(el.dataset.idx)].blobUrl;
      audioPlayer.play();
    });
  });

  resultsList.querySelectorAll(".result-item").forEach(el => {
    el.addEventListener("click", () => {
      const r = results[parseInt(el.dataset.idx)];
      textInputArea.value = r.text;

      // Look up full voice metadata
      const voiceInfo = allVoices.find(v => v.ShortName === r.voice);
      if (voiceInfo) {
        selectVoice(voiceInfo.ShortName, voiceInfo.Locale, voiceInfo.Gender);
        // Also set the language dropdown
        languageSelect.value = voiceInfo.Locale;
      } else {
        selectedVoice = r.voice;
        voiceSelectedName.textContent = r.voice;
        voiceSelectedMeta.textContent = "—";
      }

      speedSlider.value = r.rate;
      pitchSlider.value = r.pitch;
      updateSliderDisplay();
      updateSSMLPreview();
      renderVoiceList();

      // Switch to Text Input tab
      $$(".tab").forEach(t => t.classList.remove("active-tab"));
      document.querySelector('[data-tab="text"]').classList.add("active-tab");
      $$(".tab-content").forEach(tc => tc.classList.remove("active"));
      $("#tab-text").classList.add("active");
    });
  });
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
