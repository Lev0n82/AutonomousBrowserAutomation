const chrome = globalThis.browser ?? globalThis.chrome;
const defaults = {
  bridgeUrl: "http://127.0.0.1:11435",
  token: ""
};

const bridgeUrl = document.getElementById("bridge-url");
const token = document.getElementById("token");
const status = document.getElementById("status");

async function restore() {
  let config = await chrome.storage.local.get(defaults);
  if (!config.token) {
    // Read-only prefill from the packaged runtime-config.json written by the
    // launcher; manual values in storage always take precedence once saved.
    try {
      const response = await fetch(chrome.runtime.getURL("runtime-config.json"), {
        cache: "no-store"
      });
      if (response.ok) {
        const managed = await response.json();
        if (managed.token) {
          config = {
            bridgeUrl: String(
              managed.bridgeUrl || config.bridgeUrl || defaults.bridgeUrl
            ).replace(/\/+$/, ""),
            token: String(managed.token)
          };
        }
      }
    } catch {
      // runtime-config.json is optional when used without the launcher.
    }
  }
  bridgeUrl.value = config.bridgeUrl || defaults.bridgeUrl;
  token.value = config.token || "";
}

async function save(event) {
  event.preventDefault();
  await chrome.storage.local.set({
    bridgeUrl: bridgeUrl.value.trim().replace(/\/+$/, ""),
    token: token.value.trim()
  });
  status.textContent = "Settings saved.";
  chrome.runtime.sendMessage({ type: "AUTONOMOUS_BROWSER_CONFIG_CHANGED" }).catch(() => {});
}

async function testConnection() {
  status.textContent = "Testing...";
  try {
    const response = await fetch(`${bridgeUrl.value.trim().replace(/\/+$/, "")}/api/config`, {
      headers: { "X-Ollama-Comet-Token": token.value.trim() }
    });
    if (!response.ok) throw new Error(`Bridge returned ${response.status}`);
    status.textContent = "Connection successful.";
  } catch (error) {
    status.textContent = `Connection failed: ${error.message || error}`;
  }
}

document.getElementById("form").addEventListener("submit", save);
document.getElementById("test").addEventListener("click", testConnection);

const browserProfiles = {
  firefox: {
    name: "Firefox",
    launchCommand: "ollama launch chromium --bridge-only",
    launchDetail:
      "Use --bridge-only because the launcher itself drives Chromium browsers; this way only the bridge runs and Firefox stays in control of the browser side.",
    firstTime:
      "First-time manual setup (release ZIP): open <code>about:debugging#/runtime/this-firefox</code>, select <strong>Load Temporary Add-on</strong>, and choose the extracted <code>manifest.json</code>. Temporary add-ons are removed when Firefox closes.",
    openPanel:
      "Open the Firefox sidebar and select Autonomous Browser Assistant.",
    restoreCommand: "ollama launch chromium --restore"
  },
  edge: {
    name: "Edge",
    launchCommand: "ollama launch edge",
    launchDetail: "",
    firstTime:
      "First-time manual setup (release ZIP): open <code>edge://extensions</code>, enable <strong>Developer mode</strong>, select <strong>Load unpacked</strong>, and choose the extracted extension folder that contains <code>manifest.json</code>.",
    openPanel:
      "Pin Autonomous Browser Assistant from the Extensions menu, then select its toolbar icon to open the side panel.",
    restoreCommand: "ollama launch edge --restore"
  },
  chromium: {
    name: "Chromium",
    launchCommand: "ollama launch chromium",
    launchDetail: "",
    firstTime:
      "First-time manual setup (release ZIP): open <code>chrome://extensions</code>, enable <strong>Developer mode</strong>, select <strong>Load unpacked</strong>, and choose the extracted extension folder that contains <code>manifest.json</code>.",
    openPanel:
      "Pin Autonomous Browser Assistant from the extensions menu, then select its toolbar icon to open the side panel.",
    restoreCommand: "ollama launch chromium --restore"
  },
  chrome: {
    name: "Chrome",
    launchCommand: "ollama launch chrome",
    launchDetail: "",
    firstTime:
      "First-time manual setup (release ZIP): open <code>chrome://extensions</code>, enable <strong>Developer mode</strong>, select <strong>Load unpacked</strong>, and choose the extracted extension folder that contains <code>manifest.json</code>.",
    openPanel:
      "Pin Autonomous Browser Assistant from the Extensions menu, then select its toolbar icon to open the side panel.",
    restoreCommand: "ollama launch chrome --restore"
  }
};

function detectBrowser() {
  const ua = navigator.userAgent;
  if (ua.includes("Firefox/")) return "firefox";
  if (ua.includes("Edg/")) return "edge";
  if (ua.includes("Chromium") && !ua.includes("Chrome")) return "chromium";
  return "chrome";
}

function fillInstructions() {
  const profile = browserProfiles[detectBrowser()];
  const fillText = (name, value) => {
    document.querySelectorAll(`[data-fill="${name}"]`).forEach((el) => {
      el.textContent = value;
    });
  };
  fillText("launch-command", profile.launchCommand);
  fillText("restore-command", profile.restoreCommand);
  const launchDetail = document.querySelector('[data-fill="launch-detail"]');
  launchDetail.textContent = ` ${profile.launchDetail}`;
  launchDetail.hidden = !profile.launchDetail;
  fillText("open-panel", profile.openPanel);
  const firstTimeNote = document.getElementById("first-time-note");
  if (profile.firstTime) {
    firstTimeNote.innerHTML = profile.firstTime;
    firstTimeNote.hidden = false;
  }
}

fillInstructions();
restore();
