const toggle = document.getElementById("toggle");
const lastRunEl = document.getElementById("lastRun");
const lastFoundEl = document.getElementById("lastFound");
const mmLastConfirmEl = document.getElementById("mmLastConfirm");
const mmLastActionEl = document.getElementById("mmLastAction");

// Load state
chrome.storage.local.get(
  { enabled: true, lastRun: null, lastFound: null, mmLastConfirm: null, mmLastAction: null },
  (data) => {
    toggle.checked = data.enabled;
    if (data.lastRun) {
      lastRunEl.textContent = new Date(data.lastRun).toLocaleTimeString();
    }
    if (data.lastFound !== null) {
      lastFoundEl.textContent = data.lastFound;
    }
    if (data.mmLastConfirm) {
      mmLastConfirmEl.textContent = new Date(data.mmLastConfirm).toLocaleTimeString();
    }
    if (data.mmLastAction) {
      mmLastActionEl.textContent = data.mmLastAction;
    }
  }
);

// Toggle handler
toggle.addEventListener("change", () => {
  chrome.storage.local.set({ enabled: toggle.checked });
});
