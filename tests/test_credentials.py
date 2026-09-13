"""凭据读写测试：环境变量优先级、明文回退、删除（不接触真实账号）。"""

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
        patcher = mock.patch.multiple(
            credentials,
            MUBU_HOME=self.home,
            CRED_JSON=self.home / "credentials.json",
            CRED_DPAPI=self.home / "credentials.dpapi",
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # 关掉系统级后端（Windows DPAPI / macOS 钥匙串 / Linux Secret Service）：
        # 否则测试会写到开发者真实的钥匙串里，且用例之间互相污染。
        for name in ("_is_windows", "_keychain_available", "_secret_tool_available"):
            backend = mock.patch.object(credentials, name, return_value=False)
            backend.start()
            self.addCleanup(backend.stop)
        os.environ.pop("MUBU_PHONE", None)
        os.environ.pop("MUBU_PASSWORD", None)
        self.addCleanup(lambda: (os.environ.pop("MUBU_PHONE", None),
                                 os.environ.pop("MUBU_PASSWORD", None)))

    def test_env_vars_take_priority(self):
        credentials.store_credentials("13800000000", "from-file")
        os.environ["MUBU_PHONE"] = "13900000000"
        os.environ["MUBU_PASSWORD"] = "from-env"
        self.assertEqual(credentials.load_credentials(), ("13900000000", "from-env"))

    def test_plaintext_round_trip(self):
        backend = credentials.store_credentials("13800000000", "pw", backend="plaintext-file")
        self.assertEqual(backend, "plaintext-file")
        self.assertEqual(credentials.load_credentials(), ("13800000000", "pw"))
        stored = json.loads((self.home / "credentials.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["phone"], "13800000000")

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

    def test_delete_removes_files(self):
        credentials.store_credentials("13800000000", "pw", backend="plaintext-file")
        removed = credentials.delete_credentials()
        self.assertIn("plaintext-file", removed)
        with self.assertRaises(credentials.MissingCredentials):
            credentials.load_credentials()

    def test_available_backends_always_has_fallback(self):
        backends = credentials.available_backends()
        self.assertIn("env", backends)
        self.assertIn("plaintext-file", backends)

    def test_corrupt_file_is_ignored(self):
        (self.home / "credentials.json").write_text("{ not json", encoding="utf-8")
        with self.assertRaises(credentials.MissingCredentials):
            credentials.load_credentials()

    def test_module_has_no_top_level_wintypes_import(self):
        """回归测试：顶层 import ctypes.wintypes 会让 macOS/Linux 直接 import 失败。"""
        source = Path(credentials.__file__).read_text(encoding="utf-8")
        self.assertNotIn("\nimport ctypes.wintypes", source)
        self.assertNotIn("\nfrom ctypes import wintypes", source)


if __name__ == "__main__":
    unittest.main()
