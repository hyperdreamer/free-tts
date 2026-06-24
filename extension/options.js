// Options page
const DEFAULTS = { serverUrl: "http://localhost:5000", highlightColor: "#fff3cd" };
const serverUrlInput = document.getElementById("serverUrl");
const highlightColorInput = document.getElementById("highlightColor");
const statusEl = document.getElementById("status");
const saveBtn = document.getElementById("saveBtn");

function normalizeServerUrl(value) {
  try {
    const url = new URL(value || DEFAULTS.serverUrl);
    if (!["http:", "https:"].includes(url.protocol)) return null;
    if (!["localhost", "127.0.0.1", "::1", "[::1]"].includes(url.hostname)) return null;
    url.pathname = url.pathname.replace(/\/+$/, "");
    url.search = "";
    url.hash = "";
    return url.toString().replace(/\/$/, "");
  } catch {
    return null;
  }
}

function normalizeColor(value) {
  return /^#[0-9a-fA-F]{6}$/.test(value) ? value : DEFAULTS.highlightColor;
}

function setStatus(message, isError = false) {
  statusEl.textContent = message;
  statusEl.style.color = isError ? "#b00020" : "#666";
}

document.addEventListener("DOMContentLoaded", async () => {
  const cfg = await chrome.storage.sync.get(DEFAULTS);
  serverUrlInput.value = normalizeServerUrl(cfg.serverUrl) || DEFAULTS.serverUrl;
  highlightColorInput.value = normalizeColor(cfg.highlightColor);
});

saveBtn.addEventListener("click", async () => {
  const serverUrl = normalizeServerUrl(serverUrlInput.value.trim());
  if (!serverUrl) {
    setStatus("Use a localhost or 127.0.0.1 HTTP(S) server URL.", true);
    return;
  }
  const highlightColor = normalizeColor(highlightColorInput.value);
  await chrome.storage.sync.set({ serverUrl, highlightColor });
  serverUrlInput.value = serverUrl;
  highlightColorInput.value = highlightColor;
  setStatus("Saved.");
  setTimeout(() => { statusEl.textContent = ""; }, 2000);
});
