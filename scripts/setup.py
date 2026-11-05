#!/usr/bin/env python3
"""Portable distribution check and installer for the Antigravity Bridge Codex plugin."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform as py_platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Callable, Sequence

MARKETPLACE_DIR_NAME = "gemini-bridge"
MARKETPLACE_NAME = "gemini-bridge-local"
PLUGIN_NAME = "antigravity-bridge"
PLUGIN_SOURCE_REL = "./plugins/antigravity-bridge"
PLUGIN_SPEC = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"

REQUIRED_MODELS = ("gemini-4-argon-high", "gemini-4-argon-medium")
MODEL_PROBE_TIMEOUT_SECONDS = 20.0
DEFAULT_CMD_TIMEOUT_SECONDS = 30.0
MAX_CAPTURED_OUTPUT_CHARS = 4000

ALLOWED_ROOT_FILES = ("plugin.json", "mcp.json", "README.md", "README.tr.md", "LICENSE")
REQUIRED_ROOT_FILES = ("plugin.json", "mcp.json", "README.md")
ALLOWED_ROOT_DIRS = ("scripts", "skills", "assets")

EXCLUDED_NAMES = frozenset({
    "__pycache__",
    "logs",
    "state",
    "tests",
    "test",
    ".git",
    ".agents",
    ".codex-plugin",
    ".DS_Store",
})
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".log", ".pem", ".key", ".env", ".bak")
EXCLUDED_SECRET_PATTERNS = re.compile(
    r"(?i)(?:^|[._-])(secret|token|credential|password|settings\.json|config\.json)(?:$|[._-])"
)

STRIPPED_ENV_KEYS = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
)

SECRET_REDACT_RE = re.compile(
    r"(?i)((?:api[_-]?key|secret|token|password|credential|authorization)"
    r"\s*[:=]\s*)(\S+)"
)

AUTO_ROUTE_START_MARKER = "<!-- GEMINI-BRIDGE:AUTO-ROUTE:START -->"
AUTO_ROUTE_END_MARKER = "<!-- GEMINI-BRIDGE:AUTO-ROUTE:END -->"
AUTO_ROUTE_MARKER_TOKEN = "GEMINI-BRIDGE:AUTO-ROUTE"

AUTO_ROUTE_BLOCK = (
    f"{AUTO_ROUTE_START_MARKER}\n"
    "## Gemini Bridge Auto-Routing\n\n"
    "Automatically use the `gemini-bridge` (`$gemini-bridge`) skill to delegate routine "
    "implementation, codebase exploration, research collection, summarization, build/test/lint "
    "execution, and Chrome DevTools verification to local Antigravity (`agy` with Gemini 4).\n"
    "Codex retains general planning, architecture, UI shell and interaction design, icons/logos, "
    "important image generation, deep reasoning, and final review/decisions.\n"
    f"{AUTO_ROUTE_END_MARKER}"
)

DEVELOPMENT_ALLOW_PERMISSIONS: tuple[str, ...] = (
    "command(rg)",
    "command(grep)",
    "command(node --test)",
    "command(npm run dev)",
    "command(npm run preview)",
    "command(npx -y modern-web-guidance@latest search)",
    "command(npx -y modern-web-guidance@latest retrieve)",
    "command(npx -y modern-web-guidance@latest list)",
    "command(pnpx modern-web-guidance@latest search)",
    "command(pnpx modern-web-guidance@latest retrieve)",
    "command(pnpx modern-web-guidance@latest list)",
    "read_url(registry.npmjs.org)",
    "command(ls)",
    "command(pwd)",
    "command(git status)",
    "command(git diff)",
    "command(npm run test)",
    "command(npm run build)",
    "command(npm run lint)",
    "command(python3 -m unittest)",
    "mcp(chrome-devtools/*)",
    "read_url(localhost)",
    "read_url(127.0.0.1)",
    "execute_url(localhost)",
    "execute_url(127.0.0.1)",
)

NVM_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


class SetupError(Exception):
    """Raised when a setup check or installation step fails."""

    def __init__(self, message: str, step: str = "setup") -> None:
        super().__init__(message)
        self.step = step
        self.message = message


def redact_secrets(text: str) -> str:
    return SECRET_REDACT_RE.sub(r"\1[REDACTED]", text)


def bound_output(text: str, limit: int = MAX_CAPTURED_OUTPUT_CHARS) -> str:
    cleaned = redact_secrets(text.strip())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "...[truncated]"


def get_source_root() -> Path:
    return Path(__file__).resolve().parent.parent


def get_home_dir() -> Path:
    env_home = os.environ.get("HOME", "").strip()
    if env_home:
        return Path(os.path.abspath(os.path.expanduser(env_home)))
    return Path(os.path.abspath(str(Path.home())))


def get_codex_home() -> Path:
    env_codex = os.environ.get("CODEX_HOME", "").strip()
    if env_codex:
        return Path(os.path.abspath(os.path.expanduser(env_codex)))
    return get_home_dir() / ".codex"


def get_marketplace_root(codex_home: Path | None = None) -> Path:
    base = codex_home if codex_home is not None else get_codex_home()
    return base / "plugin-marketplaces" / MARKETPLACE_DIR_NAME


def get_durable_plugin_dir(codex_home: Path | None = None) -> Path:
    return get_marketplace_root(codex_home) / "plugins" / PLUGIN_NAME


def get_marketplace_manifest_path(codex_home: Path | None = None) -> Path:
    return get_marketplace_root(codex_home) / ".agents" / "plugins" / "marketplace.json"


def get_codex_agents_path(codex_home: Path | None = None) -> Path:
    base = codex_home if codex_home is not None else get_codex_home()
    return base / "AGENTS.md"


def get_agy_settings_path(home: Path | None = None) -> Path:
    base = home if home is not None else get_home_dir()
    return base / ".gemini" / "antigravity-cli" / "settings.json"


def get_gemini_config_path(home: Path | None = None) -> Path:
    base = home if home is not None else get_home_dir()
    return base / ".gemini" / "config" / "config.json"


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".tmp-{path.name}-")
    try:
        os.fchmod(fd, mode)
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp_name, str(path))
        os.chmod(path, mode)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def atomic_write_json(path: Path, payload: dict[str, Any], mode: int = 0o600) -> None:
    raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    atomic_write_bytes(path, raw, mode=mode)


def load_json_dict(path: Path, step: str) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise SetupError(f"Failed to parse JSON object from {path}: {exc}", step=step) from exc
    if not isinstance(data, dict):
        raise SetupError(f"Expected JSON object in {path}, got {type(data).__name__}", step=step)
    return data


def parse_nvm_semver(version_name: str) -> tuple[int, int, int] | None:
    m = NVM_SEMVER_RE.match(version_name.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def discover_nvm_bin_dirs(home: Path | None = None) -> list[Path]:
    base_home = home if home is not None else get_home_dir()
    nvm_nodes_dir = base_home / ".nvm" / "versions" / "node"
    if not nvm_nodes_dir.is_dir():
        return []
    versioned: list[tuple[tuple[int, int, int], str, Path]] = []
    try:
        entries = list(nvm_nodes_dir.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.startswith("v"):
            continue
        bin_dir = entry / "bin"
        if not bin_dir.is_dir():
            continue
        parsed = parse_nvm_semver(entry.name)
        if parsed is None:
            continue
        versioned.append((parsed, entry.name, bin_dir))
    versioned.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in versioned]


def get_system_bin_dirs() -> list[Path]:
    raw = os.environ.get("AGY_SETUP_SYSTEM_BINS")
    if raw is None:
        raw = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    return [Path(p) for p in raw.split(os.pathsep) if p.strip()]


def get_candidate_bin_dirs(include_nvm: bool = True, home: Path | None = None) -> list[Path]:
    base_home = home if home is not None else get_home_dir()
    dirs: list[Path] = []
    seen: set[str] = set()

    def _add(p: Path) -> None:
        key = os.path.abspath(str(p))
        if key not in seen:
            seen.add(key)
            dirs.append(Path(key))

    for part in os.environ.get("PATH", "").split(os.pathsep):
        if part.strip():
            _add(Path(part.strip()))

    local_bin = base_home / ".local" / "bin"
    if local_bin.is_dir():
        _add(local_bin)

    if include_nvm:
        for extra_rel in (
            Path(".nvm/current/bin"),
            Path(".volta/bin"),
            Path(".fnm/current/bin"),
        ):
            extra_dir = base_home / extra_rel
            if extra_dir.is_dir():
                _add(extra_dir)
        for nvm_bin in discover_nvm_bin_dirs(base_home):
            _add(nvm_bin)

    for sys_dir in get_system_bin_dirs():
        if sys_dir.is_dir():
            _add(sys_dir)

    return dirs


def _is_executable_file(path: Path) -> bool:
    try:
        return path.is_file() and os.access(str(path), os.X_OK)
    except OSError:
        return False


def find_executable_in_dirs(
    names: Sequence[str],
    dirs: Sequence[Path],
    validator: Callable[[Path], bool] | None = None,
) -> Path | None:
    for directory in dirs:
        for name in names:
            candidate = directory / name
            if not _is_executable_file(candidate):
                continue
            if validator is not None and not validator(candidate):
                continue
            return Path(os.path.abspath(str(candidate)))
    return None


def check_platform() -> dict[str, Any]:
    plat = sys.platform
    system_name = py_platform.system()
    if plat == "darwin" or plat.startswith("linux"):
        return {
            "ok": True,
            "platform": plat,
            "system": system_name,
            "supported": True,
            "detail": f"Supported platform ({system_name} / {plat})",
        }
    if plat in ("win32", "cygwin", "msys") or system_name.lower().startswith("win"):
        return {
            "ok": False,
            "platform": plat,
            "system": system_name,
            "supported": False,
            "detail": "Windows is unsupported because antigravity-bridge requires POSIX fcntl locks.",
        }
    return {
        "ok": False,
        "platform": plat,
        "system": system_name,
        "supported": False,
        "detail": f"Unsupported platform: {system_name} ({plat}). Only macOS and Linux are supported.",
    }


def _probe_python_version(executable: Path) -> tuple[bool, str | None]:
    try:
        proc = subprocess.run(
            [
                str(executable),
                "-c",
                "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}'); "
                "raise SystemExit(0 if sys.version_info >= (3, 11) else 1)",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5.0,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, None
    ver = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else None
    return proc.returncode == 0, ver


def check_python() -> dict[str, Any]:
    current_ver = f"{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}"
    if sys.version_info < (3, 11):
        return {
            "ok": False,
            "executable": sys.executable,
            "version": current_ver,
            "detail": f"Python >= 3.11 required, running {current_ver}",
        }

    dirs = get_candidate_bin_dirs(include_nvm=False)
    discovered_ver: str | None = None

    def _valid_py(cand: Path) -> bool:
        nonlocal discovered_ver
        ok, ver = _probe_python_version(cand)
        if ok:
            discovered_ver = ver
        return ok

    found = find_executable_in_dirs(
        ("python3", "python3.14", "python3.13", "python3.12", "python3.11", "python"),
        dirs,
        validator=_valid_py,
    )
    if found is not None:
        return {
            "ok": True,
            "executable": str(found),
            "version": discovered_ver or current_ver,
            "detail": f"Python {discovered_ver or current_ver}",
        }

    if sys.executable and _is_executable_file(Path(sys.executable)):
        return {
            "ok": True,
            "executable": os.path.abspath(sys.executable),
            "version": current_ver,
            "detail": f"Python {current_ver}",
        }

    return {
        "ok": False,
        "executable": None,
        "version": current_ver,
        "detail": "Could not locate a Python >= 3.11 executable",
    }


def check_agy() -> dict[str, Any]:
    found = find_executable_in_dirs(("agy",), get_candidate_bin_dirs(include_nvm=False))
    return {
        "ok": found is not None,
        "executable": str(found) if found else None,
        "detail": str(found) if found else "Executable 'agy' not found in PATH or standard locations",
    }


def check_codex() -> dict[str, Any]:
    found = find_executable_in_dirs(("codex",), get_candidate_bin_dirs(include_nvm=False))
    return {
        "ok": found is not None,
        "executable": str(found) if found else None,
        "detail": str(found) if found else "Executable 'codex' not found in PATH or standard locations",
    }


def supported_node_version(text: str) -> bool:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    if not match:
        return False
    major, minor, patch = map(int, match.groups())
    return major >= 23 or (major == 22 and minor >= 12) or (major == 20 and minor >= 19)


def check_node_npx(required: bool = False) -> dict[str, Any]:
    dirs = get_candidate_bin_dirs(include_nvm=True)
    self_scripts = {
        os.path.abspath(str(get_source_root() / "scripts" / "launch-chrome.sh")),
        os.path.abspath(str(get_source_root() / "scripts" / "launch.sh")),
    }

    def _not_launcher(cand: Path) -> bool:
        return os.path.abspath(str(cand)) not in self_scripts

    node_bin = find_executable_in_dirs(("node",), dirs, validator=_not_launcher)
    npx_bin = find_executable_in_dirs(("npx",), dirs, validator=_not_launcher)
    ok = node_bin is not None and npx_bin is not None
    node_version = ""
    version_error = ""
    if node_bin is not None:
        try:
            result = subprocess.run([str(node_bin), "--version"], capture_output=True, text=True, timeout=5, check=False)
            node_version = result.stdout.strip()
            if result.returncode != 0 or not supported_node_version(node_version):
                ok = False
                version_error = "Chrome MCP requires Node 20.19+, 22.12+, or 23+; selected version: " + (node_version or "unknown")
        except (OSError, subprocess.TimeoutExpired) as exc:
            ok = False
            version_error = "Could not verify Node version: " + str(exc)
    missing = []
    if node_bin is None:
        missing.append("node")
    if npx_bin is None:
        missing.append("npx")
    detail = (
        f"node={node_bin}, npx={npx_bin}"
        if ok
        else version_error or f"Missing {', '.join(missing)} in PATH, ~/.nvm/versions/node, or standard locations"
    )
    return {
        "ok": ok,
        "required": required,
        "executable": str(node_bin) if node_bin else None,
        "node_executable": str(node_bin) if node_bin else None,
        "node_version": node_version,
        "npx_executable": str(npx_bin) if npx_bin else None,
        "detail": detail,
    }


def check_chrome(required: bool = False) -> dict[str, Any]:
    dirs = get_candidate_bin_dirs(include_nvm=False)
    found = find_executable_in_dirs(
        ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"),
        dirs,
    )
    if found is None:
        override = os.environ.get("AGY_SETUP_CHROME_PATHS")
        if override is not None:
            app_candidates = [Path(p) for p in override.split(os.pathsep) if p.strip()]
        else:
            home = get_home_dir()
            app_candidates = [
                home / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome",
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path("/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"),
                Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
                Path("/usr/bin/google-chrome"),
                Path("/usr/bin/google-chrome-stable"),
                Path("/usr/bin/chromium"),
                Path("/usr/bin/chromium-browser"),
                Path("/snap/bin/chromium"),
            ]
        for cand in app_candidates:
            if _is_executable_file(cand):
                found = Path(os.path.abspath(str(cand)))
                break

    return {
        "ok": found is not None,
        "required": required,
        "executable": str(found) if found else None,
        "detail": str(found) if found else "Google Chrome / Chromium executable not found",
    }


def collect_discovered_bin_dirs(*checks: dict[str, Any]) -> list[str]:
    dirs: list[str] = []
    seen: set[str] = set()
    for info in checks:
        for key in ("executable", "node_executable", "npx_executable"):
            val = info.get(key)
            if isinstance(val, str) and val.strip():
                parent = os.path.abspath(str(Path(val).parent))
                if parent not in seen:
                    seen.add(parent)
                    dirs.append(parent)
    return dirs


def build_child_env(discovered_bin_dirs: Sequence[str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    for key in STRIPPED_ENV_KEYS:
        env.pop(key, None)
    caller_path = env.get("PATH", "")
    prepended: list[str] = []
    seen: set[str] = set()
    for d in discovered_bin_dirs or ():
        if d and d not in seen:
            seen.add(d)
            prepended.append(d)
    if prepended:
        if caller_path:
            env["PATH"] = os.pathsep.join([*prepended, caller_path])
        else:
            env["PATH"] = os.pathsep.join(prepended)
    return env


def run_command(
    cmd: list[str],
    env: dict[str, str],
    timeout: float = DEFAULT_CMD_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    if not isinstance(cmd, list) or not cmd or not all(isinstance(part, str) for part in cmd):
        raise ValueError("cmd must be a non-empty list of strings")
    proc = subprocess.run(
        cmd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        shell=False,
        check=False,
    )
    return (
        proc.returncode,
        bound_output(proc.stdout),
        bound_output(proc.stderr),
    )


def check_models(
    agy_executable: str | None,
    env: dict[str, str],
    offline: bool = False,
    timeout: float = MODEL_PROBE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if offline:
        return {
            "ok": True,
            "skipped": True,
            "exit_code": None,
            "required_models": list(REQUIRED_MODELS),
            "available_models": [],
            "output": "",
            "detail": "Skipped model probe (--offline)",
        }
    if not agy_executable:
        return {
            "ok": False,
            "skipped": False,
            "exit_code": None,
            "required_models": list(REQUIRED_MODELS),
            "available_models": [],
            "output": "",
            "detail": "Cannot probe models because 'agy' executable is missing",
        }
    try:
        code, out, err = run_command([agy_executable, "models"], env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "skipped": False,
            "exit_code": None,
            "required_models": list(REQUIRED_MODELS),
            "available_models": [],
            "output": "",
            "detail": f"agy models timed out after {timeout}s",
        }
    except OSError as exc:
        return {
            "ok": False,
            "skipped": False,
            "exit_code": None,
            "required_models": list(REQUIRED_MODELS),
            "available_models": [],
            "output": "",
            "detail": f"Failed to run agy models: {exc}",
        }

    combined = out if out else err
    found_models = [m for m in REQUIRED_MODELS if m in (out + "\n" + err)]
    ok = code == 0 and len(found_models) == len(REQUIRED_MODELS)
    if code != 0:
        detail = f"agy models exited with code {code}: {combined}"
    elif not ok:
        missing = [m for m in REQUIRED_MODELS if m not in found_models]
        detail = f"Missing required models in agy models output: {', '.join(missing)}"
    else:
        detail = f"Verified models: {', '.join(found_models)}"

    return {
        "ok": ok,
        "skipped": False,
        "exit_code": code,
        "required_models": list(REQUIRED_MODELS),
        "available_models": found_models,
        "output": combined,
        "detail": detail,
    }


def run_check(with_chrome: bool = False, offline: bool = False) -> dict[str, Any]:
    plat_info = check_platform()
    py_info = check_python()
    agy_info = check_agy()
    codex_info = check_codex()
    node_info = check_node_npx(required=with_chrome)
    chrome_info = check_chrome(required=with_chrome)

    discovered_dirs = collect_discovered_bin_dirs(
        py_info, agy_info, codex_info, node_info, chrome_info
    )
    child_env = build_child_env(discovered_dirs)
    models_info = check_models(
        agy_info.get("executable"),
        env=child_env,
        offline=offline,
        timeout=MODEL_PROBE_TIMEOUT_SECONDS,
    )

    errors: list[str] = []
    if not plat_info["ok"]:
        errors.append(f"platform: {plat_info['detail']}")
    if not py_info["ok"]:
        errors.append(f"python: {py_info['detail']}")
    if not agy_info["ok"]:
        errors.append(f"agy: {agy_info['detail']}")
    if not codex_info["ok"]:
        errors.append(f"codex: {codex_info['detail']}")
    if not offline and not models_info["ok"]:
        errors.append(f"models: {models_info['detail']}")
    if with_chrome:
        if not node_info["ok"]:
            errors.append(f"node/npx: {node_info['detail']}")
        if not chrome_info["ok"]:
            errors.append(f"chrome: {chrome_info['detail']}")

    npx_info = {
        "ok": node_info.get("npx_executable") is not None,
        "required": with_chrome,
        "executable": node_info.get("npx_executable"),
    }

    return {
        "ok": len(errors) == 0,
        "offline": offline,
        "with_chrome": with_chrome,
        "platform": plat_info,
        "python": py_info,
        "agy": agy_info,
        "codex": codex_info,
        "node": node_info,
        "npx": npx_info,
        "chrome": chrome_info,
        "models": models_info,
        "discovered_bin_dirs": discovered_dirs,
        "errors": errors,
    }


def _is_excluded_name(name: str) -> bool:
    if not name or name.startswith("."):
        return True
    if name in EXCLUDED_NAMES:
        return True
    lower = name.lower()
    if any(lower.endswith(sfx) for sfx in EXCLUDED_SUFFIXES):
        return True
    if EXCLUDED_SECRET_PATTERNS.search(name):
        return True
    return False


def _copy_filtered_tree(src_dir: Path, dst_dir: Path) -> list[str]:
    copied: list[str] = []
    for root, dirs, files in os.walk(src_dir, followlinks=False):
        root_path = Path(root)
        rel_root = root_path.relative_to(src_dir)

        kept_dirs: list[str] = []
        for d in sorted(dirs):
            d_path = root_path / d
            if d_path.is_symlink() or _is_excluded_name(d):
                continue
            kept_dirs.append(d)
        dirs[:] = kept_dirs

        target_root = dst_dir / rel_root if str(rel_root) != "." else dst_dir
        target_root.mkdir(parents=True, exist_ok=True)

        for fname in sorted(files):
            if _is_excluded_name(fname):
                continue
            src_file = root_path / fname
            if src_file.is_symlink() or not src_file.is_file():
                continue
            dst_file = target_root / fname
            shutil.copy2(src_file, dst_file)
            if dst_dir.name == "scripts" or dst_file.suffix in (".sh", ".py"):
                os.chmod(dst_file, 0o755)
            else:
                os.chmod(dst_file, 0o644)
            copied.append(str(dst_file))
    return copied


def copy_plugin_allowlist(source_root: Path, dest_plugin_dir: Path) -> dict[str, Any]:
    real_src = Path(os.path.realpath(source_root))
    real_dst = Path(os.path.realpath(dest_plugin_dir))

    for req in REQUIRED_ROOT_FILES:
        req_path = source_root / req
        if not req_path.is_file():
            raise SetupError(f"Required plugin file missing in source: {req}", step="copy_files")

    for req_dir in ("scripts", "skills"):
        if not (source_root / req_dir).is_dir():
            raise SetupError(f"Required plugin directory missing in source: {req_dir}/", step="copy_files")

    if real_src == real_dst:
        for script_file in (dest_plugin_dir / "scripts").glob("*"):
            if script_file.is_file() and script_file.suffix in (".sh", ".py"):
                try:
                    os.chmod(script_file, 0o755)
                except OSError:
                    pass
        return {
            "copied": False,
            "skipped_same_path": True,
            "source": str(source_root),
            "destination": str(dest_plugin_dir),
        }

    if real_src in real_dst.parents:
        raise SetupError(
            f"Destination '{dest_plugin_dir}' cannot be inside source directory '{source_root}'",
            step="copy_files",
        )
    if real_dst in real_src.parents:
        raise SetupError(
            f"Source '{source_root}' cannot be inside destination directory '{dest_plugin_dir}'",
            step="copy_files",
        )

    # Strict validation before removing/replacing only the exact managed target directory.
    if (
        dest_plugin_dir.name != PLUGIN_NAME
        or dest_plugin_dir.parent.name != "plugins"
        or dest_plugin_dir.parent.parent.name != MARKETPLACE_DIR_NAME
    ):
        raise SetupError(
            f"Refusing to manage unexpected destination path: {dest_plugin_dir}",
            step="copy_files",
        )

    plugins_parent = dest_plugin_dir.parent
    plugins_parent.mkdir(parents=True, exist_ok=True)

    staging_dir = Path(
        tempfile.mkdtemp(dir=str(plugins_parent), prefix=f".stage-{PLUGIN_NAME}-")
    )
    try:
        for fname in ALLOWED_ROOT_FILES:
            src_f = source_root / fname
            if src_f.is_file() and not src_f.is_symlink():
                dst_f = staging_dir / fname
                shutil.copy2(src_f, dst_f)
                os.chmod(dst_f, 0o644)

        for dname in ALLOWED_ROOT_DIRS:
            src_d = source_root / dname
            if src_d.is_dir() and not src_d.is_symlink():
                _copy_filtered_tree(src_d, staging_dir / dname)

        if dest_plugin_dir.exists():
            if dest_plugin_dir.is_symlink() or dest_plugin_dir.is_file():
                dest_plugin_dir.unlink()
            else:
                shutil.rmtree(dest_plugin_dir)
        os.replace(str(staging_dir), str(dest_plugin_dir))
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)

    return {
        "copied": True,
        "skipped_same_path": False,
        "source": str(source_root),
        "destination": str(dest_plugin_dir),
    }


def write_marketplace_manifest(
    marketplace_root: Path,
    dest_plugin_dir: Path,
) -> Path:
    manifest_path = marketplace_root / ".agents" / "plugins" / "marketplace.json"
    plugin_json_path = dest_plugin_dir / "plugin.json"
    plugin_meta = load_json_dict(plugin_json_path, step="write_marketplace_manifest")

    existing = load_json_dict(manifest_path, step="write_marketplace_manifest")
    existing["name"] = MARKETPLACE_NAME

    plugin_entry: dict[str, Any] = {
        "name": PLUGIN_NAME,
        "source": PLUGIN_SOURCE_REL,
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "Developer Tools",
    }
    if isinstance(plugin_meta.get("version"), str):
        plugin_entry["version"] = plugin_meta["version"]
    if isinstance(plugin_meta.get("description"), str):
        plugin_entry["description"] = plugin_meta["description"]

    old_plugins = existing.get("plugins")
    new_plugins: list[dict[str, Any]] = []
    replaced = False
    if isinstance(old_plugins, list):
        for item in old_plugins:
            if isinstance(item, dict) and item.get("name") == PLUGIN_NAME:
                if not replaced:
                    merged = dict(item)
                    merged.update(plugin_entry)
                    new_plugins.append(merged)
                    replaced = True
            elif isinstance(item, dict):
                new_plugins.append(item)
    if not replaced:
        new_plugins.append(plugin_entry)

    existing["plugins"] = new_plugins
    atomic_write_json(manifest_path, existing, mode=0o600)
    return manifest_path


def register_codex_plugin(
    codex_executable: str,
    marketplace_root: Path,
    env: dict[str, str],
) -> dict[str, Any]:
    cmd_mp = [codex_executable, "plugin", "marketplace", "add", str(marketplace_root)]
    try:
        mp_code, mp_out, mp_err = run_command(cmd_mp, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError(
            f"Failed running codex plugin marketplace add: {exc}",
            step="codex_marketplace_add",
        ) from exc
    if mp_code != 0:
        detail = mp_err or mp_out or f"exit code {mp_code}"
        raise SetupError(
            f"codex plugin marketplace add failed (exit {mp_code}): {detail}",
            step="codex_marketplace_add",
        )

    cmd_add = [codex_executable, "plugin", "add", PLUGIN_SPEC, "--json"]
    try:
        add_code, add_out, add_err = run_command(cmd_add, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError(
            f"Failed running codex plugin add: {exc}",
            step="codex_plugin_add",
        ) from exc
    if add_code != 0:
        detail = add_err or add_out or f"exit code {add_code}"
        raise SetupError(
            f"codex plugin add failed (exit {add_code}): {detail}",
            step="codex_plugin_add",
        )

    return {
        "marketplace_add": {
            "command": cmd_mp,
            "exit_code": mp_code,
            "output": mp_out or mp_err,
        },
        "plugin_add": {
            "command": cmd_add,
            "exit_code": add_code,
            "output": add_out or add_err,
        },
    }


def _has_chrome_devtools_server(mcp_list_output: str) -> bool:
    return bool(
        re.search(r"(?<![A-Za-z0-9_-])chrome-devtools(?![A-Za-z0-9_-])", mcp_list_output)
    )


def configure_chrome_mcp(
    agy_executable: str,
    durable_launch_chrome: Path,
    env: dict[str, str],
    replace_chrome: bool = False,
) -> dict[str, Any]:
    if not durable_launch_chrome.is_file():
        raise SetupError(
            f"Durable Chrome launcher missing at {durable_launch_chrome}",
            step="configure_chrome_mcp",
        )
    try:
        list_code, list_out, list_err = run_command([agy_executable, "mcp", "list"], env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError(
            f"Failed querying existing MCP servers via agy mcp list: {exc}",
            step="configure_chrome_mcp",
        ) from exc

    if list_code != 0:
        raise SetupError(
            f"agy mcp list failed (exit {list_code}): {list_err or list_out}",
            step="configure_chrome_mcp",
        )

    already_exists = _has_chrome_devtools_server(f"{list_out}\n{list_err}")
    if already_exists and not replace_chrome:
        return {
            "enabled": True,
            "configured": True,
            "changed": False,
            "preserved_existing": True,
            "conflict": True,
            "replaced": False,
            "server": "chrome-devtools",
            "command": None,
            "requested_command": str(durable_launch_chrome),
        }

    # agy mcp add updates an existing server; never remove a working server first.
    add_cmd = [agy_executable, "mcp", "add", "chrome-devtools", str(durable_launch_chrome)]
    try:
        add_code, add_out, add_err = run_command(add_cmd, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError(
            f"Failed running agy mcp add chrome-devtools: {exc}",
            step="configure_chrome_mcp",
        ) from exc

    if add_code != 0:
        raise SetupError(
            f"agy mcp add chrome-devtools failed (exit {add_code}): {add_err or add_out}",
            step="configure_chrome_mcp",
        )

    return {
        "enabled": True,
        "configured": True,
        "changed": True,
        "preserved_existing": False,
        "conflict": False,
        "replaced": bool(already_exists and replace_chrome),
        "server": "chrome-devtools",
        "command": str(durable_launch_chrome),
        "output": add_out or add_err,
    }


def validate_and_build_agents_md(existing_bytes: bytes) -> tuple[bytes, bool]:
    try:
        text = existing_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SetupError(f"AGENTS.md is not valid UTF-8: {exc}", step="configure_auto_route") from exc

    start_count = text.count(AUTO_ROUTE_START_MARKER)
    end_count = text.count(AUTO_ROUTE_END_MARKER)
    token_count = text.count(AUTO_ROUTE_MARKER_TOKEN)

    if start_count != end_count or start_count > 1 or token_count != (start_count + end_count):
        raise SetupError(
            "Malformed Gemini Bridge auto-route marker pair in AGENTS.md",
            step="configure_auto_route",
        )

    block_bytes = AUTO_ROUTE_BLOCK.encode("utf-8")
    start_marker_bytes = AUTO_ROUTE_START_MARKER.encode("utf-8")
    end_marker_bytes = AUTO_ROUTE_END_MARKER.encode("utf-8")

    if start_count == 1:
        start_idx = existing_bytes.find(start_marker_bytes)
        end_idx = existing_bytes.find(end_marker_bytes)
        if start_idx < 0 or end_idx < 0 or end_idx < start_idx:
            raise SetupError(
                "Malformed Gemini Bridge auto-route marker ordering in AGENTS.md",
                step="configure_auto_route",
            )
        end_after = end_idx + len(end_marker_bytes)
        # Preserve every single byte before start_idx and after end_after.
        updated = existing_bytes[:start_idx] + block_bytes + existing_bytes[end_after:]
        return updated, updated != existing_bytes

    if not existing_bytes:
        updated = block_bytes + b"\n"
    elif existing_bytes.endswith(b"\n\n"):
        updated = existing_bytes + block_bytes + b"\n"
    elif existing_bytes.endswith(b"\n"):
        updated = existing_bytes + b"\n" + block_bytes + b"\n"
    else:
        updated = existing_bytes + b"\n\n" + block_bytes + b"\n"
    return updated, updated != existing_bytes


def configure_auto_route(agents_path: Path) -> dict[str, Any]:
    existing_bytes = b""
    if agents_path.exists():
        if agents_path.is_dir():
            raise SetupError(f"AGENTS.md path is a directory: {agents_path}", step="configure_auto_route")
        try:
            existing_bytes = agents_path.read_bytes()
        except OSError as exc:
            raise SetupError(f"Failed reading {agents_path}: {exc}", step="configure_auto_route") from exc

    updated_bytes, changed = validate_and_build_agents_md(existing_bytes)
    atomic_write_bytes(agents_path, updated_bytes, mode=0o600)
    return {
        "enabled": True,
        "changed": changed,
        "path": str(agents_path),
    }


def configure_browser_permissions(settings_path: Path) -> dict[str, Any]:
    settings = load_json_dict(settings_path, step="configure_browser_permissions")
    permissions = settings.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        raise SetupError("permissions must be an object", step="configure_browser_permissions")
    allow = permissions.setdefault("allow", [])
    if not isinstance(allow, list):
        raise SetupError("permissions.allow must be a list", step="configure_browser_permissions")
    added = []
    for rule in ("mcp(chrome-devtools/*)", "read_url(localhost)", "read_url(127.0.0.1)", "execute_url(localhost)", "execute_url(127.0.0.1)"):
        if rule not in allow:
            allow.append(rule); added.append(rule)
    atomic_write_json(settings_path, settings, mode=0o600)
    return {"changed": bool(added), "added_permissions": added}


def configure_development_permissions(settings_path: Path) -> dict[str, Any]:
    settings = load_json_dict(settings_path, step="configure_development_permissions")
    before_serialized = json.dumps(settings, sort_keys=True)

    perms = settings.get("permissions")
    if perms is None:
        perms = {}
        settings["permissions"] = perms

    added: list[str] = []
    if isinstance(perms, dict):
        allow_list = perms.get("allow")
        if allow_list is None:
            allow_list = []
            perms["allow"] = allow_list
        elif not isinstance(allow_list, list):
            raise SetupError(
                f"Expected permissions.allow in {settings_path} to be a list",
                step="configure_development_permissions",
            )
        for rule in DEVELOPMENT_ALLOW_PERMISSIONS:
            if rule not in allow_list:
                allow_list.append(rule)
                added.append(rule)
    else:
        raise SetupError(
            f"Expected permissions in {settings_path} to be an object",
            step="configure_development_permissions",
        )

    after_serialized = json.dumps(settings, sort_keys=True)
    changed = before_serialized != after_serialized or not settings_path.exists()
    atomic_write_json(settings_path, settings, mode=0o600)
    return {
        "enabled": True,
        "changed": changed,
        "added_permissions": added,
        "path": str(settings_path),
    }


def configure_credit_overage(settings_path: Path, gemini_config_path: Path) -> dict[str, Any]:
    settings = load_json_dict(settings_path, step="configure_credit_overage")
    settings_before = json.dumps(settings, sort_keys=True)
    settings_existed = settings_path.exists()
    settings["useG1Credits"] = False
    settings_changed = (not settings_existed) or (json.dumps(settings, sort_keys=True) != settings_before)

    config = load_json_dict(gemini_config_path, step="configure_credit_overage")
    config_before = json.dumps(config, sort_keys=True)
    config_existed = gemini_config_path.exists()
    user_settings = config.get("userSettings")
    if user_settings is None:
        user_settings = {}
        config["userSettings"] = user_settings
    elif not isinstance(user_settings, dict):
        raise SetupError(
            f"Expected userSettings in {gemini_config_path} to be a JSON object",
            step="configure_credit_overage",
        )
    user_settings["useAiCredits"] = False
    config_changed = (not config_existed) or (json.dumps(config, sort_keys=True) != config_before)

    atomic_write_json(settings_path, settings, mode=0o600)
    atomic_write_json(gemini_config_path, config, mode=0o600)
    return {
        "enabled": True,
        "changed": settings_changed or config_changed,
        "settings_path": str(settings_path),
        "config_path": str(gemini_config_path),
        "useG1Credits": False,
        "useAiCredits": False,
    }


def run_install(
    source_root: Path | None = None,
    with_chrome: bool = False,
    replace_chrome: bool = False,
    auto_route: bool = False,
    allow_development: bool = False,
    disable_credit_overage: bool = False,
    offline: bool = False,
) -> dict[str, Any]:
    if replace_chrome and not with_chrome:
        raise SetupError("--replace-chrome requires --with-chrome", step="validate_flags")

    prereq = run_check(with_chrome=with_chrome, offline=offline)
    if not prereq["ok"]:
        raise SetupError(
            "Prerequisite check failed: " + "; ".join(prereq["errors"]),
            step="prerequisites",
        )

    codex_home = get_codex_home()
    home_dir = get_home_dir()
    agents_path = get_codex_agents_path(codex_home)
    settings_path = get_agy_settings_path(home_dir)
    gemini_config_path = get_gemini_config_path(home_dir)

    # Pre-validate opt-in config files and markers before mutating anything on disk.
    if auto_route and agents_path.exists():
        if agents_path.is_dir():
            raise SetupError(f"AGENTS.md path is a directory: {agents_path}", step="configure_auto_route")
        validate_and_build_agents_md(agents_path.read_bytes())
    if allow_development:
        load_json_dict(settings_path, step="configure_development_permissions")
    if disable_credit_overage:
        load_json_dict(settings_path, step="configure_credit_overage")
        cfg_pre = load_json_dict(gemini_config_path, step="configure_credit_overage")
        if "userSettings" in cfg_pre and not isinstance(cfg_pre["userSettings"], dict):
            raise SetupError(
                f"Expected userSettings in {gemini_config_path} to be a JSON object",
                step="configure_credit_overage",
            )

    src_root = (source_root if source_root is not None else get_source_root()).resolve()
    marketplace_root = get_marketplace_root(codex_home)
    dest_plugin_dir = get_durable_plugin_dir(codex_home)

    copy_info = copy_plugin_allowlist(src_root, dest_plugin_dir)
    manifest_path = write_marketplace_manifest(marketplace_root, dest_plugin_dir)

    child_env = build_child_env(prereq["discovered_bin_dirs"])
    codex_reg = register_codex_plugin(
        codex_executable=str(prereq["codex"]["executable"]),
        marketplace_root=marketplace_root,
        env=child_env,
    )

    changes: list[str] = [
        "plugin_files_copied" if copy_info["copied"] else "plugin_files_preserved_in_place",
        "marketplace_manifest_written",
        "codex_plugin_registered",
    ]

    chrome_mcp_result: dict[str, Any] = {"enabled": False, "changed": False}
    if with_chrome:
        durable_launch_chrome = dest_plugin_dir / "scripts" / "launch-chrome.sh"
        chrome_mcp_result = configure_chrome_mcp(
            agy_executable=str(prereq["agy"]["executable"]),
            durable_launch_chrome=durable_launch_chrome,
            env=child_env,
            replace_chrome=replace_chrome,
        )
        browser_permissions = configure_browser_permissions(settings_path)
        chrome_mcp_result["permissions"] = browser_permissions
        if browser_permissions["changed"]:
            changes.append("chrome_mcp_permissions_configured")
        if chrome_mcp_result.get("changed"):
            changes.append("chrome_mcp_configured")
        elif chrome_mcp_result.get("preserved_existing"):
            changes.append("chrome_mcp_preserved_existing")

    auto_route_result: dict[str, Any] = {"enabled": False, "changed": False}
    if auto_route:
        auto_route_result = configure_auto_route(agents_path)
        if auto_route_result.get("changed"):
            changes.append("agents_md_auto_route_updated")

    dev_perms_result: dict[str, Any] = {"enabled": False, "changed": False}
    if allow_development:
        dev_perms_result = configure_development_permissions(settings_path)
        if dev_perms_result.get("changed"):
            changes.append("development_permissions_updated")

    credit_result: dict[str, Any] = {"enabled": False, "changed": False}
    if disable_credit_overage:
        credit_result = configure_credit_overage(settings_path, gemini_config_path)
        if credit_result.get("changed"):
            changes.append("credit_overage_disabled")

    return {
        "ok": True,
        "installed": True,
        "marketplace_name": MARKETPLACE_NAME,
        "marketplace_root": str(marketplace_root),
        "marketplace_manifest": str(manifest_path),
        "plugin_dir": str(dest_plugin_dir),
        "plugin_spec": PLUGIN_SPEC,
        "copy": copy_info,
        "codex_registration": codex_reg,
        "opt_in": {
            "chrome_mcp": chrome_mcp_result,
            "auto_route": auto_route_result,
            "allow_development": dev_perms_result,
            "disable_credit_overage": credit_result,
        },
        "changes": changes,
    }


def _print_check_human(report: dict[str, Any]) -> None:
    status = "OK" if report["ok"] else "FAILED"
    print(f"Gemini Bridge prerequisite check: {status}")
    for key in ("platform", "python", "agy", "codex", "node", "chrome", "models"):
        item = report.get(key, {})
        item_ok = "ok" if item.get("ok") else ("optional-missing" if not item.get("required", True) else "MISSING")
        if key == "models" and item.get("skipped"):
            item_ok = "skipped"
        print(f"  - {key}: {item_ok} ({item.get('detail', '')})")
    if report["errors"]:
        for err in report["errors"]:
            print(f"Error: {err}", file=sys.stderr)


def _print_install_human(report: dict[str, Any]) -> None:
    print(f"Installed {report['plugin_spec']} to {report['plugin_dir']}")
    print(f"Marketplace manifest: {report['marketplace_manifest']}")
    print(f"Changes applied: {', '.join(report['changes'])}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="setup.py",
        description="Check prerequisites and install the Gemini Bridge plugin for Codex.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    p_check = subparsers.add_parser("check", help="Check platform and CLI prerequisites")
    p_check.add_argument("--json", action="store_true", help="Emit structured JSON report")
    p_check.add_argument("--offline", action="store_true", help="Skip live agy models probe")
    p_check.add_argument(
        "--with-chrome",
        action="store_true",
        help="Require Node.js/npx and Chrome for Chrome DevTools MCP",
    )

    p_install = subparsers.add_parser("install", help="Install plugin into CODEX_HOME")
    p_install.add_argument("--json", action="store_true", help="Emit structured JSON report")
    p_install.add_argument("--offline", action="store_true", help="Skip live agy models probe")
    p_install.add_argument(
        "--source",
        type=Path,
        default=None,
        help="Override plugin source root directory",
    )
    p_install.add_argument(
        "--with-chrome",
        action="store_true",
        help="Configure chrome-devtools MCP server in Antigravity CLI",
    )
    p_install.add_argument(
        "--replace-chrome",
        action="store_true",
        help="Replace existing chrome-devtools MCP server when used with --with-chrome",
    )
    p_install.add_argument(
        "--auto-route",
        action="store_true",
        help="Add or update the marked Gemini Bridge auto-route block in CODEX_HOME/AGENTS.md",
    )
    p_install.add_argument(
        "--allow-development",
        action="store_true",
        help="Merge narrow local development permissions into ~/.gemini/antigravity-cli/settings.json",
    )
    p_install.add_argument(
        "--disable-credit-overage",
        action="store_true",
        help="Set useG1Credits=false and userSettings.useAiCredits=false in Gemini CLI configs",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "check":
        report = run_check(with_chrome=args.with_chrome, offline=args.offline)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            _print_check_human(report)
        return 0 if report["ok"] else 1

    if args.subcommand == "install":
        try:
            report = run_install(
                source_root=args.source,
                with_chrome=args.with_chrome,
                replace_chrome=args.replace_chrome,
                auto_route=args.auto_route,
                allow_development=args.allow_development,
                disable_credit_overage=args.disable_credit_overage,
                offline=args.offline,
            )
        except SetupError as exc:
            err_payload = {
                "ok": False,
                "installed": False,
                "failed_step": exc.step,
                "error": redact_secrets(exc.message),
            }
            if args.json:
                print(json.dumps(err_payload, indent=2, sort_keys=True))
            else:
                print(
                    f"Installation failed at step '{exc.step}': {redact_secrets(exc.message)}",
                    file=sys.stderr,
                )
            return 1

        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            _print_install_human(report)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
