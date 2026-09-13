"""本地备份引擎测试：增量、断点续传、白名单、dry-run（不联网）。"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import temp_dir  # noqa: E402

from mubu_web_mcp import backup  # noqa: E402


def definition(*items):
    """items: (文本, 子节点文本列表)"""
    def node(text, index, children=()):
        return {"id": f"n{index}", "text": text, "children":
                [node(child, f"{index}-{j}") for j, child in enumerate(children)]}

    return {"nodes": [node(text, str(i), children)
                      for i, (text, children) in enumerate(items)]}


class FakeClient:
    def __init__(self, folders=None, documents=None, docs=None):
        self.min_interval = 0.0
        self.timeout = 5
        self._folders = folders or {}
        self._documents = documents or {}
        self._docs = docs or {}
        self.list_calls = []
        self.get_doc_calls = []
        self.ensure_token_calls = 0

    def list_dir(self, folder_id="0"):
        self.list_calls.append(folder_id)
        return {"folders": self._folders.get(folder_id, []),
                "documents": self._documents.get(folder_id, [])}

    def get_doc(self, doc_id):
        self.get_doc_calls.append(doc_id)
        return {"definition": json.dumps(self._docs[doc_id], ensure_ascii=False),
                "baseVersion": 1}

    @staticmethod
    def doc_tree(data):
        return json.loads(data["definition"])

    def ensure_token(self):
        self.ensure_token_calls += 1
        return "T"


class HelperTests(unittest.TestCase):
    def test_safe_filename(self):
        self.assertEqual(backup.safe_filename('a/b\\c:d*e?f"g<h>i|j'), "a_b_c_d_e_f_g_h_i_j")
        self.assertEqual(backup.safe_filename("   "), "untitled")
        self.assertEqual(backup.safe_filename("会议记录"), "会议记录")
        self.assertLessEqual(len(backup.safe_filename("x" * 500)), 80)
        self.assertEqual(backup.safe_filename("..."), "untitled")

    def test_asset_url_whitelist(self):
        allowed = ["https://assets.mubu.com/a.png", "https://api2.mubu.com/x.jpg",
                   "https://mubu.com/y.png", "http://cdn.mubu.com/z.png"]
        blocked = ["https://evil.com/a.png", "https://mubu.com.evil.com/a.png",
                   "https://notmubu.com/a.png", "ftp://mubu.com/a.png",
                   "file:///etc/passwd", "https://xn--mubu.com/a.png?x=1"]
        for url in allowed:
            self.assertTrue(backup.is_allowed_asset_url(url), url)
        for url in blocked:
            self.assertFalse(backup.is_allowed_asset_url(url), url)


class BackupEngineTests(unittest.TestCase):
    def setUp(self):
        self.root = temp_dir(self)
        self.out = self.root / "backup"
        self.folders = {
            "0": [{"id": "f1", "name": "工作"}],
            "f1": [],
        }
        self.documents = {
            "0": [{"id": "d1", "name": "会议记录", "updateTime": 100}],
            "f1": [{"id": "d2", "name": "项目/计划", "updateTime": 200}],
        }
        self.docs = {
            "d1": definition(("会议记录", ["要点一", "要点二"])),
            "d2": definition(("项目计划", [])),
        }
        self.client = FakeClient(self.folders, self.documents, self.docs)

    def options(self, **kwargs):
        defaults = dict(out_dir=self.out, interval_ms=0)
        defaults.update(kwargs)
        return backup.BackupOptions(**defaults)

    def test_backup_writes_markdown_and_manifest(self):
        stats = backup.run_backup(self.client, self.options())
        self.assertEqual(stats["documentsWritten"], 2)
        self.assertEqual(stats["foldersScanned"], 2)
        files = sorted(p.relative_to(self.out).as_posix()
                       for p in self.out.rglob("*.md"))
        self.assertEqual(len(files), 2)
        manifest = json.loads((self.out / backup.MANIFEST_NAME).read_text(encoding="utf-8"))
        entry = manifest["entries"]["d1"]
        self.assertEqual(entry["name"], "会议记录")
        self.assertTrue(entry["file"].endswith(".md"))
        self.assertEqual(entry["sha256"], __import__("hashlib").sha256(
            (self.out / entry["file"]).read_bytes()).hexdigest())
        content = (self.out / entry["file"]).read_text(encoding="utf-8")
        self.assertIn("# 会议记录", content)
        self.assertIn("- 要点一", content)

    def test_incremental_skips_unchanged_without_fetching(self):
        backup.run_backup(self.client, self.options())
        self.client.get_doc_calls.clear()
        self.client.list_calls.clear()
        stats = backup.run_backup(self.client, self.options())
        self.assertEqual(stats["documentsSkipped"], 2)
        self.assertEqual(stats["documentsWritten"], 0)
        self.assertEqual(self.client.get_doc_calls, [])  # 未变更就不拉正文
        self.assertTrue(self.client.list_calls)          # 仍然要列目录

    def test_changed_document_is_refetched(self):
        backup.run_backup(self.client, self.options())
        self.documents["0"][0]["updateTime"] = 999
        self.client.get_doc_calls.clear()
        stats = backup.run_backup(self.client, self.options())
        self.assertEqual(stats["documentsWritten"], 1)
        self.assertEqual(self.client.get_doc_calls, ["d1"])

    def test_state_file_removed_after_success(self):
        backup.run_backup(self.client, self.options())
        self.assertFalse((self.out / backup.STATE_NAME).exists())

    def test_resume_from_saved_state(self):
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / backup.STATE_NAME).write_text(json.dumps({
            "queue": [["f1", "工作/", 1]],
            "processed": ["d1"],
        }), encoding="utf-8")
        stats = backup.run_backup(self.client, self.options())
        self.assertEqual(stats["foldersScanned"], 1)
        self.assertEqual(stats["documentsWritten"], 1)   # 只补 d2
        self.assertEqual(self.client.get_doc_calls, ["d2"])

    def test_dry_run_writes_nothing(self):
        stats = backup.run_backup(self.client, self.options(dry_run=True))
        self.assertEqual(stats["documentsWritten"], 0)
        self.assertEqual(self.client.get_doc_calls, [])
        self.assertEqual(list(self.out.glob("*.md")), [])
        self.assertFalse((self.out / backup.MANIFEST_NAME).exists())

    def test_max_docs_limit(self):
        stats = backup.run_backup(self.client, self.options(max_docs=1))
        self.assertEqual(stats["documentsSeen"], 1)
        self.assertTrue(any("上限" in e for e in stats["errors"]))

    def test_folder_scope(self):
        stats = backup.run_backup(self.client, self.options(folder_id="f1"))
        self.assertEqual(stats["documentsWritten"], 1)
        self.assertEqual(self.client.list_calls, ["f1"])

    def test_list_failure_is_recorded_not_fatal(self):
        with mock.patch.object(self.client, "list_dir",
                               side_effect=backup.MubuError("boom")):
            stats = backup.run_backup(self.client, self.options())
        self.assertTrue(any("boom" in e for e in stats["errors"]))

    def test_assets_are_downloaded_within_whitelist(self):
        self.docs["d1"] = {"nodes": [{
            "id": "n1", "text": "带图文档",
            "children": [{"id": "n2", "text": "见图",
                          "img": "https://assets.mubu.com/a/pic.png"}]}]}
        stored = {}

        class FakeAssetResponse:
            headers = {}

            def __init__(self, url):
                self._url = url

            def read(self):
                return b"PNGDATA"

            def geturl(self):
                return self._url

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            url = request.full_url if hasattr(request, "full_url") else request
            stored["url"] = url
            return FakeAssetResponse(url)

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            stats = backup.run_backup(self.client, self.options(download_assets=True))
        self.assertEqual(stats["assetsDownloaded"], 1)
        self.assertTrue(stored["url"].startswith("https://assets.mubu.com/"))
        assets = list(self.out.rglob("*.assets/*"))
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0].read_bytes(), b"PNGDATA")

    def test_asset_redirect_to_foreign_host_is_rejected(self):
        self.docs["d1"] = {"nodes": [{
            "id": "n1", "text": "带图文档",
            "children": [{"id": "n2", "text": "见图",
                          "img": "https://assets.mubu.com/a/pic.png"}]}]}

        class RedirectedResponse:
            headers = {}

            def read(self):
                return b"EVIL"

            def geturl(self):
                return "https://evil.example.com/pic.png"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch("urllib.request.urlopen", lambda *a, **k: RedirectedResponse()):
            stats = backup.run_backup(self.client, self.options(download_assets=True))
        self.assertEqual(stats["assetsDownloaded"], 0)
        self.assertTrue(any("非幕布域名" in e for e in stats["errors"]))
        self.assertEqual(list(self.out.rglob("*.assets/*")), [])

    def test_foreign_urls_are_never_requested(self):
        self.docs["d1"] = {"nodes": [{
            "id": "n1", "text": "带外链",
            "children": [{"id": "n2", "text": "见图",
                          "img": "https://evil.example.com/pic.png"}]}]}
        with mock.patch("urllib.request.urlopen") as opener:
            stats = backup.run_backup(self.client, self.options(download_assets=True))
        opener.assert_not_called()
        self.assertEqual(stats["assetsDownloaded"], 0)

    def test_backup_never_calls_write_endpoints(self):
        """只读保证：备份过程不得出现任何创建/修改调用。"""
        client = self.client
        with mock.patch.object(client, "list_dir", wraps=client.list_dir), \
                mock.patch.object(client, "get_doc", wraps=client.get_doc):
            backup.run_backup(client, self.options())
        self.assertFalse(hasattr(client, "import_doc"))
        self.assertFalse(hasattr(client, "create_folder"))


if __name__ == "__main__":
    unittest.main()
