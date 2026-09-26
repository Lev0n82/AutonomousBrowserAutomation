# PR: `cmd/launch: add chrome integration`

Upstream: closes / addresses ollama/ollama#18667

## What

Adds a `chrome` integration to `cmd/launch`, enabling `ollama launch chrome` (alias `chromium`) to start Chrome/Chromium routed to Ollama models.

## Why

#18667 asks for first-class Chrome support in the launcher. This rides on the same helper layer as the `comet` integration (#18666) and follows the standard registry shape, so the two land as a small, consistent series.

## Implementation

- `cmd/launch/chrome.go` — `Chrome` runner implementing `Runner` and `SupportedIntegration` (Windows-gated with clean unsupported reporting elsewhere). Launches Chrome with a dedicated profile directory so it never disturbs the user's main browser profile.
- **Chrome 136+ note:** Chrome restricts the automation surface to isolated profiles from version 136 on; the runner always uses an isolated profile, which keeps it compatible with current Chrome builds.
- `cmd/launch/registry.go` — `chrome` spec entry with `chromium` alias, `CheckInstalled` probing standard install locations, `EnsureInstalled` behind a confirmation prompt.
- `docs/integrations/chrome.mdx` — install + usage docs in the existing `docs/integrations/<name>.mdx` style.
- Tests shared with comet in `cmd/launch/comet_chrome_test.go`.

## Testing

- `go build ./cmd/launch/...`
- `go test ./cmd/launch/...`

## Limitations / discussion points

1. Auto-install vs URL-only guidance (same open question as #18666 PR).
2. Windows-first; other platforms can follow.
3. Chromium on Linux is supported via the same runner when the binary is present.