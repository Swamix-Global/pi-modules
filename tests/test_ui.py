"""Real Chromium render/interaction checks against a temporary Python backend.

Uses the installed headless shell (or CHROME_BIN) and CDP via websockets.
No production database, inference calls, logged-in profile, or credentials.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from websockets.sync.client import connect

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.db import Database

ROOT = Path(__file__).resolve().parent.parent


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_json(url):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                return json.load(response)
        except (OSError, ValueError):
            threading.Event().wait(0.05)
    raise AssertionError(f"Endpoint not ready: {url}")


def run():
    candidates = list(
        (Path.home() / ".cache/ms-playwright").glob(
            "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"
        )
    )
    chrome = os.getenv("CHROME_BIN") or (str(candidates[-1]) if candidates else None)
    if not chrome:
        raise SystemExit("Set CHROME_BIN to a Chrome/Chromium executable")
    with tempfile.TemporaryDirectory() as folder:
        db_path = Path(folder) / "ui.db"
        db = Database(db_path)
        rid = db.start_request("POST /r/test/chat/completions")
        db.finish_request(
            rid,
            {
                "model": "fixture-model",
                "status": 200,
                "changed": True,
                "before_tokens": 8000,
                "after_tokens": 2000,
                "duration_ms": 500,
                "cache_hits": 1,
                "error": None,
                "original": {
                    "messages": [{"role": "user", "content": "UI regression evidence"}]
                },
                "compressed": {"messages": [{"role": "user", "content": "UI summary"}]},
                "changes": [
                    {
                        "kind": "block",
                        "before": "UI regression evidence",
                        "after": "UI summary",
                        "cached": True,
                    }
                ],
            },
        )
        backend_port, chrome_port = free_port(), free_port()
        env = {
            **os.environ,
            "PI_CONTEXT_ROUTER_DB": str(db_path),
            "PI_CONTEXT_ROUTER_PORT": str(backend_port),
        }
        server = subprocess.Popen(
            [sys.executable, "-m", "backend.main"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        browser = None
        try:
            wait_json(f"http://127.0.0.1:{backend_port}/health")
            browser = subprocess.Popen(
                [
                    chrome,
                    "--headless",
                    "--no-sandbox",
                    "--disable-gpu",
                    f"--remote-debugging-port={chrome_port}",
                    f"--user-data-dir={folder}/browser",
                    "about:blank",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            wait_json(f"http://127.0.0.1:{chrome_port}/json/version")
            new_tab = urllib.request.Request(
                f"http://127.0.0.1:{chrome_port}/json/new", method="PUT"
            )
            with urllib.request.urlopen(new_tab) as response:
                tab = json.load(response)
            with connect(tab["webSocketDebuggerUrl"], open_timeout=5) as ws:
                seq, console_errors = 0, []

                def cdp(method, params=None):
                    nonlocal seq
                    seq += 1
                    ws.send(
                        json.dumps(
                            {"id": seq, "method": method, "params": params or {}}
                        )
                    )
                    while True:
                        reply = json.loads(ws.recv(timeout=20))
                        if reply.get("method") == "Runtime.exceptionThrown":
                            console_errors.append(reply["params"])
                        if reply.get("id") == seq:
                            assert "error" not in reply, reply
                            return reply.get("result", {})

                cdp("Runtime.enable")
                cdp("Page.enable")
                cdp(
                    "Page.navigate",
                    {"url": f"http://127.0.0.1:{backend_port}/dashboard/"},
                )
                script = r"""
                (async () => {
                  const wait = async (test) => {
                    const end = performance.now() + 10000;
                    while (!test()) {
                      if (performance.now() > end) throw Error('UI condition timed out');
                      await new Promise(r => setTimeout(r, 25));
                    }
                  };
                  const assert = (ok, message) => { if (!ok) throw Error(message); };
                  await wait(() => document.querySelector('canvas'));
                  await wait(() => !document.body.innerText.includes('Connecting'));
                  assert(document.body.innerText.includes('Python'), 'Python badge missing');
                  const visible = el => el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0;
                  const contrast = () => {
                    for (const el of document.querySelectorAll('h1,h2,button,td,label')) {
                      if (!visible(el) || !el.textContent.trim()) continue;
                      const fg = getComputedStyle(el).color;
                      let parent = el, bg;
                      while (parent) {
                        bg = getComputedStyle(parent).backgroundColor;
                        if (bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') break;
                        parent = parent.parentElement;
                      }
                      assert(fg !== bg, 'Invisible text: ' + el.textContent);
                    }
                  };
                  contrast();
                  const theme = document.querySelector('button[aria-label*="Switch to"]');
                  theme.click(); await new Promise(r => requestAnimationFrame(r)); contrast();
                  document.querySelector('#tab-requests').click();
                  await wait(() => document.querySelector('button[aria-label="Expand request 1"]'));
                  assert(location.search.includes('tab=requests'), 'Tab URL missing');
                  document.querySelector('button[aria-label="Expand request 1"]').click();
                  await wait(() => document.body.innerText.includes('UI regression evidence'));
                  assert(document.querySelector('button[aria-label="Copy Original JSON"]'), 'Copy button missing');
                  assert(document.querySelector('button[aria-label="Download Forwarded JSON"]'), 'Download missing');
                  assert(location.search.includes('request=1'), 'Detail URL missing');
                  document.querySelector('#tab-settings').click();
                  await new Promise(r => requestAnimationFrame(r));
                  await wait(() => document.querySelector('input[aria-label="Context token budget"]') && !document.body.innerText.includes('Loading settings'));
                  const input = document.querySelector('input[aria-label="Context token budget"]');
                  input.value = '16000'; input.dispatchEvent(new Event('input', {bubbles:true}));
                  [...document.querySelectorAll('button')].find(b=>b.textContent.includes('Apply settings')).click();
                  await wait(() => document.body.innerText.includes('Settings saved.'));
                  contrast();
                  return {theme:document.documentElement.className, settings:'saved',history:'rendered',chart:'rendered'};
                })()
                """
                result = cdp(
                    "Runtime.evaluate",
                    {"expression": script, "awaitPromise": True, "returnByValue": True},
                )
                assert "exceptionDetails" not in result, result
                assert not console_errors, console_errors
                print("UI desktop:", result["result"].get("value"))
                # An external request must appear without clicking Reload.
                db.start_request("GET /r/test/live-refresh-regression")
                live_check = r"""(async () => {
                  document.querySelector('#tab-requests').click();
                  const deadline = performance.now() + 8000;
                  while (!document.body.innerText.includes('live-refresh-regression')) {
                    if (performance.now() > deadline) throw Error('Request history did not auto-refresh');
                    await new Promise(r => setTimeout(r, 50));
                  }
                  return true;
                })()"""
                result = cdp(
                    "Runtime.evaluate",
                    {
                        "expression": live_check,
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                )
                assert "exceptionDetails" not in result and result["result"].get(
                    "value"
                ), result
                print("UI live request refresh: passed")
                cdp(
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "width": 390,
                        "height": 844,
                        "deviceScaleFactor": 1,
                        "mobile": True,
                    },
                )
                result = cdp(
                    "Runtime.evaluate",
                    {
                        "expression": "document.documentElement.scrollWidth <= innerWidth",
                        "returnByValue": True,
                    },
                )
                assert result["result"].get("value"), "Mobile layout overflows the page"
                print("UI mobile: no page overflow")
                assert (
                    db.settings({}) == {}
                )  # Empty defaults never leak unknown persisted keys.
                assert (
                    db.rows("SELECT value FROM settings WHERE key='tokens'")[0]["value"]
                    == "16000"
                )
        finally:
            for process in (browser, server):
                if process:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


if __name__ == "__main__":
    run()
