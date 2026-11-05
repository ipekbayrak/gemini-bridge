"""Unit and integration tests for scripts/setup.py, scripts/launch.sh, and scripts/launch-chrome.sh."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
SETUP_SCRIPT = SCRIPTS_DIR / "setup.py"
LAUNCH_SCRIPT = SCRIPTS_DIR / "launch.sh"
LAUNCH_CHROME_SCRIPT = SCRIPTS_DIR / "launch-chrome.sh"

sys.path.insert(0, str(SCRIPTS_DIR))
import setup  # noqa: E402


FAKE_AGY_TEMPLATE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import os
    import sys
    from pathlib import Path

    argv = sys.argv[1:]
    log_path = os.environ.get("FAKE_CLI_LOG")
    if log_path:
        entry = {
            "cli": "agy",
            "argv": argv,
            "cwd": os.getcwd(),
            "path_env": os.environ.get("PATH", ""),
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\\n")

    if argv == ["models"]:
        mode = os.environ.get("FAKE_AGY_MODELS_MODE", "ok")
        if mode == "fail":
            sys.stderr.write("Error: not logged in (API_KEY=secret_should_not_leak)\\n")
            sys.exit(1)
        if mode == "missing_model":
            print("gemini-3-flash")
            sys.exit(0)
        print("gemini-4-argon-high\\ngemini-4-argon-medium\\nTOKEN=secret_tok_999")
        sys.exit(0)

    if argv == ["mcp", "list"]:
        state_file = os.environ.get("FAKE_AGY_MCP_STATE")
        if state_file and Path(state_file).is_file():
            print(Path(state_file).read_text(encoding="utf-8"))
        else:
            print("No MCP servers configured.")
        sys.exit(0)

    if len(argv) >= 2 and argv[:2] == ["mcp", "remove"]:
        state_file = os.environ.get("FAKE_AGY_MCP_STATE")
        if state_file and Path(state_file).is_file():
            Path(state_file).unlink()
        sys.exit(0)

    if len(argv) >= 4 and argv[:2] == ["mcp", "add"]:
        if os.environ.get("FAKE_AGY_MCP_ADD_FAIL") == "1":
            sys.stderr.write("mcp add failed\\n")
            sys.exit(2)
        state_file = os.environ.get("FAKE_AGY_MCP_STATE")
        if state_file:
            Path(state_file).write_text(f"{argv[2]}: {argv[3]}\\n", encoding="utf-8")
        print(f"Added MCP server {argv[2]}")
        sys.exit(0)

    sys.exit(0)
    """
)

FAKE_CODEX_TEMPLATE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import os
    import sys

    argv = sys.argv[1:]
    log_path = os.environ.get("FAKE_CLI_LOG")
    if log_path:
        entry = {
            "cli": "codex",
            "argv": argv,
            "cwd": os.getcwd(),
            "path_env": os.environ.get("PATH", ""),
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\\n")

    if argv[:3] == ["plugin", "marketplace", "add"]:
        if os.environ.get("FAKE_CODEX_MP_FAIL") == "1":
            sys.stderr.write("marketplace registration failed (SECRET=hidden_mp_secret)\\n")
            sys.exit(3)
        print(json.dumps({"status": "marketplace_added", "root": argv[3]}))
        sys.exit(0)

    if argv[:2] == ["plugin", "add"]:
        if os.environ.get("FAKE_CODEX_PLUGIN_FAIL") == "1":
            sys.stderr.write("plugin add failed\\n")
            sys.exit(4)
        print(json.dumps({"status": "plugin_added", "plugin": argv[2]}))
        sys.exit(0)

    sys.exit(0)
    """
)


def _write_exec(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


class SetupAndLaunchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_ctx = tempfile.TemporaryDirectory(prefix="agy-setup-test-")
        self.root = Path(self.tmp_ctx.name).resolve()
        self.home_dir = self.root / "home"
        self.codex_home = self.home_dir / ".codex"
        self.bin_dir = self.root / "bin"
        self.sys_bin_dir = self.root / "sys_bin"
        self.log_file = self.root / "cli_calls.jsonl"
        self.mcp_state_file = self.root / "agy_mcp_state.txt"

        self.home_dir.mkdir(parents=True)
        self.codex_home.mkdir(parents=True)
        self.bin_dir.mkdir(parents=True)
        self.sys_bin_dir.mkdir(parents=True)

        self.fake_agy = _write_exec(self.bin_dir / "agy", FAKE_AGY_TEMPLATE)
        self.fake_codex = _write_exec(self.bin_dir / "codex", FAKE_CODEX_TEMPLATE)
        self.fake_chrome = _write_exec(
            self.bin_dir / "google-chrome",
            "#!/bin/bash\necho 'Google Chrome 124.0'\n",
        )
        self.fake_node = _write_exec(
            self.bin_dir / "node",
            "#!/bin/bash\necho 'v20.19.0'\n",
        )
        self.fake_npx = _write_exec(
            self.bin_dir / "npx",
            "#!/bin/bash\necho \"npx-invoked:$*\"\n",
        )

        # Ensure python3 >= 3.11 is available in bin_dir via symlink to current sys.executable
        self.python_link = self.bin_dir / "python3"
        if not self.python_link.exists():
            os.symlink(sys.executable, self.python_link)

    def tearDown(self) -> None:
        self.tmp_ctx.cleanup()

    def _base_child_env(
        self,
        *,
        home: Path | None = None,
        codex_home: Path | None = None,
        path_dirs: list[Path] | None = None,
        system_bins: str = "",
        chrome_paths: str = "",
    ) -> dict[str, str]:
        env = dict(os.environ)
        env["HOME"] = str(home if home is not None else self.home_dir)
        env["CODEX_HOME"] = str(codex_home if codex_home is not None else self.codex_home)
        if path_dirs is not None:
            env["PATH"] = os.pathsep.join(str(p) for p in path_dirs)
        else:
            env["PATH"] = f"{self.bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"
        env["AGY_SETUP_SYSTEM_BINS"] = system_bins
        env["AGY_SETUP_CHROME_PATHS"] = chrome_paths
        env["FAKE_CLI_LOG"] = str(self.log_file)
        env["FAKE_AGY_MCP_STATE"] = str(self.mcp_state_file)
        for k in (
            "FAKE_AGY_MODELS_MODE",
            "FAKE_AGY_MCP_ADD_FAIL",
            "FAKE_CODEX_MP_FAIL",
            "FAKE_CODEX_PLUGIN_FAIL",
        ):
            env.pop(k, None)
        return env

    def _run_setup_cli(
        self,
        args: list[str],
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        run_env = env if env is not None else self._base_child_env()
        return subprocess.run(
            [sys.executable, str(SETUP_SCRIPT), *args],
            env=run_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            shell=False,
            check=False,
        )

    def _read_cli_logs(self) -> list[dict]:
        if not self.log_file.is_file():
            return []
        return [
            json.loads(line)
            for line in self.log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _create_source_copy(self, target_dir: Path, include_license: bool = True) -> Path:
        target_dir.mkdir(parents=True, exist_ok=True)
        for fname in ("plugin.json", "mcp.json", "README.md"):
            shutil.copy2(REPO_ROOT / fname, target_dir / fname)
        if include_license:
            (target_dir / "LICENSE").write_text("MIT License\n", encoding="utf-8")
        for dname in ("scripts", "skills", "assets"):
            src_d = REPO_ROOT / dname
            if src_d.is_dir():
                shutil.copytree(src_d, target_dir / dname, dirs_exist_ok=True)
        return target_dir

    def test_check_online_and_offline_and_secret_redaction(self) -> None:
        env = self._base_child_env()
        res_online = self._run_setup_cli(["check", "--json"], env=env)
        self.assertEqual(res_online.returncode, 0, res_online.stderr)
        report = json.loads(res_online.stdout)
        self.assertTrue(report["ok"])
        self.assertTrue(report["platform"]["ok"])
        self.assertTrue(report["python"]["ok"])
        self.assertTrue(report["agy"]["ok"])
        self.assertTrue(report["codex"]["ok"])
        self.assertTrue(report["node"]["ok"])
        self.assertTrue(report["chrome"]["ok"])
        self.assertTrue(report["models"]["ok"])
        self.assertFalse(report["models"]["skipped"])
        self.assertEqual(
            report["models"]["available_models"],
            ["gemini-4-argon-high", "gemini-4-argon-medium"],
        )
        # Ensure secret token from fake agy models output was redacted
        self.assertNotIn("secret_tok_999", res_online.stdout)
        self.assertIn("[REDACTED]", report["models"]["output"])

        # Verify agy models was actually called once
        logs = self._read_cli_logs()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["cli"], "agy")
        self.assertEqual(logs[0]["argv"], ["models"])

        # Offline check skips agy models probe
        self.log_file.unlink()
        res_offline = self._run_setup_cli(["check", "--offline", "--json"], env=env)
        self.assertEqual(res_offline.returncode, 0)
        rep_off = json.loads(res_offline.stdout)
        self.assertTrue(rep_off["ok"])
        self.assertTrue(rep_off["models"]["skipped"])
        self.assertEqual(self._read_cli_logs(), [])

    def test_check_prerequisite_failures_and_optional_chrome_node(self) -> None:
        # Remove node, npx, and chrome: default check should still succeed (optional)
        self.fake_node.unlink()
        self.fake_npx.unlink()
        self.fake_chrome.unlink()
        env = self._base_child_env(path_dirs=[self.bin_dir], system_bins="", chrome_paths="")

        res_no_chrome = self._run_setup_cli(["check", "--offline", "--json"], env=env)
        self.assertEqual(res_no_chrome.returncode, 0)
        rep = json.loads(res_no_chrome.stdout)
        self.assertTrue(rep["ok"])
        self.assertFalse(rep["node"]["ok"])
        self.assertFalse(rep["chrome"]["ok"])

        # With --with-chrome, missing node/npx/chrome must fail with nonzero exit
        res_req_chrome = self._run_setup_cli(
            ["check", "--offline", "--with-chrome", "--json"],
            env=env,
        )
        self.assertNotEqual(res_req_chrome.returncode, 0)
        rep_fail = json.loads(res_req_chrome.stdout)
        self.assertFalse(rep_fail["ok"])
        self.assertTrue(any("node/npx" in e for e in rep_fail["errors"]))
        self.assertTrue(any("chrome" in e for e in rep_fail["errors"]))

        # Missing codex fails default check
        self.fake_codex.unlink()
        res_no_codex = self._run_setup_cli(["check", "--offline", "--json"], env=env)
        self.assertNotEqual(res_no_codex.returncode, 0)
        rep_nc = json.loads(res_no_codex.stdout)
        self.assertFalse(rep_nc["ok"])
        self.assertFalse(rep_nc["codex"]["ok"])

    def test_windows_platform_and_old_python_rejected(self) -> None:
        with mock.patch.dict(os.environ, self._base_child_env(), clear=True):
            with mock.patch.object(sys, "platform", "win32"), mock.patch(
                "platform.system", return_value="Windows"
            ):
                plat = setup.check_platform()
                self.assertFalse(plat["ok"])
                self.assertIn("fcntl", plat["detail"])
                rep = setup.run_check(offline=True)
                self.assertFalse(rep["ok"])

            with mock.patch.object(sys, "version_info", (3, 10, 9)):
                py_rep = setup.check_python()
                self.assertFalse(py_rep["ok"])

    def test_dependency_discovery_prepends_dirs_and_preserves_caller_path(self) -> None:
        # Place agy in ~/.local/bin and node/npx in ~/.nvm/versions/node/v20.19.0/bin
        self.fake_agy.unlink()
        self.fake_node.unlink()
        self.fake_npx.unlink()

        local_bin = self.home_dir / ".local" / "bin"
        nvm_v18 = self.home_dir / ".nvm" / "versions" / "node" / "v18.19.0" / "bin"
        nvm_v20 = self.home_dir / ".nvm" / "versions" / "node" / "v20.19.0" / "bin"
        nvm_v9 = self.home_dir / ".nvm" / "versions" / "node" / "v9.11.2" / "bin"

        _write_exec(local_bin / "agy", FAKE_AGY_TEMPLATE)
        for nvm_dir in (nvm_v18, nvm_v20, nvm_v9):
            _write_exec(nvm_dir / "node", f"#!/bin/bash\necho {nvm_dir.parent.name}\n")
            _write_exec(nvm_dir / "npx", "#!/bin/bash\necho npx\n")

        caller_custom_dir = self.root / "caller_custom_path"
        caller_custom_dir.mkdir()
        env = self._base_child_env(
            path_dirs=[self.bin_dir, caller_custom_dir],
            system_bins="",
        )

        res = self._run_setup_cli(["check", "--with-chrome", "--json"], env=env)
        self.assertEqual(res.returncode, 0, res.stderr)
        report = json.loads(res.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(report["agy"]["executable"], str(local_bin / "agy"))
        # Semantic version sorting must choose v20.19.0 over v18.19.0 and v9.11.2
        self.assertEqual(report["node"]["executable"], str(nvm_v20 / "node"))
        self.assertEqual(report["npx"]["executable"], str(nvm_v20 / "npx"))

        # Verify child process received prepended discovered bin dirs and preserved caller PATH
        logs = self._read_cli_logs()
        self.assertEqual(len(logs), 1)
        child_path = logs[0]["path_env"]
        self.assertTrue(child_path.endswith(env["PATH"]))
        self.assertIn(str(local_bin), child_path.split(os.pathsep)[:4])
        self.assertIn(str(nvm_v20), child_path.split(os.pathsep)[:4])

    def test_install_from_directory_with_spaces_and_allowlist_exclusions(self) -> None:
        spaced_root = self.root / "Dir With Spaces"
        src_dir = self._create_source_copy(spaced_root / "Plugin Source Copy", include_license=True)
        spaced_home = spaced_root / "User Home"
        spaced_codex = spaced_home / "Codex Home"
        spaced_home.mkdir(parents=True)
        spaced_codex.mkdir(parents=True)

        # Add unknown, hidden, test, state, log, pyc, __pycache__, and secret files to source
        (src_dir / "unknown_root_file.txt").write_text("do not copy", encoding="utf-8")
        (src_dir / ".env").write_text("SECRET=123", encoding="utf-8")
        (src_dir / "tests").mkdir(exist_ok=True)
        (src_dir / "tests" / "test_extra.py").write_text("pass", encoding="utf-8")
        (src_dir / "state").mkdir(exist_ok=True)
        (src_dir / "state" / "job-1.json").write_text("{}", encoding="utf-8")
        (src_dir / "logs").mkdir(exist_ok=True)
        (src_dir / "logs" / "app.log").write_text("log", encoding="utf-8")
        (src_dir / "scripts" / "__pycache__").mkdir(exist_ok=True)
        (src_dir / "scripts" / "__pycache__" / "bridge.cpython-311.pyc").write_bytes(b"\x00\x01")
        (src_dir / "scripts" / "stale.pyc").write_bytes(b"\x00\x01")
        (src_dir / "scripts" / ".hidden_secret").write_text("hidden", encoding="utf-8")
        (src_dir / "scripts" / "my_secret_backup.txt").write_text("secret", encoding="utf-8")
        (src_dir / "scripts" / "settings.json").write_text("{}", encoding="utf-8")
        (src_dir / "skills" / ".DS_Store").write_text("junk", encoding="utf-8")

        # Pre-create an unrelated sibling plugin in marketplace to verify preservation
        mp_root = spaced_codex / "plugin-marketplaces" / "gemini-bridge"
        sibling_dir = mp_root / "plugins" / "unrelated-sibling"
        sibling_dir.mkdir(parents=True)
        (sibling_dir / "plugin.json").write_text('{"name": "unrelated-sibling"}\n', encoding="utf-8")
        mp_manifest = mp_root / ".agents" / "plugins" / "marketplace.json"
        mp_manifest.parent.mkdir(parents=True)
        mp_manifest.write_text(
            json.dumps(
                {
                    "name": "gemini-bridge-local",
                    "customField": "keep-me",
                    "plugins": [{"name": "unrelated-sibling", "source": "./plugins/unrelated-sibling"}],
                }
            ),
            encoding="utf-8",
        )

        env = self._base_child_env(home=spaced_home, codex_home=spaced_codex)
        res = self._run_setup_cli(
            ["install", "--source", str(src_dir), "--json"],
            env=env,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["installed"])

        dest_dir = mp_root / "plugins" / "antigravity-bridge"
        self.assertEqual(Path(payload["plugin_dir"]), dest_dir)
        self.assertTrue((dest_dir / "plugin.json").is_file())
        self.assertTrue((dest_dir / "mcp.json").is_file())
        self.assertTrue((dest_dir / "README.md").is_file())
        self.assertTrue((dest_dir / "LICENSE").is_file())
        self.assertTrue((dest_dir / "scripts" / "bridge.py").is_file())
        self.assertTrue((dest_dir / "scripts" / "setup.py").is_file())
        self.assertTrue((dest_dir / "scripts" / "launch.sh").is_file())
        self.assertTrue((dest_dir / "scripts" / "launch-chrome.sh").is_file())
        self.assertTrue((dest_dir / "skills" / "gemini-bridge" / "SKILL.md").is_file())
        self.assertTrue((dest_dir / "assets" / "icon.svg").is_file())

        # Verify excluded files were NOT copied
        self.assertFalse((dest_dir / "unknown_root_file.txt").exists())
        self.assertFalse((dest_dir / ".env").exists())
        self.assertFalse((dest_dir / "tests").exists())
        self.assertFalse((dest_dir / "state").exists())
        self.assertFalse((dest_dir / "logs").exists())
        self.assertFalse((dest_dir / "scripts" / "__pycache__").exists())
        self.assertFalse((dest_dir / "scripts" / "stale.pyc").exists())
        self.assertFalse((dest_dir / "scripts" / ".hidden_secret").exists())
        self.assertFalse((dest_dir / "scripts" / "my_secret_backup.txt").exists())
        self.assertFalse((dest_dir / "scripts" / "settings.json").exists())
        self.assertFalse((dest_dir / "skills" / ".DS_Store").exists())

        # Verify sibling plugin and marketplace manifest entries were preserved
        self.assertTrue((sibling_dir / "plugin.json").is_file())
        manifest_data = json.loads(mp_manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest_data["name"], "gemini-bridge-local")
        self.assertEqual(manifest_data["customField"], "keep-me")
        by_name = {p["name"]: p for p in manifest_data["plugins"]}
        self.assertIn("unrelated-sibling", by_name)
        self.assertIn("antigravity-bridge", by_name)
        self.assertEqual(by_name["antigravity-bridge"]["source"], "./plugins/antigravity-bridge")

        # Verify default install did NOT touch AGENTS.md, settings.json, config.json, or agy mcp add
        self.assertFalse((spaced_codex / "AGENTS.md").exists())
        self.assertFalse((spaced_home / ".gemini" / "antigravity-cli" / "settings.json").exists())
        self.assertFalse((spaced_home / ".gemini" / "config" / "config.json").exists())

        # Verify exact command arguments passed to codex
        logs = self._read_cli_logs()
        codex_calls = [c for c in logs if c["cli"] == "codex"]
        self.assertEqual(len(codex_calls), 2)
        self.assertEqual(
            codex_calls[0]["argv"],
            ["plugin", "marketplace", "add", str(mp_root)],
        )
        self.assertEqual(
            codex_calls[1]["argv"],
            ["plugin", "add", "antigravity-bridge@gemini-bridge-local", "--json"],
        )
        agy_calls = [c for c in logs if c["cli"] == "agy"]
        self.assertEqual([c["argv"] for c in agy_calls], [["models"]])

    def test_optional_flags_idempotence_and_preserving_unrelated_configs(self) -> None:
        env = self._base_child_env()

        # Pre-populate AGENTS.md with exact bytes (including CRLF and LF)
        agents_path = self.codex_home / "AGENTS.md"
        prefix_bytes = b"# Global Instructions\r\nKeep this exact CRLF line.\nAnd this LF line.\n"
        agents_path.write_bytes(prefix_bytes)

        # Pre-populate settings.json and config.json with unrelated keys
        settings_path = self.home_dir / ".gemini" / "antigravity-cli" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps(
                {
                    "theme": "solarized",
                    "useG1Credits": True,
                    "permissions": {
                        "allow": ["command(echo hello)"],
                        "deny": ["command(rm -rf /)"],
                    },
                }
            ),
            encoding="utf-8",
        )

        config_path = self.home_dir / ".gemini" / "config" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(
                {
                    "releaseChannel": "stable",
                    "userSettings": {
                        "telemetry": False,
                        "useAiCredits": True,
                    },
                }
            ),
            encoding="utf-8",
        )

        flags = [
            "install",
            "--with-chrome",
            "--auto-route",
            "--allow-development",
            "--disable-credit-overage",
            "--json",
        ]
        res1 = self._run_setup_cli(flags, env=env)
        self.assertEqual(res1.returncode, 0, res1.stderr)
        p1 = json.loads(res1.stdout)
        self.assertTrue(p1["ok"])
        self.assertTrue(p1["opt_in"]["chrome_mcp"]["changed"])
        self.assertTrue(p1["opt_in"]["auto_route"]["changed"])
        self.assertTrue(p1["opt_in"]["allow_development"]["changed"])
        self.assertTrue(p1["opt_in"]["disable_credit_overage"]["changed"])

        # Verify durable launch-chrome.sh path was used in agy mcp add
        expected_launcher = (
            self.codex_home
            / "plugin-marketplaces"
            / "gemini-bridge"
            / "plugins"
            / "antigravity-bridge"
            / "scripts"
            / "launch-chrome.sh"
        )
        logs1 = self._read_cli_logs()
        mcp_add_calls = [
            c for c in logs1 if c["cli"] == "agy" and c["argv"][:2] == ["mcp", "add"]
        ]
        self.assertEqual(len(mcp_add_calls), 1)
        self.assertEqual(
            mcp_add_calls[0]["argv"],
            ["mcp", "add", "chrome-devtools", str(expected_launcher)],
        )

        # Verify AGENTS.md preserved prefix_bytes byte-for-byte
        agents_after_1 = agents_path.read_bytes()
        self.assertTrue(agents_after_1.startswith(prefix_bytes))
        self.assertIn(b"gemini-bridge", agents_after_1)
        self.assertEqual(stat.S_IMODE(agents_path.stat().st_mode), 0o600)

        # Append suffix bytes after the marker block and run a second time to test in-place replacement
        suffix_bytes = b"\r\n# Footer Section\r\nMust remain untouched byte-for-byte."
        agents_with_suffix = agents_after_1 + suffix_bytes
        agents_path.write_bytes(agents_with_suffix)

        res2 = self._run_setup_cli(flags, env=env)
        self.assertEqual(res2.returncode, 0, res2.stderr)
        p2 = json.loads(res2.stdout)
        self.assertTrue(p2["ok"])
        # Existing chrome-devtools server is preserved without --replace-chrome
        self.assertTrue(p2["opt_in"]["chrome_mcp"]["preserved_existing"])
        self.assertFalse(p2["opt_in"]["chrome_mcp"]["changed"])
        self.assertFalse(p2["opt_in"]["allow_development"]["changed"])
        self.assertFalse(p2["opt_in"]["disable_credit_overage"]["changed"])

        agents_after_2 = agents_path.read_bytes()
        self.assertEqual(agents_after_2, agents_with_suffix)
        self.assertEqual(agents_after_2.count(setup.AUTO_ROUTE_START_MARKER.encode("utf-8")), 1)
        self.assertEqual(agents_after_2.count(setup.AUTO_ROUTE_END_MARKER.encode("utf-8")), 1)

        # Verify settings.json permissions and credit flags and 0600 mode
        self.assertEqual(stat.S_IMODE(settings_path.stat().st_mode), 0o600)
        settings_data = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(settings_data["theme"], "solarized")
        self.assertIs(settings_data["useG1Credits"], False)
        self.assertEqual(settings_data["permissions"]["deny"], ["command(rm -rf /)"])
        allow_list = settings_data["permissions"]["allow"]
        self.assertIn("command(echo hello)", allow_list)
        for req_perm in (
            "command(rg)",
            "command(ls)",
            "command(pwd)",
            "command(git status)",
            "command(git diff)",
            "command(npm run test)",
            "command(npm run build)",
            "command(npm run lint)",
            "command(python3 -m unittest)",
            "mcp(chrome-devtools/*)",
        ):
            self.assertIn(req_perm, allow_list)
            self.assertEqual(allow_list.count(req_perm), 1)
        # Ensure read_url/execute_url only allow localhost and 127.0.0.1 and no global wildcards exist
        for perm in allow_list:
            self.assertNotIn(perm, ("*", "command(*)", "mcp(*)", "read_url(*)", "execute_url(*)"))
            if perm.startswith(("read_url(", "execute_url(")):
                self.assertTrue("localhost" in perm or "127.0.0.1" in perm or perm == "read_url(registry.npmjs.org)", perm)

        # Verify config.json credit flag, unrelated keys, and 0600 mode
        self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
        config_data = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(config_data["releaseChannel"], "stable")
        self.assertIs(config_data["userSettings"]["telemetry"], False)
        self.assertIs(config_data["userSettings"]["useAiCredits"], False)

    def test_existing_chrome_conflict_and_replace_chrome(self) -> None:
        env = self._base_child_env()
        # Pre-populate existing chrome-devtools MCP server in fake agy state
        self.mcp_state_file.write_text("chrome-devtools: /custom/existing/chrome-mcp\n", encoding="utf-8")

        # 1. --with-chrome without --replace-chrome must NOT overwrite existing server
        res_keep = self._run_setup_cli(["install", "--with-chrome", "--json"], env=env)
        self.assertEqual(res_keep.returncode, 0, res_keep.stderr)
        data_keep = json.loads(res_keep.stdout)
        self.assertTrue(data_keep["opt_in"]["chrome_mcp"]["preserved_existing"])
        self.assertTrue(data_keep["opt_in"]["chrome_mcp"]["conflict"])
        self.assertFalse(data_keep["opt_in"]["chrome_mcp"]["replaced"])
        self.assertEqual(
            self.mcp_state_file.read_text(encoding="utf-8"),
            "chrome-devtools: /custom/existing/chrome-mcp\n",
        )

        # 2. --with-chrome --replace-chrome replaces the existing server with durable launch-chrome.sh
        res_replace = self._run_setup_cli(
            ["install", "--with-chrome", "--replace-chrome", "--json"],
            env=env,
        )
        self.assertEqual(res_replace.returncode, 0, res_replace.stderr)
        data_rep = json.loads(res_replace.stdout)
        self.assertTrue(data_rep["opt_in"]["chrome_mcp"]["replaced"])
        self.assertTrue(data_rep["opt_in"]["chrome_mcp"]["changed"])
        durable_launcher = (
            self.codex_home
            / "plugin-marketplaces"
            / "gemini-bridge"
            / "plugins"
            / "antigravity-bridge"
            / "scripts"
            / "launch-chrome.sh"
        )
        self.assertIn(str(durable_launcher), self.mcp_state_file.read_text(encoding="utf-8"))

        # 3. --replace-chrome without --with-chrome fails
        res_bad_flag = self._run_setup_cli(["install", "--replace-chrome", "--json"], env=env)
        self.assertNotEqual(res_bad_flag.returncode, 0)
        bad_data = json.loads(res_bad_flag.stdout)
        self.assertFalse(bad_data["installed"])
        self.assertEqual(bad_data["failed_step"], "validate_flags")

    def test_malformed_agents_md_markers_rejected_without_mutation(self) -> None:
        env = self._base_child_env()
        agents_path = self.codex_home / "AGENTS.md"

        malformed_cases = [
            b"# Header\n<!-- GEMINI-BRIDGE:AUTO-ROUTE:START -->\nno end marker\n",
            b"# Header\n<!-- GEMINI-BRIDGE:AUTO-ROUTE:END -->\nno start marker\n",
            (
                b"<!-- GEMINI-BRIDGE:AUTO-ROUTE:END -->\n"
                b"<!-- GEMINI-BRIDGE:AUTO-ROUTE:START -->\n"
            ),
            (
                b"<!-- GEMINI-BRIDGE:AUTO-ROUTE:START -->\nA\n<!-- GEMINI-BRIDGE:AUTO-ROUTE:END -->\n"
                b"<!-- GEMINI-BRIDGE:AUTO-ROUTE:START -->\nB\n<!-- GEMINI-BRIDGE:AUTO-ROUTE:END -->\n"
            ),
            b"# Header\n<!-- GEMINI-BRIDGE:AUTO-ROUTE:BROKEN -->\n",
        ]

        for raw in malformed_cases:
            agents_path.write_bytes(raw)
            res = self._run_setup_cli(["install", "--auto-route", "--json"], env=env)
            self.assertNotEqual(res.returncode, 0)
            err_data = json.loads(res.stdout)
            self.assertFalse(err_data["ok"])
            self.assertFalse(err_data["installed"])
            self.assertEqual(err_data["failed_step"], "configure_auto_route")
            # File must remain untouched byte-for-byte
            self.assertEqual(agents_path.read_bytes(), raw)

    def test_source_equals_destination_and_step_failure_reporting(self) -> None:
        env = self._base_child_env()
        # First install normally
        res1 = self._run_setup_cli(["install", "--json"], env=env)
        self.assertEqual(res1.returncode, 0, res1.stderr)
        dest_dir = Path(json.loads(res1.stdout)["plugin_dir"])

        # Now run install with --source == destination
        res_same = self._run_setup_cli(
            ["install", "--source", str(dest_dir), "--json"],
            env=env,
        )
        self.assertEqual(res_same.returncode, 0, res_same.stderr)
        same_data = json.loads(res_same.stdout)
        self.assertTrue(same_data["ok"])
        self.assertTrue(same_data["installed"])
        self.assertTrue(same_data["copy"]["skipped_same_path"])
        self.assertTrue((dest_dir / "plugin.json").is_file())
        self.assertTrue((dest_dir / "scripts" / "bridge.py").is_file())

        # Test codex marketplace add failure reports exact step and does not claim installation
        env_mp_fail = self._base_child_env()
        env_mp_fail["FAKE_CODEX_MP_FAIL"] = "1"
        res_mp_fail = self._run_setup_cli(["install", "--json"], env=env_mp_fail)
        self.assertNotEqual(res_mp_fail.returncode, 0)
        mp_err = json.loads(res_mp_fail.stdout)
        self.assertFalse(mp_err["ok"])
        self.assertFalse(mp_err["installed"])
        self.assertEqual(mp_err["failed_step"], "codex_marketplace_add")
        self.assertNotIn("hidden_mp_secret", res_mp_fail.stdout)

        # Test codex plugin add failure reports exact step
        env_add_fail = self._base_child_env()
        env_add_fail["FAKE_CODEX_PLUGIN_FAIL"] = "1"
        res_add_fail = self._run_setup_cli(["install", "--json"], env=env_add_fail)
        self.assertNotEqual(res_add_fail.returncode, 0)
        add_err = json.loads(res_add_fail.stdout)
        self.assertFalse(add_err["ok"])
        self.assertFalse(add_err["installed"])
        self.assertEqual(add_err["failed_step"], "codex_plugin_add")

    def test_launch_sh_resolves_python311_preserves_cwd_and_defaults_to_serve(self) -> None:
        # Create a fake old python3 (3.9) first in PATH, and a real >=3.11 python3 in a fallback bin dir
        old_py_dir = self.root / "old_py"
        new_py_dir = self.root / "new_py"
        old_py_dir.mkdir()
        new_py_dir.mkdir()
        _write_exec(old_py_dir / "python3", "#!/bin/bash\nexit 1\n")
        os.symlink(sys.executable, new_py_dir / "python3")

        caller_cwd = self.root / "caller_working_dir"
        caller_cwd.mkdir()

        env = self._base_child_env(
            path_dirs=[old_py_dir, self.bin_dir],
            system_bins=str(new_py_dir),
        )
        # Remove python3 from self.bin_dir so launch.sh must skip old_py_dir and find new_py_dir
        self.python_link.unlink()

        # Test default serve argument via MCP ping request from caller_cwd
        ping_req = json.dumps({"jsonrpc": "2.0", "id": 99, "method": "ping"}) + "\n"
        proc = subprocess.run(
            [str(LAUNCH_SCRIPT)],
            input=ping_req,
            cwd=str(caller_cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=True,
        )
        resp = json.loads(proc.stdout.strip())
        self.assertEqual(resp, {"jsonrpc": "2.0", "id": 99, "result": {}})
        self.assertEqual(proc.stderr, "")

        # When no Python >= 3.11 is available anywhere, launch.sh exits nonzero with stderr diagnostic
        env_no_py = self._base_child_env(path_dirs=[old_py_dir], system_bins="")
        proc_fail = subprocess.run(
            [str(LAUNCH_SCRIPT)],
            cwd=str(caller_cwd),
            env=env_no_py,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertNotEqual(proc_fail.returncode, 0)
        self.assertEqual(proc_fail.stdout, "")
        self.assertIn("Python >= 3.11", proc_fail.stderr)

    def test_launch_chrome_sh_nvm_semver_ordering_and_missing_error(self) -> None:
        self.fake_node.unlink()
        self.fake_npx.unlink()

        # Create multiple nvm versions: v9.9.9, v18.20.0, v20.9.0, v20.19.1
        for ver in ("v9.9.9", "v18.20.0", "v20.9.0", "v20.19.1"):
            vbin = self.home_dir / ".nvm" / "versions" / "node" / ver / "bin"
            _write_exec(vbin / "node", f"#!/bin/bash\necho '{ver}'\n")
            _write_exec(
                vbin / "npx",
                textwrap.dedent(
                    f"""\
                    #!/bin/bash
                    echo '{{"selected":"{ver}","args":"'"$*"'","node_which":"'"$(command -v node)"'","cwd":"'"$(pwd)"'"}}'
                    """
                ),
            )

        caller_cwd = self.root / "chrome_caller_cwd"
        caller_cwd.mkdir()
        env = self._base_child_env(path_dirs=[self.bin_dir], system_bins="")

        proc = subprocess.run(
            [str(LAUNCH_CHROME_SCRIPT)],
            cwd=str(caller_cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=True,
        )
        out = json.loads(proc.stdout.strip())
        self.assertEqual(out["selected"], "v20.19.1")
        self.assertEqual(
            out["args"],
            "-y chrome-devtools-mcp@1.12.0 --isolated --headless --categoryExtensions --no-usage-statistics --no-performance-crux",
        )
        self.assertTrue(out["node_which"].endswith("/v20.19.1/bin/node"))
        self.assertEqual(Path(out["cwd"]).resolve(), caller_cwd.resolve())

        # Remove ~/.nvm to verify clear helpful error when node/npx is missing
        shutil.rmtree(self.home_dir / ".nvm")
        proc_missing = subprocess.run(
            [str(LAUNCH_CHROME_SCRIPT)],
            cwd=str(caller_cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertNotEqual(proc_missing.returncode, 0)
        self.assertEqual(proc_missing.stdout, "")
        self.assertIn("chrome-devtools-mcp@1.12.0", proc_missing.stderr)

    def test_patched_dependency_functions_and_launcher_no_recursion(self) -> None:
        # Test patching dependency functions directly in setup module
        with (
            mock.patch.object(setup, "get_home_dir", return_value=self.home_dir),
            mock.patch.object(setup, "get_codex_home", return_value=self.codex_home),
            mock.patch.object(
                setup,
                "check_agy",
                return_value={"ok": False, "executable": None, "detail": "agy missing"},
            ),
        ):
            rep = setup.run_check(offline=True)
            self.assertFalse(rep["ok"])
            self.assertTrue(any("agy" in e for e in rep["errors"]))
            with self.assertRaises(setup.SetupError) as ctx:
                setup.run_install(offline=True)
            self.assertEqual(ctx.exception.step, "prerequisites")

        # Verify launch-chrome.sh does not recurse if a symlink named npx points to launch-chrome.sh
        self.fake_node.unlink()
        self.fake_npx.unlink()
        loop_dir = self.root / "loop_bin"
        loop_dir.mkdir()
        _write_exec(loop_dir / "node", "#!/bin/bash\necho v20.0.0\n")
        os.symlink(LAUNCH_CHROME_SCRIPT, loop_dir / "npx")
        env = self._base_child_env(path_dirs=[loop_dir], system_bins="")
        proc = subprocess.run(
            [str(LAUNCH_CHROME_SCRIPT)],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("chrome-devtools-mcp@1.12.0", proc.stderr)


def tearDownModule() -> None:
    for d in (SCRIPTS_DIR / "__pycache__", REPO_ROOT / "tests" / "__pycache__"):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
