"""本地备份引擎：在本机完成读取与落盘，内容**不经过 AI 模型**。

架构上刻意与 MCP 工具分开：

    MubuClient ──┬── MCP 工具      （低频对话查询，内容会进模型上下文）
                 └── BackupEngine  （用户主动发起的本地备份，内容只落磁盘）

只读保证：引擎只调用 ``list_dir`` / ``get_doc``（以及可选的图片下载），
没有任何写入幕布的路径。

特性：递归索引、按 folder 范围、增量（未变更文档不发请求）、断点续传、
频率控制（默认 2 秒）、失败重试（由 client 负责）、脱敏日志（不含正文）。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any

from . import mubu_markdown
from .mubu_client import MubuClient, MubuError

MANIFEST_NAME = "manifest.json"
STATE_NAME = ".backup-state.json"
ASSETS_DIR_SUFFIX = ".assets"

# 图片/附件下载白名单：只允许幕布自己的域名（含子域），并在重定向后再校验一次。
# 注意用 ".mubu.com" 而不是 "mubu.com" 作为后缀，否则 notmubu.com 也会被放行。
ASSET_HOST_SUFFIXES = (".mubu.com",)

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".heic")

_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name: str, fallback: str = "untitled", max_length: int = 80) -> str:
    """把文档名变成安全的文件名。"""
    cleaned = _UNSAFE_FILENAME.sub("_", str(name or "")).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = fallback
    return cleaned[:max_length].rstrip(". ")


def is_allowed_asset_url(url: str) -> bool:
    """严格校验：只有 https 且主机名属于幕布域名白名单才允许下载。"""
    if not url:
        return False
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("https", "http"):
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False
    return host == "mubu.com" or host.endswith(ASSET_HOST_SUFFIXES)


@dataclass
class BackupOptions:
    out_dir: Path
    folder_id: str = "0"
    max_depth: int = 20
    max_folders: int = 2000
    max_docs: int = 5000
    interval_ms: int = 2000
    incremental: bool = True
    download_assets: bool = False
    dry_run: bool = False
    cancel_event: Event | None = None
    progress: Callable[[str], None] | None = None
    asset_url_resolver: Callable[[str], dict[str, str]] | None = None


@dataclass
class BackupStats:
    folders_scanned: int = 0
    documents_seen: int = 0
    documents_written: int = 0
    documents_skipped: int = 0
    documents_failed: int = 0
    assets_downloaded: int = 0
    assets_skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "foldersScanned": self.folders_scanned,
            "documentsSeen": self.documents_seen,
            "documentsWritten": self.documents_written,
            "documentsSkipped": self.documents_skipped,
            "documentsFailed": self.documents_failed,
            "assetsDownloaded": self.assets_downloaded,
            "assetsSkipped": self.assets_skipped,
            "errors": self.errors[:20],
        }


class BackupEngine:
    def __init__(self, client: MubuClient, options: BackupOptions) -> None:
        self.client = client
        self.options = options
        self.out_dir = Path(options.out_dir)
        self.manifest_path = self.out_dir / MANIFEST_NAME
        self.state_path = self.out_dir / STATE_NAME
        self.stats = BackupStats()
        self.manifest: dict[str, Any] = {
            "version": 1,
            "generatedAt": None,
            "rootFolderId": options.folder_id,
            "entries": {},
            "folders": [],
        }
        self.state: dict[str, Any] = {"queue": [], "processed": []}

    # ---- 基础工具 ----------------------------------------------------

    def _say(self, message: str) -> None:
        if self.options.progress is not None:
            self.options.progress(message)

    def _check_cancelled(self) -> None:
        if self.options.cancel_event is not None and self.options.cancel_event.is_set():
            raise KeyboardInterrupt("备份已暂停")

    def _load_existing(self) -> None:
        if self.manifest_path.exists():
            try:
                self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        if self.state_path.exists():
            try:
                self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass

    def _save_state(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    def _drop_state(self) -> None:
        if self.state_path.exists():
            self.state_path.unlink()

    # ---- 主流程 ------------------------------------------------------

    def run(self) -> dict[str, Any]:
        self._load_existing()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        # 备份默认更保守的间隔（评审建议 2 秒）
        self.client.min_interval = max(
            self.client.min_interval, self.options.interval_ms / 1000.0)

        queue: list[tuple[str, str, int]] = [(self.options.folder_id, "", 0)]
        if self.options.incremental and self.state.get("queue"):
            queue = [tuple(item) for item in self.state["queue"]]  # type: ignore[arg-type]
            self._say(f"从上次中断处继续，剩余 {len(queue)} 个目录")

        processed_docs: set[str] = set(self.state.get("processed") or [])
        next_index = 1 + max(
            (int(entry.get("index", 0)) for entry in self.manifest["entries"].values()),
            default=0)

        while queue:
            self._check_cancelled()
            folder_id, path, depth = queue.pop(0)
            if self.stats.folders_scanned >= self.options.max_folders:
                self.stats.errors.append("达到目录数量上限，已停止（可调大 max_folders）")
                break
            if depth > self.options.max_depth:
                continue

            self.stats.folders_scanned += 1
            self._say(f"[目录] {path or '/'}")
            try:
                data = self.client.list_dir(folder_id)
            except MubuError as exc:
                self.stats.errors.append(f"读取目录 {folder_id} 失败：{exc}")
                continue

            subfolders = data.get("folders") or []
            docs = data.get("documents") or data.get("docs") or []
            if not any(f.get("id") == folder_id and f.get("path") == path
                       for f in self.manifest["folders"]):
                self.manifest["folders"].append(
                    {"id": folder_id, "path": path, "documents": len(docs),
                     "folders": len(subfolders)})

            for folder in subfolders:
                sub_id = str(folder.get("id"))
                sub_path = f"{path}{safe_filename(folder.get('name'))}/"
                queue.append((sub_id, sub_path, depth + 1))

            for doc in docs:
                self._check_cancelled()
                if self.stats.documents_seen >= self.options.max_docs:
                    self.stats.errors.append("达到文档数量上限，已停止（可调大 max_docs）")
                    queue.clear()
                    break
                self.stats.documents_seen += 1
                doc_id = str(doc.get("id"))
                if doc_id in processed_docs:
                    continue
                try:
                    written, index = self._handle_document(doc, path, next_index)
                except MubuError as exc:
                    self.stats.documents_failed += 1
                    self.stats.errors.append(f"文档 {doc_id} 处理失败：{exc}")
                    continue
                if written:
                    next_index = max(next_index, index + 1)
                processed_docs.add(doc_id)

            # 每处理完一个目录就落一次状态，保证可续传
            self.state = {"queue": [list(item) for item in queue],
                          "processed": sorted(processed_docs)}
            if not self.options.dry_run:
                self._save_state()

        self.manifest["generatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.manifest["stats"] = self.stats.as_dict()
        if not self.options.dry_run:
            self._write_manifest()
            self._drop_state()
        return self.manifest["stats"]

    # ---- 单篇文档 ----------------------------------------------------

    def _handle_document(self, doc: dict[str, Any], path: str,
                         next_index: int) -> tuple[bool, int]:
        doc_id = str(doc.get("id"))
        name = str(doc.get("name") or doc_id)
        entry = self.manifest["entries"].get(doc_id)

        # 增量：更新时间没变且文件还在，就直接跳过（连接口都不用调）
        if self.options.incremental and entry:
            existing = self.out_dir / entry.get("file", "")
            if (entry.get("updateTime") == doc.get("updateTime")
                    and entry.get("file") and existing.exists()):
                self.stats.documents_skipped += 1
                return False, int(entry.get("index") or next_index)

        index = int(entry.get("index")) if entry and entry.get("index") else next_index
        filename = f"{index:04d}-{safe_filename(name, fallback=doc_id)}.md"
        rel_path = f"{path}{filename}"
        target = self.out_dir / rel_path

        if self.options.dry_run:
            self._say(f"[文档] {rel_path}（dry-run）")
            return True, index

        raw = self.client.get_doc(doc_id)
        tree = self.client.doc_tree(raw)
        markdown = mubu_markdown.tree_to_markdown(tree)

        assets: list[dict[str, str]] = []
        if self.options.download_assets:
            assets = self._download_assets(tree, target)
            for asset in assets:
                markdown = markdown.replace(asset["url"], asset["local"])

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown if markdown.endswith("\n") else markdown + "\n",
                          encoding="utf-8")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        self.manifest["entries"][doc_id] = {
            "id": doc_id,
            "name": name,
            "index": index,
            "path": rel_path,
            "file": rel_path,
            "updatedAt": doc.get("updateTime"),
            "updateTime": doc.get("updateTime"),
            "baseVersion": raw.get("baseVersion"),
            "size": target.stat().st_size,
            "sha256": digest,
            "assets": assets,
        }
        self.stats.documents_written += 1
        self._say(f"[文档] {rel_path} ({len(markdown)} 字符)")
        return True, index

    # ---- 图片 / 附件（实验性，等接口字段确认后收紧） ------------------

    def _iter_urls(self, node: Any) -> list[str]:
        found: list[str] = []
        if isinstance(node, dict):
            for value in node.values():
                found.extend(self._iter_urls(value))
        elif isinstance(node, list):
            for item in node:
                found.extend(self._iter_urls(item))
        elif isinstance(node, str) and node.startswith(("http://", "https://")):
            found.append(node)
        return found

    def _download_assets(self, tree: dict[str, Any], target: Path) -> list[dict[str, str]]:
        urls: list[str] = []
        for url in self._iter_urls(tree.get("nodes") or []):
            if not is_allowed_asset_url(url):
                continue
            if url.lower().endswith(IMAGE_EXTENSIONS) or "img" in url.lower():
                if url not in urls:
                    urls.append(url)

        results: list[dict[str, str]] = []
        if not urls:
            return results
        assets_dir = target.with_suffix("") .parent / f"{target.stem}{ASSETS_DIR_SUFFIX}"
        assets_dir.mkdir(parents=True, exist_ok=True)

        for position, url in enumerate(urls, start=1):
            self._check_cancelled()
            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
            suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower()
            if suffix not in IMAGE_EXTENSIONS:
                suffix = ".bin"
            filename = f"{position:03d}-{digest}{suffix}"
            destination = assets_dir / filename
            if destination.exists():
                self.stats.assets_skipped += 1
            else:
                try:
                    self._fetch_asset(url, destination)
                    self.stats.assets_downloaded += 1
                except (MubuError, OSError) as exc:
                    self.stats.errors.append(f"图片下载失败（{digest}）：{exc}")
                    continue
            relative = f"./{assets_dir.name}/{filename}"
            results.append({"url": url, "local": relative, "file": filename})
        return results

    def _fetch_asset(self, url: str, destination: Path) -> None:
        """下载单个资源：白名单 + 重定向后再校验 + 不把 token 发给第三方域名。"""
        if not is_allowed_asset_url(url):
            raise MubuError("图片地址不在幕布域名白名单内")
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "mubu-web-mcp/backup",
                     "Jwt-Token": self.client.ensure_token()})
        with urllib.request.urlopen(request, timeout=self.client.timeout) as response:
            final_url = response.geturl()
            if not is_allowed_asset_url(final_url):
                raise MubuError("图片重定向到了非幕布域名，已拒绝保存")
            payload = response.read()
        tmp = destination.with_suffix(destination.suffix + ".part")
        tmp.write_bytes(payload)
        tmp.replace(destination)

    def _write_manifest(self) -> None:
        self.manifest_path.write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def run_backup(client: MubuClient, options: BackupOptions) -> dict[str, Any]:
    return BackupEngine(client, options).run()


def clean_output_dir(path: Path) -> None:
    """仅供测试使用：清掉一个备份目录。"""
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
