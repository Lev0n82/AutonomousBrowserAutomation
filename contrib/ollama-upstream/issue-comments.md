# Upstream issue comments (paste-ready)

These are paste-ready comments for the upstream tracking issues. The GitHub MCP tools available in this environment are read-only for other users' repos, so posting is manual.

---

## For ollama/ollama#18666 — `ollama launch comet`

```markdown
Working on this from the consumer side — happy to share an implementation that can be upstreamed.

**Approach:** a `comet` integration in `cmd/launch` that reuses the existing runner/registry architecture (same pattern as `claude`, `droid`, etc.):

- `cmd/launch/comet.go` — `Comet` runner implementing `Runner` + `SupportedIntegration`. Launches Comet against Ollama's local endpoint with the requested model routed to the browser agent.
- `cmd/launch/chrome.go` — companion `Chrome` runner (tracked in #18667) sharing the same helpers.
- Registry entries in `cmd/launch/registry.go` with `perplexity` (comet) and `chromium` (chrome) aliases.
- Docs under `docs/integrations/comet.mdx` following the existing `<name>.mdx` convention.

**Design note:** Comet's agent protocol is proprietary and version-dependent, so instead of speaking CDP directly, the runner delegates to a small companion launcher that handles profile isolation, env routing (`OLLAMA_HOST`), and model selection. This keeps upstream code simple and avoids coupling ollama to Perplexity's internal protocol; it mirrors how `claude` delegates to the Claude CLI.

**Install experience:** `CheckInstalled` looks for the companion launcher in the standard per-user locations; `EnsureInstalled` offers an opt-in install (confirm prompt, then the official installer URL) — same shape as the `EnsureInstalled` hooks already in the registry. We're open to either (a) the confirm-prompt auto-install, or (b) URL-only guidance if maintainers prefer no installer fetching.

**Status:** working code + tests + docs + registry patch ready; happy to open the PR once there's a green light, or earlier if preferred.
```

---

## For ollama/ollama#18667 — `ollama launch chrome/chromium`

```markdown
Working on this too — implementation ready for upstream.

**Approach:** a `chrome` integration in `cmd/launch` alongside the `comet` one (#18666), sharing helpers:

- `cmd/launch/chrome.go` — `Chrome` runner implementing `Runner` + `SupportedIntegration`, launching Chrome (or `chromium` on Linux) against the Ollama endpoint with an isolated user-data dir so it doesn't disturb the user's main profile.
- Registry entry with `chromium` alias, `CheckInstalled`/`EnsureInstalled` in the standard registry shape.
- `docs/integrations/chrome.mdx`.

**Version note:** Chrome restricts the relevant automation surface to isolated profiles from Chrome 136+; the integration handles that by always launching with a dedicated profile directory.

**Install experience:** same open question as #18666 — confirm-prompt auto-install vs URL-only guidance; both are implemented in a switchable way.

**Status:** code + tests + docs + registry patch ready; can open PRs for both integrations on maintainer go-ahead.
```