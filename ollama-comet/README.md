# Ollama launcher for Perplexity Comet

This integration adds a user-level command:

```powershell
ollama launch chrome
ollama launch edge
ollama launch comet
```

It does **not** patch or replace the signed Comet executable. It starts a loopback-only compatibility bridge and launches an isolated Comet profile with the discovered `--perplexity-backend-url` switch.

## What works

- Local Ollama models through `http://127.0.0.1:11434`
- Direct Ollama Cloud through `https://ollama.com` with a Windows DPAPI-protected API key
- Local/Cloud provider and model selection in the assistant UI
- Model selection with `--model`
- The native Comet assistant button opens the local Ollama assistant
- Chrome and Edge launch in isolated profiles connected through the independent
  Autonomous Browser Assistant extension
- A responsive pastel-teal assistant layout with an independently scrolling conversation and a composer that stays visible in smaller windows
- Safe Markdown rendering, including headings, lists, code blocks, and tables
- Image paste/upload plus local text extraction from PDF, DOCX, and XLSX attachments
- Ollama tool calling can read and control tabs in the isolated Comet profile
- Existing Ollama commands are delegated to the official `ollama.exe`

## Browser actions

The loopback bridge connects to Comet's signed `comet-agent` extension through its native `/agent` WebSocket protocol and exposes the verified RPC method names:

- `Navigate`: open a URL or navigate back/forward
- `ReadPage`: return page text plus referenced interactive elements
- `GetPageText`: return readable page text
- `FormInput`: set a referenced form control
- `TabsCreate`: create and activate a tab
- `TabsList`: list open tabs with titles, URLs, and tab IDs
- `EvaluateJS`: evaluate a JavaScript snippet in the active page and return the JSON result
- `ComputerBatch`: run `SCREENSHOT`, `WAIT`, `LEFT_CLICK`, `RIGHT_CLICK`, `DOUBLE_CLICK`, `TRIPLE_CLICK`, `TYPE`, `KEY`, `SCROLL`, `LEFT_CLICK_DRAG`, and `SCROLL_TO` actions

Navigation, reading, scrolling, and harmless form preparation can run automatically. Submitting forms, sending messages, purchases, deletion, downloads, credential entry, and account changes require explicit confirmation in the user's latest request.

The conversation remains in the Ollama assistant section while the signed Comet extension activates and controls tabs in the main browser section. The native sidecar is opened once when a task begins and is not automatically collapsed or repeatedly forced open, so the user remains free to close it manually. Native RPC responses include Comet tab context, accessibility references, screenshots, and native numeric tab IDs.

## Model vision

`enable_vision` in `%LOCALAPPDATA%\OllamaComet\config.json` controls whether recent agent screenshots are attached to the model conversation:

- `auto` (default): screenshots are attached when the selected model advertises vision capability.
- `true`: always attach up to four recent screenshots.
- `false`: never attach screenshots; the agent reasons from page structure only.

## Azure Key Vault tools

When `vault_uri` and `vault_name` are configured in `%LOCALAPPDATA%\OllamaComet\config.json`, the assistant gains five vault tools:

| Tool | Purpose | Gating |
|---|---|---|
| `VaultConnect` | Verify connectivity (companion health check or Azure device-code sign-in) | None |
| `VaultListCredentials` | List vault accounts grouped by email, with category and password presence | None |
| `VaultGetCredential` | Look up the password for an email; masked unless reveal is allowed | Masked by default |
| `VaultLogin` | Navigate to a site, locate credential fields, and sign in with the vault password | Requires sensitive actions allowed |
| `VaultResetPassword` | Rotate a live password and sync the new value back to the vault | Requires sensitive actions allowed **and** `confirm=true` |

### Configuration keys

| Key | Purpose |
|---|---|
| `vault_uri` | Key Vault URI, e.g. `https://qa-dev-app.vault.azure.net` |
| `vault_name` | Vault name, e.g. `qa-dev-app` |
| `vault_api_port` | Local companion app API port (default `8080`; `0` forces direct Azure REST access) |
| `vault_api_key` / `vault_api_key_secret_uri` | Companion API key, static or resolved from a secret |
| `keychain_dev_uri` / `keychain_qa_uri` | Dev/QA keychain secret URIs |
| `azure_tenant_id` / `azure_client_id` | Azure AD app for direct vault access |
| `graph_tenant_id` / `graph_client_id` | Microsoft Graph app for OTP mailbox access |
| `reset_email` | Mailbox that receives password-reset OTP codes |

Passwords are stored as one secret per account: the email is lowercased with `_` → `---`, `@` → `--`, `.` → `-`, and the password secret gains a `---password` suffix. Account domains classify as `EntraID` (`ontario.ca`, `gov.on.ca`, `oag.on.ca`) or `OPS-BPS-Secure` (everything else). Vault passwords are masked (`abc***`) in tool output unless the user explicitly allows sensitive data. When a login is rejected because the password is expired or incorrect, the agent can propose `VaultResetPassword`: it sends a forgot-password request to OPS-BPS Secure, reads the OTP from the reset mailbox through Microsoft Graph, generates a strong 17-character password, submits the reset, and writes the new password back to the vault as `<email>---password`.

The launcher binds the bridge and DevTools bootstrap endpoint to `127.0.0.1`; DevTools is used only to open an allowed Perplexity bootstrap origin and dispatch `START_AGENT`. All navigation, reading, form input, and computer actions are executed by the signed Comet extension. The assistant API and `/agent` WebSocket use a random per-launch token and control only the isolated profile at `%LOCALAPPDATA%\OllamaComet\Comet Profile`.

## Azure DevOps QA tools

When `ado_org` and `ado_pat` are configured, the assistant gains ten tools to locate Excel-based test cases in project git repositories, inspect them, execute GRACE tests, and publish results to Azure Test Plans:

| Tool | Purpose | Gating |
|---|---|---|
| `AdoConnect` | Validate the connection and list accessible projects | None |
| `AdoListRepositories` | List git repositories in a project | None |
| `AdoFindTestFiles` | Search a repository for `.xlsx` test files | None |
| `AdoInspectTestFile` | Peek inside a workbook: sheets, preview strings, GRACE-likeness | None |
| `AdoListTestPlans` | List test plans | None |
| `AdoListSuites` | List suites under a plan | None |
| `AdoListTestPoints` | List test points under a suite | None |
| `AdoListTestRuns` | List recent test runs (including GRACE self-reported runs) | None |
| `AdoRunGraceTest` | Send an Excel workbook to the GRACE API for execution | Requires `confirm=true` |
| `AdoPublishTestRun` | Publish xlsx test results to a test run in Azure Test Plans | Requires `confirm=true` |

### Configuration keys

| Key | Purpose |
|---|---|
| `ado_org` | Azure DevOps organization, e.g. `https://dev.azure.com/myorg` or just `myorg` |
| `ado_pat` | Personal access token (read/write on Work Items, Test Management, Code) |
| `ado_project` | Optional default project name; tools also accept a `project` argument |
| `ado_grace_api` | Optional GRACE API base URL for `AdoRunGraceTest` |
| `ado_grace_token` | Optional GRACE API bearer token |

The work item links created for published runs use the configured `ado_org`. Results rows are read from the workbook's test-result sheets (test case, test point, step, outcome) and posted to the run. PATs are never displayed in tool output; `AdoConnect` returns only a masked confirmation. When `ado_grace_api` is set, `AdoRunGraceTest` posts the workbook to the GRACE API, which reports outcomes to Azure Test Plans itself; poll `AdoListTestRuns` to observe them.

## Launchpad tools (1-click launch)

The sidecar shows a 🚀 Launchpad strip of configured environments. Each button queues a confirmed sign-in task for that environment: open the URL, fill the login form with the account's vault credential, submit, and classify the outcome.

| Tool | Purpose | Gating |
|---|---|---|
| `ListEnvironments` | List configured launchpad environments | None |
| `LaunchEnvironment` | Open an environment and sign in with its vault credential | Requires `confirm=true` and sensitive-action approval |

Manage environments in the sidecar (🚀 Launchpad → **Manage**), or edit `config.json`:

| Key | Purpose |
|---|---|
| `environments` | `{ "Name": { "url": "...", "account": "email" } }` |

Outcomes: `signed_in`, `mfa_required` / `captcha_detected` (automation stops and hands control back to the user), `login_form_still_present` (verify manually), `password_expired` (offer `VaultResetPassword`, OPS-BPS-Secure accounts only), or `preview` when `confirm` is false. Requires the vault to be configured.

## Autonomous execution

Browser tasks run without a reasoning-step, action-count, repetition, or elapsed-time limit. They continue until the model returns a final response, the user stops the task, or a sensitive action requires explicit confirmation. Cloud models can therefore continue consuming API usage until stopped.

### Multistate send and message queue

The composer's **Send** button is multistate:

- **▶ Play** — idle state; sends the message and starts an autonomous task.
- **■ Stop (red, pulsing)** — shown while a task is executing; clicking it cancels the running task (replaces the old separate Cancel button).

While a task is running, a **playlist-add** button appears below the play button for queueing additional messages:

- Queued messages are listed in a panel above the composer, numbered in order, and run automatically when the current task finishes.
- Each queued row has a **fast-forward** button that interrupts the currently running task and sends that message immediately. The interrupted task is re-queued with a note telling the model to review the progress so far and resume it if still appropriate.

## Attachments

Select the paperclip button or paste an image directly into the composer. Up to six attachments can be included in one message, with a 20 MB limit per file.

- PNG, JPEG, WebP, GIF, and other `image/*` inputs are sent to Ollama as vision inputs. The selected model must support images.
- PDF text is extracted locally with `pypdf`.
- DOCX paragraphs and XLSX worksheet values are extracted locally with Python's standard ZIP/XML support.
- Extracted document text is capped at approximately 80,000 characters per file and includes filename, page, or sheet markers when available.
- Legacy `.doc` and `.xls` files are not supported; save them as DOCX or XLSX first.

Files remain on the local bridge except for the prompt content and images sent to the selected Ollama provider. In Cloud mode, those model inputs are sent to Ollama Cloud.

## Enterprise extension policies

On managed machines, `ExtensionInstallBlocklist = 1 = *` blocks unpacked extensions, so `ollama launch chrome` / `ollama launch edge` can fail with *"Extension installation is blocked by policy"*. Before launching Chrome or Edge, the launcher:

1. Computes the unpacked extension ID from the installed extension folder's absolute path (first 16 bytes of SHA-256, lowercase hex-path, alphabet `a`–`p`) and reports it in `-Validate` output.
2. Checks `HKLM\SOFTWARE\Policies\Google\Chrome` / `...\Microsoft\Edge` for blocklist/allowlist/`ExtensionSettings` state.
3. Auto-writes the allowlist entry (`ExtensionInstallAllowlist`) — to HKLM when elevated, otherwise to the per-user `HKCU` policy hive. `ExtensionSettings` wildcard-blocks override the allowlist and are merged to `installation_mode: allowed` only in an elevated session.
4. If machine policy is managed and the session is not elevated, it warns with the extension ID and remediation options instead of failing silently.

Run once from an elevated terminal to fix HKLM, or send the printed ID (e.g. `hbkpoamkaedodghekihigfbbhkibkfjm`) to IT for allowlisting. `ollama launch chromium` is unaffected — `--load-extension` there is not subject to the enterprise blocklist.

## Limitations

- This does not reproduce Perplexity search citations, account services, or the proprietary `/agent` server.
- Internal browser pages and organization-restricted URLs cannot be read or scripted.
- Browser performance depends on the selected model's tool-calling quality. `granite4.1:3b` supports tools but larger models generally plan more reliably.
- Autonomous tasks wait up to `chat_timeout` seconds (default 600) for each Ollama response and retry once. If you still see timeouts, raise `chat_timeout` in `%LOCALAPPDATA%\OllamaComet\config.json` or pick a faster model.
- Scanned PDFs without embedded text require OCR before upload.
- Ollama Cloud mode requires an Ollama API key. Cloud model discovery and chat use the official `/api/tags` and `/api/chat` endpoints.

## Install

```powershell
.\Install-OllamaComet.ps1
```

Open a new terminal after installation.

If an already-open terminal reports `unknown integration: comet`, it is still resolving the official `ollama.exe` before the wrapper. Either restart the terminal application or refresh that shell with:

```powershell
$env:Path = "$env:LOCALAPPDATA\OllamaComet\bin;$env:Path"
```

## Use

```powershell
ollama launch comet
ollama launch comet --model granite4.1:3b
ollama launch comet --config
ollama launch comet --restore
```

For Ollama Cloud, run `ollama launch comet --config`, select cloud mode, enter the model and API key, and then launch normally. The API key is encrypted with Windows Data Protection API and can only be decrypted by the current Windows account on this computer.

After setup, use the **Model provider** selector in the assistant to switch between **Local Ollama** and **Ollama Cloud**. Each provider remembers its last selected model. The API key is never sent to the browser UI.

**Note on restarting the bridge manually:** always restart through the launcher (`ollama launch comet`). It decrypts the cloud API key from Windows-protected storage and passes it to the bridge process. Starting `bridge.py` directly (for example, `python bridge.py --port 11435 --token <token>`) does not inherit the key, so `cloud_configured` becomes `false` and the Ollama Cloud model list in the assistant settings stops loading.
