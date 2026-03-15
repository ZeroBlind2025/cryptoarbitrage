// Background service worker — keeps the portfolio tab alive via alarms.
// If the user closes the tab, the alarm re-opens it.

const PORTFOLIO_URL = "https://polymarket.com/portfolio";
const ALARM_NAME = "polymarket-auto-redeem";

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
