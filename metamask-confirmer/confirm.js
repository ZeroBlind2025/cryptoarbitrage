#!/usr/bin/env node
// MetaMask Auto-Confirmer — Puppeteer companion script
// Watches for MetaMask notification popups and clicks "Confirm" / "Done".
//
// Usage:
//   node confirm.js [--chrome-port 9222]
//
// Prerequisites:
//   1. npm install puppeteer-core
//   2. Launch Chrome with remote debugging:
//      google-chrome --remote-debugging-port=9222 --user-data-dir="$HOME/.config/google-chrome"
//
// The script connects to your existing Chrome instance (where MetaMask is
// installed) and polls for MetaMask popup pages. When it finds one with a
// Confirm or Done button, it clicks it.

const puppeteer = require("puppeteer-core");

const CHROME_DEBUG_PORT = (() => {
  const idx = process.argv.indexOf("--chrome-port");
  return idx !== -1 ? parseInt(process.argv[idx + 1], 10) : 9222;
})();

const POLL_MS = 1500;
const CONFIRM_TEXTS = ["confirm", "done"];

async function main() {
  console.log(`[MetaMask-Confirmer] Connecting to Chrome on port ${CHROME_DEBUG_PORT}...`);

  const browser = await puppeteer.connect({
    browserURL: `http://127.0.0.1:${CHROME_DEBUG_PORT}`,
    defaultViewport: null,
  });

  console.log("[MetaMask-Confirmer] Connected. Watching for MetaMask popups...");

  // Track pages we've already handled to avoid double-clicking
  const handled = new Set();

  // eslint-disable-next-line no-constant-condition
  while (true) {
    try {
      const pages = await browser.pages();

      for (const page of pages) {
        const url = page.url();

        // MetaMask notification popups have URLs like:
        // chrome-extension://<id>/notification.html
        if (!url.includes("notification.html") && !url.includes("confirm")) continue;
        if (handled.has(url + page._target._targetId)) continue;

        const clicked = await tryClickConfirm(page);
        if (clicked) {
          handled.add(url + page._target._targetId);
        }
      }
    } catch (err) {
      // Browser may have closed tabs, pages may have navigated away — ignore
      if (err.message.includes("disconnected") || err.message.includes("closed")) {
        console.log("[MetaMask-Confirmer] Browser disconnected. Reconnecting in 5s...");
        await sleep(5000);
        try {
          const newBrowser = await puppeteer.connect({
            browserURL: `http://127.0.0.1:${CHROME_DEBUG_PORT}`,
            defaultViewport: null,
          });
          // Replace browser reference — can't reassign const, so we use the
          // reconnect pattern below
          return mainLoop(newBrowser, handled);
        } catch {
          console.log("[MetaMask-Confirmer] Reconnect failed. Exiting.");
          process.exit(1);
        }
      }
    }

    await sleep(POLL_MS);
  }
}

async function mainLoop(browser, handled) {
  // eslint-disable-next-line no-constant-condition
  while (true) {
    try {
      const pages = await browser.pages();

      for (const page of pages) {
        const url = page.url();
        if (!url.includes("notification.html") && !url.includes("confirm")) continue;

        const targetId = page._target?._targetId || url;
        if (handled.has(url + targetId)) continue;

        const clicked = await tryClickConfirm(page);
        if (clicked) {
          handled.add(url + targetId);
        }
      }
    } catch (err) {
      if (err.message.includes("disconnected") || err.message.includes("closed")) {
        console.log("[MetaMask-Confirmer] Browser disconnected. Exiting.");
        process.exit(1);
      }
    }

    await sleep(POLL_MS);
  }
}

async function tryClickConfirm(page) {
  try {
    const result = await page.evaluate((texts) => {
      const buttons = document.querySelectorAll('button, [role="button"]');
      for (const btn of buttons) {
        const btnText = (btn.textContent || "").trim().toLowerCase();
        if (texts.includes(btnText) && !btn.disabled) {
          btn.click();
          return btnText;
        }
      }
      return null;
    }, CONFIRM_TEXTS);

    if (result) {
      console.log(`[MetaMask-Confirmer] Clicked "${result}" at ${new Date().toLocaleTimeString()}`);
      return true;
    }
  } catch {
    // Page may have closed between listing and evaluating — that's fine
  }
  return false;
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

main().catch((err) => {
  console.error("[MetaMask-Confirmer] Fatal:", err.message);
  process.exit(1);
});
