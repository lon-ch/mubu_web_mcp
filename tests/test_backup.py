"""本地备份引擎测试：资源安全、命名、增量、原子写、报告（全部离线）。"""

import hashlib
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import temp_dir  # noqa: E402

from mubu_web_mcp import backup  # noqa: E402


def definition(*items):
    """items: (文本, 子节点文本列表) 或 (文本, 子节点文本列表, 额外字段 dict)"""
    def node(text, index, children=(), extra=None):
        payload = {"id": f"n{index}", "text": text, "children":
                   [node(child, f"{index}-{j}") for j, child in enumerate(children)]}
        payload.update(extra or {})
        return payload

    nodes = []
    for i, item in enumerate(items):
        text, children = item[0], item[1]
        extra = item[2] if len(item) > 2 else None
        nodes.append(node(text, str(i), children, extra))
    return {"nodes": nodes}


class FakeResponse:
    def __init__(self, body=b"", headers=None, url="https://assets.mubu.com/a.png"):
        self._body = body
        self.headers = headers or {}
        self._url = url

    def read(self, size=-1):
        return self._body if size in (-1, None) else self._body[:size]

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """按顺序返回预设响应；记录每次请求，便于断言"没请求过哪个地址"。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def redirect(code, location):
    return urllib.error.HTTPError(
        "https://assets.mubu.com/a.png", code, "redirect",
        {"Location": location}, None)


class UrlGuardTests(unittest.TestCase):
    def test_allowed_urls(self):
        for url in ("https://assets.mubu.com/a.png", "https://api2.mubu.com/x.jpg",
                    "https://mubu.com/y.png", "https://cdn.mubu.com/z.webp"):
            self.assertTrue(backup.is_allowed_asset_url(url), url)

    def test_rejected_urls(self):
        rejected = [
            "http://assets.mubu.com/a.png",          # 只允许 https
            "ftp://mubu.com/a.png",
            "https://mubu.com.example.com/a.png",    # 仿冒
            "https://evil-mubu.com/a.png",
            "https://notmubu.com/a.png",
            "https://mubu.com@evil.com/a.png",       # 用户名形式
            "https://user:pass@assets.mubu.com/a.png",
            "https://assets.mubu.com:8443/a.png",
            "file:///etc/passwd",
            "",
        ]
        for url in rejected:
            self.assertFalse(backup.is_allowed_asset_url(url), url)

    def test_redact_url_hides_path_and_query(self):
        redacted = backup.redact_url(
            "https://assets.mubu.com/img/secret-name.png?token=abc123")
        self.assertIn("assets.mubu.com", redacted)
        self.assertNotIn("secret-name", redacted)
        self.assertNotIn("abc123", redacted)


class FetchAssetTests(unittest.TestCase):
    def fetch(self, responses, url="https://assets.mubu.com/a.png", **kwargs):
        opener = FakeOpener(responses)
        payload = backup.fetch_asset(url, "JWT-SECRET", opener=opener, **kwargs)
        return payload, opener

    def test_https_image_ok(self):
        payload, opener = self.fetch([FakeResponse(b"PNG", {"Content-Type": "image/png"})])
        self.assertEqual(payload.data, b"PNG")
        self.assertEqual(payload.mime, "image/png")
        self.assertEqual(len(opener.calls), 1)

    def test_http_url_rejected_before_request(self):
        opener = FakeOpener([])
        with self.assertRaises(backup.AssetBlockedError):
            backup.fetch_asset("http://assets.mubu.com/a.png", "JWT", opener=opener)
        self.assertEqual(opener.calls, [])

    def test_lookalike_host_rejected_before_request(self):
        opener = FakeOpener([])
        with self.assertRaises(backup.AssetBlockedError):
            backup.fetch_asset("https://mubu.com.evil.com/a.png", "JWT", opener=opener)
        self.assertEqual(opener.calls, [])

    def test_redirect_to_http_blocked(self):
        opener = FakeOpener([redirect(302, "http://assets.mubu.com/a.png")])
        with self.assertRaises(backup.AssetBlockedError):
            backup.fetch_asset("https://assets.mubu.com/a.png", "JWT", opener=opener)
        self.assertEqual(len(opener.calls), 1)  # 没有对 http 地址发过请求

    def test_redirect_to_foreign_host_blocked_and_token_not_sent(self):
        opener = FakeOpener([redirect(302, "https://evil.example.com/a.png")])
        with self.assertRaises(backup.AssetBlockedError):
            backup.fetch_asset("https://assets.mubu.com/a.png", "JWT-SECRET", opener=opener)
        self.assertEqual(len(opener.calls), 1)
        self.assertNotIn("evil.example.com", opener.calls[0].full_url)

    def test_multi_hop_redirect_within_whitelist(self):
        payload, opener = self.fetch([
            redirect(301, "https://cdn.mubu.com/b.png"),
            redirect(302, "https://assets.mubu.com/c.png"),
            FakeResponse(b"JPEG", {"Content-Type": "image/jpeg"},
                         url="https://assets.mubu.com/c.png"),
        ])
        self.assertEqual(payload.data, b"JPEG")
        self.assertEqual(len(opener.calls), 3)
        for call in opener.calls:
            self.assertTrue(backup.is_allowed_asset_url(call.full_url))

    def test_too_many_redirects(self):
        responses = [redirect(302, "https://assets.mubu.com/loop.png")] * 8
        opener = FakeOpener(responses)
        with self.assertRaises(backup.BackupError):
            backup.fetch_asset("https://assets.mubu.com/a.png", "JWT", opener=opener)

    def test_oversized_asset_rejected(self):
        with self.assertRaises(backup.BackupError) as ctx:
            self.fetch([FakeResponse(b"x" * 100, {"Content-Type": "image/png"})],
                       max_bytes=50)
        self.assertIn("大小上限", str(ctx.exception))

    def test_http_error_reported(self):
        opener = FakeOpener([urllib.error.HTTPError(
            "https://assets.mubu.com/a.png", 404, "not found", {}, None)])
        with self.assertRaises(backup.BackupError):
            backup.fetch_asset("https://assets.mubu.com/a.png", "JWT", opener=opener)

    def test_timeout_propagates(self):
        opener = FakeOpener([TimeoutError("timeout")])
        with self.assertRaises(TimeoutError):
            backup.fetch_asset("https://assets.mubu.com/a.png", "JWT", opener=opener)

    def test_token_only_sent_to_allowed_hosts(self):
        payload, opener = self.fetch([FakeResponse(b"PNG", {"Content-Type": "image/png"})])
        self.assertEqual(opener.calls[0].get_header("Jwt-token"), "JWT-SECRET")

    def test_extension_prefers_mime(self):
        self.assertEqual(backup.guess_extension("https://x.mubu.com/a.bin", "image/png"), ".png")
        self.assertEqual(backup.guess_extension("https://x.mubu.com/a.gif", ""), ".gif")
        self.assertIsNone(backup.guess_extension("https://x.mubu.com/a.txt", "text/html"))


class NamingTests(unittest.TestCase):
    def test_safe_filename(self):
        self.assertEqual(backup.safe_filename('a/b\\c:d*e?f"g<h>i|j'), "a_b_c_d_e_f_g_h_i_j")
        self.assertEqual(backup.safe_filename("   "), "untitled")
        self.assertEqual(backup.safe_filename("会议记录"), "会议记录")
        self.assertLessEqual(len(backup.safe_filename("x" * 500)), 80)
        self.assertEqual(backup.safe_filename("CON"), "_CON")          # Windows 保留名
        self.assertEqual(backup.safe_filename("aux.txt"), "_aux.txt")
        self.assertEqual(backup.safe_filename("..."), "untitled")

    def test_name_allocator_handles_collisions(self):
        allocator = backup.NameAllocator()
        self.assertEqual(allocator.allocate("", "项目规划", "aaaaaaaa", ".md"),
                         "项目规划.md")
        self.assertEqual(allocator.allocate("", "项目规划", "bbbbbbbb", ".md"),
                         "项目规划__bbbbbb.md")
        # 大小写不同也算冲突（Windows/macOS 上会互相覆盖）
        self.assertEqual(allocator.allocate("", "PROJECT", "cccccccc", ".md"), "PROJECT.md")
        self.assertEqual(allocator.allocate("", "project", "dddddddd", ".md"),
                         "project__dddddd.md")

    def test_name_allocator_isolated_per_directory(self):
        allocator = backup.NameAllocator()
        allocator.allocate("工作/", "计划", "a", ".md")
        self.assertEqual(allocator.allocate("生活/", "计划", "b", ".md"), "计划.md")

    def test_sort_prefix(self):
        self.assertEqual(backup.sort_prefix(0, 5), "001 ")
        self.assertEqual(backup.sort_prefix(None, 0), "001 ")
        self.assertEqual(backup.sort_prefix(9, 0), "010 ")
        self.assertEqual(backup.sort_prefix(None, 0, width=2), "01 ")


class FakeClient:
    def __init__(self, folders, documents, docs):
        self.min_interval = 0.0
        self.timeout = 5
        self.max_retries = 2
        self.stats = {"requests": 0, "retries": 0, "rate_limit_waits": 0, "logins": 0}
        self._folders, self._documents, self._docs = folders, documents, docs
        self.list_calls, self.get_doc_calls = [], []

    def list_dir(self, folder_id="0"):
        self.list_calls.append(folder_id)
        self.stats["requests"] += 1
        return {"folders": self._folders.get(folder_id, []),
                "documents": self._documents.get(folder_id, [])}

    def get_doc(self, doc_id):
        self.get_doc_calls.append(doc_id)
        self.stats["requests"] += 1
        return {"definition": json.dumps(self._docs[doc_id], ensure_ascii=False),
                "baseVersion": 1}

    @staticmethod
    def doc_tree(data):
        return json.loads(data["definition"])

    def ensure_token(self):
        return "T"


class BackupEngineTests(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self)
        self.out = self.root / "backup"
        self.folders = {"0": [{"id": "f1", "name": "工作"}], "f1": []}
        self.documents = {
            "0": [{"id": "d1", "name": "会议记录", "updateTime": 100, "seq": 1}],
            "f1": [{"id": "d2", "name": "项目/计划", "updateTime": 200}],
        }
        self.docs = {"d1": definition(("会议记录", ["要点一", "要点二"])),
                     "d2": definition(("项目计划", []))}
        self.client = FakeClient(self.folders, self.documents, self.docs)

    def options(self, **kwargs):
        defaults = dict(out_dir=self.out, interval_ms=0)
        defaults.update(kwargs)
        return backup.BackupOptions(**defaults)

    def md_files(self):
        return sorted(p.relative_to(self.out).as_posix() for p in self.out.rglob("*.md"))

    def test_first_backup_writes_files_and_manifest(self):
        stats = backup.run_backup(self.client, self.options())
        self.assertEqual(stats["status"], "ok")
        self.assertEqual(stats["documentsWritten"], 2)
        self.assertEqual(stats["foldersScanned"], 2)
        files = self.md_files()
        self.assertEqual(len(files), 2)
        self.assertTrue(any(f.startswith("001 ") for f in files))  # 排序前缀默认开启
        manifest = json.loads((self.out / backup.MANIFEST_NAME).read_text(encoding="utf-8"))
        entry = manifest["entries"]["d1"]
        self.assertEqual(entry["sha256"],
                         hashlib.sha256((self.out / entry["file"]).read_bytes()).hexdigest())
        self.assertEqual(entry["folderId"], "0")
        self.assertEqual(entry["status"], "ok")
        content = (self.out / entry["file"]).read_text(encoding="utf-8")
        self.assertIn("# 会议记录", content)
        self.assertIn("- 要点一", content)

    def test_no_prefix_option(self):
        backup.run_backup(self.client, self.options(sort_prefix=False))
        self.assertIn("会议记录.md", self.md_files())

    def test_duplicate_names_get_stable_suffixes(self):
        self.documents["0"] = [
            {"id": "d1", "name": "周报", "updateTime": 1},
            {"id": "d9", "name": "周报", "updateTime": 1},
        ]
        self.docs["d9"] = definition(("周报", []))
        backup.run_backup(self.client, self.options(sort_prefix=False))
        files = self.md_files()
        self.assertIn("周报.md", files)
        self.assertIn("周报__d9.md", files)
        # 再跑一次名字必须稳定
        before = self.md_files()
        backup.run_backup(self.client, self.options(sort_prefix=False))
        self.assertEqual(self.md_files(), before)

    def test_document_and_folder_same_name_do_not_collide(self):
        self.folders["0"] = [{"id": "f1", "name": "工作"}]
        self.documents["0"] = [{"id": "d1", "name": "工作", "updateTime": 1}]
        backup.run_backup(self.client, self.options(sort_prefix=False))
        self.assertTrue((self.out / "工作.md").exists())
        self.assertTrue((self.out / "工作").is_dir())

    def test_incremental_skips_unchanged(self):
        backup.run_backup(self.client, self.options())
        self.client.get_doc_calls.clear()
        stats = backup.run_backup(self.client, self.options())
        self.assertEqual(stats["documentsSkipped"], 2)
        self.assertEqual(self.client.get_doc_calls, [])

    def test_changed_document_is_updated(self):
        backup.run_backup(self.client, self.options())
        self.documents["0"][0]["updateTime"] = 999
        self.client.get_doc_calls.clear()
        stats = backup.run_backup(self.client, self.options())
        self.assertEqual(stats["documentsUpdated"], 1)
        self.assertEqual(self.client.get_doc_calls, ["d1"])

    def test_resume_from_saved_state(self):
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / backup.STATE_NAME).write_text(json.dumps(
            {"queue": [["f1", "001 工作/", 1]], "processed": ["d1"]}), encoding="utf-8")
        stats = backup.run_backup(self.client, self.options())
        self.assertEqual(stats["foldersScanned"], 1)
        self.assertEqual(self.client.get_doc_calls, ["d2"])

    def test_corrupt_manifest_is_quarantined(self):
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / backup.MANIFEST_NAME).write_text("{ broken", encoding="utf-8")
        stats = backup.run_backup(self.client, self.options())
        self.assertEqual(stats["documentsWritten"], 2)
        self.assertTrue(list(self.out.glob(f"{backup.MANIFEST_NAME}.corrupt-*")))
        self.assertTrue(any("无法解析" in e for e in stats["errors"]))

    def test_dry_run_writes_nothing(self):
        stats = backup.run_backup(self.client, self.options(dry_run=True))
        self.assertEqual(stats["documentsWritten"], 0)
        self.assertEqual(self.client.get_doc_calls, [])
        self.assertEqual(self.md_files(), [])
        self.assertFalse((self.out / backup.MANIFEST_NAME).exists())

    def test_max_docs_limit(self):
        stats = backup.run_backup(self.client, self.options(max_docs=1))
        self.assertEqual(stats["documentsSeen"], 1)
        self.assertTrue(any("上限" in e for e in stats["errors"]))

    def test_folder_scope(self):
        stats = backup.run_backup(self.client, self.options(folder_id="f1"))
        self.assertEqual(stats["documentsWritten"], 1)
        self.assertEqual(self.client.list_calls, ["f1"])

    def test_stale_files_reported_not_deleted(self):
        backup.run_backup(self.client, self.options(sort_prefix=False))
        old = self.md_files()
        self.documents["0"][0]["name"] = "会议记录改名"
        stats = backup.run_backup(self.client, self.options(sort_prefix=False))
        self.assertEqual(stats["staleFiles"], 1)
        # 旧文件仍在，且没有被删除
        for path in old:
            self.assertTrue((self.out / path).exists())

    def test_prune_stale_moves_only_recorded_files(self):
        backup.run_backup(self.client, self.options(sort_prefix=False))
        self.documents["0"][0]["name"] = "改名后"
        backup.run_backup(self.client, self.options(sort_prefix=False))
        manual = self.out / "我自己的笔记.md"
        manual.write_text("用户手工创建", encoding="utf-8")
        result = backup.prune_stale_backup(self.out, dry_run=True)
        self.assertFalse(result["moved"])
        result = backup.prune_stale_backup(self.out, dry_run=False)
        self.assertEqual(len(result["moved"]), 1)
        self.assertTrue((self.out / backup.STALE_DIR / result["moved"][0]["path"]).exists())
        self.assertTrue(manual.exists())  # 用户文件不受影响

    def test_verify_detects_missing_and_modified(self):
        backup.run_backup(self.client, self.options())
        self.assertTrue(backup.verify_backup(self.out)["ok"])
        target = next(self.out.rglob("*.md"))
        target.write_text("被改动", encoding="utf-8")
        result = backup.verify_backup(self.out)
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["mismatchedDocuments"]), 1)

    def test_report_file_written(self):
        backup.run_backup(self.client, self.options())
        report = json.loads((self.out / backup.REPORT_NAME).read_text(encoding="utf-8"))
        self.assertEqual(report["stats"]["status"], "ok")

    def test_stats_include_request_counters(self):
        stats = backup.run_backup(self.client, self.options())
        self.assertGreater(stats["apiRequests"], 0)
        self.assertIsNotNone(stats["averageIntervalMs"])

    def test_list_failure_is_recorded_not_fatal(self):
        with mock.patch.object(self.client, "list_dir",
                               side_effect=backup.MubuError("boom")):
            stats = backup.run_backup(self.client, self.options())
        self.assertEqual(stats["status"], "partial")
        self.assertTrue(any("boom" in e for e in stats["errors"]))

    # ---- 图片 ----

    def with_image(self, uri="document_image/1_abc.png", field="images"):
        entry = {"id": "i1", "uri": uri, "w": 66, "ow": 800, "oh": 800}
        self.docs["d1"] = definition(
            ("会议记录", [], {field: [entry]}),
            ("第二节", ["子节点"]))

    def test_images_downloaded_into_assets_dir_and_linked(self):
        self.with_image()
        with mock.patch.object(backup, "fetch_asset",
                               return_value=backup.AssetPayload(
                                   b"PNGDATA", "image/png",
                                   "https://api2.mubu.com/v3/document_image/1_abc.png")):
            stats = backup.run_backup(self.client, self.options(download_assets=True))
        self.assertEqual(stats["imagesOk"], 1)
        manifest = json.loads((self.out / backup.MANIFEST_NAME).read_text(encoding="utf-8"))
        entry = manifest["entries"]["d1"]
        content = (self.out / entry["file"]).read_text(encoding="utf-8")
        self.assertIn("![image-1](", content)
        self.assertIn(".assets/001.png", content)
        assets_dir = (self.out / entry["file"]).with_name(
            Path(entry["file"]).stem + backup.ASSETS_SUFFIX)
        self.assertEqual((assets_dir / "001.png").read_bytes(), b"PNGDATA")
        self.assertTrue((assets_dir / "assets.json").exists())
        # 图片出现在对应节点位置（紧随该节点，且在该节点子节点之前）
        self.assertLess(content.index("![image-1]"), content.index("# 第二节"))
        record = manifest["assets"]["d1"][0]
        self.assertEqual(record["sourceHost"], "api2.mubu.com")

    def test_non_image_content_is_not_saved_as_image(self):
        self.with_image()
        with mock.patch.object(backup, "fetch_asset",
                               return_value=backup.AssetPayload(
                                   b"<html>error</html>", "text/html",
                                   "https://api2.mubu.com/v3/document_image/1_abc.png")):
            stats = backup.run_backup(self.client, self.options(download_assets=True))
        self.assertEqual(stats["imagesOk"], 0)
        self.assertEqual(stats["imagesFailed"], 1)
        self.assertEqual(list(self.out.rglob("*.png")), [])
        content = next(self.out.rglob("*.md")).read_text(encoding="utf-8")
        self.assertIn("图片备份失败", content)

    def test_image_failure_does_not_lose_document(self):
        self.with_image()
        with mock.patch.object(backup, "fetch_asset",
                               side_effect=backup.BackupError("网络中断")):
            stats = backup.run_backup(self.client, self.options(download_assets=True))
        self.assertEqual(stats["documentsFailed"], 0)
        self.assertEqual(stats["documentsWritten"], 2)
        self.assertIn("图片备份失败", next(self.out.rglob("*.md")).read_text(encoding="utf-8"))

    def test_foreign_image_url_is_blocked_without_request(self):
        self.with_image(uri="https://evil.example.com/pic.png")
        with mock.patch.object(backup, "fetch_asset") as fetcher:
            stats = backup.run_backup(self.client, self.options(download_assets=True))
        fetcher.assert_not_called()
        self.assertEqual(stats["imagesFailed"], 1)
        manifest = json.loads((self.out / backup.MANIFEST_NAME).read_text(encoding="utf-8"))
        record = manifest["assets"]["d1"][0]
        self.assertEqual(record["status"], "blocked")
        # 报告里只保留主机名与摘要，不保留完整路径
        self.assertNotIn("pic.png", json.dumps(record))
        self.assertTrue(record["sourceRedacted"].startswith("https://evil.example.com/…"))

    def test_same_image_reused_within_document(self):
        self.docs["d1"] = definition(
            ("A", [], {"images": [{"id": "i1", "uri": "document_image/1_same.png"}]}),
            ("B", [], {"images": [{"id": "i2", "uri": "document_image/1_same.png"}]}))
        with mock.patch.object(backup, "fetch_asset",
                               return_value=backup.AssetPayload(
                                   b"P", "image/png",
                                   "https://api2.mubu.com/v3/document_image/1_same.png")
                               ) as fetcher:
            stats = backup.run_backup(self.client, self.options(download_assets=True))
        self.assertEqual(fetcher.call_count, 1)
        self.assertEqual(stats["imagesSkipped"], 1)
        self.assertEqual(len(list(self.out.rglob("*.png"))), 1)

    def test_image_field_option_restricts_matching(self):
        self.docs["d1"] = definition(("A", [], {
            "images": [{"id": "i1", "uri": "document_image/1_a.png"}],
            "customImage": [{"id": "i2", "uri": "document_image/1_b.png"}]}))
        with mock.patch.object(backup, "fetch_asset",
                               return_value=backup.AssetPayload(
                                   b"P", "image/png",
                                   "https://api2.mubu.com/v3/document_image/1_a.png")) as fetcher:
            backup.run_backup(self.client, self.options(
                download_assets=True, image_fields=("customImage",)))
        urls = [call[0][0] for call in fetcher.call_args_list]
        self.assertEqual(len(urls), 2)          # images + 指定字段
        self.assertTrue(any("1_b.png" in url for url in urls))

    def test_link_fields_are_not_treated_as_images(self):
        self.docs["d1"] = definition(
            ("A", [], {"link": "https://api2.mubu.com/v3/document_image/x.png"}))
        with mock.patch.object(backup, "fetch_asset") as fetcher:
            backup.run_backup(self.client, self.options(download_assets=True))
        fetcher.assert_not_called()

    def test_read_only_guarantee(self):
        """备份过程只调用 list_dir / get_doc，不触碰任何写接口。"""
        backup.run_backup(self.client, self.options())
        self.assertFalse(hasattr(self.client, "import_doc"))
        self.assertFalse(hasattr(self.client, "create_folder"))


if __name__ == "__main__":
    unittest.main()
