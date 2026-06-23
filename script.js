/**
 * SSML TTS Generator — Frontend Logic
 *
 * Two modes:
 *   1. SSML tab — raw SSML editing with the default template.
 *   2. Test Input tab — user-friendly controls that build SSML behind the scenes.
 *
 * The Test Input always generates valid SSML using the required template
 * structure, substituting voice name, prosody rate/pitch, and text content.
 */

// ---------------------------------------------------------------------------
// Default SSML template (as specified by the user)
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

// ---------------------------------------------------------------------------
// Common edge-tts voices (English subset — expand as needed)
// ---------------------------------------------------------------------------
const VOICES = [
  { name: "en-US-AvaMultilingualNeural", locale: "en-US", label: "Ava" },
  { name: "en-US-AvaNeural",              locale: "en-US", label: "Ava" },
  { name: "en-US-AndrewMultilingualNeural", locale: "en-US", label: "Andrew" },
  { name: "en-US-AndrewNeural",           locale: "en-US", label: "Andrew" },
  { name: "en-US-AnaNeural",             locale: "en-US", label: "Ana" },
  { name: "en-US-AriaNeural",            locale: "en-US", label: "Aria" },
  { name: "en-US-BrianMultilingualNeural", locale: "en-US", label: "Brian" },
  { name: "en-US-BrianNeural",           locale: "en-US", label: "Brian" },
  { name: "en-US-ChristopherNeural",     locale: "en-US", label: "Christopher" },
  { name: "en-US-EmmaMultilingualNeural", locale: "en-US", label: "Emma" },
  { name: "en-US-EmmaNeural",            locale: "en-US", label: "Emma" },
  { name: "en-US-EricNeural",            locale: "en-US", label: "Eric" },
  { name: "en-US-GuyNeural",             locale: "en-US", label: "Guy" },
  { name: "en-US-JennyNeural",           locale: "en-US", label: "Jenny" },
  { name: "en-US-MichelleNeural",        locale: "en-US", label: "Michelle" },
  { name: "en-US-RogerNeural",           locale: "en-US", label: "Roger" },
  { name: "en-US-SteffanNeural",         locale: "en-US", label: "Steffan" },
  { name: "en-GB-SoniaNeural",           locale: "en-GB", label: "Sonia" },
  { name: "en-GB-RyanNeural",            locale: "en-GB", label: "Ryan" },
  { name: "en-GB-LibbyNeural",           locale: "en-GB", label: "Libby" },
  { name: "en-GB-MaisieNeural",          locale: "en-GB", label: "Maisie" },
  { name: "en-AU-NatashaNeural",         locale: "en-AU", label: "Natasha" },
  { name: "en-AU-WilliamNeural",         locale: "en-AU", label: "William" },
  { name: "en-CA-ClaraNeural",           locale: "en-CA", label: "Clara" },
  { name: "en-CA-LiamNeural",            locale: "en-CA", label: "Liam" },
  { name: "en-IN-NeerjaNeural",          locale: "en-IN", label: "Neerja" },
  { name: "en-IN-PrabhatNeural",         locale: "en-IN", label: "Prabhat" },
];

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let selectedVoice = DEFAULT_VOICE;
let results = []; // { text, voice, rate, pitch, blobUrl }

// ---------------------------------------------------------------------------
// Backend URL
// ---------------------------------------------------------------------------
const BACKEND_URL = "http://localhost:5000/generate-and-download-tts";

// ---------------------------------------------------------------------------
// DOM references
// ---------------------------------------------------------------------------
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// SSML tab
const ssmlInput      = $("#ssmlInput");
const downloadSsmlBtn = $("#downloadSsmlBtn");

// Tabs
const tabs           = $$(".tab");
const tabContents    = $$(".tab-content");

// Test Input
const voiceSearch    = $("#voiceSearch");
const voiceDropdown  = $("#voiceDropdown");
const voiceSelected  = $("#voiceSelected");
const testTextInput  = $("#testTextInput");
const charCount      = $("#charCount");
const speedSlider    = $("#speedSlider");
const speedValue     = $("#speedValue");
const pitchSlider    = $("#pitchSlider");
const pitchValue     = $("#pitchValue");
const previewBtn     = $("#previewBtn");
const downloadTestBtn = $("#downloadTestBtn");

// Right panel
const panelTabs      = $$(".panel-tab");
const panelContents  = $$(".panel-content");
const ssmlPreview    = $("#ssmlPreview");
const resultsList    = $("#resultsList");

// Audio
const audioPlayer    = $("#audioPlayer");

// ---------------------------------------------------------------------------
// SSML construction (replaces template placeholders)
// ---------------------------------------------------------------------------
function buildSSML(voice, text, rateSliderVal, pitchSliderVal) {
  // rate: slider -50..200 → SSML rate percentage
  //   slider  0 → SSML rate="100%" → backend → +0%
  //   slider +50 → SSML rate="150%" → backend → +50%
  //   slider -20 → SSML rate="80%"  → backend → -20%
  const rateAttr = (100 + rateSliderVal) + "%";

  // pitch: slider -50..50 → SSML pitch
  //   0 → "0%" (backend converts to +0Hz)
  //   ±N → "±NHz"
  const pitchAttr = pitchSliderVal === 0
    ? "0%"
    : (pitchSliderVal > 0 ? "+" : "") + pitchSliderVal + "Hz";

  return SSML_TEMPLATE
    .replace("VOICE_NAME", voice)
    .replace("RATE%", rateAttr)
    .replace("PITCH%", pitchAttr)
    .replace("TEXT_CONTENT", text);
}

// Fill the SSML textarea with defaults
function fillDefaultSSML() {
  ssmlInput.value = buildSSML(DEFAULT_VOICE, DEFAULT_TEXT, 0, 0);
}

// ---------------------------------------------------------------------------
// Tab switching
// ---------------------------------------------------------------------------
tabs.forEach(tab => {
  tab.addEventListener("click", () => {
    const target = tab.dataset.tab;

    tabs.forEach(t => t.classList.remove("active-tab"));
    tab.classList.add("active-tab");

    tabContents.forEach(tc => tc.classList.remove("active"));
    $(`#tab-${target}`).classList.add("active");
  });
});

// Panel tab switching
panelTabs.forEach(tab => {
  tab.addEventListener("click", () => {
    const target = tab.dataset.panel;

    panelTabs.forEach(t => t.classList.remove("active"));
    tab.classList.add("active");

    panelContents.forEach(pc => pc.classList.remove("active"));
    $(`#${target}`).classList.add("active");
  });
});

// ---------------------------------------------------------------------------
// Voice search / dropdown
// ---------------------------------------------------------------------------
function renderVoiceDropdown(filter = "") {
  const lower = filter.toLowerCase();
  const filtered = VOICES.filter(v =>
    v.name.toLowerCase().includes(lower) ||
    v.label.toLowerCase().includes(lower) ||
    v.locale.toLowerCase().includes(lower)
  );

  if (filtered.length === 0) {
    voiceDropdown.innerHTML = '<div class="voice-option" style="color:var(--text-muted)">No voices found</div>';
    voiceDropdown.classList.add("open");
    return;
  }

  voiceDropdown.innerHTML = filtered.map(v => {
    const active = v.name === selectedVoice ? " active" : "";
    return `<div class="voice-option${active}" data-voice="${v.name}" data-locale="${v.locale}">
      <span>${v.label}</span>
      <span class="voice-option-locale">${v.locale}</span>
    </div>`;
  }).join("");

  voiceDropdown.classList.add("open");
}

function selectVoice(name, locale) {
  selectedVoice = name;
  voiceSelected.innerHTML = `
    <span class="voice-name">${name}</span>
    <span class="voice-locale">${locale}</span>
  `;
  voiceSearch.value = "";
  voiceDropdown.classList.remove("open");
  updateSSMLPreview();
}

voiceSearch.addEventListener("focus", () => renderVoiceDropdown(voiceSearch.value));
voiceSearch.addEventListener("input", () => renderVoiceDropdown(voiceSearch.value));

voiceDropdown.addEventListener("click", (e) => {
  const opt = e.target.closest(".voice-option");
  if (!opt) return;
  const name = opt.dataset.voice;
  const locale = opt.dataset.locale;
  if (name) selectVoice(name, locale);
});

// Close dropdown on outside click
document.addEventListener("click", (e) => {
  if (!voiceSearch.contains(e.target) && !voiceDropdown.contains(e.target)) {
    voiceDropdown.classList.remove("open");
  }
});

// Keyboard navigation in dropdown
voiceSearch.addEventListener("keydown", (e) => {
  const opts = voiceDropdown.querySelectorAll(".voice-option");
  if (!opts.length) return;

  const current = voiceDropdown.querySelector(".voice-option.active");
  let idx = Array.from(opts).indexOf(current);

  if (e.key === "ArrowDown") {
    e.preventDefault();
    idx = (idx + 1) % opts.length;
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    idx = (idx - 1 + opts.length) % opts.length;
  } else if (e.key === "Enter") {
    e.preventDefault();
    if (current) {
      selectVoice(current.dataset.voice, current.dataset.locale);
    }
    return;
  } else {
    return;
  }

  opts.forEach(o => o.classList.remove("active"));
  opts[idx].classList.add("active");
});

// ---------------------------------------------------------------------------
// Sliders → live value display + SSML preview
// ---------------------------------------------------------------------------
function updateSliderDisplay() {
  speedValue.textContent = speedSlider.value + "%";
  pitchValue.textContent = pitchSlider.value + "%";
}

function updateSSMLPreview() {
  const text = testTextInput.value || "[enter text]";
  const ssml = buildSSML(selectedVoice, text, parseInt(speedSlider.value), parseInt(pitchSlider.value));
  ssmlPreview.textContent = ssml;

  // Also update char count
  charCount.textContent = testTextInput.value.length + " characters";
}

speedSlider.addEventListener("input", () => { updateSliderDisplay(); updateSSMLPreview(); });
pitchSlider.addEventListener("input", () => { updateSliderDisplay(); updateSSMLPreview(); });
testTextInput.addEventListener("input", () => { updateSSMLPreview(); });

// ---------------------------------------------------------------------------
// TTS API call
// ---------------------------------------------------------------------------
async function callTTS(ssml) {
  const resp = await fetch(BACKEND_URL, {
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
// Test Input: preview & download
// ---------------------------------------------------------------------------
async function handleTestGenerate(preview = false) {
  const text = testTextInput.value.trim();
  if (!text) return alert("Please enter text to synthesize.");

  const rate = parseInt(speedSlider.value);
  const pitch = parseInt(pitchSlider.value);
  const ssml = buildSSML(selectedVoice, text, rate, pitch);

  // Disable buttons
  previewBtn.disabled = true;
  downloadTestBtn.disabled = true;
  const label = preview ? "Generating preview..." : "Generating MP3...";
  const btn = preview ? previewBtn : downloadTestBtn;
  const origText = btn.textContent;
  btn.textContent = label;

  try {
    const blob = await callTTS(ssml);

    // Add to results
    const blobUrl = URL.createObjectURL(blob);
    results.unshift({ text, voice: selectedVoice, rate, pitch, blobUrl });
    if (results.length > 20) results.pop(); // cap
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
    downloadTestBtn.disabled = false;
    previewBtn.textContent = "▶ Preview Audio";
    downloadTestBtn.textContent = "⬇ Download MP3";
  }
}

previewBtn.addEventListener("click", () => handleTestGenerate(true));
downloadTestBtn.addEventListener("click", () => handleTestGenerate(false));

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

  // Play on click
  resultsList.querySelectorAll(".result-play").forEach(el => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      const idx = parseInt(el.dataset.idx);
      audioPlayer.src = results[idx].blobUrl;
      audioPlayer.play();
    });
  });

  // Click row to re-fill inputs
  resultsList.querySelectorAll(".result-item").forEach(el => {
    el.addEventListener("click", () => {
      const idx = parseInt(el.dataset.idx);
      const r = results[idx];
      testTextInput.value = r.text;

      // Find and select the voice
      const voiceObj = VOICES.find(v => v.name === r.voice);
      if (voiceObj) selectVoice(voiceObj.name, voiceObj.locale);
      else {
        selectedVoice = r.voice;
        voiceSelected.innerHTML = `
          <span class="voice-name">${r.voice}</span>
          <span class="voice-locale">—</span>`;
      }

      speedSlider.value = r.rate;
      pitchSlider.value = r.pitch;
      updateSliderDisplay();
      updateSSMLPreview();

      // Switch to test tab
      tabs.forEach(t => t.classList.remove("active-tab"));
      document.querySelector('[data-tab="test"]').classList.add("active-tab");
      tabContents.forEach(tc => tc.classList.remove("active"));
      $("#tab-test").classList.add("active");
    });
  });
}

// ---------------------------------------------------------------------------
// Keyboard shortcut: Ctrl+Enter in SSML textarea → download
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
function init() {
  fillDefaultSSML();
  updateSliderDisplay();
  updateSSMLPreview();
}

init();
