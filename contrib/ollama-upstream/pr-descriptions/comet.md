# PR: `cmd/launch: add comet integration for Perplexity Comet`

Upstream: closes / addresses ollama/ollama#18666

## What

Adds a `comet` integration to `cmd/launch`, enabling `ollama launch comet` (alias `perplexity`) to start Perplexity Comet routed to Ollama models, in the same registry/runner architecture as existing integrations (`claude`, `droid`, `codex`, ...).

## Why

Users running Ollama locally want their local models available in Comet without hand-wiring environment variables or launch flags. This gives Comet the same one-command treatment as other supported integrations (tracked in #18666). A companion `chrome` integration (#18667) is included in the same PR series since the two share helper code.

## Implementation

- `cmd/launch/comet.go` — `Comet` runner implementing `Runner` and `SupportedIntegration` (Windows-gated; returns a clear unsupported error elsewhere). Delegates protocol handling to a companion launcher so ollama is not coupled to Comet's proprietary, version-dependent agent protocol — same delegation philosophy as the `claude` runner.
- Install flow: `CheckInstalled` probes the standard per-user install locations; `EnsureInstalled` offers an opt-in install behind a confirmation prompt (pattern follows the existing `ensureClaudeInstalled` style). Registry falls back to a URL hint when auto-install is declined.
- `cmd/launch/registry.go` — new `IntegrationSpec` entries for `chrome` and `comet`, appended to `launcherIntegrationOrder` after `qwen`.
- Tests in `cmd/launch/comet_chrome_test.go` covering registry lookup (canonical name + aliases), display names, platform gating, and argument construction.

## Testing

- `go build ./cmd/launch/...`
- `go test ./cmd/launch/...` — new tests green; existing suite unaffected (registry additions only).

## Limitations / discussion points

1. **Auto-install mechanics:** `EnsureInstalled` fetches the companion installer from its official URL and executes it after a user confirmation prompt. If maintainers prefer not to execute downloaded installers, the registry already degrades to a URL-only hint — happy to ship either mode.
2. **Windows-first:** Comet's supported surface is Windows; the runner reports unsupported cleanly on other platforms. Linux/macOS support can follow once Comet ships there.
3. **Delegation vs native CDP:** we delegate to a companion launcher rather than speaking CDP natively, to avoid coupling ollama to Comet's internal protocol. Open to folding that logic into ollama instead if maintainers prefer a native implementation.
4. Assumption: `powershell -File script.ps1 -ExtraArguments a b c` binds consecutive values to a `string[]` parameter (documented `-File` behavior); covered by an argv-construction test.