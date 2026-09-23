const chrome = globalThis.browser ?? globalThis.chrome;
const defaults = {
  bridgeUrl: "http://127.0.0.1:11435",
  token: ""
};

const bridgeUrl = document.getElementById("bridge-url");
const token = document.getElementById("token");
const status = document.getElementById("status");

async function restore() {
  const config = await chrome.storage.local.get(defaults);
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
restore();
