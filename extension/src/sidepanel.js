const chrome = globalThis.browser ?? globalThis.chrome;
const defaults = {
  bridgeUrl: "http://127.0.0.1:11435",
  token: ""
};

const assistant = document.getElementById("assistant");
const setup = document.getElementById("setup");
const status = document.getElementById("status");

function openSettings() {
  chrome.runtime.openOptionsPage();
}

async function loadAssistant() {
  const config = await chrome.storage.local.get(defaults);
  const bridgeUrl = String(config.bridgeUrl || defaults.bridgeUrl).replace(/\/+$/, "");
  const token = String(config.token || "");
  if (!token) {
    status.textContent = "Not configured";
    setup.hidden = false;
    assistant.hidden = true;
    return;
  }
  status.textContent = "Connected to local bridge";
  setup.hidden = true;
  assistant.hidden = false;
  assistant.src = `${bridgeUrl}/sidecar?token=${encodeURIComponent(token)}`;
}

document.getElementById("settings").addEventListener("click", openSettings);
document.getElementById("configure").addEventListener("click", openSettings);
chrome.storage.onChanged.addListener(loadAssistant);
loadAssistant();
