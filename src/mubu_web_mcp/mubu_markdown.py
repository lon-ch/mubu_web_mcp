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
# 幕布 → Markdown
# --------------------------------------------------------------------------

def _emit_notes(note: Any, indent: str, lines: list[str]) -> None:
    """备注保留段落结构：每一行单独输出一个引用行。"""
    text = str(note or "").rstrip()
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
            alt = _clean(image.get("alt")) or f"图片{position:03d}"
            lines.append(f"{indent}![{alt}]({image['local']})")
        else:
            lines.append(f"{indent}> 图片备份失败：原始图片地址已记录在备份清单中。")


def _node_to_markdown(node: dict[str, Any], level: int, lines: list[str],
                      image_resolver: Any = None) -> None:
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
    if checked is None:
        lines.append(f"{indent}{marker} {_clean(node.get('text'))}")
    else:
        lines.append(f"{indent}{marker} [{'x' if checked else ' '}] "
                     f"{_clean(node.get('text'))}")
    if image_resolver is not None:
        _emit_images(image_resolver(node), "  " * (level + 1), lines)
    for child in node.get("children") or []:
        _node_to_markdown(child, level + 1, lines, image_resolver)
    _emit_notes(node.get("note"), indent, lines)


def tree_to_markdown(tree: dict[str, Any], image_resolver: Any = None) -> str:
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
        title = _clean(node.get("text"))
        if title:
            lines.append(f"# {title}")
        if image_resolver is not None:
            _emit_images(image_resolver(node), "", lines)
        for child in node.get("children") or []:
            _node_to_markdown(child, 0, lines, image_resolver)
        _emit_notes(node.get("note"), "", lines)
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
            target = node_at(depth) or deepest_up_to(depth) or (top[-1] if top else None)
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
