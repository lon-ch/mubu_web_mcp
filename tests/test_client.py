"""客户端测试：请求报文、错误码映射、重试、token 缓存（全部用假响应，不联网）。"""

import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import temp_dir  # noqa: E402
from mubu_web_mcp import mubu_client  # noqa: E402
from mubu_web_mcp.mubu_client import AuthError, MubuClient, MubuError  # noqa: E402


class FakeResponse:
    def __init__(self, payload, status=200):
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_urlopen(payloads):
    """按顺序返回预设响应；元素可以是 dict / (dict, status) / Exception。"""
    calls = []

    def _urlopen(request, timeout=None):
        calls.append(request)
        payload = payloads.pop(0) if payloads else {"code": 0, "data": {}}
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, tuple):
            return FakeResponse(payload[0], status=payload[1])
        return FakeResponse(payload)

    return _urlopen, calls


def body_of(request):
    return json.loads(request.data.decode("utf-8"))


def header_of(request, name):
    for key, value in request.headers.items():
        if key.lower() == name.lower():
            return value
    return None


LOGIN = {"code": 0, "data": {"token": "T", "id": 42, "name": "小明", "memberId": "M"}}


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.home = temp_dir(self)
        patcher = mock.patch.multiple(mubu_client, MUBU_HOME=self.home,
                                     TOKEN_FILE=self.home / "token.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        # 测试里不需要真的限速
        env = mock.patch.dict(os.environ, {"MUBU_MIN_INTERVAL_MS": "0"})
        env.start()
        self.addCleanup(env.stop)

    def client(self):
        return MubuClient(phone="13800000000", password="pw")

    def test_login_payload_and_headers(self):
        urlopen, calls = make_urlopen([LOGIN])
        with mock.patch("urllib.request.urlopen", urlopen):
            self.client().ensure_token(force=True)
        request = calls[0]
        self.assertTrue(request.full_url.endswith("/user/phone_login"))
        self.assertEqual(body_of(request), {
            "phone": "13800000000", "password": "pw", "callbackType": 0})
        for header in ("data-unique-id", "x-session-id", "x-request-id",
                       "x-reg-entrance", "Origin", "Referer"):
            self.assertIsNotNone(header_of(request, header), header)

    def test_authenticated_request_carries_token_header(self):
        urlopen, calls = make_urlopen([LOGIN, {"code": 0, "data": {"folders": []}}])
        with mock.patch("urllib.request.urlopen", urlopen):
            self.client().list_dir("0")
        self.assertEqual(header_of(calls[1], "Jwt-Token"), "T")

    def test_token_is_cached_to_disk(self):
        urlopen, calls = make_urlopen([LOGIN, {"code": 0, "data": {}}])
        with mock.patch("urllib.request.urlopen", urlopen):
            self.client().list_dir("0")
        self.assertEqual(len(calls), 2)
        cached = json.loads((self.home / "token.json").read_text(encoding="utf-8"))
        self.assertEqual(cached["token"], "T")
        self.assertEqual(cached["user_id"], "42")

    def test_cached_token_is_reused_without_login(self):
        urlopen, calls = make_urlopen([LOGIN])
        with mock.patch("urllib.request.urlopen", urlopen):
            info = self.client().whoami()
        self.assertEqual(len(calls), 1)
        self.assertEqual(info["user_id"], "42")

        urlopen2, calls2 = make_urlopen([])
        with mock.patch("urllib.request.urlopen", urlopen2):
            again = MubuClient().whoami()
        self.assertEqual(calls2, [])
        self.assertEqual(again["name"], "小明")

    def test_expired_token_triggers_relogin_once(self):
        urlopen, calls = make_urlopen([
            LOGIN,
            {"code": 2, "msg": "Login Expired"},
            LOGIN,
            {"code": 0, "data": {"folders": []}},
        ])
        with mock.patch("urllib.request.urlopen", urlopen):
            result = self.client().list_dir("0")
        self.assertEqual(result, {"folders": []})
        self.assertEqual(len(calls), 4)

    def test_login_failure_maps_to_auth_error(self):
        urlopen, _ = make_urlopen([{"code": 1204, "msg": "phone or password error"}])
        with mock.patch("urllib.request.urlopen", urlopen):
            with self.assertRaises(AuthError) as ctx:
                self.client().whoami()
        self.assertIn("手机号或密码不正确", str(ctx.exception))

    def test_illegal_request_includes_hint(self):
        urlopen, _ = make_urlopen([LOGIN, {"code": 17, "msg": "illegal request"}])
        with mock.patch("urllib.request.urlopen", urlopen):
            with self.assertRaises(MubuError) as ctx:
                self.client().list_dir("0")
        self.assertEqual(ctx.exception.code, 17)
        self.assertIn("缺少必要的请求头", str(ctx.exception))

    def test_permission_error_includes_hint(self):
        urlopen, _ = make_urlopen([LOGIN, {"code": 6, "msg": "Permission error"}])
        with mock.patch("urllib.request.urlopen", urlopen):
            with self.assertRaises(MubuError) as ctx:
                self.client().get_doc("d1")
        self.assertIn("没有权限", str(ctx.exception))

    def test_retries_on_server_error(self):
        urlopen, calls = make_urlopen([
            LOGIN,
            ({"code": 0}, 503),
            {"code": 0, "data": {"folders": []}},
        ])
        with mock.patch("urllib.request.urlopen", urlopen), mock.patch("time.sleep"):
            result = self.client().list_dir("0")
        self.assertEqual(result, {"folders": []})
        self.assertEqual(len(calls), 3)

    def test_retries_on_network_error(self):
        urlopen, calls = make_urlopen([LOGIN, urllib.error.URLError("boom"),
                                       {"code": 0, "data": {}}])
        with mock.patch("urllib.request.urlopen", urlopen), mock.patch("time.sleep"):
            self.client().list_dir("0")
        self.assertEqual(len(calls), 3)

    def test_network_error_eventually_raises(self):
        urlopen, calls = make_urlopen([LOGIN, urllib.error.URLError("x"),
                                       urllib.error.URLError("x"),
                                       urllib.error.URLError("x")])
        with mock.patch("urllib.request.urlopen", urlopen), mock.patch("time.sleep"):
            with self.assertRaises(MubuError):
                self.client().list_dir("0")
        self.assertEqual(len(calls), 4)

    def test_non_json_response_raises(self):
        class Raw:
            status = 200

            def read(self):
                return b"<html>oops</html>"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch("urllib.request.urlopen", lambda *a, **k: Raw()):
            with self.assertRaises(MubuError):
                self.client().ensure_token(force=True)

    def test_import_doc_payload(self):
        urlopen, calls = make_urlopen([LOGIN, {"code": 0, "data": {"id": "D1"}}])
        nodes = [{"id": "root", "text": "标题",
                  "children": [{"id": "n1", "text": "要点", "children": []}]}]
        with mock.patch("urllib.request.urlopen", urlopen):
            doc_id = self.client().import_doc("标题", nodes, folder_id="F1")
        self.assertEqual(doc_id, "D1")
        payload = body_of(calls[1])
        self.assertEqual(payload["name"], "标题")
        self.assertEqual(payload["folderId"], "F1")
        self.assertEqual(payload["itemCount"], 2)
        self.assertEqual(json.loads(payload["define"]), {"nodes": nodes})

    def test_create_doc_does_not_send_content(self):
        """幕布会忽略 content，所以我们干脆不发它。"""
        urlopen, calls = make_urlopen([LOGIN, {"code": 0, "data": {"id": "D2"}}])
        with mock.patch("urllib.request.urlopen", urlopen):
            self.assertEqual(self.client().create_doc("空文档", folder_id="F1"), "D2")
        self.assertNotIn("content", body_of(calls[1]))

    def test_refuses_non_mubu_host(self):
        client = self.client()
        with mock.patch.object(mubu_client, "API_BASE", "https://evil.example.com/v3/api"):
            with self.assertRaises(MubuError) as ctx:
                client._post("/list/get", {})
        self.assertIn("拒绝向非幕布域名", str(ctx.exception))

    def test_doc_tree_parses_definition(self):
        definition = {"nodes": [{"text": "T", "children": []}]}
        self.assertEqual(MubuClient.doc_tree({"definition": json.dumps(definition)}), definition)
        self.assertEqual(MubuClient.doc_tree({"definition": "not json"}), {})
        self.assertEqual(MubuClient.doc_tree({"definition": definition}), definition)
        self.assertEqual(MubuClient.doc_tree({}), {})

    def test_throttle_sleeps_only_when_needed(self):
        client = self.client()
        client.min_interval = 0.5
        client._last_request_at = 100.0
        with mock.patch("time.sleep") as sleeper, \
                mock.patch("time.time", return_value=100.0):
            client._throttle()
        sleeper.assert_called_once()

        client._last_request_at = 90.0
        with mock.patch("time.sleep") as sleeper, \
                mock.patch("time.time", return_value=100.0):
            client._throttle()
        sleeper.assert_not_called()


if __name__ == "__main__":
    unittest.main()
