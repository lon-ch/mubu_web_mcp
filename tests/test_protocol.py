"""MCP 协议层测试：握手、结构化输出、分页、只读模式、脱敏（不联网）。"""

import json
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mubu_web_mcp import __version__, server  # noqa: E402
from mubu_web_mcp.credentials import MissingCredentials  # noqa: E402


def make_definition(*texts):
    return {"nodes": [{"id": f"d{i}", "text": text, "children": []}
                      for i, text in enumerate(texts)]}


class FakeClient:
    """替身客户端，不产生任何网络请求。"""

    def __init__(self):
        self.min_interval = 0.5
        self.jitter = 0.15
        self.max_retries = 2
        self.use_process_lock = True
        self.stats = {"requests": 3, "retries": 1, "rate_limit_waits": 0, "logins": 1}
        self.last_error_code = None
        self.redefinition = make_definition("标题", "第二节点", "第三节点")

    def whoami(self):
        return {"name": "测试账号", "user_id": "1", "member_id": None,
                "token_expires_in_seconds": 600}

    def list_dir(self, folder_id="0"):
        self.folder_id = folder_id
        if folder_id != "0":
            return {"folders": [], "documents": []}
        return {
            "folders": [{"id": "f1", "name": "文件夹", "folderId": "0", "updateTime": 123}],
            "documents": [{"id": "d1", "name": "文档", "folderId": "0",
                           "updateTime": 456, "seq": 2}],
        }

    def get_doc(self, doc_id):
        return {"definition": json.dumps(self.redefinition, ensure_ascii=False),
                "baseVersion": 7, "name": "文档", "author": {"id": 1}}

    @staticmethod
    def doc_tree(data):
        return json.loads(data["definition"])

    def import_doc(self, name, nodes, folder_id="0"):
        self.last_import = (name, nodes, folder_id)
        return "newdoc"

    def create_folder(self, name, folder_id="0"):
        return "newfolder"


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeClient()
        server._local.client = self.fake
        self.addCleanup(lambda: setattr(server._local, "client", None))

    def call(self, name, arguments=None, read_only=False):
        return server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments or {}}},
                             read_only=read_only)

    def structured(self, name, arguments=None, read_only=False):
        result = self.call(name, arguments, read_only)
        self.assertFalse(result["result"].get("isError"), result)
        self.assertIn("structuredContent", result["result"])
        return result["result"]["structuredContent"]

    # ---- 握手 ----

    def test_initialize(self):
        result = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(result["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(result["result"]["serverInfo"]["name"], server.SERVER_NAME)
        self.assertEqual(result["result"]["serverInfo"]["version"], __version__)

    def test_notifications_and_ping(self):
        self.assertIsNone(server.handle({"jsonrpc": "2.0",
                                         "method": "notifications/initialized"}))
        self.assertIsNone(server.handle({"jsonrpc": "2.0",
                                         "method": "notifications/cancelled",
                                         "params": {"requestId": 1}}))
        self.assertEqual(server.handle({"jsonrpc": "2.0", "id": 1,
                                        "method": "ping"})["result"], {})

    def test_unknown_method(self):
        result = server.handle({"jsonrpc": "2.0", "id": 1, "method": "no/such"})
        self.assertEqual(result["error"]["code"], -32601)

    # ---- 工具清单 ----

    def test_tools_list_has_structured_and_write_tools(self):
        names = [t["name"] for t in
                 server.handle({"jsonrpc": "2.0", "id": 1,
                                "method": "tools/list"})["result"]["tools"]]
        for expected in ("mubu_list", "mubu_get_doc", "mubu_get_doc_json",
                         "mubu_search", "mubu_inspect", "mubu_diagnostics",
                         "mubu_create_doc", "mubu_create_folder"):
            self.assertIn(expected, names)

    def test_structured_tools_declare_output_schema(self):
        tools = {t["name"]: t for t in
                 server.handle({"jsonrpc": "2.0", "id": 1,
                                "method": "tools/list"})["result"]["tools"]}
        for name in ("mubu_list", "mubu_get_doc_json", "mubu_inspect"):
            self.assertIn("outputSchema", tools[name], name)
            self.assertIn("properties", tools[name]["outputSchema"])

    def test_read_only_hides_write_tools(self):
        result = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                               read_only=True)
        names = [t["name"] for t in result["result"]["tools"]]
        self.assertNotIn("mubu_create_doc", names)
        self.assertNotIn("mubu_create_folder", names)
        self.assertIn("mubu_get_doc", names)

    def test_read_only_blocks_write_call(self):
        result = self.call("mubu_create_doc", {"name": "x", "markdown": "- a"},
                           read_only=True)
        self.assertEqual(result["error"]["code"], -32602)
        self.assertIn("只读", result["error"]["message"])

    def test_unknown_tool(self):
        self.assertEqual(self.call("mubu_nope")["error"]["code"], -32602)

    # ---- 结构化输出 ----

    def test_whoami(self):
        data = self.structured("mubu_whoami")
        self.assertEqual(data["name"], "测试账号")
        self.assertFalse(data["hasMemberId"])

    def test_list_structured_fields(self):
        data = self.structured("mubu_list", {"folder_id": "0"})
        self.assertEqual(data["folderId"], "0")
        folder = data["folders"][0]
        self.assertEqual(folder["id"], "f1")
        self.assertEqual(folder["parentId"], "0")
        self.assertEqual(folder["updatedAt"], 123)
        self.assertEqual(folder["type"], "folder")
        doc = data["documents"][0]
        self.assertEqual(doc["parentId"], "0")
        self.assertEqual(doc["order"], 2)
        self.assertEqual(doc["updateTime"], 456)  # 原始字段保留

    def test_list_text_still_available(self):
        text = self.call("mubu_list", {"folder_id": "0"})["result"]["content"][0]["text"]
        self.assertIn("id=f1", text)
        self.assertIn("id=d1", text)

    def test_get_doc_markdown(self):
        text = self.call("mubu_get_doc", {"doc_id": "d1"})["result"]["content"][0]["text"]
        self.assertIn("# 标题", text)

    def test_get_doc_json_is_complete_and_parseable(self):
        """回归测试：以前这里会对 JSON 做字符串截断，导致调用方拿到半截 JSON。"""
        self.fake.redefinition = make_definition(*[f"节点{i}" for i in range(4000)])
        result = self.call("mubu_get_doc", {"doc_id": "d1", "format": "json"})
        text = result["result"]["content"][0]["text"]
        parsed = json.loads(text)  # 必须能解析，不能是半截
        self.assertEqual(len(parsed["nodes"]), 4000)
        self.assertEqual(parsed["totalNodes"], 4000)
        self.assertEqual(result["result"]["structuredContent"]["totalNodes"], 4000)

    def test_get_doc_json_paging(self):
        first = self.structured("mubu_get_doc_json", {"doc_id": "d1", "limit": 2})
        self.assertEqual(first["totalTopLevelNodes"], 3)
        self.assertEqual(len(first["nodes"]), 2)
        self.assertTrue(first["hasMore"])
        self.assertEqual(first["nextCursor"], "2")

        second = self.structured("mubu_get_doc_json",
                                 {"doc_id": "d1", "cursor": first["nextCursor"], "limit": 2})
        self.assertEqual(len(second["nodes"]), 1)
        self.assertFalse(second["hasMore"])
        self.assertIsNone(second["nextCursor"])
        self.assertEqual(second["nodes"][0]["text"], "第三节点")

    def test_get_doc_json_metadata_preserved(self):
        data = self.structured("mubu_get_doc_json", {"doc_id": "d1"})
        self.assertEqual(data["baseVersion"], 7)
        self.assertEqual(data["metadata"]["name"], "文档")
        self.assertIn("author", data["metadata"])

    def test_get_doc_requires_id(self):
        result = self.call("mubu_get_doc", {})
        self.assertTrue(result["result"]["isError"])
        self.assertIn("doc_id", result["result"]["content"][0]["text"])

    # ---- 脱敏结构报告 ----

    def test_inspect_reports_fields_without_content(self):
        secret_text = "这是一段不该出现在报告里的正文"
        self.fake.redefinition = {"nodes": [{
            "id": "n1", "text": secret_text, "note": "秘密备注", "finish": True,
            "children": [{"id": "n2", "text": "子节点",
                          "img": "https://assets.mubu.com/a/b.png"}]}]}
        result = self.call("mubu_inspect", {"doc_id": "d1"})
        payload = json.dumps(result["result"], ensure_ascii=False)
        self.assertNotIn(secret_text, payload)
        self.assertNotIn("秘密备注", payload)
        data = result["result"]["structuredContent"]
        self.assertEqual(data["totalNodes"], 2)
        self.assertIn("img", data["nodeFieldCounts"])
        self.assertIn("img", data["urlFields"])
        self.assertIn("assets.mubu.com", data["urlFields"]["img"][0])

    def test_diagnostics_reports_counters_only(self):
        data = self.structured("mubu_diagnostics")
        self.assertEqual(data["requests"], 3)
        self.assertEqual(data["retries"], 1)
        self.assertEqual(data["minIntervalMs"], 500)
        self.assertNotIn("token", json.dumps(data).lower())

    # ---- 搜索 ----

    def test_search_structured_hits(self):
        data = self.structured("mubu_search", {"keyword": "文档"})
        self.assertEqual(data["hits"][0]["type"], "document")
        self.assertEqual(data["hits"][0]["id"], "d1")
        self.assertFalse(data["truncated"])

    def test_search_no_hit(self):
        result = self.call("mubu_search", {"keyword": "不存在"})
        self.assertIn("没有找到", result["result"]["content"][0]["text"])
        self.assertEqual(result["result"]["structuredContent"]["hits"], [])

    def test_search_requires_keyword(self):
        result = self.call("mubu_search", {})
        self.assertTrue(result["result"]["isError"])

    # ---- 写入 ----

    def test_create_doc_sends_node_tree(self):
        result = self.call("mubu_create_doc",
                           {"name": "新文档", "markdown": "# 新文档\n- 要点\n"})
        self.assertFalse(result["result"]["isError"])
        name, nodes, folder = self.fake.last_import
        self.assertEqual(name, "新文档")
        self.assertEqual(folder, "0")
        self.assertEqual(nodes[0]["text"], "新文档")
        self.assertEqual(nodes[0]["children"][0]["text"], "要点")
        self.assertEqual(result["result"]["structuredContent"]["docId"], "newdoc")

    # ---- 错误处理 ----

    def test_missing_credentials_is_reported_not_raised(self):
        with mock.patch.object(self.fake, "whoami", side_effect=MissingCredentials("没凭据")):
            result = self.call("mubu_whoami")
        self.assertTrue(result["result"]["isError"])
        self.assertIn("凭据未配置", result["result"]["content"][0]["text"])

    def test_cancelled_call_is_reported(self):
        from mubu_web_mcp.mubu_client import CancelledError
        with mock.patch.object(self.fake, "list_dir", side_effect=CancelledError("已取消")):
            result = self.call("mubu_list", {})
        self.assertTrue(result["result"]["isError"])
        self.assertIn("已取消", result["result"]["content"][0]["text"])


class ServeLoopTests(unittest.TestCase):
    """serve() 的并发与取消行为（不联网，直接喂 stdin）。"""

    def test_cancel_notification_sets_event(self):
        events = {}

        def fake_handle(request, read_only=False, cancel_event=None):
            events["event"] = cancel_event
            # 模拟一个进行中的请求：等取消通知把它唤醒
            cancel_event.wait(timeout=5)
            return {"jsonrpc": "2.0", "id": request.get("id"), "result": {}}

        payload = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
                        "params": {"requestId": 1}}),
        ]) + "\n"
        with mock.patch.object(server, "handle", fake_handle), \
                mock.patch("sys.stdin", __import__("io").StringIO(payload)), \
                mock.patch("sys.stdout", __import__("io").StringIO()):
            server.serve()
        self.assertIsInstance(events.get("event"), threading.Event)
        self.assertTrue(events["event"].is_set())


if __name__ == "__main__":
    unittest.main()
