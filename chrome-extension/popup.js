const toggle = document.getElementById("toggle");
const lastRunEl = document.getElementById("lastRun");
const lastFoundEl = document.getElementById("lastFound");

// Load state
chrome.storage.local.get({ enabled: true, lastRun: null, lastFound: null }, (data) => {
  toggle.checked = data.enabled;
  if (data.lastRun) {
    const d = new Date(data.lastRun);
    lastRunEl.textContent = d.toLocaleTimeString();
  }
  if (data.lastFound !== null) {
    lastFoundEl.textContent = data.lastFound;
  }
});

// Toggle handler
toggle.addEventListener("change", () => {
  chrome.storage.local.set({ enabled: toggle.checked });
});
