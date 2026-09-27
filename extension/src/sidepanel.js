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

// Fallback pairing: if storage has no token yet, read the packaged
// runtime-config.json (written by the launcher) and seed storage from it.
async function resolveConfig() {
  const config = await chrome.storage.local.get(defaults);
  if (config.token) return config;
  try {
    const response = await fetch(chrome.runtime.getURL("runtime-config.json"), {
      cache: "no-store"
    });
    if (response.ok) {
      const managed = await response.json();
      if (managed.token) {
        const seed = {
          bridgeUrl: String(
            managed.bridgeUrl || config.bridgeUrl || defaults.bridgeUrl
          ).replace(/\/+$/, ""),
          token: String(managed.token)
        };
        await chrome.storage.local.set(seed);
        return seed;
      }
    }
  } catch {
    // runtime-config.json is optional when used without the launcher.
  }
  return config;
}

async function loadAssistant() {
  const config = await resolveConfig();
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
