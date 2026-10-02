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
        token_cases = {rid: "8.0 k → 2.0 k (-75.0%)"}
        for before, after, expected in (
            (12345, 4567, "12.3 k → 4.6 k (-63.0%)"),
            (1500, 1500, "1.5 k → 1.5 k (0.0%)"),
            (1000, 1500, "1.0 k → 1.5 k (+50.0%)"),
            (0, 0, "0.0 k → 0.0 k (0.0%)"),
            (0, 500, "0.0 k → 0.5 k (—)"),
            (100000, 99999, "100.0 k → 100.0 k (0.0%)"),
        ):
            case_id = db.start_request("POST /r/test/chat/completions")
            fixture = db.detail(rid)
            fixture.update(before_tokens=before, after_tokens=after)
            db.finish_request(case_id, fixture)
            token_cases[case_id] = expected
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
                cdp("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Kolkata"})
                cdp("Emulation.setLocaleOverride", {"locale": "en-US"})
                cdp(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {
                        "source": r"""
                        window.chartTexts = [];
                        window.chartNow = Date.parse('2026-10-01T18:25:00Z');
                        window.chartStatsReads = 0;
                        const fillText = CanvasRenderingContext2D.prototype.fillText;
                        CanvasRenderingContext2D.prototype.fillText = function(text, ...args) {
                          window.chartTexts.push(String(text));
                          if (window.chartTexts.length > 3000) window.chartTexts.shift();
                          return fillText.call(this, text, ...args);
                        };
                        const fetchOriginal = window.fetch;
                        window.fetch = async (...args) => {
                          const response = await fetchOriginal(...args);
                          if (args[0] !== '/stats') return response;
                          const stats = await response.json();
                          stats.now = window.chartNow;
                          stats.timeline = [{
                            time: window.chartNow, requests: 3,
                            before_tokens: 8000, after_tokens: 2000
                          }];
                          window.chartStatsReads++;
                          return new Response(JSON.stringify(stats), {
                            status: response.status,
                            headers: {'Content-Type': 'application/json'}
                          });
                        };
                        """
                    },
                )
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
                  await wait(() => window.chartTexts.includes('Oct 1'));
                  assert(!window.chartTexts.includes('Oct 2'), 'Chart used browser time instead of server time');
                  const formatTime = ms => new Date(ms).toLocaleString(undefined, {
                    year:'numeric', month:'numeric', day:'numeric',
                    hour:'numeric', minute:'2-digit', second:'2-digit'
                  });
                  const checkTooltip = async expected => {
                    const canvas = document.querySelector('canvas');
                    const rect = canvas.getBoundingClientRect();
                    window.chartTexts = [];
                    for (let offset = 20; offset < 180; offset += 5) {
                      canvas.dispatchEvent(new MouseEvent('mousemove', {
                        bubbles:true, clientX:rect.right - offset, clientY:rect.top + 80
                      }));
                      await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
                      if (window.chartTexts.includes(expected)) return;
                    }
                    throw Error('Chart tooltip timestamp missing: ' + expected + '; got: ' + window.chartTexts.join('|'));
                  };
                  await checkTooltip(formatTime(window.chartNow));
                  const reads = window.chartStatsReads;
                  window.chartNow += 300000;
                  await wait(() => window.chartStatsReads > reads && window.chartTexts.includes('Oct 2'));
                  await checkTooltip(formatTime(window.chartNow));
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
                  const headers = [...document.querySelectorAll('#panel-requests th')].map(h => h.textContent.trim());
                  assert(JSON.stringify(headers) === JSON.stringify(['Status','Model','Created','Duration','Tokens','Cached','Detail']), 'Unexpected log columns: ' + headers);
                  for (const [id, expected] of Object.entries(TOKEN_CASES)) {
                    const button = document.querySelector(`button[aria-label="Expand request ${id}"]`);
                    const row = button.closest('tr');
                    assert(row.cells.length === 7, 'Wrong column count');
                    assert(row.cells[4].textContent.trim() === expected, 'Wrong token flow: ' + row.cells[4].textContent);
                  }
                  document.querySelector('button[aria-label="Expand request 1"]').click();
                  await wait(() => document.body.innerText.includes('UI regression evidence'));
                  assert(document.querySelector('button[aria-label="Copy Original JSON"]'), 'Copy button missing');
                  assert(document.querySelector('button[aria-label="Download Forwarded JSON"]'), 'Download missing');
                  assert(location.search.includes('request=1'), 'Detail URL missing');
                  const detailCard = document.querySelector('#panel-requests h2').parentElement.parentElement;
                  assert([...detailCard.querySelectorAll('p')].some(p => p.textContent.trim() === '8.0 k → 2.0 k (-75.0%)'), 'Detail token flow missing');
                  assert(![...detailCard.querySelectorAll('p')].some(p => p.textContent.trim() === 'Route'), 'Route remains in details');
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
                script = script.replace("TOKEN_CASES", json.dumps(token_cases))
                result = cdp(
                    "Runtime.evaluate",
                    {"expression": script, "awaitPromise": True, "returnByValue": True},
                )
                assert "exceptionDetails" not in result, result
                assert not console_errors, console_errors
                print("UI desktop:", result["result"].get("value"))
                # An external request must appear without clicking Reload.
                live_id = db.start_request("GET /r/test/live-refresh-regression")
                live_check = r"""(async () => {
                  document.querySelector('#tab-requests').click();
                  const deadline = performance.now() + 8000;
                  while (!document.querySelector('button[aria-label="Expand request LIVE_ID"]')) {
                    if (performance.now() > deadline) throw Error('Request history did not auto-refresh');
                    await new Promise(r => setTimeout(r, 50));
                  }
                  return true;
                })()"""
                live_check = live_check.replace("LIVE_ID", str(live_id))
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
