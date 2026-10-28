"""Manual live Pi RPC telemetry driver. No mocks, unit assertions, or transcript logging.

Run with the python tool (runpy.run_path is sufficient). Repeat --turn for each real
prompt, or supply --turns-file containing a private JSON list of prompts. Requests
incur real provider usage. The default is an ephemeral session with normal extensions.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import urllib.request


def stats(url: str) -> dict:
    try:
        with urllib.request.urlopen(url.rstrip('/') + '/stats', timeout=3) as response:
            data = json.load(response)
        return {k: data.get(k) for k in ('requests', 'unchanged', 'stripped', 'pairs_dropped',
                                        'media_removed', 'usage_tokens', 'last_request')}
    except (OSError, ValueError):
        return {'unavailable': True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--turn', action='append', default=[])
    parser.add_argument('--turns-file', type=Path)
    parser.add_argument('--cwd', type=Path, required=True)
    parser.add_argument('--router', default='http://127.0.0.1:4141')
    parser.add_argument('--reload-after', type=int, action='append', default=[])
    parser.add_argument('--restart-router-after', type=int, action='append', default=[],
                        help='Explicit manual service-restart checkpoint; affects other router clients')
    parser.add_argument('--extension', action='append', default=[])
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    turns = args.turn + (json.loads(args.turns_file.read_text()) if args.turns_file else [])
    if not turns:
        parser.error('at least one real turn is required')
    args.cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
    command = ['pi', '--mode', 'rpc', '--no-session', '--provider', args.provider,
               '--model', args.model, '--thinking', 'off', '--offline', '--no-mcp',
               '--no-context-files', '--no-skills', '--no-prompt-templates', '--tools', 'read',
               '--system-prompt', 'You are validating context transport. Follow each user request exactly. Use read only if asked. Reply briefly; do not explore, delegate, or run other tools.']
    for extension in args.extension:
        command.extend(['-e', extension])
    process = subprocess.Popen(command, cwd=args.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True)
    incoming: queue.Queue = queue.Queue()

    def pump() -> None:
        try:
            for line in process.stdout:
                try:
                    incoming.put(json.loads(line))
                except ValueError:
                    pass
        finally:
            incoming.put(None)

    threading.Thread(target=pump, daemon=True).start()
    report: dict = {'provider': args.provider, 'model': args.model, 'turns': [], 'before': stats(args.router)}
    current: dict = {}
    serial = 0

    def receive(deadline: float) -> dict:
        item = incoming.get(timeout=max(0.01, deadline - time.monotonic()))
        if item is None:
            raise RuntimeError('Pi exited before RPC completion')
        if item.get('type') == 'message_end':
            message = item.get('message', {})
            if message.get('role') == 'assistant':
                usage = message.get('usage', {})
                current.setdefault('usage', []).append({k: usage.get(k, 0) for k in ('input', 'cacheRead', 'cacheWrite', 'output')})
                if message.get('stopReason') in ('error', 'aborted'):
                    current['provider_error'] = True
        if item.get('type') == 'extension_ui_request':
            if item.get('method') == 'notify':
                message = item.get('message', '')
                if message.startswith('Context safety:'):
                    current['context_notices'] = current.get('context_notices', 0) + 1
                if message.startswith('Context safety stopped'):
                    current['guard_stopped'] = True
            else:
                raise RuntimeError('Pi requested unexpected interactive UI')
        return item

    def request(kind: str, **fields) -> dict:
        nonlocal serial
        serial += 1
        request_id = f'manual-{serial}'
        process.stdin.write(json.dumps({'id': request_id, 'type': kind, **fields}) + '\n')
        process.stdin.flush()
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            item = receive(deadline)
            if item.get('type') == 'response' and item.get('id') == request_id:
                if not item.get('success'):
                    raise RuntimeError(f'RPC {kind} failed')
                return item.get('data') or {}
        raise TimeoutError(f'RPC {kind} deadline')

    try:
        state = request('get_state')
        model = state.get('model') or {}
        report['context_window'] = model.get('contextWindow')
        for index, prompt in enumerate(turns, 1):
            current = {'turn': index}
            start = time.monotonic()
            response = request('prompt', message=prompt)
            if response.get('disposition') != 'handled':
                deadline = time.monotonic() + args.timeout
                while receive(deadline).get('type') != 'agent_settled':
                    pass
            current['seconds'] = round(time.monotonic() - start, 3)
            current['router'] = stats(args.router)
            report['turns'].append(current)
            print(json.dumps(current), flush=True)
            if index in args.restart_router_after:
                subprocess.run(['systemctl', '--user', 'restart', 'pi-context-router.service'], check=True, timeout=45,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if not stats(args.router).get('unavailable'):
                        report.setdefault('router_restarts', []).append({'after_turn': index, 'ready': True})
                        break
                    time.sleep(0.2)
                else:
                    raise RuntimeError('router did not become ready after restart')
            if index in args.reload_after:
                current = {}
                response = request('prompt', message='/context-reload')
                state = request('get_state')
                report.setdefault('reloads', []).append({'after_turn': index, 'handled': response.get('disposition') == 'handled', 'idle': not state.get('isStreaming', True)})
        report['after'] = stats(args.router)
    finally:
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            import signal
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(report, stream, indent=2)
        print(json.dumps({'completed_turns': len(report['turns']), 'pi_exit': process.returncode, 'reloads': report.get('reloads', [])}), flush=True)


if __name__ == '__main__':
    main()
