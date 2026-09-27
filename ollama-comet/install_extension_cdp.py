#!/usr/bin/env python3
"""Load an unpacked Chrome/Edge extension over the CDP debug interface.

Chrome 137+ removed --load-extension support in branded builds, and managed
browsers often block drag-and-drop / "Load unpacked". When the browser is
started with --remote-debugging-port and --enable-unsafe-extension-debugging,
the DevTools domain Extensions.loadUnpacked still works.

Prints a JSON result on stdout:
    {"ok": true, "extension_id": "..."}   on success
    {"ok": false, "error": "..."}         on failure
Exit code 0 on success, 1 on failure.
"""

import argparse
import json
import sys
import time
import urllib.request

try:
    import websocket
except ImportError:
    print(json.dumps({"ok": False, "error": "websocket-client package is required (pip install websocket-client)"}))
    sys.exit(1)


def get_ws_url(port, timeout):
    url = "http://127.0.0.1:{}/json/version".format(port)
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                info = json.loads(response.read().decode("utf-8"))
            ws_url = info.get("webSocketDebuggerUrl")
            if ws_url:
                return ws_url
            last_error = "webSocketDebuggerUrl missing from /json/version"
        except Exception as exc:  # browser may still be starting up
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError("could not reach CDP endpoint on port {}: {}".format(port, last_error))


def main():
    parser = argparse.ArgumentParser(description="Load an unpacked extension via CDP")
    parser.add_argument("--port", type=int, required=True, help="browser remote-debugging-port")
    parser.add_argument("--path", required=True, help="absolute path of the unpacked extension directory")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for the debug endpoint")
    args = parser.parse_args()

    try:
        ws_url = get_ws_url(args.port, args.timeout)
        # Chrome 111+ rejects WebSocket handshakes carrying an Origin header.
        connection = websocket.create_connection(ws_url, timeout=args.timeout, suppress_origin=True)
        try:
            request_id = 1
            connection.send(json.dumps({
                "id": request_id,
                "method": "Extensions.loadUnpacked",
                "params": {"path": args.path},
            }))
            deadline = time.time() + args.timeout
            while time.time() < deadline:
                message = json.loads(connection.recv())
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise RuntimeError("Extensions.loadUnpacked failed: {}".format(message["error"].get("message", message["error"])))
                extension_id = (message.get("result") or {}).get("id")
                if not extension_id:
                    raise RuntimeError("Extensions.loadUnpacked returned no extension id")
                print(json.dumps({"ok": True, "extension_id": extension_id}))
                return 0
            raise RuntimeError("timed out waiting for Extensions.loadUnpacked response")
        finally:
            try:
                connection.close()
            except Exception:
                pass
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())