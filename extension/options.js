// Options page
const DEFAULT_SERVER = "http://localhost:5000";

document.addEventListener("DOMContentLoaded", async () => {
  const { serverUrl } = await chrome.storage.sync.get({ serverUrl: DEFAULT_SERVER });
  document.getElementById("serverUrl").value = serverUrl;
});

document.getElementById("saveBtn").addEventListener("click", async () => {
  const serverUrl = document.getElementById("serverUrl").value.trim();
  await chrome.storage.sync.set({ serverUrl });
  document.getElementById("status").textContent = "Saved.";
  setTimeout(() => { document.getElementById("status").textContent = ""; }, 2000);
});
