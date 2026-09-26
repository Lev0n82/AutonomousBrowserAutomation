# Upstream ollama integration kit

Everything needed to land `ollama launch comet` and `ollama launch chrome` in
[ollama/ollama](https://github.com/ollama/ollama) the moment maintainers give a
green light on [#18666](https://github.com/ollama/ollama/issues/18666) and
[#18667](https://github.com/ollama/ollama/issues/18667).

This kit mirrors the upstream `cmd/launch` architecture (registry + runners, as
of upstream blob `ac3220a1bc9ca2d6c990c94146a7cb8d165f2eed`) and reuses the
companion launchers shipped in this repository.

## Contents → upstream paths

| Kit file | Upstream path (ollama/ollama) | PR |
| --- | --- | --- |
| `cmd/launch/comet.go` | `cmd/launch/comet.go` | comet |
| `cmd/launch/chrome.go` | `cmd/launch/chrome.go` | chrome |
| `cmd/launch/comet_chrome_test.go` | `cmd/launch/comet_chrome_test.go` | both |
| `cmd/launch/registry-comet-chrome.patch` | applies to `cmd/launch/registry.go` | both |
| `docs/integrations/comet.mdx` | `docs/integrations/comet.mdx` | comet |
| `docs/integrations/chrome.mdx` | `docs/integrations/chrome.mdx` | chrome |
| `proposal-issue-ollama-launch-comet.md` | (reference — proposal issue draft) | — |
| `issue-comments.md` | (paste-ready comments for #18666 / #18667) | — |
| `pr-descriptions/comet.md` | (PR body) | comet |
| `pr-descriptions/chrome.md` | (PR body) | chrome |

## Before opening the PR

1. Post the paste-ready comments from `issue-comments.md` on
   [#18666](https://github.com/ollama/ollama/issues/18666) and
   [#18667](https://github.com/ollama/ollama/issues/18667) so maintainers know
   the implementation is ready.
2. Fork `ollama/ollama` and create a branch, e.g. `launch-comet-chrome`.
3. Re-check upstream `cmd/launch/registry.go` hasn't changed since
   `ac3220a1` (the patch is built against that exact blob). If it moved,
   re-apply the two edits by hand (order line + two spec entries) or regenerate.

## Applying to the fork

```bash
# from the fork checkout (based on current upstream main)
cp cmd/launch/comet.go cmd/launch/chrome.go cmd/launch/comet_chrome_test.go .   # from kit cmd/launch/
mkdir -p docs/integrations && cp docs/integrations/*.mdx docs/integrations/
git apply cmd/launch/registry-comet-chrome.patch
go build ./cmd/launch/...
go test ./cmd/launch/...
```

## Commit + PR

Two commits, upstream style (lowercase, `area: summary`):

- `cmd/launch: add comet integration for Perplexity Comet` — comet.go + comet.mdx + registry patch + tests
- `cmd/launch: add chrome integration` — chrome.go + chrome.mdx + tests

Open one PR containing both (they share the registry patch and test file) with
the body from `pr-descriptions/comet.md` + `chrome.md`, or split into two PRs
against the same series if reviewers prefer atomic changes.

## Design notes for review discussion

- **Delegation architecture:** Comet's agent protocol is proprietary and
  version-dependent, so the runners delegate to the companion launchers in this
  repo rather than speaking CDP natively. This mirrors how the `claude`
  integration delegates to the Claude CLI. Flagged as discussion point #3 in
  the comet PR description.
- **Auto-install:** `EnsureInstalled` runs an opt-in installer behind a
  confirmation prompt; the registry already degrades to URL-only guidance if
  maintainers prefer not to execute fetched installers.
- **Windows-first:** both runners implement `SupportedIntegration` with a
  Windows gate and report unsupported cleanly elsewhere.
- **Private info excluded:** no internal tenant IDs, secrets, or vault tooling
  are referenced anywhere in this kit — upstream contribution only contains
  public, self-contained code.

## Patch provenance

`registry-comet-chrome.patch` was generated against upstream blob
`ac3220a1bc9ca2d6c990c94146a7cb8d165f2eed` (`cmd/launch/registry.go`) and
verified with `git apply --check` on a pristine copy:
`ac3220a1 → 333833c`, +29/−1 (one order-line change, two spec entries).