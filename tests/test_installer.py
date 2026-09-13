"""安装器测试：只验证生成的配置结构，不写真实用户目录。"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import temp_dir  # noqa: E402

from mubu_web_mcp import installer  # noqa: E402


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self)

    def test_entry_shape(self):
        agent = installer.AGENTS["codex"]
        entry = installer.entry_for(agent)
        self.assertIn("command", entry)
        self.assertEqual(entry["args"], ["-m", "mubu_web_mcp"])

    def test_read_only_env(self):
        entry = installer.entry_for(installer.AGENTS["codex"], read_only=True)
        self.assertEqual(entry["env"]["MUBU_READ_ONLY"], "1")

    def test_vscode_entry_has_type(self):
        entry = installer.entry_for(installer.AGENTS["vscode"])
        self.assertEqual(entry["type"], "stdio")

    def test_json_write_is_idempotent_and_preserves_other_servers(self):
        path = self.root / "claude_desktop_config.json"
        path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}),
                        encoding="utf-8")
        entry = installer.entry_for(installer.AGENTS["claude-desktop"])
        installer._write_json(path, "mcpServers", entry)
        installer._write_json(path, "mcpServers", entry)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("other", data["mcpServers"])
        self.assertEqual(data["mcpServers"][installer.SERVER_KEY]["args"],
                         ["-m", "mubu_web_mcp"])
        self.assertTrue(path.with_suffix(path.suffix + ".bak").exists())

    def test_json_write_reports_invalid_existing_file(self):
        path = self.root / "broken.json"
        path.write_text("{ nope", encoding="utf-8")
        message = installer._write_json(path, "mcpServers", {"command": "x"})
        self.assertIn("跳过", message)
        self.assertEqual(path.read_text(encoding="utf-8"), "{ nope")

    def test_toml_write_and_replace(self):
        path = self.root / "config.toml"
        path.write_text('model = "x"\n\n[mcp_servers.other]\ncommand = "y"\n', encoding="utf-8")
        entry = installer.entry_for(installer.AGENTS["codex"])
        installer._write_toml(path, entry)
        installer._write_toml(path, entry)  # 再来一次，不应重复
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count(f"[mcp_servers.{installer.SERVER_KEY}]"), 1)
        self.assertIn('model = "x"', text)
        self.assertIn("[mcp_servers.other]", text)
        self.assertIn('args = ["-m", "mubu_web_mcp"]', text)

    def test_manual_json_is_valid(self):
        data = json.loads(installer.manual_json())
        self.assertIn(installer.SERVER_KEY, data["mcpServers"])

    def test_dry_run_does_not_write(self):
        agent = installer.AGENTS["codex"]
        path = self.root / "dry.toml"
        original = agent.config_paths
        agent.config_paths = lambda: [path]
        try:
            results = installer.install(["codex"], dry_run=True)
        finally:
            agent.config_paths = original
        self.assertFalse(path.exists())
        self.assertTrue(any("将写入" in r for r in results))


if __name__ == "__main__":
    unittest.main()
