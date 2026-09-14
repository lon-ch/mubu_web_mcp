"""Markdown ⇄ 幕布大纲节点 的相互转换。

幕布文档结构（``get_doc`` 返回的 definition）::

    {"nodes": [ {"text": 文档标题, "children": [节点...]} ]}

节点字段：``text`` / ``children`` / ``note``（备注）/ ``finish``（是否勾选）
写回时用：``{"id": ..., "text": ..., "children": [...], "note": ..., "finish": bool}``
"""

from __future__ import annotations

import re
import uuid
from typing import Any

__all__ = ["make_image_resolver", "markdown_to_tree", "tree_to_markdown"]


def _new_node(text: str) -> dict[str, Any]:
    return {"id": uuid.uuid4().hex[:12], "text": text, "children": []}


def _clean(text: Any) -> str:
    return str(text or "").replace("\n", " ").strip()


# --------------------------------------------------------------------------
# 富文本：幕布的 text/note 里其实是 HTML（<span>、<table>、<b>…）
# --------------------------------------------------------------------------

_TABLE_RE = re.compile(r"<table[\s\S]*?</table>", re.IGNORECASE)
_ANCHOR_RE = re.compile(r'<a[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a>', re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_ROW_RE = re.compile(r"<tr[\s\S]*?</tr>", re.IGNORECASE)
_CELL_RE = re.compile(r"<t[hd][^>]*>([\s\S]*?)</t[hd]>", re.IGNORECASE)

_INLINE_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    (r"<br\s*/?>", "\n"),
    (r"</?(?:span|div|p|font|u|tbody|thead)[^>]*>", ""),
    (r"<(?:b|strong)[^>]*>", "**"),
    (r"</(?:b|strong)>", "**"),
    (r"<(?:i|em)[^>]*>", "*"),
    (r"</(?:i|em)>", "*"),
    (r"<(?:code|tt)[^>]*>", "`"),
    (r"</(?:code|tt)>", "`"),
    (r"<img[^>]*alt=\"([^\"]*)\"[^>]*>", r"![\1]"),
)


def _plain_text(fragment: str) -> str:
    """剥掉标签并还原实体，用于表格单元格等内容。"""
    import html as _html

    return _html.unescape(_TAG_RE.sub("", fragment)).replace("\xa0", " ").strip()


def _table_to_markdown(html_table: str) -> str:
    """把幕布的 HTML 表格转成标准 Markdown 竖线表格（与官方导出一致）。"""
    rows: list[list[str]] = []
    for row_html in _ROW_RE.findall(html_table):
        cells = [_plain_text(cell).replace("\n", " ")
                 for cell in _CELL_RE.findall(row_html)]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    header = rows[0]
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in rows[1:]]
    return "\n".join(lines)


def html_to_markdown(text: Any) -> str:
    """把幕布富文本转成 Markdown。

    认得的标签转成 Markdown，认不出的**原样保留**（宁可留着也不静默丢内容）。
    """
    import html as _html

    raw = str(text or "")
    if "<" not in raw:
        return _html.unescape(raw).replace("\xa0", " ")

    tables: list[str] = []

    def stash(match: re.Match) -> str:
        tables.append(_table_to_markdown(match.group(0)))
        return f"\x00{len(tables) - 1}\x00"

    result = _TABLE_RE.sub(stash, raw)
    result = _ANCHOR_RE.sub(
        lambda m: f"[{_plain_text(m.group(2))}]({m.group(1)})", result)
    for pattern, replacement in _INLINE_REPLACEMENTS:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    result = _html.unescape(result).replace("\xa0", " ")
    for index, table in enumerate(tables):
        result = result.replace(f"\x00{index}\x00", table)
    return result


# --------------------------------------------------------------------------
# 幕布 → Markdown
# --------------------------------------------------------------------------

def _emit_notes(note: Any, indent: str, lines: list[str]) -> None:
    """备注保留段落结构：每一行单独输出一个引用行。"""
    text = html_to_markdown(note).rstrip()
    if not text:
        return
    for line in text.splitlines():
        lines.append(f"{indent}> {_clean(line)}" if line.strip() else f"{indent}>")


def make_image_resolver(records: list[dict[str, Any]],
                        fields: tuple[str, ...] = ()):
    """把备份引擎产出的图片记录变成 (node) -> 图片列表 的解析器。

    ``fields`` 预留：真实文档字段确认后可用它做精确匹配（当前由备份引擎负责识别）。
    """
    by_node: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        node_id = str(record.get("nodeId") or "")
        by_node.setdefault(node_id, []).append(record)

    def resolver(node: dict[str, Any]) -> list[dict[str, Any]]:
        return by_node.get(str(node.get("id") or ""), [])

    return resolver


def _emit_images(images: list[dict[str, Any]] | None, indent: str,
                 lines: list[str]) -> None:
    for position, image in enumerate(images or [], start=1):
        if image.get("status") == "ok" and image.get("local"):
            alt = _clean(image.get("alt")) or f"image-{position}"
            lines.append(f"{indent}![{alt}]({image['local']})")
        else:
            lines.append(f"{indent}> 图片备份失败：原始图片地址已记录在备份清单中。")


# 幕布内部文档链接：https://mubu.com/app/edit/home/<文档ID>
MUBU_DOC_LINK = re.compile(r"https?://mubu\.com/app/edit/home/([A-Za-z0-9]+)")


def rewrite_mubu_links(text: str, link_resolver: Any = None) -> str:
    """把幕布内部文档链接换成本地相对路径；解析不到就保留原链接。"""
    if not text or link_resolver is None:
        return text

    def replace(match: re.Match) -> str:
        return link_resolver(match.group(1)) or match.group(0)

    return MUBU_DOC_LINK.sub(replace, text)


def _task_metadata(node: dict[str, Any]) -> str:
    """幕布特有的任务字段保留成 HTML 注释：渲染时不可见，也不污染正文。"""
    parts: list[str] = []
    for key in ("taskStatus", "deadline", "remindAt"):
        value = node.get(key)
        if value not in (None, 0):
            parts.append(f"{key}={value}")
    if node.get("collapsed"):
        parts.append("collapsed=true")
    return f"<!-- mubu: {' '.join(parts)} -->" if parts else ""


def _node_to_markdown(node: dict[str, Any], level: int, lines: list[str],
                      image_resolver: Any = None, link_resolver: Any = None) -> None:
    if not isinstance(node, dict):
        return
    indent = "  " * level
    # 有序列表：字段确认前只在明确给出有序标记时才输出编号
    list_type = str(node.get("listType") or node.get("list_type") or "").lower()
    ordered = list_type in ("ordered", "number", "ol") or node.get("ordered") is True
    marker = "1." if ordered else "-"
    checked = node.get("finish")
    if checked is None:
        checked = node.get("checked")
    rendered = html_to_markdown(node.get("text")).strip()
    emoji = str(node.get("emoji") or "").strip()
    if emoji:
        rendered = f"{emoji} {rendered}".strip()
    if "\n" in rendered:
        # 表格等块级内容：整块输出，不加列表标记（与官方导出一致）
        for line in rendered.splitlines():
            lines.append(f"{indent}{rewrite_mubu_links(line, link_resolver)}")
        text = ""
    else:
        text = rewrite_mubu_links(rendered, link_resolver)
    if checked is None:
        if text:
            lines.append(f"{indent}{marker} {text}")
    else:
        if text:
            lines.append(f"{indent}{marker} [{'x' if checked else ' '}] {text}")
    metadata = _task_metadata(node)
    if metadata:
        lines.append(f"{indent}{metadata}")
    # 官方导出约定：备注紧跟节点行、缩进深一级，然后才是子节点
    _emit_notes(rewrite_mubu_links(node.get("note") or "", link_resolver),
                "  " * (level + 1), lines)
    if image_resolver is not None:
        _emit_images(image_resolver(node), indent, lines)
    for child in node.get("children") or []:
        _node_to_markdown(child, level + 1, lines, image_resolver, link_resolver)


def tree_to_markdown(tree: dict[str, Any], image_resolver: Any = None,
                     link_resolver: Any = None) -> str:
    """把幕布文档结构渲染成 Markdown。

    ``image_resolver`` 由 :func:`make_image_resolver` 提供；不传则完全保持旧行为。
    """
    nodes = tree.get("nodes") if isinstance(tree, dict) else None
    if not nodes:
        if isinstance(tree, dict) and (tree.get("text") or tree.get("children")):
            nodes = [tree]
        else:
            return ""
    lines: list[str] = []
    for node in nodes:
        title = _clean(html_to_markdown(node.get("text")).splitlines()[0]
                       if html_to_markdown(node.get("text")).strip() else "")
        if title:
            lines.append(f"# {title}")
        metadata = _task_metadata(node)
        if metadata:
            lines.append(metadata)
        _emit_notes(rewrite_mubu_links(node.get("note") or "", link_resolver), "  ", lines)
        if image_resolver is not None:
            _emit_images(image_resolver(node), "", lines)
        for child in node.get("children") or []:
            _node_to_markdown(child, 0, lines, image_resolver, link_resolver)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Markdown → 幕布
# --------------------------------------------------------------------------

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
_BULLET = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(.*)$")
_NOTE = re.compile(r"^(\s*)>\s?(.*)$")
_CHECKBOX = re.compile(r"^\[([ xX])\]\s*(.*)$")


def markdown_to_tree(markdown: str, title: str | None = None) -> dict[str, Any]:
    """把 Markdown 解析成幕布节点树（标题 / 缩进列表 / 勾选 / 引用备注）。

    备注行的缩进决定它挂在哪一层：0 缩进属于顶层节点，2 个空格属于第二层，
    以此类推 —— 这跟 :func:`tree_to_markdown` 的输出规则互为逆运算。
    """
    doc_title = (title or "").strip()
    top: list[dict[str, Any]] = []
    stack: list[tuple] = []

    def node_at(target_depth: int) -> dict[str, Any] | None:
        found = None
        for depth, node in stack:
            if depth == target_depth:
                found = node
            elif depth > target_depth:
                break
        return found

    def deepest_up_to(max_depth: int) -> dict[str, Any] | None:
        found = None
        for depth, node in stack:
            if depth <= max_depth:
                found = node
            else:
                break
        return found

    for raw in markdown.splitlines():
        if not raw.strip():
            continue

        heading = _HEADING.match(raw)
        if heading:
            text = heading.group(1).strip()
            if not doc_title:
                doc_title = text
                stack = []
                continue
            # 文档名已经作为标题传入，Markdown 首个标题与它同名 —— 别重复
            if not top and text == doc_title:
                stack = []
                continue
            node = _new_node(text)
            top.append(node)
            stack = [(0, node)]
            continue

        note = _NOTE.match(raw)
        if note:
            depth = len(note.group(1)) // 2
            # 官方导出的备注比所属节点深一级
            target = (node_at(depth - 1) or node_at(depth)
                      or deepest_up_to(depth) or (top[-1] if top else None))
            if target is not None:
                target["note"] = note.group(2).strip()
            continue

        bullet = _BULLET.match(raw)
        if bullet:
            depth = len(bullet.group(1)) // 2
            text = bullet.group(2).strip()
            node = _new_node("")
            checkbox = _CHECKBOX.match(text)
            if checkbox:
                node["finish"] = checkbox.group(1).lower() == "x"
                text = checkbox.group(2).strip()
            node["text"] = text
            parent = node_at(depth - 1) if depth > 0 else None
            if parent is None:
                top.append(node)
            else:
                parent.setdefault("children", []).append(node)
            stack = [entry for entry in stack if entry[0] < depth] + [(depth, node)]
            continue

        node = _new_node(raw.strip())
        top.append(node)
        stack = [(0, node)]

    if not doc_title and top:
        # 没有标题也没传 title：把第一项当作标题，它的子节点提升为正文
        head = top[0]
        doc_title = head["text"]
        top = list(head.get("children") or []) + top[1:]
    if not doc_title:
        doc_title = "未命名"

    return {"id": "root", "text": doc_title, "children": top}
