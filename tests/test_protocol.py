"""MCP 协议层测试：握手、工具清单、只读模式、错误处理（不联网）。"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mubu_web_mcp import __version__, server  # noqa: E402


class FakeClient:
    """替身客户端，不产生任何网络请求。"""

    def whoami(self):
        return {"name": "测试账号", "user_id": "1", "member_id": None,
                "token_expires_in_seconds": 600}

    def list_dir(self, folder_id="0"):
        self.folder_id = folder_id
        if folder_id != "0":
            return {"folders": [], "documents": []}
        return {"folders": [{"id": "f1", "name": "文件夹"}],
                "documents": [{"id": "d1", "name": "文档"}]}

    def get_doc(self, doc_id):
        return {"definition": json.dumps({"nodes": [{"text": "标题", "children": []}]})}

    def doc_tree(self, data):
        return json.loads(data["definition"])

    def import_doc(self, name, nodes, folder_id="0"):
        self.last_import = (name, nodes, folder_id)
        return "newdoc"

    def create_folder(self, name, folder_id="0"):
        return "newfolder"


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeClient()
        patcher = mock.patch.object(server, "_client", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, name, arguments=None, read_only=False):
        return server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments or {}}},
                             read_only=read_only)

    def test_initialize_reports_name_and_version(self):
        result = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(result["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(result["result"]["serverInfo"]["name"], server.SERVER_NAME)
        self.assertEqual(result["result"]["serverInfo"]["version"], __version__)

    def test_initialize_echoes_client_protocol_version(self):
        result = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"protocolVersion": "2099-01-01"}})
        self.assertEqual(result["result"]["protocolVersion"], "2099-01-01")

    def test_notifications_get_no_response(self):
        self.assertIsNone(server.handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_ping(self):
        result = server.handle({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(result["result"], {})

    def test_unknown_method(self):
        result = server.handle({"jsonrpc": "2.0", "id": 1, "method": "no/such"})
        self.assertEqual(result["error"]["code"], -32601)

    def test_tools_list(self):
        result = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = [t["name"] for t in result["result"]["tools"]]
        self.assertIn("mubu_list", names)
        self.assertIn("mubu_create_doc", names)

    def test_read_only_hides_write_tools(self):
        result = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                               read_only=True)
        names = [t["name"] for t in result["result"]["tools"]]
        self.assertNotIn("mubu_create_doc", names)
        self.assertNotIn("mubu_create_folder", names)
        self.assertIn("mubu_get_doc", names)

    def test_read_only_blocks_write_call(self):
        result = self.call("mubu_create_doc", {"name": "x", "markdown": "- a"}, read_only=True)
        self.assertEqual(result["error"]["code"], -32602)
        self.assertIn("只读", result["error"]["message"])

    def test_whoami(self):
        result = self.call("mubu_whoami")
        self.assertFalse(result["result"]["isError"])
        self.assertIn("测试账号", result["result"]["content"][0]["text"])

    def test_list_output(self):
        text = self.call("mubu_list", {"folder_id": "0"})["result"]["content"][0]["text"]
        self.assertIn("文件夹", text)
        self.assertIn("id=f1", text)
        self.assertEqual(self.fake.folder_id, "0")

    def test_list_empty_subfolder(self):
        text = self.call("mubu_list", {"folder_id": "f1"})["result"]["content"][0]["text"]
        self.assertIn("共 0 个文件夹、0 篇文档", text)

    def test_get_doc_markdown_and_json(self):
        md = self.call("mubu_get_doc", {"doc_id": "d1"})["result"]["content"][0]["text"]
        self.assertIn("# 标题", md)
        raw = self.call("mubu_get_doc", {"doc_id": "d1", "format": "json"})
        self.assertIn("nodes", raw["result"]["content"][0]["text"])

    def test_get_doc_requires_id(self):
        result = self.call("mubu_get_doc", {})
        self.assertTrue(result["result"]["isError"])
        self.assertIn("doc_id", result["result"]["content"][0]["text"])

    def test_create_doc_sends_node_tree(self):
        result = self.call("mubu_create_doc",
                           {"name": "新文档", "markdown": "# 新文档\n- 要点\n"})
        self.assertFalse(result["result"]["isError"])
        name, nodes, folder = self.fake.last_import
        self.assertEqual(name, "新文档")
        self.assertEqual(folder, "0")
        self.assertEqual(nodes[0]["text"], "新文档")
        self.assertEqual(nodes[0]["children"][0]["text"], "要点")

    def test_search_finds_by_name(self):
        text = self.call("mubu_search", {"keyword": "文档"})["result"]["content"][0]["text"]
        self.assertIn("找到 1 条", text)
        self.assertIn("id=d1", text)

    def test_search_no_hit(self):
        text = self.call("mubu_search", {"keyword": "不存在的东西"})["result"]["content"][0]["text"]
        self.assertIn("没有找到", text)

    def test_missing_credentials_is_reported_not_raised(self):
        from mubu_web_mcp.credentials import MissingCredentials
        with mock.patch.object(self.fake, "whoami", side_effect=MissingCredentials("没凭据")):
            result = self.call("mubu_whoami")
        self.assertTrue(result["result"]["isError"])
        self.assertIn("凭据未配置", result["result"]["content"][0]["text"])

    def test_unknown_tool(self):
        result = self.call("mubu_nope")
        self.assertEqual(result["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
