#!/usr/bin/env python3
"""Route CLI calls and each MCP request to the currently installed bridge.

No model/provider calls are made for runtime discovery. Access contracts travel
unchanged; policy errors are returned without retry or fallback to another engine.
"""
from __future__ import annotations
import json
import re
import subprocess
import os
import shutil
import sys
from pathlib import Path


class DispatchError(Exception):
    pass


def _version(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise DispatchError('Invalid installed bridge version')
    return tuple(map(int, value.split('.')))


def resolve_current():
    try:
        executable = shutil.which('codex') or str(Path.home()/'.local/bin/codex')
        p = subprocess.run([executable, 'plugin', 'list', '--marketplace', 'gemini-bridge-local', '--json'],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20, check=False)
        if p.returncode:
            raise DispatchError('Cannot inspect installed Gemini Bridge catalog')
        catalog = json.loads(p.stdout)
        entries = [v for v in catalog.get('installed', []) if v.get('name') == 'antigravity-bridge'
                   and v.get('installed') is True and v.get('enabled') is True]
        if len(entries) != 1:
            raise DispatchError('Exactly one enabled Gemini Bridge installation is required')
        entry = entries[0]
        version = entry.get('version')
        if _version(version) < (0, 3, 0):
            raise DispatchError('Gemini Bridge 0.3.0 or newer is required; access scope will not be dropped')
        source = entry.get('source', {})
        root = Path(source.get('path', ''))
        if not root.is_absolute():
            raise DispatchError('Installed plugin source must be an absolute local directory')
        codex_home = Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex').expanduser()
        pointer = codex_home/'plugin-marketplaces/gemini-bridge/active-runtime.json'
        if pointer.is_file():
            active = json.loads(pointer.read_text())
            if active.get('version') == version and active.get('plugin') == 'antigravity-bridge@gemini-bridge-local':
                cached = Path(active.get('path', ''))
                cache_root = (codex_home/'plugins/cache/gemini-bridge-local/antigravity-bridge').resolve()
                if not cached.is_absolute() or not cached.resolve(strict=True).is_relative_to(cache_root):
                    raise DispatchError('Installed cache pointer is outside the managed plugin cache')
                root = cached
        root = root.resolve(strict=True)
        manifest = json.loads((root/'plugin.json').read_text())
        if manifest.get('name') != 'antigravity-bridge' or manifest.get('version') != version:
            raise DispatchError('Installed catalog and plugin manifest do not match')
        bridge = root/'scripts/bridge.py'
        if bridge.is_symlink() or not bridge.resolve(strict=True).is_relative_to(root):
            raise DispatchError('Bridge executable must be contained in the installed plugin')
        found = re.search(r'^SERVER_VERSION\s*=\s*"([^"]+)"', bridge.read_text(), re.M)
        if not found or found.group(1) != version:
            raise DispatchError('Installed bridge code and manifest versions do not match')
        return bridge, version
    except (OSError, ValueError, AttributeError, TypeError, subprocess.TimeoutExpired) as exc:
        raise DispatchError('Installed Gemini Bridge could not be resolved safely: '+str(exc)) from exc


def _rpc_failure(request, message):
    result = {'content': [{'type': 'text', 'text': message}], 'isError': True,
              'structuredContent': {'code': 'runtime_unavailable', 'error': message,
                                    'next_action': 'report_blocker_no_retry'}}
    if request.get('method') == 'tools/call':
        return {'jsonrpc': '2.0', 'id': request.get('id'), 'result': result}
    return {'jsonrpc': '2.0', 'id': request.get('id'),
            'error': {'code': -32603, 'message': message}}


def dispatch_line(line):
    try:
        request = json.loads(line)
    except ValueError:
        return [{'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}]
    if not isinstance(request, dict):
        return [{'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid request'}}]
    if 'id' not in request:
        return []
    try:
        bridge, version = resolve_current()
        # Published 0.3.0 already supports a single line through serve + EOF.
        # Newer engines have an explicit one-request entry point.
        command = 'serve' if _version(version) == (0, 3, 0) else 'rpc-one'
        p = subprocess.run([sys.executable, str(bridge), command], input=line+'\n',
                           capture_output=True, text=True, timeout=60, check=False)
        if p.returncode:
            raise DispatchError('Current bridge request failed; no retry or stale fallback performed')
        messages = [json.loads(row) for row in p.stdout.splitlines() if row.strip()]
        if len(messages) != 1 or not isinstance(messages[0], dict) or messages[0].get('id') != request.get('id'):
            raise DispatchError('Current bridge returned an invalid response')
        response = messages[0]
        if ('result' in response) == ('error' in response):
            raise DispatchError('Current bridge returned an invalid result/error envelope')
        if not isinstance(response.get('result', response.get('error')), dict):
            raise DispatchError('Current bridge returned an invalid MCP result')
        if '_meta' in response and not isinstance(response['_meta'], dict):
            raise DispatchError('Current bridge returned invalid metadata')
        response.setdefault('_meta', {})['geminiBridgeVersion'] = version
        if request.get('method') == 'initialize' and 'result' in response:
            response['result'].setdefault('capabilities', {})['tools'] = {'listChanged': True}
            response['result']['instructions'] = ('Use the current bridge engine. Preserve human-authorized access declarations. '
                'A denial is a blocker, never a reason to widen permissions or repeat a generic approval question.')
        return [response]
    except (DispatchError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return [_rpc_failure(request, str(exc))]


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ['serve']:
        previous_version = None
        for line in sys.stdin:
            if not line.strip(): continue
            messages = dispatch_line(line.rstrip('\n'))
            for response in messages:
                version = response.get('_meta', {}).get('geminiBridgeVersion')
                if previous_version is not None and version and version != previous_version:
                    sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'})+'\n')
                if version:
                    previous_version = version
                # Re-query tools on each request; never retain an old engine module.
                sys.stdout.write(json.dumps(response)+'\n');sys.stdout.flush()
        return 0
    try:
        bridge, version = resolve_current()
        if args == ['--where']:
            print(json.dumps({'bridge': str(bridge), 'bridge_version': version, 'routing': 'current_installed'}))
            return 0
        if not args or args[0] not in {'start', 'status', 'result', 'cancel', 'doctor'}:
            raise DispatchError('Allowed commands: --where, serve, start, status, result, cancel, doctor')
        # Preserve every argument, including --access-file, without shell interpolation.
        return subprocess.run([sys.executable, str(bridge), *args], check=False).returncode
    except (DispatchError, OSError) as exc:
        print(json.dumps({'error': str(exc), 'code': 'runtime_unavailable',
                          'next_action': 'report_blocker_no_retry'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
