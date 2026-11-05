"""Unit and integration tests for scripts/bridge.py using fake agy fixtures."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

BRIDGE_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bridge.py"
sys.path.insert(0, str(BRIDGE_SCRIPT.parent))
import bridge  # noqa: E402


FAKE_AGY_SCRIPT = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import os
    import signal
    import subprocess
    import sys
    import time
    from pathlib import Path

    argv = sys.argv[1:]

    if argv == ["--version"]:
        print("agy-fake 2.4.0 (API_KEY=super_secret_should_be_redacted)")
        sys.exit(0)
    if argv == ["models"]:
        print("gemini-4-argon-high\\ngemini-4-argon-medium")
        sys.exit(0)
    if argv == ["mcp", "list"]:
        print("mcp: ok (TOKEN=hidden_tok_123)")
        sys.exit(0)

    mode = os.environ.get("FAKE_AGY_MODE", "success")
    record_file = os.environ.get("FAKE_AGY_RECORD")
    if record_file:
        rec = {
            "argv": argv,
            "cwd": os.getcwd(),
            "has_gemini_key": "GEMINI_API_KEY" in os.environ,
            "has_google_key": "GOOGLE_API_KEY" in os.environ,
            "has_google_creds": "GOOGLE_APPLICATION_CREDENTIALS" in os.environ,
            "pid": os.getpid(),
            "pgid": os.getpgrp(),
        }
        Path(record_file).write_text(json.dumps(rec), encoding="utf-8")

    # Emit an init and tool_use event
    print(json.dumps({"type": "system", "subtype": "init"}), flush=True)
    print(
        json.dumps({
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "view_file",
                        "input": {"path": "README.md", "secret_blob": "LONG_TOOL_INPUT_DATA"},
                    }
                ]
            },
        }),
        flush=True,
    )
    print(
        json.dumps({
            "type": "tool_result",
            "name": "view_file",
            "content": "FULL_TOOL_OUTPUT_SHOULD_NOT_BE_IN_STATUS_99999",
        }),
        flush=True,
    )

    if mode == "slow_cancel":
        # Spawn a child process in the same process group to verify whole-pgid termination
        child_pid_file = os.environ.get("FAKE_AGY_CHILD_PID_FILE")
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if child_pid_file:
            Path(child_pid_file).write_text(str(child.pid), encoding="utf-8")
        time.sleep(60)
        sys.exit(0)

    if mode == "slow_timeout":
        time.sleep(60)
        sys.exit(0)

    if mode == "hold_briefly":
        time.sleep(0.45)

    if mode == "nonzero_exit":
        sys.stderr.write("fatal execution failure\\n")
        sys.stderr.flush()
        sys.exit(2)

    if mode == "missing_result":
        print(json.dumps({"type": "assistant", "text": "partial work without result"}), flush=True)
        sys.exit(0)

    if mode == "permission_denied":
        print(
            json.dumps({
                "type": "tool_call",
                "name": "run_command",
            }),
            flush=True,
        )
        print(
            json.dumps({
                "type": "tool_result",
                "name": "run_command",
                "is_error": True,
                "content": "Error: Permission denied for shell execution in accept-edits mode",
            }),
            flush=True,
        )
        print(
            json.dumps({
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "Completed edits, but running tests was blocked because permission denied.",
                "usage": {"input_tokens": 120, "output_tokens": 45},
            }),
            flush=True,
        )
        sys.exit(0)

    if mode == "oversized_response":
        big_text = "A" * 15000
        print(
            json.dumps({
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": big_text,
                "usage": {"input_tokens": 500, "output_tokens": 1000},
            }),
            flush=True,
        )
        sys.exit(0)

    # Default: success
    print(
        json.dumps({
            "type": "tool_use",
            "name": "write_to_file",
            "input": {"path": "out.py"},
        }),
        flush=True,
    )
    print(
        json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Summary: updated out.py. Changed files: out.py. Tests: ok. Blockers: none.",
            "usage": {"input_tokens": 210, "output_tokens": 85},
        }),
        flush=True,
    )
    sys.exit(0)
    """
)


class BridgeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_ctx = tempfile.TemporaryDirectory(prefix="agy-bridge-test-")
        self.root = Path(self.tmp_ctx.name).resolve()
        self.state_dir = self.root / "state"
        self.bin_dir = self.root / "bin"
        self.workspace_a = self.root / "ws_a"
        self.workspace_b = self.root / "ws_b"
        self.bin_dir.mkdir(parents=True)
        self.workspace_a.mkdir(parents=True)
        self.workspace_b.mkdir(parents=True)

        self.fake_agy = self.bin_dir / "agy"
        self.fake_agy.write_text(FAKE_AGY_SCRIPT, encoding="utf-8")
        self.fake_agy.chmod(0o755)

        self.orig_env = os.environ.copy()
        os.environ["AGY_BRIDGE_STATE"] = str(self.state_dir)
        os.environ["PATH"] = f"{self.bin_dir}{os.pathsep}{self.orig_env.get('PATH', '')}"
        os.environ.pop("FAKE_AGY_MODE", None)
        os.environ.pop("FAKE_AGY_RECORD", None)
        os.environ.pop("FAKE_AGY_CHILD_PID_FILE", None)
        os.environ.pop("AGY_BRIDGE_WORKER_TIMEOUT_OVERRIDE", None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.orig_env)
        self.tmp_ctx.cleanup()

    def _wait_for_terminal(self, job_id: str, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = bridge.op_status(job_id)
            if st["status"] in bridge.TERMINAL_STATUSES:
                return st
            time.sleep(0.03)
        self.fail(f"Job {job_id} did not reach terminal status within {timeout}s")

    def test_default_heavy_and_medium_model_routing_and_cli_invocation(self) -> None:
        record_heavy = self.root / "rec_heavy.json"
        os.environ["FAKE_AGY_RECORD"] = str(record_heavy)
        os.environ["GEMINI_API_KEY"] = "should_be_stripped_1"
        os.environ["GOOGLE_API_KEY"] = "should_be_stripped_2"
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/tmp/fake_creds.json"

        started_heavy = bridge.op_start(
            workspace=str(self.workspace_a),
            task="Implement parser helper",
        )
        self.assertEqual(started_heavy["model"], "gemini-4-argon-high")
        self.assertEqual(started_heavy["profile"], "heavy")
        self.assertEqual(started_heavy["status"], "running")
        self.assertIsInstance(started_heavy["pid"], int)

        st_heavy = self._wait_for_terminal(started_heavy["job_id"])
        self.assertEqual(st_heavy["status"], "succeeded")
        self.assertEqual(st_heavy["last_tool"], "write_to_file")
        self.assertNotIn("FULL_TOOL_OUTPUT_SHOULD_NOT_BE_IN_STATUS_99999", json.dumps(st_heavy))

        rec = json.loads(record_heavy.read_text(encoding="utf-8"))
        argv = rec["argv"]
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertEqual(argv[0:2], ["--model", "gemini-4-argon-high"])
        self.assertIn("--mode", argv)
        self.assertEqual(argv[argv.index("--mode") + 1], "accept-edits")
        self.assertIn("--disable-slash-commands", argv)
        settings = json.loads(argv[argv.index("--settings") + 1])
        self.assertFalse(settings["useG1Credits"])
        self.assertTrue(settings["enableTerminalSandbox"])
        self.assertEqual(settings["toolPermission"], "proceed-in-sandbox")
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertEqual(argv[argv.index("--print-timeout") + 1], "900s")
        prompt_val = argv[argv.index("-p") + 1]
        self.assertIn("AGENTS.md", prompt_val)
        self.assertIn("NOT a security sandbox", prompt_val)
        self.assertIn("Implement parser helper", prompt_val)
        self.assertEqual(Path(rec["cwd"]).resolve(), self.workspace_a.resolve())
        self.assertFalse(rec["has_gemini_key"])
        self.assertFalse(rec["has_google_key"])
        self.assertFalse(rec["has_google_creds"])

        res_heavy = bridge.op_result(started_heavy["job_id"])
        self.assertEqual(res_heavy["status"], "succeeded")
        self.assertFalse(res_heavy["verified"])
        self.assertEqual(res_heavy["exit_code"], 0)
        self.assertEqual(res_heavy["model"], "gemini-4-argon-high")
        self.assertEqual(res_heavy["usage"], {"input_tokens": 210, "output_tokens": 85})
        self.assertIn("Summary: updated out.py", res_heavy["response"])
        self.assertTrue(Path(res_heavy["evidence_logs"]["stream_log"]).is_file())

        # Check directory 0700 and log file 0600 permissions
        state_mode = stat.S_IMODE(self.state_dir.stat().st_mode)
        self.assertEqual(state_mode, 0o700)
        stream_mode = stat.S_IMODE(Path(res_heavy["evidence_logs"]["stream_log"]).stat().st_mode)
        self.assertEqual(stream_mode, 0o600)

        # Now test medium profile routing
        record_med = self.root / "rec_med.json"
        os.environ["FAKE_AGY_RECORD"] = str(record_med)
        started_med = bridge.op_start(
            workspace=str(self.workspace_a),
            task="Medium task",
            profile="medium",
            timeout_seconds=120,
        )
        self.assertEqual(started_med["model"], "gemini-4-argon-medium")
        self._wait_for_terminal(started_med["job_id"])
        rec_m = json.loads(record_med.read_text(encoding="utf-8"))
        self.assertEqual(rec_m["argv"][0:2], ["--model", "gemini-4-argon-medium"])
        self.assertEqual(
            rec_m["argv"][rec_m["argv"].index("--print-timeout") + 1],
            "120s",
        )

    def test_concurrent_same_workspace_rejected_different_workspace_allowed(self) -> None:
        os.environ["FAKE_AGY_MODE"] = "hold_briefly"
        j1 = bridge.op_start(workspace=str(self.workspace_a), task="Task A1")
        try:
            with self.assertRaises(bridge.BridgeError) as ctx:
                bridge.op_start(workspace=str(self.workspace_a), task="Task A2")
            self.assertIn("already has an active job", str(ctx.exception))

            # Different workspace is allowed concurrently
            j2 = bridge.op_start(workspace=str(self.workspace_b), task="Task B1")
            self.assertEqual(j2["status"], "running")
            self._wait_for_terminal(j2["job_id"])
        finally:
            self._wait_for_terminal(j1["job_id"])

    def test_nonzero_exit_and_missing_result_fail(self) -> None:
        os.environ["FAKE_AGY_MODE"] = "nonzero_exit"
        j_err = bridge.op_start(workspace=str(self.workspace_a), task="Crash task")
        st_err = self._wait_for_terminal(j_err["job_id"])
        self.assertEqual(st_err["status"], "failed")
        res_err = bridge.op_result(j_err["job_id"])
        self.assertEqual(res_err["status"], "failed")
        self.assertEqual(res_err["exit_code"], 2)

        os.environ["FAKE_AGY_MODE"] = "missing_result"
        j_nores = bridge.op_start(workspace=str(self.workspace_a), task="Incomplete stream")
        st_nores = self._wait_for_terminal(j_nores["job_id"])
        self.assertEqual(st_nores["status"], "failed")
        res_nores = bridge.op_result(j_nores["job_id"])
        self.assertEqual(res_nores["status"], "failed")
        self.assertEqual(res_nores["exit_code"], 0)
        self.assertIn("Missing terminal result event", res_nores["error"])

    def test_permission_soft_denial_distinguished_as_needs_review(self) -> None:
        os.environ["FAKE_AGY_MODE"] = "permission_denied"
        job = bridge.op_start(workspace=str(self.workspace_a), task="Run restricted command")
        st = self._wait_for_terminal(job["job_id"])
        self.assertEqual(st["status"], "needs_review")
        self.assertEqual(st["last_tool"], "run_command")
        res = bridge.op_result(job["job_id"])
        self.assertEqual(res["status"], "needs_review")
        self.assertEqual(res["exit_code"], 0)
        self.assertFalse(res["verified"])
        self.assertTrue(res["permission_denials"])

    def test_response_bounded_to_12000_chars(self) -> None:
        os.environ["FAKE_AGY_MODE"] = "oversized_response"
        job = bridge.op_start(workspace=str(self.workspace_a), task="Verbose output")
        self._wait_for_terminal(job["job_id"])
        res = bridge.op_result(job["job_id"])
        self.assertEqual(res["status"], "succeeded")
        self.assertEqual(len(res["response"]), 12000)
        self.assertTrue(res["response_truncated"])

    def test_timeout_terminates_process_group(self) -> None:
        os.environ["FAKE_AGY_MODE"] = "slow_timeout"
        os.environ["AGY_BRIDGE_WORKER_TIMEOUT_OVERRIDE"] = "0.25"
        job = bridge.op_start(
            workspace=str(self.workspace_a),
            task="Slow task",
            timeout_seconds=30,
        )
        st = self._wait_for_terminal(job["job_id"], timeout=5.0)
        self.assertEqual(st["status"], "timed_out")
        res = bridge.op_result(job["job_id"])
        self.assertEqual(res["status"], "timed_out")

    def test_cancellation_uses_marker_and_kills_whole_process_group(self) -> None:
        os.environ["FAKE_AGY_MODE"] = "slow_cancel"
        child_pid_file = self.root / "child.pid"
        os.environ["FAKE_AGY_CHILD_PID_FILE"] = str(child_pid_file)

        job = bridge.op_start(workspace=str(self.workspace_a), task="Cancel me")
        deadline = time.monotonic() + 2.0
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.03)
        self.assertTrue(child_pid_file.exists())
        child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())

        # Verify op_cancel never calls os.kill/os.killpg directly (uses cancel.req marker)
        with mock.patch("os.killpg") as mock_killpg:
            cancelled = bridge.op_cancel(job["job_id"])
            mock_killpg.assert_not_called()

        self.assertEqual(cancelled["status"], "cancelled")
        res = bridge.op_result(job["job_id"])
        self.assertEqual(res["status"], "cancelled")

        # Verify grandchild process in agy process group was also terminated
        time.sleep(0.1)
        self.assertFalse(bridge._is_pid_alive(child_pid))

    def test_unexpectedly_dead_worker_marked_interrupted_and_releases_lock(self) -> None:
        os.environ["FAKE_AGY_MODE"] = "slow_cancel"
        job = bridge.op_start(workspace=str(self.workspace_a), task="Worker crash simulation")
        worker_pid = job["worker_pid"]
        agy_pid = job["pid"]

        # Simulate unexpected worker death and clean up its agy child in test
        os.kill(worker_pid, signal.SIGKILL)
        try:
            os.killpg(agy_pid, signal.SIGKILL)
        except OSError:
            pass
        time.sleep(0.1)

        st = bridge.op_status(job["job_id"])
        self.assertEqual(st["status"], "interrupted")

        # Lock should now be released so another job can start on workspace_a
        os.environ["FAKE_AGY_MODE"] = "success"
        job2 = bridge.op_start(workspace=str(self.workspace_a), task="Recovery job")
        st2 = self._wait_for_terminal(job2["job_id"])
        self.assertEqual(st2["status"], "succeeded")

    def test_malformed_inputs_and_path_traversal_fail(self) -> None:
        # Relative workspace
        with self.assertRaises(bridge.BridgeError):
            bridge.op_start(workspace="relative/path", task="task")
        # Non-existent workspace
        with self.assertRaises(bridge.BridgeError):
            bridge.op_start(workspace=str(self.root / "missing_dir"), task="task")
        # Invalid profile
        with self.assertRaises(bridge.BridgeError):
            bridge.op_start(workspace=str(self.workspace_a), task="task", profile="light")
        # Timeout out of range or boolean
        with self.assertRaises(bridge.BridgeError):
            bridge.op_start(workspace=str(self.workspace_a), task="task", timeout_seconds=10)
        with self.assertRaises(bridge.BridgeError):
            bridge.op_start(workspace=str(self.workspace_a), task="task", timeout_seconds=1801)
        with self.assertRaises(bridge.BridgeError):
            bridge.op_start(workspace=str(self.workspace_a), task="task", timeout_seconds=True)
        # Path traversal job IDs
        for bad_id in ("../escape", "../../etc/passwd", "job/../../x", ".hidden", "", "a b"):
            with self.assertRaises(bridge.BridgeError):
                bridge.op_status(bad_id)
            with self.assertRaises(bridge.BridgeError):
                bridge.op_result(bad_id)
            with self.assertRaises(bridge.BridgeError):
                bridge.op_cancel(bad_id)

    def test_mcp_stdio_protocol_and_server_restart_persistence(self) -> None:
        # 1. Initialize, notifications/initialized, ping, tools/list, and agy_start on Server 1
        reqs_server_1 = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "agy_start",
                    "arguments": {
                        "workspace": str(self.workspace_a),
                        "task": "MCP task execution",
                        "profile": "heavy",
                        "timeout_seconds": 60,
                    },
                },
            },
            # Unknown argument should return isError: True
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "agy_start",
                    "arguments": {
                        "workspace": str(self.workspace_a),
                        "task": "Bad args",
                        "extra_unknown": 123,
                    },
                },
            },
            # Path traversal job_id in tools/call returns isError: True
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "agy_status",
                    "arguments": {"job_id": "../traversal"},
                },
            },
            # Unknown JSON-RPC method returns -32601
            {"jsonrpc": "2.0", "id": 7, "method": "nonexistent/method"},
        ]

        stdin_payload = "\n".join(json.dumps(r) for r in reqs_server_1) + "\n{bad json\n"
        proc1 = subprocess.run(
            [sys.executable, str(BRIDGE_SCRIPT), "serve"],
            input=stdin_payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        lines1 = [json.loads(line) for line in proc1.stdout.strip().splitlines()]
        # notifications/initialized produces no response, so 7 requests + 1 parse error = 8 lines
        self.assertEqual(len(lines1), 8)
        self.assertEqual(lines1[0]["id"], 1)
        self.assertEqual(lines1[0]["result"]["serverInfo"]["name"], "antigravity-bridge")
        self.assertEqual(lines1[1], {"jsonrpc": "2.0", "id": 2, "result": {}})

        tool_names = [t["name"] for t in lines1[2]["result"]["tools"]]
        self.assertEqual(
            tool_names,
            ["agy_start", "agy_status", "agy_result", "agy_cancel", "agy_doctor"],
        )

        start_res = lines1[3]["result"]
        self.assertFalse(start_res["isError"])
        job_id = json.loads(start_res["content"][0]["text"])["job_id"]

        self.assertTrue(lines1[4]["result"]["isError"])
        self.assertTrue(lines1[5]["result"]["isError"])
        self.assertEqual(lines1[6]["error"]["code"], -32601)
        self.assertEqual(lines1[7]["error"]["code"], -32700)

        # Wait for job completion and query from a brand new MCP server process (Server 2)
        self._wait_for_terminal(job_id)
        reqs_server_2 = [
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {"name": "agy_result", "arguments": {"job_id": job_id}},
            },
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {"name": "agy_doctor", "arguments": {}},
            },
        ]
        proc2 = subprocess.run(
            [sys.executable, str(BRIDGE_SCRIPT), "serve"],
            input="\n".join(json.dumps(r) for r in reqs_server_2) + "\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        lines2 = [json.loads(line) for line in proc2.stdout.strip().splitlines()]
        self.assertEqual(len(lines2), 2)
        result_payload = json.loads(lines2[0]["result"]["content"][0]["text"])
        self.assertEqual(result_payload["status"], "succeeded")
        self.assertEqual(result_payload["job_id"], job_id)

        doctor_payload = json.loads(lines2[1]["result"]["content"][0]["text"])
        self.assertTrue(doctor_payload["ok"])
        self.assertNotIn("super_secret_should_be_redacted", json.dumps(doctor_payload))
        self.assertNotIn("hidden_tok_123", json.dumps(doctor_payload))

    def test_direct_cli_commands_and_error_exit_codes(self) -> None:
        task_file = self.root / "task.txt"
        task_file.write_text("CLI task body", encoding="utf-8")

        p_start = subprocess.run(
            [
                sys.executable,
                str(BRIDGE_SCRIPT),
                "start",
                "--workspace",
                str(self.workspace_a),
                "--task-file",
                str(task_file),
                "--profile",
                "medium",
                "--timeout",
                "60",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        start_data = json.loads(p_start.stdout)
        job_id = start_data["job_id"]
        self.assertEqual(start_data["model"], "gemini-4-argon-medium")

        self._wait_for_terminal(job_id)

        p_res = subprocess.run(
            [sys.executable, str(BRIDGE_SCRIPT), "result", job_id],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        res_data = json.loads(p_res.stdout)
        self.assertEqual(res_data["status"], "succeeded")

        # Operation error on CLI must return nonzero exit code
        p_bad = subprocess.run(
            [sys.executable, str(BRIDGE_SCRIPT), "status", "nonexistent_job_id"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertNotEqual(p_bad.returncode, 0)


if __name__ == "__main__":
    unittest.main()
