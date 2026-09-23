# Ollama launcher for Perplexity Comet

This integration adds a user-level command:

```powershell
ollama launch comet
```

It does **not** patch or replace the signed Comet executable. It starts a loopback-only compatibility bridge and launches an isolated Comet profile with the discovered `--perplexity-backend-url` switch.

## What works

- Local Ollama models through `http://127.0.0.1:11434`
- Direct Ollama Cloud through `https://ollama.com` with a Windows DPAPI-protected API key
- Local/Cloud provider and model selection in the assistant UI
- Model selection with `--model`
- The native Comet assistant button opens the local Ollama assistant
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
- `ComputerBatch`: run `SCREENSHOT`, `WAIT`, `LEFT_CLICK`, `RIGHT_CLICK`, `DOUBLE_CLICK`, `TRIPLE_CLICK`, `TYPE`, `KEY`, `SCROLL`, `LEFT_CLICK_DRAG`, and `SCROLL_TO` actions

Navigation, reading, scrolling, and harmless form preparation can run automatically. Submitting forms, sending messages, purchases, deletion, downloads, credential entry, and account changes require explicit confirmation in the user's latest request.

The conversation remains in the Ollama assistant section while the signed Comet extension activates and controls tabs in the main browser section. The native sidecar is opened once when a task begins and is not automatically collapsed or repeatedly forced open, so the user remains free to close it manually. Native RPC responses include Comet tab context, accessibility references, screenshots, and native numeric tab IDs.

The launcher binds the bridge and DevTools bootstrap endpoint to `127.0.0.1`; DevTools is used only to open an allowed Perplexity bootstrap origin and dispatch `START_AGENT`. All navigation, reading, form input, and computer actions are executed by the signed Comet extension. The assistant API and `/agent` WebSocket use a random per-launch token and control only the isolated profile at `%LOCALAPPDATA%\OllamaComet\Comet Profile`.

## Autonomous execution

Browser tasks run without a reasoning-step, action-count, repetition, or elapsed-time limit. They continue until the model returns a final response, the user selects **Cancel autonomous task**, or a sensitive action requires explicit confirmation. Cloud models can therefore continue consuming API usage until cancelled.

## Attachments

Select the paperclip button or paste an image directly into the composer. Up to six attachments can be included in one message, with a 20 MB limit per file.

- PNG, JPEG, WebP, GIF, and other `image/*` inputs are sent to Ollama as vision inputs. The selected model must support images.
- PDF text is extracted locally with `pypdf`.
- DOCX paragraphs and XLSX worksheet values are extracted locally with Python's standard ZIP/XML support.
- Extracted document text is capped at approximately 80,000 characters per file and includes filename, page, or sheet markers when available.
- Legacy `.doc` and `.xls` files are not supported; save them as DOCX or XLSX first.

Files remain on the local bridge except for the prompt content and images sent to the selected Ollama provider. In Cloud mode, those model inputs are sent to Ollama Cloud.

## Limitations

- This does not reproduce Perplexity search citations, account services, or the proprietary `/agent` server.
- Internal browser pages and organization-restricted URLs cannot be read or scripted.
- Browser performance depends on the selected model's tool-calling quality. `granite4.1:3b` supports tools but larger models generally plan more reliably.
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
