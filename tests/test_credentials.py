"""凭据与秘密存储测试。

这些用例**绝不触碰真实的操作系统凭据存储**：Windows DPAPI、macOS 钥匙串、
Linux Secret Service 全部在 setUp 里被替换为不可用，只测试明文回退路径与环境变量。
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import temp_dir  # noqa: E402

from mubu_web_mcp import credentials  # noqa: E402


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self)
        patcher = mock.patch.object(credentials, "MUBU_HOME", self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 关掉系统级后端：测试不得访问真实钥匙串 / DPAPI / Secret Service
        for name in ("_is_windows", "_keychain_available", "_secret_tool_available"):
            backend = mock.patch.object(credentials, name, return_value=False)
            backend.start()
            self.addCleanup(backend.stop)
        # 也禁止子进程被调用（security / secret-tool）
        runner = mock.patch.object(credentials.subprocess, "run",
                                   side_effect=AssertionError("测试不应调用系统凭据命令"))
        runner.start()
        self.addCleanup(runner.stop)
        os.environ.pop("MUBU_PHONE", None)
        os.environ.pop("MUBU_PASSWORD", None)
        self.addCleanup(lambda: (os.environ.pop("MUBU_PHONE", None),
                                 os.environ.pop("MUBU_PASSWORD", None)))

    def test_env_vars_take_priority(self):
        credentials.store_credentials("13800000000", "from-file", backend="plaintext-file")
        os.environ["MUBU_PHONE"] = "13900000000"
        os.environ["MUBU_PASSWORD"] = "from-env"
        self.assertEqual(credentials.load_credentials(), ("13900000000", "from-env"))

    def test_plaintext_round_trip(self):
        backend = credentials.store_credentials("13800000000", "pw", backend="plaintext-file")
        self.assertEqual(backend, "plaintext-file")
        self.assertEqual(credentials.load_credentials(), ("13800000000", "pw"))
        stored = json.loads(credentials.secrets_json_path().read_text(encoding="utf-8"))
        inner = json.loads(stored[credentials.CREDENTIALS_SLOT])
        self.assertEqual(inner["phone"], "13800000000")
        self.assertEqual(inner["password"], "pw")

    def test_missing_credentials_raises(self):
        with self.assertRaises(credentials.MissingCredentials):
            credentials.load_credentials()

    def test_empty_values_rejected(self):
        with self.assertRaises(credentials.CredentialsError):
            credentials.store_credentials("", "pw")
        with self.assertRaises(credentials.CredentialsError):
            credentials.store_credentials("13800000000", "")

    def test_unknown_backend_rejected(self):
        with self.assertRaises(credentials.CredentialsError):
            credentials.store_credentials("13800000000", "pw", backend="magic")

    def test_explicit_unavailable_backend_rejected(self):
        for backend in ("dpapi", "keychain", "secret-service"):
            with self.assertRaises(credentials.CredentialsError):
                credentials.store_credentials("13800000000", "pw", backend=backend)

    def test_default_backend_falls_back_to_file(self):
        self.assertEqual(credentials.default_backend(), "plaintext-file")
        self.assertEqual(credentials.available_backends(), ["env", "plaintext-file"])

    def test_secret_slots_are_independent(self):
        credentials.store_secret("token", "T1")
        credentials.store_secret(credentials.CREDENTIALS_SLOT,
                                 json.dumps({"phone": "1", "password": "p"}))
        self.assertEqual(credentials.load_secret("token"), "T1")
        credentials.delete_secret("token")
        self.assertIsNone(credentials.load_secret("token"))
        # 删掉 token 槽位不应影响凭据
        self.assertEqual(credentials.load_credentials(), ("1", "p"))

    def test_legacy_plaintext_file_still_readable(self):
        legacy = credentials.legacy_credentials_json_path()
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({"phone": "13700000000", "password": "legacy"}),
                          encoding="utf-8")
        self.assertEqual(credentials.load_credentials(), ("13700000000", "legacy"))

    def test_delete_removes_credentials_and_token(self):
        credentials.store_credentials("13800000000", "pw", backend="plaintext-file")
        credentials.store_secret(credentials.TOKEN_SLOT, "JWT")
        removed = credentials.delete_credentials()
        self.assertIn("plaintext-file", removed)
        with self.assertRaises(credentials.MissingCredentials):
            credentials.load_credentials()
        self.assertIsNone(credentials.load_secret(credentials.TOKEN_SLOT))

    def test_corrupt_file_is_ignored(self):
        path = credentials.secrets_json_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(credentials.MissingCredentials):
            credentials.load_credentials()

    def test_secret_file_is_not_world_readable(self):
        credentials.store_secret("token", "JWT")
        mode = credentials.secrets_json_path().stat().st_mode & 0o777
        if os.name != "nt":
            self.assertEqual(mode, 0o600)

    def test_module_has_no_top_level_wintypes_import(self):
        """回归测试：顶层 import ctypes.wintypes 会让 macOS/Linux 直接 import 失败。"""
        source = Path(credentials.__file__).read_text(encoding="utf-8")
        self.assertNotIn("\nimport ctypes.wintypes", source)
        self.assertNotIn("\nfrom ctypes import wintypes", source)


if __name__ == "__main__":
    unittest.main()
