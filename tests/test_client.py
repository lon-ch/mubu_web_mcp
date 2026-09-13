"""客户端测试：请求报文、限速、错误分类、重试、token 存储、取消（全部离线）。"""

import json
import os
import sys
import unittest
import urllib.error
from email.utils import formatdate
from pathlib import Path
from threading import Event
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import temp_dir  # noqa: E402

from mubu_web_mcp import credentials, mubu_client  # noqa: E402
from mubu_web_mcp.mubu_client import (  # noqa: E402
    AuthError,
    CancelledError,
    MubuClient,
    MubuError,
    RateLimitError,
)


class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self._body = (payload if isinstance(payload, bytes)
                      else json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        self.status = status
        self.headers = headers or {}

    def read(self):
        return self._body

    def geturl(self):
        return "https://api2.mubu.com/v3/api"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_urlopen(payloads):
    """按顺序返回预设响应；元素可以是 dict / (dict, status, headers) / Exception。"""
    calls = []

    def _urlopen(request, timeout=None):
        calls.append(request)
        payload = payloads.pop(0) if payloads else {"code": 0, "data": {}}
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, tuple):
            body, status = payload[0], payload[1]
            headers = payload[2] if len(payload) > 2 else {}
            return FakeResponse(body, status=status, headers=headers)
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


class ClientTestCase(unittest.TestCase):
    """公共夹具：临时家目录 + 关闭真实系统凭据后端 + 关闭真实等待。"""

    def setUp(self):
        self.home = temp_dir(self)
        patcher = mock.patch.object(credentials, "MUBU_HOME", self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("_is_windows", "_keychain_available", "_secret_tool_available"):
            patcher = mock.patch.object(credentials, name, return_value=False)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {
            "MUBU_MIN_INTERVAL_MS": "0",
            "MUBU_JITTER_MS": "0",
            "MUBU_PROCESS_LOCK": "0",
        })
        env.start()
        self.addCleanup(env.stop)

    def client(self, **kwargs):
        return MubuClient(phone="13800000000", password="pw", **kwargs)


class RequestTests(ClientTestCase):
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

    def test_token_is_stored_in_secret_store(self):
        urlopen, calls = make_urlopen([LOGIN, {"code": 0, "data": {}}])
        with mock.patch("urllib.request.urlopen", urlopen):
            self.client().list_dir("0")
        self.assertEqual(len(calls), 2)
        stored = json.loads(credentials.load_secret(credentials.TOKEN_SLOT))
        self.assertEqual(stored["token"], "T")
        self.assertEqual(stored["user_id"], "42")

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

    def test_expired_token_triggers_single_relogin(self):
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

    def test_persistent_auth_error_does_not_loop(self):
        urlopen, calls = make_urlopen([
            LOGIN, {"code": 2, "msg": "Login Expired"},
            LOGIN, {"code": 2, "msg": "Login Expired"},
        ])
        with mock.patch("urllib.request.urlopen", urlopen):
            with self.assertRaises(AuthError):
                self.client().list_dir("0")
        self.assertEqual(len(calls), 4)  # 只重登一次

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

    def test_non_json_response_raises(self):
        with mock.patch("urllib.request.urlopen",
                        lambda *a, **k: FakeResponse(b"<html>oops</html>")):
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
        urlopen, calls = make_urlopen([LOGIN, {"code": 0, "data": {"id": "D2"}}])
        with mock.patch("urllib.request.urlopen", urlopen):
            self.assertEqual(self.client().create_doc("空文档", folder_id="F1"), "D2")
        self.assertNotIn("content", body_of(calls[1]))


class UrlGuardTests(ClientTestCase):
    def test_refuses_other_host(self):
        client = self.client()
        with mock.patch.object(mubu_client, "API_BASE", "https://evil.example.com/v3/api"):
            with self.assertRaises(MubuError) as ctx:
                client._post("/list/get", {})
        self.assertIn("拒绝向非幕布域名", str(ctx.exception))

    def test_refuses_lookalike_host(self):
        """严格 hostname 校验：后缀相同的域名也要拒绝。"""
        client = self.client()
        with mock.patch.object(mubu_client, "API_BASE",
                               "https://api2.mubu.com.evil.example/v3/api"):
            with self.assertRaises(MubuError):
                client._post("/list/get", {})

    def test_allows_exact_host_only(self):
        MubuClient._assert_mubu_url("https://api2.mubu.com/v3/api/list/get")
        for bad in ("http://api2.mubu.com/v3/api", "https://api2.mubu.com:8443/x",
                    "https://mubu.com/v3/api", "https://evil.com/x"):
            with self.assertRaises(MubuError):
                MubuClient._assert_mubu_url(bad)


class RetryTests(ClientTestCase):
    def test_retries_on_server_error_then_succeeds(self):
        urlopen, calls = make_urlopen([
            LOGIN, ({"code": 0}, 503), {"code": 0, "data": {"folders": []}}])
        with mock.patch("urllib.request.urlopen", urlopen), \
                mock.patch.object(MubuClient, "_sleep"):
            result = self.client().list_dir("0")
        self.assertEqual(result, {"folders": []})
        self.assertEqual(len(calls), 3)

    def test_server_error_exhausts_retries(self):
        urlopen, calls = make_urlopen([
            LOGIN, ({"code": 0}, 500), ({"code": 0}, 500), ({"code": 0}, 500)])
        with mock.patch("urllib.request.urlopen", urlopen), \
                mock.patch.object(MubuClient, "_sleep"):
            with self.assertRaises(MubuError):
                self.client().list_dir("0")
        self.assertEqual(len(calls), 4)

    def test_network_error_retries_then_raises(self):
        urlopen, calls = make_urlopen([LOGIN, urllib.error.URLError("x"),
                                       urllib.error.URLError("x"),
                                       urllib.error.URLError("x")])
        with mock.patch("urllib.request.urlopen", urlopen), \
                mock.patch.object(MubuClient, "_sleep"):
            with self.assertRaises(MubuError):
                self.client().list_dir("0")
        self.assertEqual(len(calls), 4)

    def test_429_respects_retry_after(self):
        urlopen, calls = make_urlopen([
            LOGIN,
            ({"code": 0}, 429, {"Retry-After": "7"}),
            {"code": 0, "data": {"folders": []}},
        ])
        waits = []
        with mock.patch("urllib.request.urlopen", urlopen), \
                mock.patch.object(MubuClient, "_sleep", side_effect=waits.append):
            result = self.client().list_dir("0")
        self.assertEqual(result, {"folders": []})
        self.assertEqual(len(calls), 3)
        self.assertGreaterEqual(waits[0], 7.0)

    def test_429_exhausted_raises_rate_limit_error(self):
        urlopen, calls = make_urlopen([
            LOGIN,
            ({"code": 0}, 429, {"Retry-After": "1"}),
            ({"code": 0}, 429, {"Retry-After": "1"}),
            ({"code": 0}, 429, {"Retry-After": "1"}),
        ])
        with mock.patch("urllib.request.urlopen", urlopen), \
                mock.patch.object(MubuClient, "_sleep"):
            with self.assertRaises(RateLimitError) as ctx:
                self.client().list_dir("0")
        self.assertEqual(ctx.exception.retry_after, 1.0)

    def test_retry_after_http_date_is_parsed(self):
        future = formatdate(usegmt=True)
        self.assertIsNotNone(MubuClient._parse_retry_after(future))
        self.assertEqual(MubuClient._parse_retry_after("12"), 12.0)
        self.assertIsNone(MubuClient._parse_retry_after("not-a-date"))

    def test_business_rate_limit_in_http_200(self):
        urlopen, calls = make_urlopen([
            LOGIN,
            {"code": 9999, "msg": "操作太频繁，请稍后再试"},
            {"code": 9999, "msg": "操作太频繁，请稍后再试"},
            {"code": 9999, "msg": "操作太频繁，请稍后再试"},
        ])
        with mock.patch("urllib.request.urlopen", urlopen), \
                mock.patch.object(MubuClient, "_sleep"):
            with self.assertRaises(RateLimitError):
                self.client().list_dir("0")
        self.assertEqual(len(calls), 4)

    def test_configurable_rate_limit_code(self):
        urlopen, _ = make_urlopen([
            LOGIN, {"code": 1001, "msg": "slow down"},
            {"code": 1001, "msg": "slow down"},
            {"code": 1001, "msg": "slow down"},
        ])
        client = self.client()
        client.rate_limit_codes = frozenset({1001})
        with mock.patch("urllib.request.urlopen", urlopen), \
                mock.patch.object(MubuClient, "_sleep"):
            with self.assertRaises(RateLimitError):
                client.list_dir("0")

    def test_backoff_is_capped(self):
        client = self.client()
        client.max_backoff = 5.0
        client.jitter = 0.0
        self.assertEqual(client._backoff(10, None), 5.0)
        self.assertEqual(client._backoff(1, 100.0), 5.0)


class ThrottleTests(ClientTestCase):
    def test_throttle_uses_shared_stamp(self):
        client = self.client()
        client.min_interval = 1.0
        client.jitter = 0.0
        client.use_process_lock = False
        stamp = credentials.home_dir() / "rate.stamp"
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text("1000.0", encoding="utf-8")
        waits = []
        with mock.patch.object(MubuClient, "_sleep", side_effect=waits.append), \
                mock.patch("time.time", return_value=1000.2):
            client._throttle()
        self.assertEqual(len(waits), 1)
        self.assertAlmostEqual(waits[0], 0.8, places=3)

    def test_no_wait_when_enough_time_passed(self):
        client = self.client()
        client.min_interval = 1.0
        client.jitter = 0.0
        client.use_process_lock = False
        stamp = credentials.home_dir() / "rate.stamp"
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text("1000.0", encoding="utf-8")
        with mock.patch.object(MubuClient, "_sleep") as sleeper, \
                mock.patch("time.time", return_value=1005.0):
            client._throttle()
        sleeper.assert_not_called()


class CancellationTests(ClientTestCase):
    def test_cancelled_before_request(self):
        event = Event()
        event.set()
        client = self.client(cancel_event=event)
        with mock.patch("urllib.request.urlopen") as opener:
            with self.assertRaises(CancelledError):
                client.ensure_token(force=True)
        opener.assert_not_called()

    def test_cancel_interrupts_sleep(self):
        event = Event()
        client = self.client(cancel_event=event)
        event.set()
        with self.assertRaises(CancelledError):
            client._sleep(10)

    def test_cancel_during_retry_stops_loop(self):
        event = Event()
        client = self.client(cancel_event=event)
        calls = []

        def urlopen(request, timeout=None):
            calls.append(request)
            if len(calls) == 1:
                return FakeResponse(LOGIN)
            event.set()  # 第二个请求失败后，重试等待期间发现已取消
            raise urllib.error.URLError("boom")

        with mock.patch("urllib.request.urlopen", urlopen):
            with self.assertRaises(CancelledError):
                client.list_dir("0")
        self.assertEqual(len(calls), 2)


class DocTreeTests(ClientTestCase):
    def test_doc_tree_parses_definition(self):
        definition = {"nodes": [{"text": "T", "children": []}]}
        self.assertEqual(MubuClient.doc_tree({"definition": json.dumps(definition)}),
                         definition)
        self.assertEqual(MubuClient.doc_tree({"definition": "not json"}), {})
        self.assertEqual(MubuClient.doc_tree({"definition": definition}), definition)
        self.assertEqual(MubuClient.doc_tree({}), {})


if __name__ == "__main__":
    unittest.main()
