"""Offline unit and regression tests for scripts/task_access.py."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
import task_access  # noqa: E402


class TaskAccessTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_ctx = tempfile.TemporaryDirectory(prefix="agy-task-access-test-")
        self.root = Path(self.tmp_ctx.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tmp_ctx.cleanup()

    def test_empty_and_none_access_preserve_default_settings_without_reading_policy(self) -> None:
        self.assertEqual(
            task_access.validate_access(None),
            {"read_urls": [], "read_files": []},
        )
        self.assertEqual(
            task_access.validate_access({}),
            {"read_urls": [], "read_files": []},
        )

        # Even if policy_path points to invalid JSON, empty access must not read or touch it.
        bad_policy = self.root / "corrupt_policy.json"
        bad_bytes = b"{not valid json"
        bad_policy.write_bytes(bad_bytes)

        for empty_input in (None, {}, {"read_urls": [], "read_files": []}):
            settings = task_access.prepare_settings(
                empty_input,
                workspace=self.workspace,
                policy_path=bad_policy,
            )
            self.assertEqual(
                settings,
                {
                    "useG1Credits": False,
                    "enableTerminalSandbox": True,
                    "toolPermission": "proceed-in-sandbox",
                },
            )
            self.assertNotIn("permissions", settings)

        self.assertEqual(bad_policy.read_bytes(), bad_bytes)

    def test_twenty_one_synthetic_urls_and_one_public_json_fixture_end_to_end(self) -> None:
        fixtures_dir = self.root / "public_fixtures"
        fixtures_dir.mkdir(parents=True, exist_ok=True)
        public_json = fixtures_dir / "published_sites_manifest.json"
        public_json.write_text(
            json.dumps({"schema": "synthetic-v1", "count": 21}),
            encoding="utf-8",
        )
        sibling_json = fixtures_dir / "unlisted_sibling.json"
        sibling_json.write_text("{}", encoding="utf-8")

        urls = [
            f"https://Site-{i:02d}.Example.com/audit/page-{i}?view=summary#section-{i}"
            for i in range(1, 21)
        ]
        # 21st URL uses an IDNA hostname and custom port
        urls.append("https://Bücher.Example.org:8443/katalog/index.html?lang=de#top")

        raw_access = {
            "read_urls": list(urls),
            "read_files": [str(public_json)],
        }
        snapshot_access = copy.deepcopy(raw_access)

        policy_file = self.root / "antigravity_policy.json"
        initial_policy = {
            "permissions": {
                "allow": ["read_url(docs.example.net)"],
                "deny": ["read_url(blocked.example.com)", f"read_file({sibling_json})"],
                "ask": ["read_url(confirm.example.com)"],
            },
            "otherUserSetting": "preserved-in-file",
        }
        initial_policy_bytes = (json.dumps(initial_policy, indent=2) + "\n").encode("utf-8")
        policy_file.write_bytes(initial_policy_bytes)

        validated = task_access.validate_access(raw_access)
        self.assertEqual(raw_access, snapshot_access)
        self.assertEqual(len(validated["read_urls"]), 21)
        self.assertEqual(len(validated["read_files"]), 1)
        self.assertEqual(validated["read_files"], [str(public_json.resolve())])

        # Verify IDNA/lowercase host normalization, retained path/query, and stripped fragment
        self.assertEqual(
            validated["read_urls"][0],
            "https://site-01.example.com/audit/page-1?view=summary",
        )
        self.assertEqual(
            validated["read_urls"][20],
            "https://xn--bcher-kva.example.org:8443/katalog/index.html?lang=de",
        )
        for normalized_url in validated["read_urls"]:
            self.assertNotIn("#", normalized_url)

        settings = task_access.prepare_settings(
            raw_access,
            workspace=self.workspace,
            policy_path=policy_file,
        )
        # Caller input and on-disk policy file bytes must never be mutated
        self.assertEqual(raw_access, snapshot_access)
        self.assertEqual(policy_file.read_bytes(), initial_policy_bytes)

        self.assertIs(settings["useG1Credits"], False)
        self.assertIs(settings["enableTerminalSandbox"], True)
        self.assertEqual(settings["toolPermission"], "proceed-in-sandbox")

        perms = settings["permissions"]
        # Preexisting allow/deny/ask rules preserved
        self.assertIn("read_url(docs.example.net)", perms["allow"])
        self.assertEqual(perms["deny"], initial_policy["permissions"]["deny"])
        self.assertEqual(perms["ask"], initial_policy["permissions"]["ask"])

        # Narrow file grant: exact canonical file is granted, never parent dir or wildcard
        self.assertIn(f"read_file({public_json.resolve()})", perms["allow"])
        self.assertNotIn(f"read_file({fixtures_dir.resolve()})", perms["allow"])
        self.assertNotIn(f"read_file({self.root})", perms["allow"])
        self.assertNotIn("read_file(*)", perms["allow"])

        # Host-level read_url rules added for all 21 synthetic sites without duplicates
        for i in range(1, 21):
            self.assertIn(f"read_url(site-{i:02d}.example.com)", perms["allow"])
        self.assertIn("read_url(xn--bcher-kva.example.org)", perms["allow"])
        self.assertEqual(len(perms["allow"]), len(set(perms["allow"])))

        # Read-only enforcement: no write_file, execute_url, run_command, or wildcard grants
        for rule in perms["allow"]:
            self.assertTrue(
                rule.startswith("read_url(") or rule.startswith("read_file("),
                f"Unexpected non-read permission rule granted: {rule}",
            )
            self.assertNotIn("*", rule)

        # Prompt contract retains exact URLs with paths/queries and canonical file path
        contract = task_access.describe_access(raw_access)
        self.assertIn("https://site-01.example.com/audit/page-1?view=summary", contract)
        self.assertIn("https://xn--bcher-kva.example.org:8443/katalog/index.html?lang=de", contract)
        self.assertIn(str(public_json.resolve()), contract)

    def test_tmp_canonical_alias_and_symlink_resolution(self) -> None:
        tmp_root = Path("/tmp")
        if not tmp_root.is_dir():
            self.skipTest("/tmp is not available on this system")

        with tempfile.TemporaryDirectory(dir="/tmp", prefix="agy-tmp-alias-") as raw_tmp_dir:
            # Keep the unresolved /tmp/... path even on macOS where /tmp -> /private/tmp
            unresolved_dir = Path("/tmp") / Path(raw_tmp_dir).name
            real_file = unresolved_dir / "public_data.json"
            real_file.write_text('{"ok": true}\n', encoding="utf-8")
            alias_link = unresolved_dir / "alias_link.json"
            alias_link.symlink_to(real_file)

            expected_canonical = str(real_file.resolve())
            validated = task_access.validate_access(
                {"read_files": [str(unresolved_dir / "public_data.json"), str(alias_link)]}
            )
            # Both the /tmp path and symlink resolve and deduplicate to the single canonical path
            self.assertEqual(validated["read_files"], [expected_canonical])

            policy_file = self.root / "empty_policy.json"
            policy_file.write_text("{}", encoding="utf-8")
            settings = task_access.prepare_settings(
                {"read_files": [str(unresolved_dir / "public_data.json")]},
                workspace=self.workspace,
                policy_path=policy_file,
            )
            self.assertEqual(
                settings["permissions"]["allow"],
                [f"read_file({expected_canonical})"],
            )

    def test_invalid_urls_wildcard_userinfo_control_chars_and_limits_rejected(self) -> None:
        bad_urls = [
            "https://*.example.com/path",
            "https://sub.*.example.com/path",
            "https://example.com*/path",
            "https://example.com/path/*",
            "https://example.com/path with space",
            "https://user:pass@example.com/private",
            "https://token@example.com/private",
            "https://example.com/path\ninjected",
            "https://example.com/path\x00null",
            "https://example.com/path\x7fdel",
            "ftp://example.com/file.txt",
            "file:///tmp/local.json",
            "javascript:alert(1)",
            "https:///missing-host",
            "https://example.com:0/bad-port",
            "https://example.com:70000/bad-port",
            "https://..invalid.example.com/",
            "",
        ]
        for bad_url in bad_urls:
            with self.subTest(bad_url=bad_url):
                with self.assertRaises(task_access.TaskAccessError) as ctx:
                    task_access.validate_access({"read_urls": [bad_url]})
                self.assertIsInstance(ctx.exception, ValueError)
                self.assertTrue(ctx.exception.code)

        # Reject unknown keys, non-dict input, non-list entries, and >100 URLs / >50 files
        with self.assertRaises(task_access.TaskAccessError):
            task_access.validate_access({"write_files": []})
        with self.assertRaises(task_access.TaskAccessError):
            task_access.validate_access(["https://example.com/"])
        with self.assertRaises(task_access.TaskAccessError):
            task_access.validate_access({"read_urls": "https://example.com/"})
        with self.assertRaises(task_access.TaskAccessError):
            task_access.validate_access(
                {"read_urls": [f"https://s{i}.example.com/" for i in range(101)]}
            )

        many_files: list[str] = []
        for i in range(51):
            fp = self.root / f"f_{i}.txt"
            fp.write_text("x", encoding="utf-8")
            many_files.append(str(fp))
        with self.assertRaises(task_access.TaskAccessError):
            task_access.validate_access({"read_files": many_files})

    def test_protected_files_and_protected_symlink_targets_rejected(self) -> None:
        safe_dir = self.root / "safe_inputs"
        safe_dir.mkdir(parents=True, exist_ok=True)

        protected_rel_paths = [
            Path("secrets") / "api_data.json",
            Path(".ssh") / "id_ed25519",
            Path(".aws") / "config",
            Path(".gnupg") / "pubring.kbx",
            Path(".gemini") / "oauth.json",
            Path(".codex") / "session.json",
            Path("project") / ".env",
            Path("project") / ".env.production",
            Path("project") / "auth.json",
            Path("project") / "credentials.json",
            Path("project") / "service-account.json",
            Path("certs") / "client.pem",
            Path("certs") / "private.key",
            Path("certs") / "bundle.p12",
            Path("certs") / "cert.pfx",
            Path("certs") / "store.jks",
            Path("certs") / "android.keystore",
        ]

        for idx, rel_path in enumerate(protected_rel_paths):
            target = self.root / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("synthetic-placeholder", encoding="utf-8")

            # Direct path must be rejected
            with self.subTest(direct=str(rel_path)):
                with self.assertRaises(task_access.TaskAccessError) as ctx:
                    task_access.validate_access({"read_files": [str(target)]})
                self.assertEqual(ctx.exception.code, "protected_target")

            # Benign-looking symlink pointing to the protected file must also be rejected
            benign_symlink = safe_dir / f"innocent_fixture_{idx}.json"
            benign_symlink.symlink_to(target)
            with self.subTest(symlink=str(rel_path)):
                with self.assertRaises(task_access.TaskAccessError) as ctx:
                    task_access.validate_access({"read_files": [str(benign_symlink)]})
                self.assertEqual(ctx.exception.code, "protected_target")

        # Relative paths, directories, missing files, and glob characters rejected
        with self.assertRaises(task_access.TaskAccessError):
            task_access.validate_access({"read_files": ["relative/file.json"]})
        with self.assertRaises(task_access.TaskAccessError):
            task_access.validate_access({"read_files": [str(safe_dir)]})
        with self.assertRaises(task_access.TaskAccessError):
            task_access.validate_access({"read_files": [str(safe_dir / "missing.json")]})
        with self.assertRaises(task_access.TaskAccessError):
            task_access.validate_access({"read_files": [str(safe_dir / "*.json")]})

    def test_policy_conflicts_raise_before_launch_and_leave_policy_unchanged(self) -> None:
        allowed_dir = self.root / "data" / "reports"
        blocked_dir = self.root / "data" / "restricted"
        allowed_dir.mkdir(parents=True, exist_ok=True)
        blocked_dir.mkdir(parents=True, exist_ok=True)

        report_file = allowed_dir / "q1.json"
        report_file.write_text("{}", encoding="utf-8")
        restricted_file = blocked_dir / "internal.json"
        restricted_file.write_text("{}", encoding="utf-8")

        policy_path = self.root / "policy.json"
        policy_data = {
            "permissions": {
                "allow": ["read_url(trusted.example.com)"],
                "deny": [
                    "read_url(forbidden.example.com)",
                    f"read_file({blocked_dir})",
                ],
                "ask": [
                    "read_url(prompt.example.com)",
                    f"read_file({report_file})",
                ],
            }
        }
        original_bytes = json.dumps(policy_data, indent=2).encode("utf-8")
        policy_path.write_bytes(original_bytes)

        conflict_cases = [
            # Exact hostname deny
            {"read_urls": ["https://forbidden.example.com/index.html"]},
            # Subdomain of denied domain
            {"read_urls": ["https://sub.forbidden.example.com/api/status"]},
            # Subdomain of ask domain
            {"read_urls": ["https://deep.sub.prompt.example.com/check"]},
            # File inside a denied parent directory
            {"read_files": [str(restricted_file)]},
            # Exact file matching an ask rule
            {"read_files": [str(report_file)]},
        ]

        for case in conflict_cases:
            with self.subTest(case=case):
                with self.assertRaises(task_access.TaskAccessError) as ctx:
                    task_access.prepare_settings(
                        case,
                        workspace=self.workspace,
                        policy_path=policy_path,
                    )
                self.assertEqual(ctx.exception.code, "policy_conflict")
                self.assertTrue(ctx.exception.resources)
                self.assertEqual(policy_path.read_bytes(), original_bytes)

        # Global wildcard deny on read_url(*) must also trigger policy_conflict
        wildcard_policy = self.root / "wildcard_policy.json"
        wildcard_bytes = json.dumps(
            {"permissions": {"allow": [], "deny": ["read_url(*)"], "ask": []}}
        ).encode("utf-8")
        wildcard_policy.write_bytes(wildcard_bytes)
        with self.assertRaises(task_access.TaskAccessError) as ctx:
            task_access.prepare_settings(
                {"read_urls": ["https://any.example.org/"]},
                workspace=self.workspace,
                policy_path=wildcard_policy,
            )
        self.assertEqual(ctx.exception.code, "policy_conflict")
        self.assertEqual(wildcard_policy.read_bytes(), wildcard_bytes)


if __name__ == "__main__":
    unittest.main()
