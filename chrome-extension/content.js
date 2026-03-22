// Polymarket Auto-Redeemer — Content Script
// Runs on polymarket.com/portfolio* pages.
// Finds and clicks all "Claim" / "Redeem" buttons, then schedules a page
// refresh every 15 minutes to pick up newly resolved markets.

(function () {
  const REFRESH_INTERVAL_MS = 15 * 60 * 1000; // 15 minutes
  const CLICK_DELAY_MS = 2000; // wait for page to settle before scanning
  const BETWEEN_CLICKS_MS = 1500; // pause between clicking multiple buttons

  // Selectors / text patterns for claimable buttons.
  // Polymarket uses different labels depending on context.
  const CLAIM_PATTERNS = [/^claim$/i, /^redeem$/i, /^claim\s+\$/i, /^redeem\s+\$/i];

  function findClaimButtons() {
    const buttons = document.querySelectorAll('button, [role="button"], a[class*="btn"]');
    const matches = [];
    for (const btn of buttons) {
      const text = (btn.textContent || "").trim();
      if (CLAIM_PATTERNS.some((re) => re.test(text))) {
        // Skip disabled / already-claimed buttons
        if (!btn.disabled && !btn.classList.contains("disabled") && btn.offsetParent !== null) {
          matches.push(btn);
        }
      }
    }
    return matches;
  }

  async function clickButtons(buttons) {
    let clicked = 0;
    for (const btn of buttons) {
      try {
        btn.scrollIntoView({ behavior: "smooth", block: "center" });
        await sleep(500);
        btn.click();
        clicked++;
        console.log(`[AutoRedeem] Clicked: "${btn.textContent.trim()}"`);
        await sleep(BETWEEN_CLICKS_MS);
      } catch (err) {
        console.warn("[AutoRedeem] Click failed:", err);
      }
    }
    return clicked;
  }

  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  // After clicking redeem, handle confirmation dialogs and MetaMask approve
  // buttons. Loops until no more approve/confirm buttons appear, since each
  // redemption may spawn a separate MetaMask approval.
  async function handleConfirmationDialogs() {
    const confirmPatterns = [/^confirm$/i, /^yes$/i, /^ok$/i, /^submit$/i, /^approve$/i, /^claim/i, /^redeem/i];
    const MAX_ROUNDS = 30; // up to 30 rounds (~60s max) to handle many redemptions
    let round = 0;
    let consecutiveEmpty = 0;

    while (round < MAX_ROUNDS && consecutiveEmpty < 3) {
      round++;
      await sleep(2000);
      const allButtons = document.querySelectorAll('button, [role="button"]');
      let found = 0;
      for (const btn of allButtons) {
        const text = (btn.textContent || "").trim();
        if (confirmPatterns.some((re) => re.test(text)) && !btn.disabled && btn.offsetParent !== null) {
          btn.click();
          found++;
          console.log(`[AutoRedeem] Clicked dialog button: "${text}" (round ${round})`);
          await sleep(1500);
        }
      }
      if (found > 0) {
        consecutiveEmpty = 0;
      } else {
        consecutiveEmpty++;
      }
    }
    if (round > 0) {
      console.log(`[AutoRedeem] Confirmation loop done after ${round} round(s)`);
    }
  }

  async function run() {
    // Check if extension is enabled
    const enabled = await new Promise((resolve) => {
      if (chrome.storage && chrome.storage.local) {
        chrome.storage.local.get({ enabled: true }, (data) => resolve(data.enabled));
      } else {
        resolve(true); // default on
      }
    });

    if (!enabled) {
      console.log("[AutoRedeem] Disabled — skipping scan");
      return;
    }

    console.log("[AutoRedeem] Scanning for claim/redeem buttons...");
    const buttons = findClaimButtons();

    if (buttons.length === 0) {
      console.log("[AutoRedeem] No claimable buttons found");
    } else {
      console.log(`[AutoRedeem] Found ${buttons.length} claimable button(s)`);
      const clicked = await clickButtons(buttons);
      console.log(`[AutoRedeem] Clicked ${clicked} button(s)`);
      await handleConfirmationDialogs();
    }

    // Log to storage for popup display
    if (chrome.storage && chrome.storage.local) {
      chrome.storage.local.set({
        lastRun: new Date().toISOString(),
        lastFound: buttons.length,
      });
    }
  }

  // Initial scan after page load settles
  setTimeout(run, CLICK_DELAY_MS);

  // Refresh page every 15 minutes to pick up new resolutions
  setInterval(() => {
    console.log("[AutoRedeem] Refreshing page...");
    location.reload();
  }, REFRESH_INTERVAL_MS);
})();
