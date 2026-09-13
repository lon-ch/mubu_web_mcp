"""MCP 服务本体（stdio 传输，零第三方依赖）。

默认暴露的工具都是保守的：只读 + 新建，不含删除、重命名、覆盖已有文档。
设置环境变量 ``MUBU_READ_ONLY=1``（或命令行 ``--read-only``）后，
连新建类工具也会从工具列表里消失。
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import __version__, mubu_markdown
from .mubu_client import MissingCredentials, MubuClient, MubuError

SERVER_NAME = "mubu_web_mcp"
DEFAULT_PROTOCOL = "2025-06-18"

MAX_SEARCH_FOLDERS = 100
MAX_SEARCH_DOCS = 300
MAX_SEARCH_DEPTH = 3


def log(message: str) -> None:
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


def configure_stdio(protocol: bool) -> None:
    """让输出在编码受限的环境下也不会崩。

    ``protocol=True`` 时（MCP 的 stdio 传输）强制 UTF-8：MCP 规定消息必须是 UTF-8，
    而 Windows 在管道里默认会用本地代码页（英文系统是 cp1252），
    不改的话只要工具返回中文就会 UnicodeEncodeError。
    交互式运行时保持控制台原有编码，只把错误处理改成不抛异常，
    这样中文控制台显示仍然正常。
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


_client: MubuClient | None = None


def client() -> MubuClient:
    global _client
    if _client is None:
        _client = MubuClient()
    return _client


# --------------------------------------------------------------------------
# 工具实现
# --------------------------------------------------------------------------

def _entry(entry: dict[str, Any], kind: str) -> str:
    return f"- [{kind}] {entry.get('name') or '(未命名)'}  id={entry.get('id', '')}"


def tool_whoami(_args: dict[str, Any]) -> str:
    info = client().whoami()
    return "\n".join([
        f"账号：{info.get('name') or '(未知)'}",
        f"user_id：{info.get('user_id') or '(未知)'}",
        f"member_id：{'已获取' if info.get('member_id') else '未获取'}",
        f"token 剩余有效期：约 {info.get('token_expires_in_seconds', 0) // 60} 分钟",
    ])


def tool_list(args: dict[str, Any]) -> str:
    folder_id = str(args.get("folder_id") or "0")
    data = client().list_dir(folder_id)
    folders = data.get("folders") or []
    docs = data.get("documents") or data.get("docs") or []
    lines = [f"目录 {folder_id} 下共 {len(folders)} 个文件夹、{len(docs)} 篇文档："]
    lines += [_entry(f, "文件夹") for f in folders]
    lines += [_entry(d, "文档") for d in docs]
    return "\n".join(lines)


def tool_get_doc(args: dict[str, Any]) -> str:
    doc_id = str(args.get("doc_id") or "").strip()
    if not doc_id:
        raise ValueError("doc_id 不能为空")
    tree = client().doc_tree(client().get_doc(doc_id))
    if (args.get("format") or "markdown").lower() == "json":
        return json.dumps(tree, ensure_ascii=False, indent=2)[:60000]
    return mubu_markdown.tree_to_markdown(tree)


def tool_search(args: dict[str, Any]) -> str:
    keyword = str(args.get("keyword") or "").strip()
    if not keyword:
        raise ValueError("keyword 不能为空")
    include_content = bool(args.get("include_content"))
    limit = min(int(args.get("limit") or 20), 50)
    needle = keyword.lower()
    hits: list[str] = []
    visited = 0
    scanned_docs = 0
    queue = [("0", "")]
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
            hits.append(f"- (无法读取目录 {folder_id}：{exc})")
            continue
        for folder in data.get("folders") or []:
            name = str(folder.get("name") or "")
            if needle in name.lower():
                hits.append(f"- [文件夹] {path}{name}  id={folder.get('id')}")
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
                hits.append(f"- [文档] {path}{name}  id={doc.get('id')}")

    if not hits:
        return f"没有找到包含「{keyword}」的内容（扫描了 {visited} 个目录）"
    suffix = "\n（结果被截断，请缩小范围）" if truncated else ""
    return f"找到 {len(hits)} 条：\n" + "\n".join(hits) + suffix


def tool_create_doc(args: dict[str, Any]) -> str:
    name = str(args.get("name") or "").strip()
    markdown = str(args.get("markdown") or "")
    folder_id = str(args.get("folder_id") or "0")
    if not name:
        raise ValueError("name 不能为空")
    tree = mubu_markdown.markdown_to_tree(markdown, title=name)
    doc_id = client().import_doc(name, [tree], folder_id=folder_id)
    if not doc_id:
        return f"已提交创建「{name}」，但接口没有返回文档 id，请在幕布里确认。"
    return f"已在幕布创建文档「{name}」，id={doc_id}（可在幕布里打开确认）"


def tool_create_folder(args: dict[str, Any]) -> str:
    name = str(args.get("name") or "").strip()
    folder_id = str(args.get("folder_id") or "0")
    if not name:
        raise ValueError("name 不能为空")
    new_id = client().create_folder(name, folder_id=folder_id)
    return f"已创建文件夹「{name}」，id={new_id or '(未返回)'}"


# --------------------------------------------------------------------------
# 工具注册表
# --------------------------------------------------------------------------

@dataclass
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    func: Callable[[dict[str, Any]], str]
    write: bool = False

    def to_mcp(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "inputSchema": self.schema}


def all_tools() -> list[Tool]:
    return [
        Tool("mubu_whoami", "查看当前幕布账号信息和登录状态。",
             {"type": "object", "properties": {}, "additionalProperties": False},
             tool_whoami),
        Tool("mubu_list", "列出幕布某个目录下的文件夹和文档。folder_id 传 \"0\" 表示根目录。",
             {"type": "object",
              "properties": {"folder_id": {"type": "string",
                                           "description": "目录 id，默认 \"0\"（根目录）"}},
              "additionalProperties": False},
             tool_list),
        Tool("mubu_get_doc", "读取一篇幕布文档的完整内容，默认返回 Markdown 大纲。",
             {"type": "object",
              "properties": {
                  "doc_id": {"type": "string", "description": "文档 id"},
                  "format": {"type": "string", "enum": ["markdown", "json"],
                             "description": "返回格式，默认 markdown"}},
              "required": ["doc_id"], "additionalProperties": False},
             tool_get_doc),
        Tool("mubu_search", "在幕布目录里按关键词搜索文件夹名和文档名（可选搜正文）。",
             {"type": "object",
              "properties": {
                  "keyword": {"type": "string", "description": "关键词"},
                  "include_content": {"type": "boolean",
                                      "description": "是否同时搜索文档正文，较慢"},
                  "limit": {"type": "integer", "description": "最多返回多少条，默认 20，上限 50"}},
              "required": ["keyword"], "additionalProperties": False},
             tool_search),
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

def handle(request: dict[str, Any], read_only: bool = False) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
        }}

    if method in ("notifications/initialized", "initialized"):
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
                    "result": {"content": [{"type": "text", "text": tool.func(arguments)}],
                               "isError": False}}
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
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        response = handle(request, read_only=read_only)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0
