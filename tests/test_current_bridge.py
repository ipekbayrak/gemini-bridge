"""Unit and integration tests for scripts/current_bridge.py."""

from __future__ import annotations

import io, json, os, subprocess, sys, tempfile, textwrap, unittest
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
CURRENT_BRIDGE_SCRIPT = SCRIPTS_DIR / "current_bridge.py"
sys.path.insert(0, str(SCRIPTS_DIR))
import current_bridge  # noqa: E402

FAKE_CODEX_BODY = textwrap.dedent(
    """\
    import json, os, sys
    from pathlib import Path
    codex_home = Path(os.environ["CODEX_HOME"])
    with open(codex_home / "codex_calls.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": sys.argv[1:]}) + "\\n")
    if sys.argv[1:] != ["plugin", "list", "--marketplace", "gemini-bridge-local", "--json"]:
        sys.exit(2)
    catalog_file = codex_home / "catalog.json"
    if not catalog_file.is_file():
        sys.exit(1)
    sys.stdout.write(catalog_file.read_text(encoding="utf-8"))
    """
)

SYNTHETIC_BRIDGE_TEMPLATE = textwrap.dedent(
    """\
    import json, os, sys
    from pathlib import Path

    SERVER_NAME = "antigravity-bridge"
    SERVER_VERSION = "{code_version}"

    def _handle_rpc(raw_line: str) -> dict | None:
        msg = json.loads(raw_line)
        if "id" not in msg:
            return None
        req_id, method = msg["id"], msg.get("method")
        if method == "initialize":
            return {{"jsonrpc": "2.0", "id": req_id, "result": {{
                "protocolVersion": "2024-11-05", "capabilities": {{"tools": {{}}}},
                "serverInfo": {{"name": SERVER_NAME, "version": SERVER_VERSION}},
            }}}}
        if method == "tools/call":
            params = msg.get("params", {{}})
            payload = {{"engine_version": SERVER_VERSION, "engine_path": str(Path(__file__).resolve()),
                       "tool": params.get("name"), "arguments": params.get("arguments")}}
            return {{"jsonrpc": "2.0", "id": req_id, "result": {{
                "content": [{{"type": "text", "text": json.dumps(payload)}}],
                "structuredContent": payload, "isError": False,
            }}}}
        return {{"jsonrpc": "2.0", "id": req_id, "result": {{"version": SERVER_VERSION}}}}

    argv = sys.argv[1:]
    if record_path := os.environ.get("FAKE_ENGINE_RECORD"):
        with open(record_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({{"version": SERVER_VERSION, "argv": argv}}) + "\\n")
    if argv == ["rpc-one"]:
        if (resp := _handle_rpc(sys.stdin.readline())) is not None:
            print(json.dumps(resp), flush=True)
        sys.exit(0)
    if argv == ["serve"]:
        for line in sys.stdin:
            if line.strip() and (resp := _handle_rpc(line)) is not None:
                print(json.dumps(resp), flush=True)
        sys.exit(0)
    if argv and argv[0] in {{"start", "status", "result", "cancel", "doctor"}}:
        sys.exit(int(os.environ.get("FAKE_ENGINE_EXIT_CODE", "0")))
    sys.exit(2)
    """
)


class CurrentBridgeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="agy-current-bridge-test-")
        self.root = Path(self.tmp.name).resolve()
        self.home, self.codex_home, self.bin_dir = self.root / "home", self.root / "home/.codex", self.root / "bin"
        for d in (self.home, self.codex_home, self.bin_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.codex_bin = self.bin_dir / "codex"
        self.codex_bin.write_text(f"#!{sys.executable}\n{FAKE_CODEX_BODY}", encoding="utf-8")
        self.codex_bin.chmod(0o755)
        self.record_file = self.root / "engine_calls.jsonl"
        self.env = {
            "HOME": str(self.home), "CODEX_HOME": str(self.codex_home),
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_ENGINE_RECORD": str(self.record_file),
        }
        self._env_patch = mock.patch.dict(os.environ, self.env, clear=False)
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()
        self.tmp.cleanup()

    def _make_plugin(
        self, name: str, version: str = "0.3.1", *, manifest_version: str | None = None,
        code_version: str | None = None, manifest_name: str = "antigravity-bridge"
    ) -> Path:
        plugin_root = self.root / name
        (plugin_root / "scripts").mkdir(parents=True, exist_ok=True)
        (plugin_root / "plugin.json").write_text(
            json.dumps({"name": manifest_name, "version": manifest_version or version}), encoding="utf-8"
        )
        bridge_py = plugin_root / "scripts" / "bridge.py"
        bridge_py.write_text(SYNTHETIC_BRIDGE_TEMPLATE.format(code_version=code_version or version), encoding="utf-8")
        bridge_py.chmod(0o755)
        return plugin_root

    def _write_catalog(self, entries: list[dict]) -> None:
        (self.codex_home / "catalog.json").write_text(json.dumps({"installed": entries}), encoding="utf-8")

    def _entry(
        self, path: Path | str, version: str = "0.3.1", *, installed: bool = True,
        enabled: bool = True, name: str = "antigravity-bridge"
    ) -> dict:
        return {"name": name, "version": version, "installed": installed, "enabled": enabled, "source": {"path": str(path)}}

    def test_resolve_current_and_validation_guards(self) -> None:
        p31 = self._make_plugin("plugin-031", "0.3.1")
        self._write_catalog([self._entry(p31, "0.3.1")])
        self.assertEqual(current_bridge.resolve_current(), ((p31 / "scripts" / "bridge.py").resolve(), "0.3.1"))

        p22 = self._make_plugin("plugin-022", "0.2.2")
        p_bad_manifest = self._make_plugin("plugin-bad-manifest", "0.3.1", manifest_version="0.3.2")
        p_bad_code = self._make_plugin("plugin-bad-code", "0.3.1", code_version="0.3.2")
        p_sym = self._make_plugin("plugin-symlink", "0.3.1")
        outside_py = self.root / "outside_bridge.py"
        outside_py.write_text(SYNTHETIC_BRIDGE_TEMPLATE.format(code_version="0.3.1"), encoding="utf-8")
        (p_sym / "scripts" / "bridge.py").unlink()
        (p_sym / "scripts" / "bridge.py").symlink_to(outside_py)

        for bad_entries in (
            [self._entry(p22, "0.2.2")],
            [self._entry(p31, "0.3.1", enabled=False)],
            [self._entry(p31, "0.3.1", installed=False)],
            [self._entry(p31, "0.3.1"), self._entry(p31, "0.3.1")],
            [self._entry("relative/plugin", "0.3.1")],
            [self._entry(p31 / "../outside-nonexistent", "0.3.1")],
            [self._entry(p_bad_manifest, "0.3.1")],
            [self._entry(p_bad_code, "0.3.1")],
            [self._entry(p_sym, "0.3.1")],
        ):
            self._write_catalog(bad_entries)
            with self.assertRaises(current_bridge.DispatchError):
                current_bridge.resolve_current()

    def test_verified_cache_survives_a_failed_source_upgrade(self) -> None:
        cached=self._make_plugin("cached-seed","0.3.1")
        cache=self.codex_home/"plugins/cache/gemini-bridge-local/antigravity-bridge/0.3.1"
        cache.parent.mkdir(parents=True);cached.rename(cache)
        mutable=self._make_plugin("mutable-source","0.3.2")
        self._write_catalog([self._entry(mutable,"0.3.1")])
        pointer=self.codex_home/"plugin-marketplaces/gemini-bridge/active-runtime.json"
        pointer.parent.mkdir(parents=True)
        pointer.write_text(json.dumps({"plugin":"antigravity-bridge@gemini-bridge-local","version":"0.3.1","path":str(cache)}))
        self.assertEqual(current_bridge.resolve_current(),(cache/"scripts/bridge.py","0.3.1"))
        pointer.unlink()
        with self.assertRaises(current_bridge.DispatchError):current_bridge.resolve_current()

    def test_dispatch_line_notifications_and_error_reporting(self) -> None:
        out = current_bridge.dispatch_line(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(out, [])
        self.assertFalse((self.codex_home / "codex_calls.jsonl").exists())

        p22 = self._make_plugin("plugin-old", "0.2.2")
        self._write_catalog([self._entry(p22, "0.2.2")])
        res = current_bridge.dispatch_line(json.dumps({
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": "agy_start", "arguments": {"workspace": "/tmp/ws", "task": "t", "access": {"read_urls": ["https://a.example"]}}},
        }))
        self.assertTrue(res[0]["result"]["isError"])
        self.assertEqual(res[0]["result"]["structuredContent"]["code"], "runtime_unavailable")

    def test_malformed_engine_response_does_not_crash_dispatcher(self) -> None:
        request=json.dumps({"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"agy_status","arguments":{"job_id":"x"}}})
        bad=[42, {"id":7,"result":None}, {"id":7,"result":{},"error":{}}, {"id":7,"result":{},"_meta":None}]
        with mock.patch.object(current_bridge,"resolve_current",return_value=(self.root/"unused.py","0.3.2")):
            for response in bad:
                with self.subTest(response=response),mock.patch.object(current_bridge.subprocess,"run",return_value=subprocess.CompletedProcess([],0,json.dumps(response)+"\n","")):
                    result=current_bridge.dispatch_line(request)[0]
                    self.assertTrue(result["result"]["isError"])
                    self.assertEqual(result["result"]["structuredContent"]["code"],"runtime_unavailable")
            good={"jsonrpc":"2.0","id":7,"result":{"content":[],"isError":False}}
            with mock.patch.object(current_bridge.subprocess,"run",return_value=subprocess.CompletedProcess([],0,json.dumps(good)+"\n","")):
                self.assertFalse(current_bridge.dispatch_line(request)[0]["result"]["isError"])

    def test_live_serve_hot_reload_and_access_integrity(self) -> None:
        p30 = self._make_plugin("plugin-030", "0.3.0")
        p31 = self._make_plugin("plugin-031-live", "0.3.1")
        self._write_catalog([self._entry(p30, "0.3.0")])

        proc = subprocess.Popen(
            [sys.executable, str(CURRENT_BRIDGE_SCRIPT), "serve"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env,
        )
        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n")
            proc.stdin.flush()
            init_resp = json.loads(proc.stdout.readline())
            self.assertEqual(init_resp["result"]["serverInfo"]["version"], "0.3.0")
            self.assertIs(init_resp["result"]["capabilities"]["tools"]["listChanged"], True)

            self._write_catalog([self._entry(p31, "0.3.1")])
            access_payload = {
                "read_urls": ["https://example.com/audit?a=1&b=2"],
                "read_files": [str(self.root / "dir with spaces" / "approved file.json")],
                "override_engine": str(p30 / "scripts" / "bridge.py"),
            }
            call_req = {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "agy_start", "arguments": {
                    "workspace": str(self.root / "workspace with spaces"),
                    "task": "Verify exact payload", "access": access_payload,
                }},
            }
            proc.stdin.write(json.dumps(call_req) + "\n")
            proc.stdin.flush()
            call_resp = json.loads(proc.stdout.readline())
            if "id" not in call_resp:
                self.assertEqual(call_resp.get("method"), "notifications/tools/list_changed")
                call_resp = json.loads(proc.stdout.readline())
            sc = call_resp["result"]["structuredContent"]
            self.assertEqual(sc["engine_version"], "0.3.1")
            self.assertEqual(sc["engine_path"], str((p31 / "scripts" / "bridge.py").resolve()))
            self.assertEqual(sc["arguments"]["access"], access_payload)
            self.assertEqual(sc["arguments"]["workspace"], str(self.root / "workspace with spaces"))

            self._write_catalog([self._entry(p31, "0.3.1", enabled=False)])
            proc.stdin.write(json.dumps({**call_req, "id": 3}) + "\n")
            proc.stdin.flush()
            fail_resp = json.loads(proc.stdout.readline())
            self.assertTrue(fail_resp["result"]["isError"])
            self.assertEqual(fail_resp["result"]["structuredContent"]["code"], "runtime_unavailable")
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream:
                    stream.close()
            proc.wait(timeout=10)

    def test_cli_where_and_forwarding_with_spaces(self) -> None:
        p31 = self._make_plugin("plugin-cli", "0.3.1")
        self._write_catalog([self._entry(p31, "0.3.1")])

        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            self.assertEqual(current_bridge.main(["--where"]), 0)
        where_data = json.loads(buf.getvalue())
        self.assertEqual(where_data["bridge_version"], "0.3.1")
        self.assertEqual(where_data["bridge"], str((p31 / "scripts" / "bridge.py").resolve()))

        cli_args = [
            "start", "--workspace", str(self.root / "my workspace" / "proj A"),
            "--task-file", str(self.root / "my workspace" / "task spec.md"),
            "--access-file", str(self.root / "my workspace" / "access grant.json"),
        ]
        with mock.patch.dict(os.environ, {"FAKE_ENGINE_EXIT_CODE": "7"}):
            self.assertEqual(current_bridge.main(cli_args), 7)
        recorded = [json.loads(line) for line in self.record_file.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(recorded[-1]["argv"], cli_args)

        err_buf = io.StringIO()
        with mock.patch("sys.stdout", err_buf):
            self.assertEqual(current_bridge.main(["unsupported-cmd"]), 1)
        self.assertEqual(json.loads(err_buf.getvalue())["code"], "runtime_unavailable")


if __name__ == "__main__":
    unittest.main()
