"""Tests for scripts/release_check.py release validation and package hygiene."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_check", ROOT / "scripts" / "release_check.py")
assert SPEC and SPEC.loader
release_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_check)


def _write_zip_with_companion(
    zip_path: Path,
    entries: dict[str, tuple[bytes, int]],
) -> tuple[Path, Path]:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, (raw, mode) in entries.items():
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.external_attr = mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, raw)
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    sha_path = zip_path.with_suffix(".sha256")
    sha_path.write_text(f"{digest}  {zip_path.name}\n", encoding="utf-8")
    return zip_path, sha_path


def _base_valid_entries() -> dict[str, tuple[bytes, int]]:
    reg = stat.S_IFREG | 0o644
    exe = stat.S_IFREG | 0o755
    entries: dict[str, tuple[bytes, int]] = {}
    for rel in release_check.REQUIRED_ARCHIVE_FILES:
        mode = exe if rel in release_check.EXEC_SCRIPTS else reg
        entries[rel] = (b"valid content\n", mode)
    return entries


class ReleaseCheckTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="agy-release-test-")
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _populate_minimal_repo(self) -> Path:
        repo = self.root / "repo"
        (repo / "scripts").mkdir(parents=True)
        (repo / "skills" / "gemini-bridge").mkdir(parents=True)
        for fname in ("plugin.json", "mcp.json", "LICENSE"):
            shutil.copy2(ROOT / fname, repo / fname)
        for sname in ("bridge.py", "launch.sh", "launch-chrome.sh"):
            dst = repo / "scripts" / sname
            shutil.copy2(ROOT / "scripts" / sname, dst)
            dst.chmod(0o755)
        shutil.copy2(
            ROOT / "skills" / "gemini-bridge" / "SKILL.md",
            repo / "skills" / "gemini-bridge" / "SKILL.md",
        )
        return repo

    def test_mismatched_versions_and_metadata_violations_rejected(self) -> None:
        repo = self._populate_minimal_repo()
        expected_version = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))["version"]
        self.assertEqual(release_check.validate_repo_metadata(repo), expected_version)

        # Version mismatch between plugin.json and bridge.py SERVER_VERSION
        plugin_data = json.loads((repo / "plugin.json").read_text(encoding="utf-8"))
        plugin_data["version"] = "9.9.9"
        (repo / "plugin.json").write_text(json.dumps(plugin_data), encoding="utf-8")
        with self.assertRaisesRegex(release_check.ReleaseCheckError, "Version mismatch"):
            release_check.validate_repo_metadata(repo)

        # Restore version, break executable bit on launch.sh
        plugin_data["version"] = expected_version
        (repo / "plugin.json").write_text(json.dumps(plugin_data), encoding="utf-8")
        (repo / "scripts" / "launch.sh").chmod(0o644)
        with self.assertRaisesRegex(release_check.ReleaseCheckError, "Executable permission bit"):
            release_check.validate_repo_metadata(repo)

    def test_mcp_command_rejects_absolute_and_traversal_paths(self) -> None:
        repo = self._populate_minimal_repo()
        cmd, args = release_check.validate_mcp_config(repo)
        self.assertEqual(cmd, "./scripts/launch.sh")
        self.assertEqual(args, ["serve"])

        bad_commands = [
            "/usr/local/bin/launch.sh",
            "~/scripts/launch.sh",
            "../launch.sh",
            "./scripts/../../escape.sh",
            "scripts\\launch.sh",
        ]
        for bad_cmd in bad_commands:
            (repo / "mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "antigravity": {
                                "type": "stdio",
                                "command": bad_cmd,
                                "args": ["serve"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(release_check.ReleaseCheckError, msg=f"Should reject {bad_cmd}"):
                release_check.validate_mcp_config(repo)

    def test_forbidden_archive_entries_and_sha256_mismatch_rejected(self) -> None:
        reg = stat.S_IFREG | 0o644
        lnk = stat.S_IFLNK | 0o777

        # 1. Valid synthetic archive passes
        ok_zip, ok_sha = _write_zip_with_companion(self.root / "ok.zip", _base_valid_entries())
        meta = release_check.validate_archive(ok_zip, ok_sha)
        self.assertEqual(meta["files"], len(release_check.REQUIRED_ARCHIVE_FILES))

        # 2. Corrupted SHA256 companion fails
        ok_sha.write_text("0" * 64 + f"  {ok_zip.name}\n", encoding="utf-8")
        with self.assertRaisesRegex(release_check.ReleaseCheckError, "SHA256"):
            release_check.validate_archive(ok_zip, ok_sha)

        # 3. Forbidden paths, dotfiles, secrets, logs, symlinks, and embedded user paths fail
        bad_cases: list[tuple[str, tuple[bytes, int]]] = [
            (".env", (b"SECRET=1", reg)),
            ("scripts/my_secret_key.txt", (b"123", reg)),
            ("scripts/settings.json", (b"{}", reg)),
            ("logs/debug.log", (b"log", reg)),
            ("state/job.json", (b"{}", reg)),
            ("scripts/__pycache__/bridge.pyc", (b"\x00", reg)),
            ("../traversal.py", (b"pass", reg)),
            ("scripts/symlink.sh", (b"target", lnk)),
            ("README.md", (b"Path: /" + b"Users/alice/private/project\n", reg)),
        ]
        for idx, (entry_name, entry_val) in enumerate(bad_cases):
            entries = _base_valid_entries()
            entries[entry_name] = entry_val
            zpath, spath = _write_zip_with_companion(self.root / f"bad_{idx}.zip", entries)
            with self.assertRaises(
                release_check.ReleaseCheckError,
                msg=f"Expected failure for archive entry {entry_name}",
            ):
                release_check.validate_archive(zpath, spath)

    def test_valid_package_release_check_smoke(self) -> None:
        expected_version = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))["version"]
        report = release_check.run_release_check(ROOT)
        self.assertTrue(report["ok"])
        self.assertEqual(report["version"], expected_version)
        self.assertEqual(report["tools"], release_check.EXPECTED_TOOLS)
        self.assertEqual(
            report["smoke_job"],
            {"status": "succeeded", "model": "gemini-4-argon-medium"},
        )
        self.assertFalse((ROOT / "dist").exists())


def tearDownModule() -> None:
    for d in (ROOT / "scripts" / "__pycache__", ROOT / "tests" / "__pycache__"):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
