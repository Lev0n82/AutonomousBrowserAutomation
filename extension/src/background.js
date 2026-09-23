const chrome = globalThis.browser ?? globalThis.chrome;
const DEFAULT_CONFIG = {
  bridgeUrl: "http://127.0.0.1:11435",
  token: ""
};
let config;
let polling = false;

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function getConfig() {
  if (!config) {
    const saved = await chrome.storage.local.get(DEFAULT_CONFIG);
    let managed = {};
    try {
      const response = await fetch(chrome.runtime.getURL("runtime-config.json"), {
        cache: "no-store"
      });
      if (response.ok) managed = await response.json();
    } catch {
      managed = {};
    }
    config = {
      bridgeUrl: String(
        managed.bridgeUrl || saved.bridgeUrl || DEFAULT_CONFIG.bridgeUrl
      ).replace(/\/+$/, ""),
      token: String(managed.token || saved.token || "")
    };
  }
  return config;
}

async function bridgeFetch(path, options = {}) {
  const current = await getConfig();
  if (!current.token) {
    throw new Error("Configure the bridge token in the extension options.");
  }
  const headers = new Headers(options.headers || {});
  headers.set("X-Ollama-Comet-Token", current.token);
  return fetch(`${current.bridgeUrl}${path}`, { ...options, headers });
}

async function activeTab(tabId) {
  if (Number.isInteger(tabId) && tabId > 0) {
    return chrome.tabs.get(tabId);
  }
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) {
    throw new Error("No active tab is available.");
  }
  return tab;
}

async function waitForTab(tabId, timeout = 15000) {
  const current = await chrome.tabs.get(tabId);
  if (current.status === "complete") return current;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error(`Timed out waiting for tab ${tabId}`));
    }, timeout);
    const listener = (updatedId, changeInfo, tab) => {
      if (updatedId === tabId && changeInfo.status === "complete") {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve(tab);
      }
    };
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function tabContext(executedTabId) {
  const tabs = await chrome.tabs.query({ currentWindow: true });
  const availableTabs = tabs
    .filter((tab) => tab.id)
    .map((tab) => ({
      tab_id: tab.id,
      title: tab.title || "",
      url: tab.url || ""
    }));
  const active = tabs.find((tab) => tab.active && tab.id);
  return {
    current_tab_id: active?.id || executedTabId || -1,
    executed_on_tab_id: executedTabId || active?.id || -1,
    available_tabs: availableTabs,
    tab_count: availableTabs.length
  };
}

async function executeInTab(tabId, func, args = []) {
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func,
    args
  });
  return results[0]?.result;
}

async function readPage(request) {
  const tab = await activeTab(request.tab_id);
  const filter = String(request.filter || "viewport").toLowerCase();
  const depth = Number.isInteger(request.depth) ? request.depth : 4;
  const result = await executeInTab(tab.id, (requestedFilter, requestedDepth) => {
    const visible = (element) => {
      const style = getComputedStyle(element);
      const rectangle = element.getBoundingClientRect();
      return style.visibility !== "hidden" && style.display !== "none" &&
        rectangle.width > 0 && rectangle.height > 0;
    };
    const inViewport = (element) => {
      const rectangle = element.getBoundingClientRect();
      return rectangle.bottom >= 0 && rectangle.right >= 0 &&
        rectangle.top <= innerHeight && rectangle.left <= innerWidth;
    };
    const selector = [
      "a[href]", "button", "input", "textarea", "select",
      "[role=button]", "[role=link]", "[role=checkbox]", "[role=radio]",
      "[contenteditable=true]"
    ].join(",");
    const elements = [...document.querySelectorAll(selector)]
      .filter(visible)
      .filter((element) => requestedFilter !== "viewport" || inViewport(element))
      .slice(0, 250);
    const lines = [];
    elements.forEach((element, index) => {
      let ref = element.getAttribute("data-ollama-comet-ref");
      if (!ref) {
        ref = `ref_${Date.now().toString(36)}_${index}`;
        element.setAttribute("data-ollama-comet-ref", ref);
      }
      const rectangle = element.getBoundingClientRect();
      const label = (
        element.getAttribute("aria-label") ||
        element.innerText ||
        element.getAttribute("placeholder") ||
        element.getAttribute("title") ||
        element.getAttribute("name") ||
        ""
      ).trim().replace(/\s+/g, " ").slice(0, 240);
      const role = element.getAttribute("role") || element.tagName.toLowerCase();
      const value = element instanceof HTMLInputElement && element.type !== "password"
        ? element.value.slice(0, 120)
        : "";
      lines.push({
        ref,
        role,
        label,
        value,
        disabled: Boolean(element.disabled),
        coordinate: [
          Math.round(rectangle.left + rectangle.width / 2),
          Math.round(rectangle.top + rectangle.height / 2)
        ]
      });
    });
    return {
      title: document.title,
      url: location.href,
      text: document.body?.innerText?.trim().slice(0, requestedDepth * 10000) || "",
      interactive_elements: lines
    };
  }, [filter, depth]);
  return { tab_context: await tabContext(tab.id), result };
}

async function getPageText(request) {
  const tab = await activeTab(request.tab_id);
  const markdown = await executeInTab(tab.id, () => {
    const title = document.title ? `# ${document.title}\n\n` : "";
    return `${title}${document.body?.innerText || ""}`.trim().slice(0, 60000);
  });
  return { tab_context: await tabContext(tab.id), markdown };
}

async function navigate(request) {
  const tab = await activeTab(request.tab_id);
  const destination = String(request.url || "");
  if (!destination) throw new Error("url is required");
  if (destination === "back") {
    await chrome.tabs.goBack(tab.id);
  } else if (destination === "forward") {
    await chrome.tabs.goForward(tab.id);
  } else {
    const url = destination.includes("://") ? destination : `https://${destination}`;
    await chrome.tabs.update(tab.id, { url });
  }
  const updated = await waitForTab(tab.id);
  return {
    tab_context: await tabContext(tab.id),
    message: `Navigated to ${updated.url || destination}`
  };
}

async function tabsCreate(request) {
  const tab = await chrome.tabs.create({
    url: request.url || "about:blank",
    active: true
  });
  if (request.url && request.url !== "about:blank") {
    await waitForTab(tab.id);
  }
  return {
    tab_context: await tabContext(tab.id),
    message: `Created new tab. Tab ID: ${tab.id}`,
    tab_id: tab.id
  };
}

async function formInput(request, allowSensitive) {
  const tab = await activeTab(request.tab_id);
  const result = await executeInTab(tab.id, (ref, value, sensitiveAllowed) => {
    const element = document.querySelector(`[data-ollama-comet-ref="${CSS.escape(ref)}"]`);
    if (!element) throw new Error(`No element found with reference: ${ref}`);
    const label = `${element.getAttribute("aria-label") || ""} ${element.getAttribute("name") || ""}`;
    if (!sensitiveAllowed &&
      (element instanceof HTMLInputElement && element.type === "password" ||
       /password|credential|card number|security code/i.test(label))) {
      throw new Error("Sensitive input requires explicit user confirmation.");
    }
    element.focus({ preventScroll: false });
    if (element instanceof HTMLSelectElement) {
      const choices = String(value).split(",").map((item) => item.trim());
      let matched = false;
      for (const option of element.options) {
        option.selected = choices.includes(option.value) || choices.includes(option.text);
        matched ||= option.selected;
      }
      if (!matched) throw new Error(`No select option matched: ${value}`);
    } else if (element instanceof HTMLInputElement &&
      ["checkbox", "radio"].includes(element.type)) {
      element.checked = element.type === "radio" || String(value).toLowerCase() === "true";
    } else {
      const prototype = element instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
      if (setter) setter.call(element, String(value));
      else element.value = String(value);
    }
    element.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
    return `Input set to "${value}"`;
  }, [request.ref || "", request.value ?? "", allowSensitive]);
  return { tab_context: await tabContext(tab.id), message: result };
}

async function withDebugger(tabId, callback) {
  if (!chrome.debugger?.attach) {
    return callback(null);
  }
  const target = { tabId };
  let attached = false;
  try {
    await chrome.debugger.attach(target, "1.3");
    attached = true;
  } catch (error) {
    if (!String(error.message).includes("Another debugger")) throw error;
  }
  try {
    return await callback(target);
  } finally {
    if (attached) {
      await chrome.debugger.detach(target).catch(() => {});
    }
  }
}

async function dispatchClick(tabId, action, allowSensitive) {
  const sensitivePattern = "submit|send|delete|purchase|buy|download|pay|order|sign in|log in";
  if (action.ref) {
    return executeInTab(tabId, (ref, clickType, sensitiveAllowed, pattern) => {
      const element = document.querySelector(`[data-ollama-comet-ref="${CSS.escape(ref)}"]`);
      if (!element) throw new Error(`No element found with reference: ${ref}`);
      const label = `${element.innerText || ""} ${element.getAttribute("aria-label") || ""}`.trim();
      if (!sensitiveAllowed && new RegExp(pattern, "i").test(label)) {
        throw new Error(`"${label}" requires explicit user confirmation.`);
      }
      element.scrollIntoView({ block: "center", inline: "center" });
      const options = { bubbles: true, cancelable: true, view: window };
      if (clickType === "RIGHT_CLICK") {
        element.dispatchEvent(new MouseEvent("contextmenu", { ...options, button: 2 }));
      } else {
        const count = clickType === "DOUBLE_CLICK" ? 2 : clickType === "TRIPLE_CLICK" ? 3 : 1;
        for (let index = 0; index < count; index += 1) element.click();
      }
      return `${clickType} on ${ref}`;
    }, [action.ref, action.action, allowSensitive, sensitivePattern]);
  }
  if (!Array.isArray(action.coordinate) || action.coordinate.length < 2) {
    throw new Error(`${action.action} requires ref or coordinate`);
  }
  const [x, y] = action.coordinate;
  const target = await executeInTab(tabId, (coordinateX, coordinateY) => {
    const element = document.elementFromPoint(coordinateX, coordinateY);
    if (!element) return "";
    return `${element.innerText || ""} ${element.getAttribute("aria-label") || ""}`.trim().slice(0, 160);
  }, [x, y]);
  if (!allowSensitive && new RegExp(sensitivePattern, "i").test(target || "")) {
    throw new Error(`"${target}" requires explicit user confirmation.`);
  }
  const button = action.action === "RIGHT_CLICK" ? "right" : "left";
  const clickCount = action.action === "DOUBLE_CLICK" ? 2 :
    action.action === "TRIPLE_CLICK" ? 3 : 1;
  if (!chrome.debugger?.sendCommand) {
    return executeInTab(tabId, (coordinateX, coordinateY, clickButton, count) => {
      const element = document.elementFromPoint(coordinateX, coordinateY);
      if (!element) throw new Error(`No element found at ${coordinateX},${coordinateY}`);
      const options = {
        bubbles: true,
        cancelable: true,
        clientX: coordinateX,
        clientY: coordinateY,
        button: clickButton === "right" ? 2 : 0,
        detail: count
      };
      if (clickButton === "right") {
        element.dispatchEvent(new MouseEvent("contextmenu", options));
      } else {
        for (let index = 0; index < count; index += 1) {
          element.dispatchEvent(new MouseEvent("mousedown", options));
          element.dispatchEvent(new MouseEvent("mouseup", options));
          element.dispatchEvent(new MouseEvent("click", options));
        }
      }
      return `${count > 1 ? `${count} clicks` : "Click"} at ${coordinateX},${coordinateY}`;
    }, [x, y, button, clickCount]);
  }
  return withDebugger(tabId, async (target) => {
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
      type: "mousePressed", x, y, button, clickCount
    });
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
      type: "mouseReleased", x, y, button, clickCount
    });
    return `${action.action} at ${x},${y}`;
  });
}

async function dispatchKey(tabId, text) {
  const aliases = {
    ENTER: { key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 },
    TAB: { key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 },
    ESCAPE: { key: "Escape", code: "Escape", windowsVirtualKeyCode: 27 },
    BACKSPACE: { key: "Backspace", code: "Backspace", windowsVirtualKeyCode: 8 },
    DELETE: { key: "Delete", code: "Delete", windowsVirtualKeyCode: 46 },
    ARROWUP: { key: "ArrowUp", code: "ArrowUp", windowsVirtualKeyCode: 38 },
    ARROWDOWN: { key: "ArrowDown", code: "ArrowDown", windowsVirtualKeyCode: 40 },
    ARROWLEFT: { key: "ArrowLeft", code: "ArrowLeft", windowsVirtualKeyCode: 37 },
    ARROWRIGHT: { key: "ArrowRight", code: "ArrowRight", windowsVirtualKeyCode: 39 }
  };
  const normalized = String(text).replace(/\s+/g, "").toUpperCase();
  const definition = aliases[normalized];
  if (!definition) {
    throw new Error(`Unsupported KEY value: ${text}`);
  }
  if (!chrome.debugger?.sendCommand) {
    return executeInTab(tabId, (keyDefinition) => {
      const target = document.activeElement || document.body;
      target.dispatchEvent(new KeyboardEvent("keydown", {
        bubbles: true,
        cancelable: true,
        ...keyDefinition
      }));
      target.dispatchEvent(new KeyboardEvent("keyup", {
        bubbles: true,
        cancelable: true,
        ...keyDefinition
      }));
      return `Pressed ${keyDefinition.key}`;
    }, [definition]);
  }
  return withDebugger(tabId, async (target) => {
    await chrome.debugger.sendCommand(target, "Input.dispatchKeyEvent", {
      type: "keyDown", ...definition
    });
    await chrome.debugger.sendCommand(target, "Input.dispatchKeyEvent", {
      type: "keyUp", ...definition
    });
    return `Pressed ${text}`;
  });
}

async function computerBatch(request) {
  const tab = await activeTab(request.tab_id);
  const results = [];
  for (const action of request.actions || []) {
    const name = String(action.action || "").toUpperCase();
    if (["LEFT_CLICK", "RIGHT_CLICK", "DOUBLE_CLICK", "TRIPLE_CLICK"].includes(name)) {
      results.push(await dispatchClick(
        tab.id,
        { ...action, action: name },
        request.allow_sensitive
      ));
    } else if (name === "TYPE") {
      if (chrome.debugger?.sendCommand) {
        results.push(await withDebugger(tab.id, async (target) => {
          await chrome.debugger.sendCommand(target, "Input.insertText", { text: String(action.text || "") });
          return `Typed "${action.text || ""}"`;
        }));
      } else {
        results.push(await executeInTab(tab.id, (text) => {
          const element = document.activeElement;
          if (!element || !("value" in element)) {
            throw new Error("Focus an editable field before using TYPE.");
          }
          const prototype = element instanceof HTMLTextAreaElement
            ? HTMLTextAreaElement.prototype
            : HTMLInputElement.prototype;
          const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
          const next = `${element.value || ""}${text}`;
          if (setter) setter.call(element, next);
          else element.value = next;
          element.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
          return `Typed "${text}"`;
        }, [String(action.text || "")]));
      }
    } else if (name === "KEY") {
      results.push(await dispatchKey(tab.id, action.text || ""));
    } else if (name === "WAIT") {
      const duration = Math.min(Math.max(Number(action.duration || 1), 0), 30);
      await sleep(duration * 1000);
      results.push(`Waited ${duration} second(s)`);
    } else if (name === "SCROLL") {
      const parameters = action.scroll_parameters || {};
      const direction = String(parameters.scroll_direction || "DOWN").toUpperCase();
      const amount = Number(parameters.viewports_to_scroll || parameters.scroll_amount || 1);
      results.push(await executeInTab(tab.id, (scrollDirection, scrollAmount) => {
        const vertical = ["UP", "DOWN"].includes(scrollDirection);
        const sign = ["UP", "LEFT"].includes(scrollDirection) ? -1 : 1;
        window.scrollBy({
          top: vertical ? sign * innerHeight * scrollAmount : 0,
          left: vertical ? 0 : sign * innerWidth * scrollAmount,
          behavior: "smooth"
        });
        return `Scrolled ${scrollDirection}`;
      }, [direction, amount]));
    } else if (name === "SCROLL_TO") {
      results.push(await executeInTab(tab.id, (ref) => {
        const element = document.querySelector(`[data-ollama-comet-ref="${CSS.escape(ref)}"]`);
        if (!element) throw new Error(`No element found with reference: ${ref}`);
        element.scrollIntoView({ block: "center", inline: "center", behavior: "smooth" });
        return `Scrolled to ${ref}`;
      }, [action.ref || ""]));
    } else if (name === "LEFT_CLICK_DRAG") {
      if (!Array.isArray(action.start_coordinate) || !Array.isArray(action.coordinate)) {
        throw new Error("LEFT_CLICK_DRAG requires start_coordinate and coordinate");
      }
      const [startX, startY] = action.start_coordinate;
      const [endX, endY] = action.coordinate;
      if (chrome.debugger?.sendCommand) {
        results.push(await withDebugger(tab.id, async (target) => {
          await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
            type: "mousePressed", x: startX, y: startY, button: "left", clickCount: 1
          });
          await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
            type: "mouseMoved", x: endX, y: endY, button: "left"
          });
          await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
            type: "mouseReleased", x: endX, y: endY, button: "left", clickCount: 1
          });
          return `Dragged from ${startX},${startY} to ${endX},${endY}`;
        }));
      } else {
        results.push(await executeInTab(tab.id, (fromX, fromY, toX, toY) => {
          const source = document.elementFromPoint(fromX, fromY);
          const destination = document.elementFromPoint(toX, toY);
          if (!source || !destination) throw new Error("Drag source or destination was not found.");
          source.dispatchEvent(new MouseEvent("mousedown", {
            bubbles: true, cancelable: true, clientX: fromX, clientY: fromY
          }));
          destination.dispatchEvent(new MouseEvent("mousemove", {
            bubbles: true, cancelable: true, clientX: toX, clientY: toY
          }));
          destination.dispatchEvent(new MouseEvent("mouseup", {
            bubbles: true, cancelable: true, clientX: toX, clientY: toY
          }));
          return `Dragged from ${fromX},${fromY} to ${toX},${toY}`;
        }, [startX, startY, endX, endY]));
      }
    } else if (name === "SCREENSHOT") {
      const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
      results.push({ screenshot: dataUrl });
    } else {
      throw new Error(`Unsupported ComputerBatch action: ${name}`);
    }
  }
  return {
    tab_context: await tabContext(tab.id),
    message: JSON.stringify(results)
  };
}

async function executeCommand(command) {
  switch (command.method) {
    case "ReadPage":
      return readPage(command.arguments || {});
    case "GetPageText":
      return getPageText(command.arguments || {});
    case "Navigate":
      return navigate(command.arguments || {});
    case "TabsCreate":
      return tabsCreate(command.arguments || {});
    case "FormInput":
      return formInput(command.arguments || {}, command.allow_sensitive);
    case "ComputerBatch":
      return computerBatch({
        ...(command.arguments || {}),
        allow_sensitive: command.allow_sensitive
      });
    default:
      throw new Error(`Unsupported browser method: ${command.method}`);
  }
}

async function poll() {
  if (polling) return;
  polling = true;
  while (polling) {
    try {
      const response = await bridgeFetch("/api/browser/next");
      if (response.status === 204) continue;
      if (!response.ok) throw new Error(`Bridge returned ${response.status}`);
      const command = await response.json();
      let result;
      try {
        result = { ok: true, value: await executeCommand(command) };
      } catch (error) {
        result = { ok: false, error: error.message || String(error) };
      }
      await bridgeFetch("/api/browser/result", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: command.id, result })
      });
    } catch (error) {
      console.warn("Autonomous browser-control poll failed", error);
      await sleep(1000);
    }
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "AUTONOMOUS_BROWSER_CONFIG_CHANGED") {
    config = null;
    polling = false;
    setTimeout(poll, 0);
    sendResponse({ ok: true });
    return false;
  }
  if (message?.type !== "AUTONOMOUS_BROWSER_COMMAND") return false;
  executeCommand(message.command)
    .then((value) => sendResponse({ ok: true, value }))
    .catch((error) => sendResponse({
      ok: false,
      error: error.message || String(error)
    }));
  return true;
});

chrome.storage.onChanged.addListener((_changes, areaName) => {
  if (areaName !== "local") return;
  config = null;
  polling = false;
  setTimeout(poll, 0);
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("keep-browser-control-active", { periodInMinutes: 0.5 });
  if (chrome.sidePanel?.setPanelBehavior) {
    chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(console.warn);
  }
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "keep-browser-control-active") poll();
});

if (chrome.action?.onClicked && chrome.sidebarAction?.open) {
  chrome.action.onClicked.addListener(() => chrome.sidebarAction.open());
}

poll();
