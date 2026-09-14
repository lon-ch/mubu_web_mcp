"""本地备份引擎：在本机完成读取与落盘，内容**不经过 AI 模型**。

    MubuClient ──┬── MCP 工具      （低频对话查询，内容会进模型上下文）
                 └── BackupEngine  （用户主动发起的本地备份，内容只落磁盘）

安全边界（本模块最需要 review 的部分）：

* 资源下载只允许 ``https://`` 且主机名属于 ``mubu.com`` 及其子域；
* 拒绝带用户名/口令的 URL，拒绝 ``mubu.com.example.com``、``evil-mubu.com`` 这类仿冒域名；
* **逐跳校验重定向**：自己在循环里处理 30x，每一跳都先校验目标再发请求，
  只在允许的幕布域名上附带 JWT，永远不会把凭据转发给别的域名；
* 限制单文件大小、请求超时与重定向次数，并且只接受图片 MIME 类型
  （避免把 HTML 错误页存成 .png）。

只读保证：引擎只调用 ``list_dir`` / ``get_doc`` 与资源 GET，没有任何写入幕布的路径。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any

from . import mubu_markdown
from .mubu_client import MubuClient, MubuError

MANIFEST_NAME = "backup-manifest.json"
STATE_NAME = ".backup-state.json"
STALE_DIR = "_backup_stale"
ASSETS_SUFFIX = ".assets"
REPORT_NAME = "backup-report.json"

# 资源白名单：只允许 https + 幕布自己的域名（".mubu.com" 后缀可避免 notmubu.com 混进来）
ALLOWED_ASSET_SCHEME = "https"
ASSET_HOST_SUFFIXES = (".mubu.com",)
ASSET_BARE_HOST = "mubu.com"

# 图片在 definition 里存的是相对路径（document_image/<user>_<uuid>.<ext>），
# 官方导出的地址就是把 uri 拼到这个前缀后面（实测返回 image/png）。
IMAGE_URL_PREFIX = "https://api2.mubu.com/v3/"
IMAGE_PATH_PREFIX = "document_image/"

MAX_REDIRECTS = 5
DEFAULT_MAX_ASSET_BYTES = 20 * 1024 * 1024

MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "image/heic": ".heic",
    "image/tiff": ".tiff",
}
IMAGE_EXTENSIONS = tuple(sorted(set(MIME_EXTENSIONS.values())))

# 明显是图片的字段名（在真实文档上确认后可用 --image-field 精确指定）
IMAGE_FIELD_HINTS = ("img", "image", "pic", "photo", "picture", "thumbnail", "cover")
LINK_FIELD_HINTS = ("link", "href", "refer", "target")

_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


class BackupError(RuntimeError):
    """备份过程中的可恢复错误。"""


class AssetBlockedError(BackupError):
    """资源地址未通过安全校验，或跳转到了不允许的域名。"""


# ---------------------------------------------------------------------------
# 文件名与排序
# ---------------------------------------------------------------------------

def safe_filename(name: str, fallback: str = "untitled", max_length: int = 80) -> str:
    """把标题变成跨平台安全的文件名（Windows 保留名、控制字符、尾随点等）。"""
    cleaned = unicodedata.normalize("NFC", str(name or ""))
    cleaned = _UNSAFE_FILENAME.sub("_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".")
    if not cleaned:
        cleaned = fallback
    stem = cleaned.split(".")[0].lower()
    if stem in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    cleaned = cleaned[:max_length].rstrip(". ")
    return cleaned or fallback


def short_id(value: str, length: int = 6) -> str:
    return str(value or "")[:length] or "unknown"


class NameAllocator:
    """同一目录内分配不冲突的文件/文件夹名（大小写不敏感，冲突时追加短 ID）。"""

    def __init__(self) -> None:
        self._used: dict[str, set[str]] = {}

    def allocate(self, parent: str, name: str, item_id: str, suffix: str = "") -> str:
        key = parent.casefold()
        used = self._used.setdefault(key, set())
        candidate = f"{name}{suffix}"
        if candidate.casefold() not in used:
            used.add(candidate.casefold())
            return candidate
        candidate = f"{name}__{short_id(item_id)}{suffix}"
        counter = 2
        while candidate.casefold() in used:
            candidate = f"{name}__{short_id(item_id)}-{counter}{suffix}"
            counter += 1
        used.add(candidate.casefold())
        return candidate


def sort_prefix(order: Any, fallback_index: int, width: int = 3) -> str:
    """排序前缀：优先用接口排序字段，取不到就用接口返回顺序。"""
    value = order if isinstance(order, int) and order >= 0 else fallback_index
    return f"{value + 1:0{width}d} "


# ---------------------------------------------------------------------------
# 资源安全
# ---------------------------------------------------------------------------

def is_allowed_asset_url(url: str) -> bool:
    """严格校验资源地址：https、无用户名口令、主机名属于幕布域名。"""
    if not url or not isinstance(url, str):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != ALLOWED_ASSET_SCHEME:
        return False
    if parts.username or parts.password:
        return False
    if parts.port not in (None, 443):
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return False
    return host == ASSET_BARE_HOST or host.endswith(ASSET_HOST_SUFFIXES)


def build_image_url(uri: str) -> str:
    """把 definition 里的相对 uri 变成可下载的绝对地址。"""
    uri = str(uri or "").lstrip("/")
    return IMAGE_URL_PREFIX + uri


def is_allowed_image_url(url: str) -> bool:
    """图片地址还要额外限制路径前缀，避免拿任意路径去试探接口。"""
    if not is_allowed_asset_url(url):
        return False
    path = urllib.parse.urlsplit(url).path.lstrip("/")
    # 允许 <host>/<prefix>… 与 <host>/v3/<prefix>…
    return path.startswith(IMAGE_PATH_PREFIX) or f"/{IMAGE_PATH_PREFIX}" in f"/{path}"


def redact_url(url: str, digest_length: int = 8) -> str:
    """把地址变成可以写进日志/报告的脱敏形式：只保留主机名与内容摘要。"""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    host = parts.hostname or "unknown"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:digest_length]
    return f"https://{host}/…{digest}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁用 urllib 的自动跳转，改由我们自己逐跳校验。"""

    def redirect_request(self, *args, **kwargs):  # noqa: D102
        return None


@dataclass
class AssetPayload:
    data: bytes
    mime: str
    final_url: str


def fetch_asset(url: str, token: str, *, timeout: float = 20.0,
                max_bytes: int = DEFAULT_MAX_ASSET_BYTES,
                opener: Any | None = None) -> AssetPayload:
    """下载一个幕布资源，逐跳校验重定向，绝不把令牌发给非幕布域名。"""
    current = url
    open_url = opener or urllib.request.build_opener(_NoRedirect()).open
    for _hop in range(MAX_REDIRECTS + 1):
        if not is_allowed_asset_url(current):
            raise AssetBlockedError(f"资源地址未通过白名单校验：{redact_url(current)}")
        headers = {"User-Agent": "mubu-web-mcp/backup"}
        if token:
            # 只有已经通过校验的幕布域名才会拿到令牌
            headers["Jwt-Token"] = token
        request = urllib.request.Request(current, headers=headers, method="GET")
        try:
            response = open_url(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") if exc.headers else None
            if exc.code in (301, 302, 303, 307, 308) and location:
                target = urllib.parse.urljoin(current, location)
                if not is_allowed_asset_url(target):
                    raise AssetBlockedError(
                        f"重定向目标不在白名单内：{redact_url(target)}") from None
                current = target
                continue
            raise BackupError(f"资源请求失败（HTTP {exc.code}）") from None
        with response:
            payload = response.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise BackupError(f"资源超过大小上限（>{max_bytes} 字节）")
            mime = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            final_url = response.geturl() if hasattr(response, "geturl") else current
        if not is_allowed_asset_url(final_url):
            raise AssetBlockedError(f"最终地址不在白名单内：{redact_url(final_url)}")
        return AssetPayload(data=payload, mime=mime, final_url=final_url)
    raise BackupError("重定向次数过多")


def guess_extension(url: str, mime: str) -> str | None:
    """扩展名优先用 MIME，其次用 URL 后缀。"""
    if mime in MIME_EXTENSIONS:
        return MIME_EXTENSIONS[mime]
    suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower()
    return suffix if suffix in IMAGE_EXTENSIONS else None


# ---------------------------------------------------------------------------
# 原子写入
# ---------------------------------------------------------------------------

def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 配置与统计
# ---------------------------------------------------------------------------

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
    sort_prefix: bool = True
    prune_stale: bool = False
    dry_run: bool = False
    image_fields: tuple[str, ...] = ()
    cancel_event: Event | None = None
    progress: Callable[[str], None] | None = None


@dataclass
class BackupStats:
    folders_scanned: int = 0
    documents_seen: int = 0
    documents_written: int = 0
    documents_updated: int = 0
    documents_skipped: int = 0
    documents_failed: int = 0
    images_ok: int = 0
    images_failed: int = 0
    images_skipped: int = 0
    links_converted: int = 0
    links_failed: int = 0
    stale_files: int = 0
    api_requests: int = 0
    asset_requests: int = 0
    retries: int = 0
    rate_limit_hits: int = 0
    elapsed_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.documents_failed or self.images_failed or self.errors:
            return "partial"
        return "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "foldersScanned": self.folders_scanned,
            "documentsSeen": self.documents_seen,
            "documentsWritten": self.documents_written,
            "documentsUpdated": self.documents_updated,
            "documentsSkipped": self.documents_skipped,
            "documentsFailed": self.documents_failed,
            "imagesOk": self.images_ok,
            "imagesFailed": self.images_failed,
            "imagesSkipped": self.images_skipped,
            "linksConverted": self.links_converted,
            "linksFailed": self.links_failed,
            "staleFiles": self.stale_files,
            "apiRequests": self.api_requests,
            "assetRequests": self.asset_requests,
            "retries": self.retries,
            "rateLimitHits": self.rate_limit_hits,
            "elapsedSeconds": round(self.elapsed_seconds, 2),
            "averageIntervalMs": (
                round(self.elapsed_seconds * 1000 / self.api_requests, 1)
                if self.api_requests else None),
            "errors": self.errors[:50],
        }


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------

class BackupEngine:
    def __init__(self, client: MubuClient, options: BackupOptions) -> None:
        self.client = client
        self.options = options
        self.out_dir = Path(options.out_dir)
        self.manifest_path = self.out_dir / MANIFEST_NAME
        self.state_path = self.out_dir / STATE_NAME
        self.stats = BackupStats()
        self.manifest: dict[str, Any] = {
            "version": 2, "generatedAt": None, "rootFolderId": options.folder_id,
            "folders": {}, "entries": {}, "assets": {}, "stale": [],
        }
        self.state: dict[str, Any] = {"queue": [], "processed": []}
        self.names = NameAllocator()
        self.stale_candidates: list[dict[str, str]] = []
        self.link_map: dict[str, str] = {}
        self._started = time.time()

    # ---- 基础 --------------------------------------------------------

    def _say(self, message: str) -> None:
        if self.options.progress is not None:
            self.options.progress(message)

    def _check_cancelled(self) -> None:
        if self.options.cancel_event is not None and self.options.cancel_event.is_set():
            raise KeyboardInterrupt("备份已暂停")

    def _api_requests_before(self) -> int:
        return int(self.client.stats.get("requests", 0))

    def _load_existing(self) -> None:
        if self.manifest_path.exists():
            loaded = load_json(self.manifest_path)
            if loaded is None:
                # manifest 损坏：先留档再重建，绝不让损坏文件继续扩散
                broken = self.manifest_path.with_name(
                    f"{MANIFEST_NAME}.corrupt-{int(time.time())}")
                try:
                    shutil.move(str(self.manifest_path), str(broken))
                    self.stats.errors.append(
                        f"原有 {MANIFEST_NAME} 无法解析，已移动到 {broken.name} 并重建")
                except OSError:
                    self.stats.errors.append(f"原有 {MANIFEST_NAME} 无法解析")
            elif isinstance(loaded, dict):
                self.manifest.update(loaded)
                self.manifest.setdefault("folders", {})
                self.manifest.setdefault("entries", {})
                self.manifest.setdefault("assets", {})
                self.manifest.setdefault("stale", [])
        if self.state_path.exists():
            loaded = load_json(self.state_path)
            if isinstance(loaded, dict):
                self.state = loaded

    def _save_state(self) -> None:
        if self.options.dry_run:
            return
        self.out_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.state_path, json.dumps(self.state, ensure_ascii=False, indent=2))

    def _drop_state(self) -> None:
        if self.state_path.exists():
            self.state_path.unlink()

    def _save_manifest(self) -> None:
        if self.options.dry_run:
            return
        atomic_write_text(self.manifest_path,
                          json.dumps(self.manifest, ensure_ascii=False, indent=2))

    # ---- 主流程 ------------------------------------------------------

    def _build_link_map(self) -> dict[str, str]:
        """预扫描目录，为每篇文档分配稳定路径，用于把幕布内部链接转成本地相对路径。

        只列目录、不拉正文，所以额外成本是"每个目录一次请求"。
        """
        mapping: dict[str, str] = {}
        names = NameAllocator()
        queue: list[tuple[str, str, int]] = [(self.options.folder_id, "", 0)]
        while queue and len(mapping) < self.options.max_docs:
            folder_id, path, depth = queue.pop(0)
            if depth > self.options.max_depth:
                continue
            try:
                data = self.client.list_dir(folder_id)
            except MubuError:
                continue
            for position, folder in enumerate(data.get("folders") or []):
                sub_id = str(folder.get("id"))
                name = safe_filename(folder.get("name"), fallback=sub_id)
                prefix = sort_prefix(folder.get("seq", folder.get("order")), position) \
                    if self.options.sort_prefix else ""
                allocated = names.allocate(path, f"{prefix}{name}", sub_id)
                queue.append((sub_id, f"{path}{allocated}/", depth + 1))
            for position, doc in enumerate(data.get("documents") or data.get("docs") or []):
                doc_id = str(doc.get("id"))
                name = safe_filename(doc.get("name"), fallback=doc_id)
                prefix = sort_prefix(doc.get("seq", doc.get("order")), position) \
                    if self.options.sort_prefix else ""
                existing = (self.manifest["entries"].get(doc_id) or {}).get("file")
                filename = (Path(existing).name if existing
                            else names.allocate(path, f"{prefix}{name}", doc_id, ".md"))
                mapping[doc_id] = f"{path}{filename}"
        return mapping

    def run(self) -> dict[str, Any]:
        self._load_existing()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.link_map = self._build_link_map()
        self.client.min_interval = max(
            self.client.min_interval, self.options.interval_ms / 1000.0)
        api_before = self._api_requests_before()
        retries_before = int(self.client.stats.get("retries", 0))
        limit_before = int(self.client.stats.get("rate_limit_waits", 0))

        queue: list[tuple[str, str, int]] = [(self.options.folder_id, "", 0)]
        if self.options.incremental and self.state.get("queue"):
            queue = [tuple(item) for item in self.state["queue"]]  # type: ignore[arg-type]
            self._say(f"从上次中断处继续，剩余 {len(queue)} 个目录")

        processed: set[str] = set(self.state.get("processed") or [])
        seen_paths: set[str] = set()

        while queue:
            self._check_cancelled()
            folder_id, path, depth = queue.pop(0)
            if self.stats.folders_scanned >= self.options.max_folders:
                self.stats.errors.append("达到目录数量上限，已停止（可调大 max-folders）")
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
            self.manifest["folders"][folder_id] = {
                "id": folder_id, "path": path, "parentId": data.get("folderId", folder_id),
                "documentCount": len(docs), "folderCount": len(subfolders),
            }

            for position, folder in enumerate(subfolders):
                self._check_cancelled()
                sub_id = str(folder.get("id"))
                name = safe_filename(folder.get("name"), fallback=sub_id)
                prefix = sort_prefix(folder.get("seq", folder.get("order")), position) \
                    if self.options.sort_prefix else ""
                allocated = self.names.allocate(path, f"{prefix}{name}", sub_id)
                queue.append((sub_id, f"{path}{allocated}/", depth + 1))

            for position, doc in enumerate(docs):
                self._check_cancelled()
                if self.stats.documents_seen >= self.options.max_docs:
                    self.stats.errors.append("达到文档数量上限，已停止（可调大 max-docs）")
                    queue.clear()
                    break
                self.stats.documents_seen += 1
                doc_id = str(doc.get("id"))
                if doc_id in processed:
                    continue
                try:
                    self._handle_document(doc, path, position)
                    seen_paths.add(self.manifest["entries"].get(doc_id, {}).get("path", ""))
                except KeyboardInterrupt:
                    self._save_state()
                    raise
                except (MubuError, BackupError, OSError) as exc:
                    self.stats.documents_failed += 1
                    self.stats.errors.append(f"文档 {doc_id} 处理失败：{exc}")
                processed.add(doc_id)

            self.state = {"queue": [list(item) for item in queue],
                          "processed": sorted(processed)}
            self._save_state()

        self._mark_stale(seen_paths)
        if self.options.prune_stale:
            self._prune_stale()

        self.stats.api_requests = max(0, self._api_requests_before() - api_before)
        self.stats.retries = max(0, int(self.client.stats.get("retries", 0)) - retries_before)
        self.stats.rate_limit_hits = max(
            0, int(self.client.stats.get("rate_limit_waits", 0)) - limit_before)
        self.stats.elapsed_seconds = time.time() - self._started

        self.manifest["generatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.manifest["stats"] = self.stats.as_dict()
        self._save_manifest()
        self._write_report()
        if not self.options.dry_run and self.stats.status == "ok":
            self._drop_state()
        return self.stats.as_dict()

    # ---- 旧的遗留文件 ------------------------------------------------

    def _mark_stale(self, seen_paths: set[str]) -> None:
        stale: list[dict[str, str]] = list(self.stale_candidates)
        known = {item["path"] for item in stale}
        for doc_id, entry in self.manifest["entries"].items():
            path = entry.get("file") or entry.get("path")
            if not path or path in seen_paths or path in known:
                continue
            target = self.out_dir / path
            if target.exists():
                stale.append({"path": path, "docId": doc_id, "reason": "路径已变化"})
                known.add(path)
        self.manifest["stale"] = stale
        self.stats.stale_files = len(stale)

    def _prune_stale(self) -> None:
        """把遗留文件移到 _backup_stale/（默认关闭，需显式 --prune-stale）。"""
        for item in list(self.manifest["stale"]):
            source = self.out_dir / item["path"]
            if not source.exists():
                continue
            destination = self.out_dir / STALE_DIR / item["path"]
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                item["movedTo"] = f"{STALE_DIR}/{item['path']}"
            except OSError as exc:
                self.stats.errors.append(f"移动遗留文件失败 {item['path']}：{exc}")

    # ---- 单篇文档 ----------------------------------------------------

    def _handle_document(self, doc: dict[str, Any], path: str, position: int) -> None:
        doc_id = str(doc.get("id"))
        name = str(doc.get("name") or doc_id)
        entry = self.manifest["entries"].get(doc_id) or {}
        update_time = doc.get("updateTime")

        prefix = sort_prefix(doc.get("seq", doc.get("order")), position) \
            if self.options.sort_prefix else ""
        filename = self.names.allocate(
            path, f"{prefix}{safe_filename(name, fallback=doc_id)}", doc_id, ".md")
        rel_path = f"{path}{filename}"
        target = self.out_dir / rel_path

        # 增量判断：更新时间、标题、目标路径都没变才算"未变化"，
        # 这样改名、移动、排序变化都能被发现并更新路径。
        if (self.options.incremental and entry.get("updateTime") == update_time
                and entry.get("name") == name
                and entry.get("file") == rel_path
                and entry.get("status") == "ok"
                and target.exists()):
            self.stats.documents_skipped += 1
            return

        # 改名或移动后，旧路径要作为遗留文件记录下来（默认只报告，不删除）
        previous = entry.get("file")
        if previous and previous != rel_path and (self.out_dir / previous).exists():
            self.stale_candidates.append(
                {"path": previous, "docId": doc_id, "reason": "文档改名或移动"})

        if self.options.dry_run:
            self._say(f"[文档] {rel_path}（dry-run）")
            return

        raw = self.client.get_doc(doc_id)
        tree = self.client.doc_tree(raw)

        converted = [0]

        def resolve_link(target_id: str) -> str | None:
            local = self.link_map.get(target_id)
            if local and local != rel_path:
                converted[0] += 1
                return local
            return None

        asset_records: list[dict[str, Any]] = []
        resolver = None
        if self.options.download_assets:
            asset_records = self._download_images(doc_id, tree, target)
            resolver = mubu_markdown.make_image_resolver(
                asset_records, self.options.image_fields)

        markdown = mubu_markdown.tree_to_markdown(
            tree, image_resolver=resolver, link_resolver=resolve_link, title=name)
        atomic_write_text(target, markdown if markdown.endswith("\n") else markdown + "\n")

        ok_images = sum(1 for a in asset_records if a["status"] == "ok")
        failed_images = sum(1 for a in asset_records if a["status"] != "ok")
        self.manifest["entries"][doc_id] = {
            "id": doc_id,
            "folderId": doc.get("folderId", self.options.folder_id),
            "name": name,
            "file": rel_path,
            "path": rel_path,
            "updateTime": update_time,
            "lastBackedUpAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "baseVersion": raw.get("baseVersion"),
            "size": target.stat().st_size,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "images": {"total": len(asset_records), "ok": ok_images, "failed": failed_images},
            "links": {"converted": converted[0]},
            "status": "ok" if not failed_images else "partial",
        }
        self.manifest["assets"][doc_id] = asset_records
        if entry.get("file"):
            self.stats.documents_updated += 1
        else:
            self.stats.documents_written += 1
        self._say(f"[文档] {rel_path}"
                  f"（{len(markdown)} 字符，图片 {ok_images}/{len(asset_records)}）")

    # ---- 图片 --------------------------------------------------------

    def _iter_nodes(self, nodes: Any):
        if isinstance(nodes, list):
            for node in nodes:
                yield from self._iter_nodes(node)
        elif isinstance(nodes, dict):
            yield nodes
            yield from self._iter_nodes(nodes.get("children") or [])

    def _image_urls_of(self, node: dict[str, Any]) -> list[tuple[str, str]]:
        """返回 (url, 说明) 列表，保持节点内的原始顺序。

        真实结构（已在真实文档上确认）：``images: [{"id", "uri", "w", "ow", "oh"}]``，
        其中 ``uri`` 是相对路径 ``document_image/<user>_<uuid>.<ext>``，需要拼成绝对地址。
        ``--image-field`` 可指定额外候选字段名，兼容其它形态。
        """
        found: list[tuple[str, str]] = []

        def from_entry(entry: Any) -> None:
            if isinstance(entry, dict):
                uri = entry.get("uri") or entry.get("url") or entry.get("src")
                if uri:
                    uri = str(uri)
                    alt = str(entry.get("name") or entry.get("alt") or "")
                    found.append((uri if "://" in uri else build_image_url(uri), alt))
            elif isinstance(entry, str) and entry:
                found.append(
                    (entry if "://" in entry else build_image_url(entry), ""))

        entries = node.get("images")
        if isinstance(entries, list):
            for entry in entries:
                from_entry(entry)
        elif entries:
            from_entry(entries)

        for extra_field in self.options.image_fields:
            value = node.get(extra_field)
            if isinstance(value, list):
                for entry in value:
                    from_entry(entry)
            elif value:
                from_entry(value)
        return found

    def _download_images(self, doc_id: str, tree: dict[str, Any],
                         target: Path) -> list[dict[str, Any]]:
        nodes = list(self._iter_nodes(tree.get("nodes") or []))
        assets_dir = target.with_name(f"{target.stem}{ASSETS_SUFFIX}")
        records: list[dict[str, Any]] = []
        by_url: dict[str, dict[str, Any]] = {}
        counter = 0

        for node in nodes:
            for url, alt in self._image_urls_of(node):
                self._check_cancelled()
                if not is_allowed_asset_url(url):
                    self.stats.images_failed += 1
                    records.append(self._image_record(doc_id, node, url, alt, "blocked",
                                                      "地址未通过白名单校验", None))
                    self.stats.errors.append(
                        f"图片地址被拒绝（{redact_url(url)}）")
                    continue
                if url in by_url:  # 同文档内重复图片复用同一个文件
                    records.append({**by_url[url], "nodeId": node.get("id"), "alt": alt})
                    self.stats.images_skipped += 1
                    continue
                counter += 1
                record = self._fetch_image(doc_id, node, url, alt, assets_dir, counter)
                records.append(record)
                by_url[url] = record

        if records:
            atomic_write_text(
                assets_dir / "assets.json",
                json.dumps(records, ensure_ascii=False, indent=2))
        return records

    def _image_record(self, doc_id: str, node: dict[str, Any], url: str, alt: str,
                      status: str, reason: str | None, local: str | None,
                      mime: str | None = None, size: int | None = None,
                      attempts: int = 0) -> dict[str, Any]:
        return {
            "docId": doc_id,
            "nodeId": node.get("id"),
            "alt": alt or None,
            "sourceRedacted": redact_url(url),
            "sourceHost": urllib.parse.urlsplit(url).hostname if url else None,
            "local": local,
            "mime": mime,
            "size": size,
            "sha256": None,
            "status": status,
            "reason": reason,
            "attempts": attempts,
            "lastAttemptAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }

    def _fetch_image(self, doc_id: str, node: dict[str, Any], url: str, alt: str,
                     assets_dir: Path, position: int) -> dict[str, Any]:
        attempts = 0
        last_error: str | None = None
        while attempts <= self.client.max_retries:
            attempts += 1
            self.stats.asset_requests += 1
            try:
                payload = fetch_asset(url, self.client.ensure_token(),
                                      timeout=self.client.timeout)
            except AssetBlockedError as exc:
                record = self._image_record(doc_id, node, url, alt, "blocked", str(exc),
                                            None, attempts=attempts)
                self.stats.images_failed += 1
                self.stats.errors.append(f"图片被拒绝：{exc}")
                return record
            except (BackupError, MubuError, OSError) as exc:
                last_error = str(exc)
                if attempts <= self.client.max_retries:
                    self.stats.retries += 1
                    continue
                break

            if payload.mime and not payload.mime.startswith("image/"):
                last_error = f"返回内容不是图片（{payload.mime}）"
                break
            extension = guess_extension(payload.final_url, payload.mime)
            if extension is None:
                last_error = "无法确定图片类型（既没有图片 MIME 也没有可识别后缀）"
                break

            filename = f"{position:03d}{extension}"
            local = f"{assets_dir.name}/{filename}"
            atomic_write_bytes(assets_dir / filename, payload.data)
            self.stats.images_ok += 1
            record = self._image_record(doc_id, node, url, alt, "ok", None, local,
                                        mime=payload.mime, size=len(payload.data),
                                        attempts=attempts)
            record["sha256"] = hashlib.sha256(payload.data).hexdigest()
            return record

        record = self._image_record(doc_id, node, url, alt, "failed", last_error, None,
                                    attempts=attempts)
        self.stats.images_failed += 1
        self.stats.errors.append(f"图片下载失败（{record['sourceRedacted']}）：{last_error}")
        return record

    # ---- 报告 --------------------------------------------------------

    def report_text(self) -> str:
        stats = self.stats
        lines = [
            "备份完成" if stats.status == "ok" else "备份部分完成（有失败项，可重跑）",
            "",
            f"文件夹：{stats.folders_scanned}",
            f"文档：{stats.documents_seen}",
            f"新增文档：{stats.documents_written}",
            f"更新文档：{stats.documents_updated}",
            f"未变化文档：{stats.documents_skipped}",
            f"失败文档：{stats.documents_failed}",
            f"图片成功：{stats.images_ok}",
            f"图片失败：{stats.images_failed}",
            f"链接转换失败：{stats.links_failed}",
            f"遗留文件：{stats.stale_files}",
            f"API 请求：{stats.api_requests}",
            f"图片请求：{stats.asset_requests}",
            f"API 重试：{stats.retries}",
            f"限流命中：{stats.rate_limit_hits}",
            f"耗时：{stats.elapsed_seconds:.1f} 秒",
        ]
        if stats.errors:
            lines.append("")
            lines.append("需要重试的项目：")
            lines.extend(f"- {message}" for message in stats.errors[:20])
        return "\n".join(lines)

    def _write_report(self) -> None:
        if self.options.dry_run:
            return
        payload = {"generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   "stats": self.stats.as_dict(),
                   "stale": self.manifest.get("stale", []),
                   "failedDocuments": [
                       {"id": doc_id, "path": entry.get("path")}
                       for doc_id, entry in self.manifest["entries"].items()
                       if entry.get("status") != "ok"]}
        atomic_write_text(self.out_dir / REPORT_NAME,
                          json.dumps(payload, ensure_ascii=False, indent=2))

    # ---- 校验 --------------------------------------------------------

    def verify(self) -> dict[str, Any]:
        """重新计算哈希，检查文件是否与 manifest 一致。"""
        missing, mismatched = [], []
        for doc_id, entry in (self.manifest.get("entries") or {}).items():
            path = self.out_dir / (entry.get("file") or "")
            if not path.exists():
                missing.append({"docId": doc_id, "path": entry.get("file")})
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if entry.get("sha256") and digest != entry["sha256"]:
                mismatched.append({"docId": doc_id, "path": entry.get("file")})
        return {"checked": len(self.manifest.get("entries") or {}),
                "missing": missing, "mismatched": mismatched,
                "ok": not missing and not mismatched}


def load_manifest(out_dir: Path) -> dict[str, Any]:
    return load_json(Path(out_dir) / MANIFEST_NAME) or {}


def verify_backup(out_dir: Path) -> dict[str, Any]:
    """校验备份目录：文件是否缺失、内容是否与 manifest 里的哈希一致。"""
    out_dir = Path(out_dir)
    manifest = load_manifest(out_dir)
    entries = manifest.get("entries") or {}
    missing: list[dict[str, Any]] = []
    mismatched: list[dict[str, Any]] = []
    for doc_id, entry in entries.items():
        path = out_dir / (entry.get("file") or "")
        if not path.exists():
            missing.append({"docId": doc_id, "path": entry.get("file")})
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if entry.get("sha256") and digest != entry["sha256"]:
            mismatched.append({"docId": doc_id, "path": entry.get("file")})
    assets = manifest.get("assets") or {}
    asset_missing = [
        {"docId": doc_id, "local": record.get("local")}
        for doc_id, records in assets.items() for record in records
        if record.get("status") == "ok" and record.get("local")
        and not (out_dir / record["local"]).exists()]
    return {
        "checkedDocuments": len(entries),
        "checkedAssets": sum(len(v) for v in assets.values()),
        "missingDocuments": missing,
        "mismatchedDocuments": mismatched,
        "missingAssets": asset_missing,
        "ok": not (missing or mismatched or asset_missing),
    }


def prune_stale_backup(out_dir: Path, dry_run: bool = True,
                       progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """把 manifest 里标记的遗留文件移到 _backup_stale/。

    只会处理 manifest 明确记录过、且文件仍然存在的路径；用户手工创建的文件不会被碰。
    """
    out_dir = Path(out_dir)
    manifest = load_manifest(out_dir)
    stale = manifest.get("stale") or []
    planned: list[dict[str, str]] = []
    moved: list[dict[str, str]] = []
    for item in stale:
        relative = item.get("path")
        if not relative:
            continue
        source = out_dir / relative
        if not source.exists():
            continue
        destination = out_dir / STALE_DIR / relative
        planned.append({"path": relative, "target": f"{STALE_DIR}/{relative}"})
        if dry_run:
            if progress:
                progress(f"[将移动] {relative} -> {STALE_DIR}/{relative}")
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            moved.append({"path": relative, "target": f"{STALE_DIR}/{relative}"})
            if progress:
                progress(f"[已移动] {relative} -> {STALE_DIR}/{relative}")
        except OSError as exc:
            planned.append({"path": relative, "error": str(exc)})
    return {"dryRun": dry_run, "planned": planned, "moved": moved}


def run_backup(client: MubuClient, options: BackupOptions) -> dict[str, Any]:
    return BackupEngine(client, options).run()
