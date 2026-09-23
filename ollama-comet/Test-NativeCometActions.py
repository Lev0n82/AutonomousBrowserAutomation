import json
import os
import pathlib
import sys
import time


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import bridge


LAB_URL = "http://127.0.0.1:11436/test-lab"

TESTS = [
    {
        "name": "Navigate",
        "prompt": f"Use Navigate in the main Comet browser section to open {LAB_URL}. Report the page title and stop.",
        "methods": ["Navigate"],
        "assert": lambda state: state["title"] == "Ollama Comet Native Action Lab",
    },
    {
        "name": "ReadPage",
        "prompt": f"Navigate the main Comet tab to {LAB_URL}. Use ReadPage with filter interactive and report the references for Action target and Text input.",
        "methods": ["ReadPage"],
    },
    {
        "name": "GetPageText",
        "prompt": f"Navigate the main Comet tab to {LAB_URL}. Use GetPageText and quote the page heading.",
        "methods": ["GetPageText"],
    },
    {
        "name": "FormInput",
        "prompt": f'Navigate to {LAB_URL}. Use ReadPage, then FormInput to set Text input to "FormInput value". Do not use TYPE and do not submit.',
        "methods": ["FormInput"],
        "assert": lambda state: state["state"]["typed"] == "FormInput value",
    },
    {
        "name": "TabsCreate",
        "prompt": f"Use TabsCreate to open {LAB_URL} in a new visible tab. Use ReadPage once and report the native tab ID.",
        "methods": ["TabsCreate", "ReadPage"],
    },
    {
        "name": "SCREENSHOT",
        "prompt": f"Navigate to {LAB_URL}. Use ComputerBatch with exactly one SCREENSHOT action, then report the visible heading.",
        "methods": ["ComputerBatch"],
        "actions": ["SCREENSHOT"],
    },
    {
        "name": "WAIT",
        "prompt": f"Navigate to {LAB_URL}. Use ComputerBatch with exactly one WAIT action for 2 seconds, then reply Wait complete.",
        "methods": ["ComputerBatch"],
        "actions": ["WAIT"],
    },
    {
        "name": "LEFT_CLICK reference",
        "prompt": f"Navigate to {LAB_URL}. Use ReadPage to get the Action target reference, then ComputerBatch LEFT_CLICK that reference exactly once.",
        "methods": ["ReadPage", "ComputerBatch"],
        "actions": ["LEFT_CLICK"],
        "assert": lambda state: state["state"]["leftClicks"] >= 1,
    },
    {
        "name": "LEFT_CLICK coordinate",
        "prompt": f"Navigate to {LAB_URL}. Use ReadPage to find the Action target coordinate, then ComputerBatch LEFT_CLICK that coordinate exactly once. Do not click by reference.",
        "methods": ["ComputerBatch"],
        "actions": ["LEFT_CLICK"],
        "assert": lambda state: state["state"]["leftClicks"] >= 1,
    },
    {
        "name": "RIGHT_CLICK",
        "prompt": f"Navigate to {LAB_URL}. Use ReadPage to find the Action target coordinate, then ComputerBatch RIGHT_CLICK that coordinate exactly once.",
        "methods": ["ComputerBatch"],
        "actions": ["RIGHT_CLICK"],
        "assert": lambda state: state["state"]["rightClicks"] >= 1,
    },
    {
        "name": "DOUBLE_CLICK",
        "prompt": f"Navigate to {LAB_URL}. Use ReadPage to find the Action target coordinate, then ComputerBatch DOUBLE_CLICK that coordinate exactly once.",
        "methods": ["ComputerBatch"],
        "actions": ["DOUBLE_CLICK"],
        "assert": lambda state: state["state"]["doubleClicks"] >= 1,
    },
    {
        "name": "TRIPLE_CLICK",
        "prompt": f"Navigate to {LAB_URL}. Use ReadPage to find the Action target coordinate, then ComputerBatch TRIPLE_CLICK that coordinate exactly once.",
        "methods": ["ComputerBatch"],
        "actions": ["TRIPLE_CLICK"],
        "assert": lambda state: state["state"]["tripleClicks"] >= 1,
    },
    {
        "name": "TYPE",
        "prompt": f'Navigate to {LAB_URL}. Use ReadPage, LEFT_CLICK the Text input reference, then ComputerBatch TYPE "Native typed value". Do not use FormInput.',
        "methods": ["ComputerBatch"],
        "actions": ["LEFT_CLICK", "TYPE"],
        "assert": lambda state: state["state"]["typed"] == "Native typed value",
    },
    {
        "name": "KEY",
        "prompt": f'Navigate to {LAB_URL}. Use ReadPage, click Text input, TYPE "old value", use KEY ControlOrMeta+KeyA, then TYPE "replacement value".',
        "methods": ["ComputerBatch"],
        "actions": ["KEY"],
        "assert": lambda state: state["state"]["typed"] == "replacement value",
    },
    {
        "name": "SCROLL vertical",
        "prompt": f"Navigate to {LAB_URL}. Use ComputerBatch SCROLL DOWN by 2 viewports, WAIT 1 second, then SCROLL UP by 1 viewport. Report the actions.",
        "methods": ["ComputerBatch"],
        "actions": ["SCROLL", "WAIT"],
        "assert": lambda state: state["state"]["scrollY"] > 0,
    },
    {
        "name": "SCROLL horizontal",
        "prompt": f"Navigate to {LAB_URL}. Use ComputerBatch SCROLL RIGHT by 1 viewport, WAIT 1 second, then SCROLL LEFT by 1 viewport. Report the actions.",
        "methods": ["ComputerBatch"],
        "actions": ["SCROLL", "WAIT"],
    },
    {
        "name": "SCROLL_TO",
        "prompt": f"Navigate to {LAB_URL}. Use ReadPage with filter all, find the Bottom target reference, and ComputerBatch SCROLL_TO that reference. Do not click it.",
        "methods": ["ReadPage", "ComputerBatch"],
        "actions": ["SCROLL_TO"],
        "assert": lambda state: state["state"]["scrollY"] > 500,
    },
    {
        "name": "LEFT_CLICK_DRAG",
        "prompt": f"Navigate to {LAB_URL}. Use ReadPage to get coordinates for Drag source and Drop target, then ComputerBatch LEFT_CLICK_DRAG from the source to the target.",
        "methods": ["ReadPage", "ComputerBatch"],
        "actions": ["LEFT_CLICK_DRAG"],
        "assert": lambda state: state["state"]["dropped"] is True,
    },
]


def assistant_target(controller, token):
    expected = f"/sidecar?token={token}"
    matches = [
        page
        for page in controller._pages()
        if expected in page.get("url", "")
    ]
    if not matches:
        raise RuntimeError("Current Ollama assistant page was not found in Comet.")
    target = controller._activate(matches[-1])
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        ready = controller._evaluate(
            target,
            """(() => ({
              model: document.getElementById("model")?.value || "",
              sendEnabled: !document.getElementById("send")?.disabled
            }))()""",
        )
        if ready.get("model") and ready.get("sendEnabled"):
            return target
        time.sleep(0.25)
    raise TimeoutError("The Ollama assistant model selector did not become ready.")


def submit_prompt(controller, target, prompt):
    expression = f"""(() => {{
      window.__ollamaCometLastResult = null;
      document.getElementById("clear").click();
      const prompt = document.getElementById("prompt");
      const setter = Object.getOwnPropertyDescriptor(
        HTMLTextAreaElement.prototype, "value"
      ).set;
      setter.call(prompt, {json.dumps(prompt)});
      prompt.dispatchEvent(new Event("input", {{ bubbles: true }}));
      document.getElementById("send").click();
      return {{ sent: true, value: prompt.value }};
    }})()"""
    return controller._evaluate(target, expression)


def wait_for_result(controller, target, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = controller._evaluate(
            target,
            """(() => ({
              done: !document.getElementById("send").disabled &&
                window.__ollamaCometLastResult !== null,
              result: window.__ollamaCometLastResult,
              status: document.getElementById("status").textContent,
              messages: [...document.querySelectorAll("#messages .message")]
                .map(element => element.textContent)
            }))()""",
        )
        if value.get("done"):
            return value
        time.sleep(0.5)
    raise TimeoutError(f"Assistant did not finish within {timeout} seconds.")


def main_state(controller):
    candidates = [
        page
        for page in controller._pages()
        if LAB_URL in page.get("url", "")
    ]
    if not candidates:
        return {"url": "", "title": "", "state": {}}
    page = candidates[-1]
    return controller._evaluate(
        page,
        """(() => ({
          url: location.href,
          title: document.title,
          visible: document.visibilityState,
          focused: document.hasFocus(),
          state: window.__ollamaCometTestState || {}
        }))()""",
    )


def trace_details(result):
    trace = result.get("tool_trace") or []
    methods = [entry.get("tool") for entry in trace]
    actions = []
    native_ids = []
    for entry in trace:
        arguments = entry.get("arguments") or {}
        for action in arguments.get("actions") or []:
            actions.append(str(action.get("action", "")).upper())
        context = (entry.get("result") or {}).get("tab_context") or {}
        native_id = context.get("executed_on_tab_id")
        if native_id:
            native_ids.append(native_id)
    return methods, actions, native_ids


def run():
    app_root = pathlib.Path(os.environ["LOCALAPPDATA"]) / "OllamaComet"
    token = (app_root / "bridge.token").read_text(encoding="ascii").strip()
    controller = bridge.CDPBrowserController()
    results = []

    for index, test in enumerate(TESTS, start=1):
        started = time.monotonic()
        row = {"index": index, "name": test["name"], "prompt": test["prompt"]}
        try:
            target = assistant_target(controller, token)
            submit_prompt(controller, target, test["prompt"])
            completed = wait_for_result(controller, target)
            result = completed.get("result") or {}
            methods, actions, native_ids = trace_details(result)
            state = main_state(controller)
            missing_methods = [
                method for method in test.get("methods", []) if method not in methods
            ]
            missing_actions = [
                action for action in test.get("actions", []) if action not in actions
            ]
            state_ok = test.get("assert", lambda _: True)(state)
            passed = (
                not result.get("error")
                and not missing_methods
                and not missing_actions
                and state_ok
                and bool(native_ids)
            )
            row.update(
                {
                    "passed": passed,
                    "methods": methods,
                    "actions": actions,
                    "native_tab_ids": sorted(set(native_ids)),
                    "state": state,
                    "missing_methods": missing_methods,
                    "missing_actions": missing_actions,
                    "assistant_reply": (result.get("message") or {}).get("content", ""),
                    "status": completed.get("status"),
                }
            )
        except Exception as error:
            row.update({"passed": False, "error": str(error)})
        row["duration_seconds"] = round(time.monotonic() - started, 2)
        results.append(row)
        print(
            json.dumps(
                {
                    "index": index,
                    "name": row["name"],
                    "passed": row["passed"],
                    "methods": row.get("methods", []),
                    "actions": row.get("actions", []),
                    "error": row.get("error"),
                    "duration_seconds": row["duration_seconds"],
                }
            ),
            flush=True,
        )

    report_path = app_root / "native-action-test-results.json"
    report_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    failed = [row for row in results if not row["passed"]]
    print(
        json.dumps(
            {
                "total": len(results),
                "passed": len(results) - len(failed),
                "failed": len(failed),
                "report": str(report_path),
            }
        ),
        flush=True,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run())
