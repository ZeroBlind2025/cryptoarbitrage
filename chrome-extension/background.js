// Background service worker — keeps the portfolio tab alive via alarms
// and auto-confirms MetaMask popups using the chrome.debugger API.
//
// MetaMask popup URL structure:
//   chrome-extension://nkbihfbeogaeaoehlefnkodbefgpgknn/home.html#notification
//   (The long string is MetaMask's Chrome Web Store extension ID.)

const PORTFOLIO_URL = "https://polymarket.com/portfolio";
const ALARM_NAME = "polymarket-auto-redeem";

// MetaMask Chrome Web Store extension ID (default installation).
// If you use a custom/dev build of MetaMask, update this ID.
const METAMASK_EXTENSION_ID = "nkbihfbeogaeaoehlefnkodbefgpgknn";
const METAMASK_URL_PREFIX = `chrome-extension://${METAMASK_EXTENSION_ID}/`;

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

  const tabs = await chrome.tabs.query({ url: "https://polymarket.com/portfolio*" });
  if (tabs.length > 0) {
    chrome.tabs.reload(tabs[0].id);
  } else {
    chrome.tabs.create({ url: PORTFOLIO_URL, active: false });
  }
});

// ── MetaMask auto-confirmer ─────────────────────────────────────────────

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function isMetaMaskUrl(url) {
  return url && url.startsWith(METAMASK_URL_PREFIX);
}

function tryHandleTab(tabId) {
  if (handledTabs.has(tabId)) return;
  handledTabs.add(tabId);
  console.log(`[MetaMask Confirmer] Detected MetaMask popup: tab ${tabId}`);
  autoConfirmMetaMask(tabId);
}

// ── Detection method 1: webNavigation.onCompleted ───────────────────────
// Most reliable — fires with full URL for all frames including extension pages.
chrome.webNavigation.onCompleted.addListener(
  (details) => {
    if (details.frameId !== 0) return; // only top-level frame
    console.log(`[MetaMask Confirmer] webNavigation.onCompleted: ${details.url}`);
    tryHandleTab(details.tabId);
  },
  { url: [{ urlPrefix: METAMASK_URL_PREFIX }] }
);

// ── Detection method 2: tabs.onUpdated ──────────────────────────────────
// Fallback — tab.url may be undefined for extension pages in some Chrome versions.
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status !== "complete") return;
  if (!isMetaMaskUrl(tab.url)) return;
  tryHandleTab(tabId);
});

// ── Detection method 3: windows.onCreated ───────────────────────────────
// MetaMask opens notifications as popup windows. Detect those and check tabs inside.
chrome.windows.onCreated.addListener(async (window) => {
  if (window.type !== "popup" && window.type !== "normal") return;

  // Small delay for the tab to load its URL
  await sleep(500);

  try {
    const tabs = await chrome.tabs.query({ windowId: window.id });
    for (const tab of tabs) {
      if (isMetaMaskUrl(tab.url)) {
        tryHandleTab(tab.id);
      } else if (!tab.url || tab.url === "" || tab.url === "about:blank") {
        // URL might not be set yet — poll briefly
        for (let i = 0; i < 5; i++) {
          await sleep(600);
          try {
            const refreshed = await chrome.tabs.get(tab.id);
            if (isMetaMaskUrl(refreshed.url)) {
              tryHandleTab(tab.id);
              break;
            }
          } catch { break; } // tab closed
        }
      }
    }
  } catch (err) {
    console.warn("[MetaMask Confirmer] windows.onCreated error:", err.message);
  }
});

// Clean up tracking when tabs close.
chrome.tabs.onRemoved.addListener((tabId) => {
  handledTabs.delete(tabId);
});

// ── Auto-confirm logic ──────────────────────────────────────────────────

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
    await sleep(2500);

    // Try multiple rounds — MetaMask may take a moment to show the button,
    // or there may be a "Next" step before "Confirm".
    const MAX_ATTEMPTS = 15;
    let totalClicked = 0;

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
          totalClicked++;
          console.log(
            `[MetaMask Confirmer] Clicked "${value.text}" (attempt ${attempt}, testId: ${value.testId})`
          );

          // Log to storage for popup display
          chrome.storage.local.set({
            mmLastConfirm: new Date().toISOString(),
            mmLastAction: value.text,
          });

          // Wait for follow-up buttons (e.g. "Next" → "Confirm")
          await sleep(2500);
        } else {
          // If we already clicked something, fewer retries needed
          if (totalClicked > 0 && attempt > totalClicked + 3) break;
          if (attempt < MAX_ATTEMPTS) await sleep(1500);
        }
      } catch (evalErr) {
        console.warn(`[MetaMask Confirmer] Evaluate failed (attempt ${attempt}):`, evalErr.message);
        if (attempt < MAX_ATTEMPTS) await sleep(1500);
      }
    }

    if (totalClicked === 0) {
      console.log(`[MetaMask Confirmer] No confirm button found after ${MAX_ATTEMPTS} attempts`);
    } else {
      console.log(`[MetaMask Confirmer] Done — clicked ${totalClicked} button(s)`);
    }
  } catch (err) {
    console.error("[MetaMask Confirmer] Error:", err.message);
  } finally {
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
