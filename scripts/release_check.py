#!/usr/bin/env python3
"""Release readiness and portable package verification for Gemini Bridge."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = ["agy_start", "agy_status", "agy_result", "agy_cancel", "agy_doctor"]
EXEC_SCRIPTS = ("scripts/launch.sh", "scripts/launch-chrome.sh")
REQUIRED_ARCHIVE_FILES = {
    "plugin.json",
    "mcp.json",
    "README.md",
    "LICENSE",
    "scripts/bridge.py",
    "scripts/setup.py",
    "scripts/package.py",
    "scripts/launch.sh",
    "scripts/launch-chrome.sh",
    "skills/gemini-bridge/SKILL.md",
    "assets/icon.svg",
}
FORBIDDEN_PARTS = frozenset({
    ".git",
    ".agents",
    ".codex-plugin",
    "__pycache__",
    "logs",
    "state",
    "cache",
    ".cache",
    "dist",
    ".DS_Store",
})
FORBIDDEN_SUFFIXES = (".pyc", ".pyo", ".log", ".pem", ".key", ".env", ".bak")
FORBIDDEN_NAME_RE = re.compile(
    r"(?i)(?:^|[._-])(secret|token|credential|password|settings\.json|config\.json)(?:$|[._-])"
)
LEAK_BYTES_RE = re.compile(
    rb"/" + rb"Users/[^/\s]+/|/" + rb"home/[^/\s]+/|job-" + rb"[0-9a-f]{16}|[A-Za-z0-9._%+-]+@gmail\.com"
)
TERMINAL_STATUSES = frozenset({"succeeded", "needs_review", "failed", "timed_out", "cancelled", "interrupted"})
SUBPROCESS_TIMEOUT = 30.0


class ReleaseCheckError(Exception):
    """Raised when a release verification check fails."""


def validate_mcp_config(root: Path) -> tuple[str, list[str]]:
    mcp_path = root / "mcp.json"
    if not mcp_path.is_file():
        raise ReleaseCheckError("Missing mcp.json")
    try:
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseCheckError(f"Invalid mcp.json: {exc}") from exc
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    srv = servers.get("antigravity") if isinstance(servers, dict) else None
    if not isinstance(srv, dict) or srv.get("type") != "stdio":
        raise ReleaseCheckError("mcp.json must define stdio server 'antigravity'")
    cmd = srv.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        raise ReleaseCheckError("mcp.json command must be a non-empty string")
    if os.path.isabs(cmd) or cmd.startswith(("/", "\\", "~")) or "\\" in cmd:
        raise ReleaseCheckError(f"MCP command must be a relative portable path: {cmd}")
    posix_parts = PurePosixPath(cmd).parts
    if not posix_parts or ".." in posix_parts or any(":" in p or p.startswith("~") for p in posix_parts):
        raise ReleaseCheckError(f"MCP command contains path traversal or unportable segment: {cmd}")
    root_real = root.resolve()
    candidate = root / cmd
    if candidate.is_symlink():
        raise ReleaseCheckError(f"MCP command target must not be a symlink: {cmd}")
    target_real = candidate.resolve()
    if not target_real.is_relative_to(root_real) or not target_real.is_file():
        raise ReleaseCheckError(f"MCP command resolves outside package or is missing: {cmd}")
    if not (target_real.stat().st_mode & 0o111):
        raise ReleaseCheckError(f"MCP command is not executable: {cmd}")
    args = srv.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ReleaseCheckError("mcp.json args must be a list of strings")
    return cmd, args


def validate_repo_metadata(root: Path = ROOT) -> str:
    plugin_path = root / "plugin.json"
    if not plugin_path.is_file():
        raise ReleaseCheckError("Missing plugin.json")
    plugin = json.loads(plugin_path.read_text(encoding="utf-8"))
    version = plugin.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ReleaseCheckError("plugin.json missing valid version")
    if plugin.get("name") != "antigravity-bridge":
        raise ReleaseCheckError(f"Unexpected plugin name: {plugin.get('name')}")
    if plugin.get("license") != "MIT":
        raise ReleaseCheckError(f"plugin.json license must be MIT, got {plugin.get('license')}")

    bridge_path = root / "scripts" / "bridge.py"
    if not bridge_path.is_file():
        raise ReleaseCheckError("Missing scripts/bridge.py")
    m = re.search(r'^SERVER_VERSION\s*=\s*"([^"]+)"', bridge_path.read_text(encoding="utf-8"), re.M)
    bridge_version = m.group(1) if m else None
    if bridge_version != version:
        raise ReleaseCheckError(f"Version mismatch: plugin.json={version} vs bridge.py={bridge_version}")

    license_path = root / "LICENSE"
    if not license_path.is_file() or "MIT License" not in license_path.read_text(encoding="utf-8"):
        raise ReleaseCheckError("MIT LICENSE file missing or invalid")

    skill_path = root / "skills" / "gemini-bridge" / "SKILL.md"
    if not skill_path.is_file() or "name: gemini-bridge" not in skill_path.read_text(encoding="utf-8"):
        raise ReleaseCheckError("skills/gemini-bridge/SKILL.md missing or invalid")

    for rel in EXEC_SCRIPTS:
        script_path = root / rel
        if not script_path.is_file() or script_path.is_symlink():
            raise ReleaseCheckError(f"Required script missing or symlink: {rel}")
        if not (script_path.stat().st_mode & 0o111):
            raise ReleaseCheckError(f"Executable permission bit missing on {rel}")

    validate_mcp_config(root)
    return version


def validate_archive(archive_path: Path, sha256_path: Path | None = None) -> dict[str, Any]:
    if not archive_path.is_file():
        raise ReleaseCheckError(f"Archive not found: {archive_path}")
    companion = sha256_path if sha256_path is not None else archive_path.with_suffix(".sha256")
    if not companion.is_file():
        raise ReleaseCheckError(f"SHA256 companion file missing: {companion}")
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    expected_companion = f"{digest}  {archive_path.name}"
    if companion.read_text(encoding="utf-8").strip() != expected_companion:
        raise ReleaseCheckError("SHA256 companion digest or filename mismatch")

    names: set[str] = set()
    modes: dict[str, int] = {}
    with zipfile.ZipFile(archive_path, "r") as zf:
        for info in zf.infolist():
            name = info.filename
            p = PurePosixPath(name)
            if not name or name.startswith(("/", "\\")) or "\\" in name or p.is_absolute() or ".." in p.parts:
                raise ReleaseCheckError(f"Unsafe or traversal path in archive: {name}")
            if any(":" in part for part in p.parts):
                raise ReleaseCheckError(f"Drive or colon segment in archive path: {name}")
            hi_mode = info.external_attr >> 16
            if stat.S_ISLNK(hi_mode):
                raise ReleaseCheckError(f"Symlink entry forbidden in archive: {name}")
            for part in p.parts:
                if part in FORBIDDEN_PARTS or (part.startswith(".") and name != ".gitignore"):
                    raise ReleaseCheckError(f"Forbidden path component in archive: {name}")
            if p.name.lower().endswith(FORBIDDEN_SUFFIXES) or FORBIDDEN_NAME_RE.search(p.name):
                raise ReleaseCheckError(f"Forbidden secret, cache, or log file in archive: {name}")
            raw = zf.read(name)
            if LEAK_BYTES_RE.search(raw):
                raise ReleaseCheckError(f"Embedded user path or task/account data in archive entry: {name}")
            names.add(name)
            modes[name] = hi_mode

    missing = sorted(REQUIRED_ARCHIVE_FILES - names)
    if missing:
        raise ReleaseCheckError(f"Archive missing required files: {', '.join(missing)}")
    for rel in EXEC_SCRIPTS:
        if not (modes.get(rel, 0) & 0o111):
            raise ReleaseCheckError(f"Archive entry {rel} is missing executable permission bits")
    return {"archive": archive_path.name, "sha256": digest, "files": len(names)}


def _extract_archive(archive_path: Path, dest_dir: Path) -> None:
    dest_real = dest_dir.resolve()
    with zipfile.ZipFile(archive_path, "r") as zf:
        for info in zf.infolist():
            target = (dest_real / info.filename).resolve()
            if not target.is_relative_to(dest_real):
                raise ReleaseCheckError(f"Archive extraction escaped destination: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info.filename))
            mode = (info.external_attr >> 16) & 0o777
            os.chmod(target, mode if mode else (0o755 if info.filename in EXEC_SCRIPTS else 0o644))


def _write_fake_agy(bin_dir: Path) -> Path:
    agy_path = bin_dir / "agy"
    script = (
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "argv = sys.argv[1:]\n"
        "if argv == ['--version']:\n"
        "    print('agy-fake 2.4.0'); raise SystemExit(0)\n"
        "if argv == ['models']:\n"
        "    print('gemini-4-argon-high\\ngemini-4-argon-medium'); raise SystemExit(0)\n"
        "if argv == ['mcp', 'list']:\n"
        "    print('No MCP servers'); raise SystemExit(0)\n"
        "model = argv[argv.index('--model') + 1] if '--model' in argv else ''\n"
        "if model != 'gemini-4-argon-medium':\n"
        "    sys.stderr.write(f'unexpected model: {model}\\n'); raise SystemExit(2)\n"
        "print(json.dumps({'event': 'step_update', 'step_update': {'state': 'DONE', 'tool_name': 'read_file', 'step_type': 'tool'}}), flush=True)\n"
        "print(json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'response': 'Release check medium task ok', 'denied_actions': [], 'usage': {'input_tokens': 10, 'output_tokens': 5}}}), flush=True)\n"
    )
    agy_path.write_text(script, encoding="utf-8")
    agy_path.chmod(0o755)
    return agy_path


def verify_packaged_runtime(extracted_dir: Path, temp_root: Path, expected_version: str) -> dict[str, Any]:
    cmd_rel, cmd_args = validate_mcp_config(extracted_dir)
    fake_bin = temp_root / "fake_bin"
    state_dir = temp_root / "state"
    workspace = temp_root / "workspace with spaces"
    fake_bin.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    _write_fake_agy(fake_bin)

    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["AGY_BRIDGE_STATE"] = str(state_dir)

    launcher = extracted_dir / cmd_rel
    rpc_input = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
    ]) + "\n"
    mcp_proc = subprocess.run(
        [str(launcher), *cmd_args],
        input=rpc_input,
        cwd=str(extracted_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
        check=False,
    )
    if mcp_proc.returncode != 0:
        raise ReleaseCheckError(f"Packaged MCP launcher failed: {mcp_proc.stderr.strip()}")
    responses = [json.loads(line) for line in mcp_proc.stdout.strip().splitlines() if line.strip()]
    if len(responses) != 2:
        raise ReleaseCheckError(f"Expected 2 MCP responses, got {len(responses)}")
    srv_info = responses[0].get("result", {}).get("serverInfo", {})
    if srv_info.get("name") != "antigravity-bridge" or srv_info.get("version") != expected_version:
        raise ReleaseCheckError(f"Unexpected MCP serverInfo: {srv_info}")
    tools = [t.get("name") for t in responses[1].get("result", {}).get("tools", [])]
    if tools != EXPECTED_TOOLS:
        raise ReleaseCheckError(f"Expected tools {EXPECTED_TOOLS}, got {tools}")

    task_file = temp_root / "task.txt"
    task_file.write_text("Run bounded release smoke task.", encoding="utf-8")
    bridge_py = extracted_dir / "scripts" / "bridge.py"

    def _cli(*args: str) -> tuple[int, dict[str, Any]]:
        p = subprocess.run(
            [sys.executable, str(bridge_py), *args],
            cwd=str(extracted_dir),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
            check=False,
        )
        return p.returncode, json.loads(p.stdout) if p.stdout.strip() else {"error": p.stderr.strip()}

    code, started = _cli("start", "--workspace", str(workspace), "--task-file", str(task_file), "--profile", "medium", "--timeout", "30")
    if code != 0 or "job_id" not in started:
        raise ReleaseCheckError(f"CLI start failed: {started}")
    job_id = str(started["job_id"])
    status = str(started.get("status", "running"))
    try:
        deadline = time.monotonic() + 8.0
        while status not in TERMINAL_STATUSES and time.monotonic() < deadline:
            time.sleep(0.04)
            _, st_payload = _cli("status", job_id)
            status = str(st_payload.get("status", ""))
        if status not in TERMINAL_STATUSES:
            raise ReleaseCheckError(f"Smoke task did not finish in time (status={status})")
        res_code, res = _cli("result", job_id)
        if res_code != 0:
            raise ReleaseCheckError(f"CLI result failed: {res}")
        if (
            res.get("status") != "succeeded"
            or res.get("model") != "gemini-4-argon-medium"
            or res.get("profile") != "medium"
            or res.get("response") != "Release check medium task ok"
            or res.get("permission_denials") != []
            or res.get("verified") is not False
        ):
            raise ReleaseCheckError(f"Unexpected smoke task result: {res}")
    finally:
        if status not in TERMINAL_STATUSES:
            _cli("cancel", job_id)
    return {"tools": tools, "job_status": res["status"], "model": res["model"]}


def run_release_check(root: Path = ROOT) -> dict[str, Any]:
    version = validate_repo_metadata(root)
    with tempfile.TemporaryDirectory(prefix="gemini-bridge-release-") as tmp:
        tmp_root = Path(tmp).resolve()
        build_out = tmp_root / "build_out"
        pkg_proc = subprocess.run(
            [sys.executable, str(root / "scripts" / "package.py"), "--output", str(build_out)],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
            check=False,
        )
        if pkg_proc.returncode != 0:
            raise ReleaseCheckError(f"package.py failed: {pkg_proc.stderr.strip() or pkg_proc.stdout.strip()}")
        pkg_info = json.loads(pkg_proc.stdout)
        archive_path = Path(pkg_info["archive"])
        archive_meta = validate_archive(archive_path)
        extracted_dir = tmp_root / "Extracted Package With Spaces"
        _extract_archive(archive_path, extracted_dir)
        runtime_meta = verify_packaged_runtime(extracted_dir, tmp_root, version)
    return {
        "ok": True,
        "version": version,
        "archive": archive_meta["archive"],
        "sha256": archive_meta["sha256"],
        "files": archive_meta["files"],
        "tools": runtime_meta["tools"],
        "smoke_job": {"status": runtime_meta["job_status"], "model": runtime_meta["model"]},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify Gemini Bridge release package and runtime.")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    parser.parse_args(argv)
    try:
        report = run_release_check(ROOT)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
