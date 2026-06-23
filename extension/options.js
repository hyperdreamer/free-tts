// Options page
const DEFAULTS = { serverUrl: "http://localhost:5000", highlightColor: "#fff3cd" };

document.addEventListener("DOMContentLoaded", async () => {
  const cfg = await chrome.storage.sync.get(DEFAULTS);
  document.getElementById("serverUrl").value = cfg.serverUrl;
  document.getElementById("highlightColor").value = cfg.highlightColor;
});

document.getElementById("saveBtn").addEventListener("click", async () => {
  const serverUrl = document.getElementById("serverUrl").value.trim();
  const highlightColor = document.getElementById("highlightColor").value;
  await chrome.storage.sync.set({ serverUrl, highlightColor });
  document.getElementById("status").textContent = "Saved.";
  setTimeout(() => { document.getElementById("status").textContent = ""; }, 2000);
});
