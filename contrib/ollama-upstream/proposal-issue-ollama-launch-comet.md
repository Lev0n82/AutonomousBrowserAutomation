# Proposal issue draft — ollama/ollama

> Paste the body below into https://github.com/ollama/ollama/issues/new
> (choose the "Feature request" category). Title is given first.
> Contributing guide followed: https://github.com/ollama/ollama/blob/main/CONTRIBUTING.md
> ("Proposing a (non-trivial) change" — problem first, then importance, usage, testing, draft docs.)

---

## Title

```
cmd/launch: add a `comet` integration for Perplexity Comet (agentic browser)
```

## Body

### Problem

`ollama launch` currently wires Ollama models into 18 surfaces, including desktop
apps (`claude-desktop`, `chatgpt`, `hermes-desktop`, `vscode`) and CLI agents
(`claude`, `codex`, `qwen`, `cline`, ...). One agent surface is missing: **agentic
browsers**. Perplexity's Comet is a Chromium-based browser whose assistant can be
driven by a locally-served agent backend — but today there is no supported
`ollama launch` path for it. Users who want Ollama models inside Comet must
manually:

1. find and run an external bridge server that speaks Comet's loopback `/agent`
   WebSocket protocol,
2. figure out the `--perplexity-backend-url` switch Comet expects,
3. hand-edit configuration to point the backend at `http://127.0.0.1:<port>`.

Every other agent surface has a first-class integration for exactly this
bootstrapping (detect install → onboard → select model → launch). The gap is
inconsistent with the rest of `cmd/launch`.

### Why this is important

- Comet is one of the most-used agentic browsers, and agentic browsers are the
  fastest-growing agent surface after CLI coding agents.
- The hard part — a loopback bridge that emulates Comet's proprietary `/agent`
  WebSocket protocol and a companion browser extension — already exists as an
  open-source integration: https://github.com/Lev0n82/AutonomousBrowserAutomation
  (branch `OllamaComet`), with installers, tests, and documentation.
- What is missing in Ollama is only the small launcher glue that every other
  integration gets: discovery, onboarding, model selection, and one-command start.

### How it would be used

```
ollama launch comet
```

- If the companion integration is not installed, the launcher offers the
  project's one-command installer (same UX as other integrations).
- Once installed, `ollama launch comet` lets the user pick a model, starts the
  bridge (loopback only, `127.0.0.1`), and launches Comet with an isolated
  profile pointing at the bridge via `--perplexity-backend-url`.
- No Comet files are patched or replaced; the launcher only orchestrates an
  externally installed integration, like `claude-desktop` and `chatgpt` already
  do for external apps.

### How it is tested

- Registry tests following existing patterns (`TestPiInstallSpec_*`,
  `TestDeepSeekHarnessRegistry`): canonical name, aliases, launcher order, and
  install spec shape.
- Launcher flow tests with stubbed exec (same style as `launch_test.go`):
  not-installed → install prompt → configured → launched; bridge lifecycle
  (start/health-check/teardown) covered by fake subprocess.
- End-to-end verification on Windows against the real integration
  (documented in the companion repo; the `/agent` protocol itself is
  integration-tested there, not in this repo).

### Proposed implementation sketch

A thin runner that delegates to the externally installed integration — the
bridge, extension, and protocol emulation stay outside the Ollama repo (no new
dependencies, nothing vendored):

```go
// cmd/launch/comet.go (sketch)
type Comet struct{}

func (c *Comet) String() string { return "Perplexity Comet" }

func (c *Comet) findBridge() (string, error) {
    // Detect the installed companion integration, e.g. via its config
    // footprint under %LOCALAPPDATA%\OllamaComet or a `ollama-comet` shim
    // on PATH; return the launch command it provides.
    ...
}

func (c *Comet) Run(model string, _ []LaunchModel, args []string) error {
    // 1) resolve the installed integration's launcher
    // 2) exec it with the selected --model and extra args; the integration
    //    starts the loopback bridge and launches Comet with
    //    --perplexity-backend-url=http://127.0.0.1:<port>
    ...
}
```

plus a `registry.go` entry:

```go
{
    Name:        "comet",
    Runner:      &Comet{},
    Description: "Perplexity Comet browser with local Ollama models",
    Install: IntegrationInstallSpec{
        CheckInstalled: ...,
        URL: "https://github.com/Lev0n82/AutonomousBrowserAutomation",
    },
}
```

and a docs page `docs/integrations/comet.mdx`.

### Known limitations (upfront)

- Comet's `/agent` protocol is proprietary and may change between Comet
  releases. The launcher only orchestrates; protocol compatibility is owned by
  the companion integration, which keeps this change small and reviewable.
- The companion integration is Windows-first today. A `Supported()` guard can
  hide the integration on platforms where the bridge is not yet available,
  same as other platform-restricted entries.

### Draft docs

A `docs/integrations/comet.mdx` page would follow the existing integration
pages: what Comet is, install the companion integration, `ollama launch comet`,
model selection, and troubleshooting. Full draft available on request.

---

## Follow-up (after maintainer feedback)

1. Open a draft PR titled `cmd/launch: add comet integration` containing
   `cmd/launch/comet.go`, the `registry.go` entry, `launcherIntegrationOrder`
   entry, registry/launcher tests, and `docs/integrations/comet.mdx`.
2. Keep the Azure Vault / credential tooling out of upstream entirely — it is
   org-specific and stays in the source repository.
3. Commit format for the PR: `<package>: <short description>` lowercase, e.g.
   `cmd/launch: add comet integration`.

## Mechanics (user actions — no gh CLI available from this session)

- Open: https://github.com/ollama/ollama/issues/new — paste Title + Body above.
- If asked for a template, pick **Feature request**.
- After a maintainer responds positively, fork `ollama/ollama`, apply the
  `comet.go` sketch as a full file, and open the draft PR.