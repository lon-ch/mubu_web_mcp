"""MCP 服务本体（stdio 传输，零第三方依赖）。

设计要点：

* 只读为主：默认工具里没有删除、重命名、移动、覆盖已有文档。
  ``MUBU_READ_ONLY=1`` 或 ``--read-only`` 之后，连新建类工具也不再暴露。
* 结构化输出：适合程序消费的工具同时返回 ``structuredContent``，
  调用方不需要解析面向人的中文列表文本。
* 绝不截断 JSON：大文档走 ``mubu_get_doc_json`` 的分页/游标，而不是切字符串。
* 支持取消：收到 MCP 的 ``notifications/cancelled`` 会中止进行中的请求与退避等待。
"""

from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import __version__, mubu_markdown
from .mubu_client import (
    CancelledError,
    MissingCredentials,
    MubuClient,
    MubuError,
)

SERVER_NAME = "mubu_web_mcp"
DEFAULT_PROTOCOL = "2025-06-18"

MAX_SEARCH_FOLDERS = 100
MAX_SEARCH_DOCS = 300
MAX_SEARCH_DEPTH = 3

# 结构报告里不展示正文，只展示字段名与计数
MAX_FIELD_SAMPLES = 3


def log(message: str) -> None:
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


def configure_stdio(protocol: bool) -> None:
    """让输出在编码受限的环境下也不会崩。

    ``protocol=True``（MCP 的 stdio 传输）时强制 UTF-8：MCP 规定消息就是 UTF-8，
    而 Windows 在管道里默认用本地代码页，不改的话返回中文就会 UnicodeEncodeError。
    交互式运行时保留控制台原编码，只把错误处理改成不抛异常。
    """
    for stream in (sys.stdout, sys.stderr):
        if not hasattr(stream, "reconfigure"):
            continue
        try:
            if protocol and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
            else:
                stream.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass


# --------------------------------------------------------------------------
# 每线程一个客户端（并发调用时互不干扰，取消事件也只作用于当前请求）
# --------------------------------------------------------------------------

_local = threading.local()


def client(cancel_event: threading.Event | None = None) -> MubuClient:
    instance = getattr(_local, "client", None)
    if instance is None:
        instance = MubuClient()
        _local.client = instance
    if cancel_event is not None:
        instance.cancel_event = cancel_event
    return instance


def reset_client() -> None:
    _local.client = None


@dataclass
class ToolResult:
    text: str
    structured: dict[str, Any] | None = None


# --------------------------------------------------------------------------
# 工具实现
# --------------------------------------------------------------------------

def _entry(entry: dict[str, Any], kind: str) -> str:
    return f"- [{kind}] {entry.get('name') or '(未命名)'}  id={entry.get('id', '')}"


def _normalize_listing(folder_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """把目录返回原样整理成结构化数据（尽量保留底层字段）。"""
    def normalize(item: dict[str, Any], kind: str) -> dict[str, Any]:
        normalized = dict(item)  # 保留原始字段
        normalized.setdefault("type", kind)
        normalized["parentId"] = item.get("folderId", folder_id)
        normalized["order"] = item.get("seq", item.get("index", item.get("sort", None)))
        if "updateTime" in item:
            normalized["updatedAt"] = item.get("updateTime")
        return normalized

    return {
        "folderId": str(folder_id),
        "folderName": data.get("folderName"),
        "folders": [normalize(f, "folder") for f in (data.get("folders") or [])],
        "documents": [normalize(d, "document")
                      for d in (data.get("documents") or data.get("docs") or [])],
    }


def tool_whoami(_args: dict[str, Any]) -> ToolResult:
    info = client().whoami()
    text = "\n".join([
        f"账号：{info.get('name') or '(未知)'}",
        f"user_id：{info.get('user_id') or '(未知)'}",
        f"member_id：{'已获取' if info.get('member_id') else '未获取'}",
        f"token 剩余有效期：约 {info.get('token_expires_in_seconds', 0) // 60} 分钟",
    ])
    return ToolResult(text, {
        "name": info.get("name"),
        "userId": info.get("user_id"),
        "hasMemberId": bool(info.get("member_id")),
        "tokenExpiresInSeconds": info.get("token_expires_in_seconds"),
    })


def tool_list(args: dict[str, Any]) -> ToolResult:
    folder_id = str(args.get("folder_id") or "0")
    structured = _normalize_listing(folder_id, client().list_dir(folder_id))
    folders, docs = structured["folders"], structured["documents"]
    lines = [f"目录 {folder_id} 下共 {len(folders)} 个文件夹、{len(docs)} 篇文档："]
    lines += [_entry(f, "文件夹") for f in folders]
    lines += [_entry(d, "文档") for d in docs]
    return ToolResult("\n".join(lines), structured)


def _document_payload(doc_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = client().get_doc(doc_id)
    tree = client().doc_tree(raw)
    nodes = tree.get("nodes") or []
    metadata = {k: v for k, v in raw.items() if k != "definition"}
    return {"nodes": nodes, "rawMeta": metadata, "tree": tree}, metadata


def tool_get_doc(args: dict[str, Any]) -> ToolResult:
    """读文档：默认 Markdown；format=json 时返回**完整** JSON（绝不截断）。"""
    doc_id = str(args.get("doc_id") or "").strip()
    if not doc_id:
        raise ValueError("doc_id 不能为空")
    payload, metadata = _document_payload(doc_id)
    if (args.get("format") or "markdown").lower() == "json":
        structured = {
            "docId": doc_id,
            "name": metadata.get("name"),
            "baseVersion": metadata.get("baseVersion"),
            "metadata": metadata,
            "totalNodes": _count_nodes(payload["nodes"]),
            "nodes": payload["nodes"],
        }
        return ToolResult(json.dumps(structured, ensure_ascii=False, indent=2), structured)
    return ToolResult(mubu_markdown.tree_to_markdown({"nodes": payload["nodes"]}))


def _count_nodes(nodes: list) -> int:
    total = 0
    for node in nodes:
        total += 1 + _count_nodes(node.get("children") or [])
    return total


def tool_get_doc_json(args: dict[str, Any]) -> ToolResult:
    """结构化读文档，支持按顶层节点分页（游标）。"""
    doc_id = str(args.get("doc_id") or "").strip()
    if not doc_id:
        raise ValueError("doc_id 不能为空")
    payload, metadata = _document_payload(doc_id)
    nodes = payload["nodes"]

    cursor = args.get("cursor")
    try:
        offset = int(cursor) if cursor not in (None, "") else int(args.get("offset") or 0)
    except (TypeError, ValueError):
        raise ValueError("cursor/offset 必须是整数") from None
    limit = int(args.get("limit") or 20)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    page = nodes[offset:offset + limit]
    has_more = offset + limit < len(nodes)
    structured = {
        "docId": doc_id,
        "name": metadata.get("name"),
        "baseVersion": metadata.get("baseVersion"),
        "metadata": metadata,
        "totalTopLevelNodes": len(nodes),
        "totalNodes": _count_nodes(nodes),
        "offset": offset,
        "limit": limit,
        "hasMore": has_more,
        "nextCursor": str(offset + limit) if has_more else None,
        "nodes": page,
    }
    text = (f"文档「{structured['name'] or doc_id}」"
            f"顶层节点 {len(nodes)} 个，本次返回 {len(page)} 个"
            f"（offset={offset}，hasMore={has_more}）")
    return ToolResult(text, structured)


def tool_search(args: dict[str, Any]) -> ToolResult:
    keyword = str(args.get("keyword") or "").strip()
    if not keyword:
        raise ValueError("keyword 不能为空")
    include_content = bool(args.get("include_content"))
    limit = min(int(args.get("limit") or 20), 50)
    root = str(args.get("folder_id") or "0")
    needle = keyword.lower()
    hits: list[dict[str, Any]] = []
    visited = 0
    scanned_docs = 0
    queue = [(root, "")]
    truncated = False

    while queue:
        folder_id, path = queue.pop(0)
        if visited >= MAX_SEARCH_FOLDERS or len(hits) >= limit:
            truncated = True
            break
        visited += 1
        try:
            data = client().list_dir(folder_id)
        except MubuError as exc:
            hits.append({"type": "error", "folderId": folder_id, "message": str(exc)})
            continue
        for folder in data.get("folders") or []:
            name = str(folder.get("name") or "")
            if needle in name.lower():
                hits.append({"type": "folder", "id": folder.get("id"),
                             "name": name, "path": path})
            if path.count("/") < MAX_SEARCH_DEPTH:
                queue.append((str(folder.get("id")), f"{path}{name}/"))
        for doc in data.get("documents") or data.get("docs") or []:
            if len(hits) >= limit:
                truncated = True
                break
            name = str(doc.get("name") or "")
            matched = needle in name.lower()
            if not matched and include_content and scanned_docs < MAX_SEARCH_DOCS:
                scanned_docs += 1
                try:
                    tree = client().doc_tree(client().get_doc(str(doc.get("id"))))
                    matched = needle in mubu_markdown.tree_to_markdown(tree).lower()
                except MubuError:
                    matched = False
            if matched:
                hits.append({"type": "document", "id": doc.get("id"), "name": name,
                             "path": path, "updatedAt": doc.get("updateTime")})

    structured = {"keyword": keyword, "scannedFolders": visited, "truncated": truncated,
                  "hits": hits}
    if not hits:
        return ToolResult(
            f"没有找到包含「{keyword}」的内容（扫描了 {visited} 个目录）", structured)
    lines = [f"找到 {len(hits)} 条："]
    for hit in hits:
        kind = {"folder": "文件夹", "document": "文档"}.get(hit.get("type", ""), "提示")
        lines.append(f"- [{kind}] {hit.get('path', '')}{hit.get('name', hit.get('message', ''))}"
                     f"  id={hit.get('id', '')}")
    if truncated:
        lines.append("（结果被截断，请缩小范围）")
    return ToolResult("\n".join(lines), structured)


def _collect_field_names(node: Any, counter: dict[str, int], depth: int = 0) -> None:
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        counter[key] = counter.get(key, 0) + 1
        if key in ("children",) and isinstance(value, list):
            for child in value:
                _collect_field_names(child, counter, depth + 1)


def _redacted_sample(node: dict[str, Any]) -> dict[str, Any]:
    """给结构报告用的样例：正文一律不返回，只留类型与安全字段。"""
    safe_keys = {"id", "type", "width", "height", "size", "fileType", "mimeType",
                 "url", "src", "href", "target", "docId", "imageId", "finish", "checked"}
    sample: dict[str, Any] = {}
    for key, value in node.items():
        if key in ("children",):
            continue
        if key in safe_keys:
            sample[key] = value if not isinstance(value, str) else value[:120]
        else:
            sample[key] = f"<{type(value).__name__}>"
    return sample


def tool_inspect(args: dict[str, Any]) -> ToolResult:
    """对一篇文档做**脱敏**结构报告：字段名、计数、疑似图片/链接字段。

    正文内容不会出现在结果里，适合在调查图片/附件/链接字段时安全地分享。
    """
    doc_id = str(args.get("doc_id") or "").strip()
    if not doc_id:
        raise ValueError("doc_id 不能为空")
    payload, metadata = _document_payload(doc_id)
    nodes = payload["nodes"]
    counter: dict[str, int] = {}
    for node in nodes:
        _collect_field_names(node, counter)

    url_fields: dict[str, list[str]] = {}

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if isinstance(value, str) and value.startswith(("http://", "https://", "//")):
                url_fields.setdefault(key, [])
                if len(url_fields[key]) < MAX_FIELD_SAMPLES:
                    host = value.split("/")[2] if "//" in value else ""
                    url_fields[key].append(f"<url host={host}>")
            elif isinstance(value, dict):
                walk(value)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

    for node in nodes:
        walk(node)

    samples = [_redacted_sample(n) for n in nodes[:MAX_FIELD_SAMPLES]]
    structured = {
        "docId": doc_id,
        "name": metadata.get("name"),
        "definitionKeys": sorted((payload["tree"] or {}).keys()),
        "nodeFieldCounts": dict(sorted(counter.items())),
        "topLevelNodes": len(nodes),
        "totalNodes": _count_nodes(nodes),
        "urlFields": url_fields,
        "redactedSamples": samples,
        "note": "正文与备注已脱敏；只保留字段名、类型和 URL 主机名。",
    }
    text = (f"文档「{structured['name'] or doc_id}」结构报告："
            f"{len(counter)} 种节点字段，共 {structured['totalNodes']} 个节点，"
            f"疑似链接字段：{', '.join(url_fields) or '无'}")
    return ToolResult(text, structured)


def tool_diagnostics(_args: dict[str, Any]) -> ToolResult:
    """脱敏诊断：请求次数、重试次数、限流等待次数、最近错误码。不含正文与凭据。"""
    instance = client()
    structured = {
        "version": __version__,
        "requests": instance.stats.get("requests", 0),
        "retries": instance.stats.get("retries", 0),
        "rateLimitWaits": instance.stats.get("rate_limit_waits", 0),
        "logins": instance.stats.get("logins", 0),
        "lastErrorCode": instance.last_error_code,
        "minIntervalMs": int(instance.min_interval * 1000),
        "jitterMs": int(instance.jitter * 1000),
        "maxRetries": instance.max_retries,
        "processLock": instance.use_process_lock,
    }
    text = (f"请求 {structured['requests']} 次、重试 {structured['retries']} 次、"
            f"限流等待 {structured['rateLimitWaits']} 次、登录 {structured['logins']} 次；"
            f"最近错误码 {structured['lastErrorCode']}")
    return ToolResult(text, structured)


def tool_create_doc(args: dict[str, Any]) -> ToolResult:
    name = str(args.get("name") or "").strip()
    markdown = str(args.get("markdown") or "")
    folder_id = str(args.get("folder_id") or "0")
    if not name:
        raise ValueError("name 不能为空")
    tree = mubu_markdown.markdown_to_tree(markdown, title=name)
    doc_id = client().import_doc(name, [tree], folder_id=folder_id)
    structured = {"docId": doc_id, "name": name, "folderId": folder_id}
    if not doc_id:
        return ToolResult(f"已提交创建「{name}」，但接口没有返回文档 id，请在幕布里确认。",
                          structured)
    return ToolResult(f"已在幕布创建文档「{name}」，id={doc_id}（可在幕布里打开确认）",
                      structured)


def tool_create_folder(args: dict[str, Any]) -> ToolResult:
    name = str(args.get("name") or "").strip()
    folder_id = str(args.get("folder_id") or "0")
    if not name:
        raise ValueError("name 不能为空")
    new_id = client().create_folder(name, folder_id=folder_id)
    return ToolResult(f"已创建文件夹「{name}」，id={new_id or '(未返回)'}",
                      {"folderId": new_id, "name": name, "parentId": folder_id})


# --------------------------------------------------------------------------
# 工具注册表
# --------------------------------------------------------------------------

_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "folderId": {"type": "string"},
        "folderName": {"type": ["string", "null"]},
        "folders": {"type": "array", "items": {"type": "object"}},
        "documents": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["folderId", "folders", "documents"],
}

_DOC_PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "docId": {"type": "string"},
        "name": {"type": ["string", "null"]},
        "baseVersion": {"type": ["integer", "null"]},
        "metadata": {"type": "object"},
        "totalTopLevelNodes": {"type": "integer"},
        "totalNodes": {"type": "integer"},
        "offset": {"type": "integer"},
        "limit": {"type": "integer"},
        "hasMore": {"type": "boolean"},
        "nextCursor": {"type": ["string", "null"]},
        "nodes": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["docId", "nodes", "offset", "limit", "hasMore"],
}

_INSPECT_SCHEMA = {
    "type": "object",
    "properties": {
        "docId": {"type": "string"},
        "name": {"type": ["string", "null"]},
        "definitionKeys": {"type": "array", "items": {"type": "string"}},
        "nodeFieldCounts": {"type": "object"},
        "topLevelNodes": {"type": "integer"},
        "totalNodes": {"type": "integer"},
        "urlFields": {"type": "object"},
        "redactedSamples": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["docId", "nodeFieldCounts", "totalNodes"],
}


@dataclass
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    func: Callable[[dict[str, Any]], ToolResult]
    write: bool = False
    output_schema: dict[str, Any] | None = None

    def to_mcp(self) -> dict[str, Any]:
        payload = {"name": self.name, "description": self.description,
                   "inputSchema": self.schema}
        if self.output_schema is not None:
            payload["outputSchema"] = self.output_schema
        return payload


def all_tools() -> list[Tool]:
    return [
        Tool("mubu_whoami", "查看当前幕布账号信息和登录状态。",
             {"type": "object", "properties": {}, "additionalProperties": False},
             tool_whoami),
        Tool("mubu_list", "列出幕布某个目录下的文件夹和文档（同时返回结构化数据）。",
             {"type": "object",
              "properties": {"folder_id": {"type": "string",
                                           "description": "目录 id，默认 \"0\"（根目录）"}},
              "additionalProperties": False},
             tool_list, output_schema=_LIST_SCHEMA),
        Tool("mubu_get_doc",
             "读取一篇幕布文档，默认返回 Markdown 大纲；format=json 时返回完整 JSON"
             "（不截断，大文档请用 mubu_get_doc_json 分页）。",
             {"type": "object",
              "properties": {
                  "doc_id": {"type": "string", "description": "文档 id"},
                  "format": {"type": "string", "enum": ["markdown", "json"],
                             "description": "返回格式，默认 markdown"}},
              "required": ["doc_id"], "additionalProperties": False},
             tool_get_doc),
        Tool("mubu_get_doc_json",
             "结构化读取文档，可按顶层节点分页（cursor 传上一次的 nextCursor）。",
             {"type": "object",
              "properties": {
                  "doc_id": {"type": "string"},
                  "cursor": {"type": "string", "description": "上一次返回的 nextCursor"},
                  "offset": {"type": "integer", "description": "顶层节点偏移，默认 0"},
                  "limit": {"type": "integer",
                            "description": "本次返回的顶层节点数，默认 20，上限 200"}},
              "required": ["doc_id"], "additionalProperties": False},
             tool_get_doc_json, output_schema=_DOC_PAGE_SCHEMA),
        Tool("mubu_search", "在幕布目录里按关键词搜索文件夹名和文档名（可选搜正文）。",
             {"type": "object",
              "properties": {
                  "keyword": {"type": "string", "description": "关键词"},
                  "include_content": {"type": "boolean",
                                      "description": "是否同时搜索文档正文，较慢"},
                  "limit": {"type": "integer", "description": "最多返回多少条，默认 20，上限 50"},
                  "folder_id": {"type": "string", "description": "搜索起点目录，默认根目录"}},
              "required": ["keyword"], "additionalProperties": False},
             tool_search),
        Tool("mubu_inspect", "对文档做脱敏结构报告：字段名、计数、疑似图片/链接字段。"
                             "不含正文，适合安全地分享给他人排查接口结构。",
             {"type": "object",
              "properties": {"doc_id": {"type": "string"}},
              "required": ["doc_id"], "additionalProperties": False},
             tool_inspect, output_schema=_INSPECT_SCHEMA),
        Tool("mubu_diagnostics", "脱敏诊断信息：请求次数、重试、限流等待、最近错误码。",
             {"type": "object", "properties": {}, "additionalProperties": False},
             tool_diagnostics),
        Tool("mubu_create_doc",
             "在幕布新建一篇文档，Markdown 会转成幕布大纲"
             "（支持标题、缩进列表、- [x] 勾选、> 备注）。",
             {"type": "object",
              "properties": {
                  "name": {"type": "string", "description": "文档标题"},
                  "markdown": {"type": "string", "description": "正文 Markdown"},
                  "folder_id": {"type": "string", "description": "存放目录 id，默认根目录 \"0\""}},
              "required": ["name", "markdown"], "additionalProperties": False},
             tool_create_doc, write=True),
        Tool("mubu_create_folder", "在幕布新建一个文件夹。",
             {"type": "object",
              "properties": {
                  "name": {"type": "string", "description": "文件夹名称"},
                  "folder_id": {"type": "string", "description": "父目录 id，默认根目录 \"0\""}},
              "required": ["name"], "additionalProperties": False},
             tool_create_folder, write=True),
    ]


def public_tools(read_only: bool) -> list[dict[str, Any]]:
    return [t.to_mcp() for t in all_tools() if not (read_only and t.write)]


def handlers(read_only: bool) -> dict[str, Tool]:
    return {t.name: t for t in all_tools() if not (read_only and t.write)}


def read_only_mode(explicit: bool = False) -> bool:
    if explicit:
        return True
    return (os.environ.get("MUBU_READ_ONLY", "") or "").strip().lower() in {
        "1", "true", "yes", "on"}


# --------------------------------------------------------------------------
# MCP 协议（stdio，换行分隔的 JSON-RPC 2.0）
# --------------------------------------------------------------------------

def _tool_result_payload(result: ToolResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "content": [{"type": "text", "text": result.text}],
        "isError": False,
    }
    if result.structured is not None:
        payload["structuredContent"] = result.structured
    return payload


def handle(request: dict[str, Any], read_only: bool = False,
           cancel_event: threading.Event | None = None) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
        }}

    if method in ("notifications/initialized", "initialized",
                  "notifications/cancelled"):
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id,
                "result": {"tools": public_tools(read_only)}}

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        tool = handlers(read_only).get(name or "")
        if tool is None:
            message = f"未知工具：{name}"
            if name and any(t.name == name for t in all_tools()):
                message = f"工具 {name} 在只读模式下被禁用（MUBU_READ_ONLY=1）"
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32602, "message": message}}
        try:
            return {"jsonrpc": "2.0", "id": request_id,
                    "result": _tool_result_payload(tool.func(arguments))}
        except CancelledError:
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": "调用已取消"}], "isError": True}}
        except MissingCredentials as exc:
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": f"凭据未配置：{exc}"}], "isError": True}}
        except Exception as exc:  # noqa: BLE001 - 任何错误都要回给调用方
            return {"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": f"调用失败：{exc}"}], "isError": True}}

    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"resources": []}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"prompts": []}}

    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32601, "message": f"不支持的方法：{method}"}}


def serve(read_only: bool = False) -> int:
    configure_stdio(protocol=True)
    read_only = read_only_mode(read_only)
    log(f"v{__version__} 已启动（stdio{', 只读模式' if read_only else ''}）")

    cancels: dict[str, threading.Event] = {}
    write_lock = threading.Lock()
    workers: list[threading.Thread] = []

    def emit(payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        with write_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def run(request: dict[str, Any], event: threading.Event) -> None:
        try:
            response = handle(request, read_only=read_only, cancel_event=event)
        except Exception as exc:  # noqa: BLE001 - 兜底，绝不让工作线程静默死掉
            response = {"jsonrpc": "2.0", "id": request.get("id"),
                        "error": {"code": -32603, "message": f"内部错误：{exc}"}}
        finally:
            cancels.pop(str(request.get("id")), None)
        if response is not None:
            emit(response)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue

        # 取消通知：把对应请求的取消事件置位
        if request.get("method") == "notifications/cancelled":
            target = (request.get("params") or {}).get("requestId")
            event = cancels.get(str(target))
            if event is not None:
                event.set()
            continue

        if "id" not in request:  # 其他通知，忽略
            continue

        event = threading.Event()
        cancels[str(request["id"])] = event
        worker = threading.Thread(target=run, args=(request, event), daemon=True)
        workers.append(worker)
        worker.start()

    for worker in workers:
        worker.join(timeout=5)
    return 0
