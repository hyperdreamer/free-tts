// Options page
const DEFAULTS = { serverUrl: "http://localhost:5000", highlightColor: "#fff3cd" };
const hostInput = document.getElementById("host");
const portInput = document.getElementById("port");
const highlightColorInput = document.getElementById("highlightColor");
const statusEl = document.getElementById("status");
const saveBtn = document.getElementById("saveBtn");

function normalizeColor(value) {
  return /^#[0-9a-fA-F]{6}$/.test(value) ? value : DEFAULTS.highlightColor;
}

function setStatus(message, isError = false) {
  statusEl.textContent = message;
  statusEl.style.color = isError ? "#b00020" : "#666";
}

function parseUrl(serverUrl) {
  try {
    const u = new URL(serverUrl);
    return { host: u.hostname, port: u.port || "5000" };
  } catch {
    return { host: "localhost", port: "5000" };
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  const cfg = await chrome.storage.sync.get(DEFAULTS);
  const { host, port } = parseUrl(cfg.serverUrl);
  hostInput.value = host;
  portInput.value = port;
  highlightColorInput.value = normalizeColor(cfg.highlightColor);
});

saveBtn.addEventListener("click", async () => {
  const host = hostInput.value.trim() || "localhost";
  const port = parseInt(portInput.value) || 5000;
  if (port < 1 || port > 65535) {
    setStatus("Port must be between 1 and 65535.", true);
    return;
  }
  const serverUrl = `http://${host}:${port}`;
  const highlightColor = normalizeColor(highlightColorInput.value);
  await chrome.storage.sync.set({ serverUrl, highlightColor });
  setStatus("Saved.");
  setTimeout(() => { statusEl.textContent = ""; }, 2000);
});
