// Background service worker — keeps the portfolio tab alive via alarms
// and auto-confirms MetaMask popups using the chrome.debugger API.

const PORTFOLIO_URL = "https://polymarket.com/portfolio";
const ALARM_NAME = "polymarket-auto-redeem";

// MetaMask Chrome Web Store extension ID (default installation).
// If you use a custom/dev build of MetaMask, update this ID.
const METAMASK_EXTENSION_ID = "nkbihfbeogaeaoehlefnkodbefgpgknn";
const METAMASK_URL_PREFIX = `chrome-extension://${METAMASK_EXTENSION_ID}/`;
const METAMASK_NOTIFICATION_URL = `chrome-extension://${METAMASK_EXTENSION_ID}/home.html#notification`;

// Confirmation button text patterns (case-insensitive).
const CONFIRM_PATTERNS = ["confirm", "approve", "sign", "submit", "next", "connect"];

// Track tabs we've already attached to, to avoid duplicate handling.
const handledTabs = new Set();

// ── Portfolio auto-refresh ──────────────────────────────────────────────

chrome.alarms.create(ALARM_NAME, { periodInMinutes: 15 });

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== ALARM_NAME) return;

  const { enabled } = await chrome.storage.local.get({ enabled: true });
  if (!enabled) return;

  // Find existing portfolio tab
  const tabs = await chrome.tabs.query({ url: "https://polymarket.com/portfolio*" });
  if (tabs.length > 0) {
    // Reload the first matching tab
    chrome.tabs.reload(tabs[0].id);
  } else {
    // No portfolio tab open — open one
    chrome.tabs.create({ url: PORTFOLIO_URL, active: false });
  }
});

// ── MetaMask auto-confirmer ─────────────────────────────────────────────

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// Detect MetaMask popup tabs as they load.
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status !== "complete") return;
  if (!tab.url || !tab.url.startsWith(METAMASK_URL_PREFIX)) return;
  if (handledTabs.has(tabId)) return;

  handledTabs.add(tabId);
  console.log(`[MetaMask Confirmer] Detected MetaMask popup: tab ${tabId}`);
  autoConfirmMetaMask(tabId);
});

// Clean up tracking when tabs close.
chrome.tabs.onRemoved.addListener((tabId) => {
  handledTabs.delete(tabId);
});

async function autoConfirmMetaMask(tabId) {
  const { enabled } = await chrome.storage.local.get({ enabled: true });
  if (!enabled) {
    handledTabs.delete(tabId);
    return;
  }

  let attached = false;

  try {
    // Attach the debugger to the MetaMask popup tab.
    await chrome.debugger.attach({ tabId }, "1.3");
    attached = true;
    console.log(`[MetaMask Confirmer] Debugger attached to tab ${tabId}`);

    // Give the MetaMask UI time to fully render.
    await sleep(2000);

    // Try multiple rounds — MetaMask may take a moment to show the button,
    // or there may be a "Next" step before "Confirm".
    const MAX_ATTEMPTS = 10;
    let clicked = false;

    for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
      // Check if tab still exists
      try {
        await chrome.tabs.get(tabId);
      } catch {
        console.log(`[MetaMask Confirmer] Tab ${tabId} closed, stopping`);
        break;
      }

      const clickScript = `
        (function() {
          const buttons = document.querySelectorAll('button[data-testid], button');
          const patterns = ${JSON.stringify(CONFIRM_PATTERNS)};
          for (const btn of buttons) {
            const text = (btn.textContent || "").trim().toLowerCase();
            const testId = (btn.getAttribute('data-testid') || "").toLowerCase();
            for (const pat of patterns) {
              if (text === pat || text.includes(pat) || testId.includes(pat)) {
                if (!btn.disabled) {
                  btn.click();
                  return JSON.stringify({ clicked: true, text: btn.textContent.trim(), testId });
                }
              }
            }
          }
          return JSON.stringify({ clicked: false });
        })()
      `;

      try {
        const result = await chrome.debugger.sendCommand(
          { tabId },
          "Runtime.evaluate",
          { expression: clickScript, returnByValue: true }
        );

        const value = JSON.parse(result.result.value || "{}");

        if (value.clicked) {
          clicked = true;
          console.log(
            `[MetaMask Confirmer] Clicked "${value.text}" (attempt ${attempt}, testId: ${value.testId})`
          );

          // Log to storage for popup display
          chrome.storage.local.set({
            mmLastConfirm: new Date().toISOString(),
            mmLastAction: value.text,
          });

          // Wait and check for follow-up buttons (e.g. "Next" → "Confirm")
          await sleep(2000);
        } else {
          // No button found yet — wait and retry
          if (attempt < MAX_ATTEMPTS) {
            await sleep(1500);
          }
        }
      } catch (evalErr) {
        console.warn(`[MetaMask Confirmer] Evaluate failed (attempt ${attempt}):`, evalErr.message);
        if (attempt < MAX_ATTEMPTS) await sleep(1500);
      }
    }

    if (!clicked) {
      console.log(`[MetaMask Confirmer] No confirm button found after ${MAX_ATTEMPTS} attempts`);
    }
  } catch (err) {
    console.error("[MetaMask Confirmer] Error:", err.message);
  } finally {
    // Always detach the debugger
    if (attached) {
      try {
        await chrome.debugger.detach({ tabId });
        console.log(`[MetaMask Confirmer] Debugger detached from tab ${tabId}`);
      } catch {
        // Tab may already be closed
      }
    }
    handledTabs.delete(tabId);
  }
}
