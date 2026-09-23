# Autonomous Browser Automation

An Ollama-powered browser assistant with a reusable WebExtension control plane.

The `OllamaComet` branch preserves the working Perplexity Comet integration and
introduces an independent extension foundation for Chrome, Microsoft Edge, and
Firefox.

## Repository layout

- `ollama-comet/` - current Windows launcher, loopback bridge, Ollama local/cloud
  support, Comet native-agent integration, attachments, and test harness.
- `extension/` - browser-neutral action engine, assistant side panel, secure
  local settings, and browser-specific Manifest V3 packages.

## Extension capabilities

- Persistent Chrome/Edge side panel and Firefox sidebar
- Authenticated loopback connection to the Ollama browser bridge
- Tab navigation and creation
- Semantic page reading with stable element references
- Form input, clicks, typing, keys, scrolling, drag, screenshots, and waits
- Explicit confirmation enforcement for sensitive actions
- Shared source with separate Chromium and Firefox manifests

Chrome and Edge use the DevTools debugger API for trusted coordinate input when
available. Firefox uses DOM-event fallbacks and semantic element references.

## Build

```powershell
.\extension\scripts\build.ps1
node .\extension\scripts\validate.mjs
```

Load `extension\dist\chromium` as an unpacked extension in Chrome or Edge.
For Firefox development, load `extension\dist\firefox\manifest.json` as a
temporary add-on from `about:debugging`.

Open extension settings and configure:

- Bridge URL: `http://127.0.0.1:11435`
- Bridge token: the current value stored by the launcher in
  `%LOCALAPPDATA%\OllamaComet\bridge.token`

The bridge and extension communicate only over loopback. Never publish the
runtime token, cloud API key, local configuration, or browser profile.

When an extension is actively polling the bridge, browser tools are routed to
that extension. If no independent extension is connected, the `OllamaComet`
branch falls back to Comet's signed native agent.

The extension necessarily reads the active page and sends requested page
content to the configured local bridge. When Ollama Cloud is selected, relevant
prompt, page, and image content is transmitted to Ollama Cloud for inference.

## Status

This is an early cross-browser foundation. The existing Comet path remains the
most complete implementation while the independent WebExtension path is tested
against Chrome, Edge, and Firefox.

## License

MIT. Perplexity Comet and Ollama are separate third-party products and are not
distributed by this repository.
