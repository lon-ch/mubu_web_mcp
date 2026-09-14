"""Markdown ⇄ 幕布节点 的转换测试（纯本地）。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mubu_web_mcp import mubu_markdown  # noqa: E402


class MarkdownToTreeTests(unittest.TestCase):
    def test_title_and_nesting(self):
        tree = mubu_markdown.markdown_to_tree(
            "# 标题\n- 一级\n  - 二级\n    - 三级\n- 另一个一级\n")
        self.assertEqual(tree["text"], "标题")
        self.assertEqual([c["text"] for c in tree["children"]], ["一级", "另一个一级"])
        second = tree["children"][0]["children"][0]
        self.assertEqual(second["text"], "二级")
        self.assertEqual(second["children"][0]["text"], "三级")

    def test_checkbox_states(self):
        tree = mubu_markdown.markdown_to_tree(
            "# T\n- [x] done\n- [ ] todo\n- plain\n")
        children = tree["children"]
        self.assertIs(children[0]["finish"], True)
        self.assertIs(children[1]["finish"], False)
        self.assertNotIn("finish", children[2])

    def test_note_attaches_to_same_indent_level(self):
        tree = mubu_markdown.markdown_to_tree(
            "# T\n- A\n  - A1\n> note for A\n- B\n")
        self.assertEqual(tree["children"][0]["note"], "note for A")
        self.assertNotIn("note", tree["children"][0]["children"][0])

    def test_duplicate_title_is_skipped(self):
        tree = mubu_markdown.markdown_to_tree("# 同名\n- 项目\n", title="同名")
        self.assertEqual([c["text"] for c in tree["children"]], ["项目"])

    def test_numbered_list_is_accepted(self):
        tree = mubu_markdown.markdown_to_tree("# T\n1. 第一\n2. 第二\n")
        self.assertEqual([c["text"] for c in tree["children"]], ["第一", "第二"])

    def test_plain_text_without_heading_becomes_title(self):
        tree = mubu_markdown.markdown_to_tree("只有一行文字")
        self.assertEqual(tree["text"], "只有一行文字")
        self.assertEqual(tree["children"], [])

    def test_empty_input_is_safe(self):
        tree = mubu_markdown.markdown_to_tree("")
        self.assertEqual(tree["text"], "未命名")
        self.assertEqual(tree["children"], [])

    def test_every_node_gets_unique_id(self):
        tree = mubu_markdown.markdown_to_tree("# T\n- a\n- b\n  - c\n")
        ids = [tree["id"], tree["children"][0]["id"], tree["children"][1]["id"],
               tree["children"][1]["children"][0]["id"]]
        self.assertEqual(len(ids), len(set(ids)))


class TreeToMarkdownTests(unittest.TestCase):
    # ---- 富文本（幕布的 text/note 实际是 HTML）----

    def test_span_is_unwrapped(self):
        self.assertEqual(mubu_markdown.html_to_markdown("<span>文字</span>"), "文字")

    def test_inline_formatting_mapped(self):
        html = "<b>粗体</b> <i>斜体</i> <code>x=1</code>"
        self.assertEqual(mubu_markdown.html_to_markdown(html),
                         "**粗体** *斜体* `x=1`")

    def test_anchor_becomes_markdown_link(self):
        html = '<a href="https://example.com">示例</a>'
        self.assertEqual(mubu_markdown.html_to_markdown(html),
                         "[示例](https://example.com)")

    def test_table_becomes_pipe_table(self):
        html = ('<div class="table-container"><table class="auto-table">'
                "<thead><tr><th>标题</th><th>数量</th></tr></thead>"
                "<tbody><tr><td>甲</td><td>2</td></tr><tr><td>乙</td><td></td></tr>"
                "</tbody></table></div>")
        lines = mubu_markdown.html_to_markdown(html).splitlines()
        self.assertEqual(lines[0], "| 标题 | 数量 |")
        self.assertEqual(lines[1], "| --- | --- |")
        self.assertEqual(lines[2], "| 甲 | 2 |")
        self.assertEqual(lines[3], "| 乙 |  |")

    def test_unknown_tag_is_preserved(self):
        self.assertIn("<mark>", mubu_markdown.html_to_markdown("<mark>高亮</mark>"))

    def test_entities_are_unescaped(self):
        self.assertEqual(mubu_markdown.html_to_markdown("a &amp; b&nbsp;c"), "a & b c")

    def test_table_node_is_emitted_as_block_without_bullet(self):
        tree = {"nodes": [{"text": "T", "children": [
            {"id": "n1", "text": "<table><tr><th>A</th></tr><tr><td>1</td></tr></table>"}]}]}
        out = mubu_markdown.tree_to_markdown(tree)
        self.assertIn("| A |", out)
        self.assertNotIn("- | A |", out)

    def test_emoji_is_prefixed(self):
        tree = {"nodes": [{"text": "T", "children": [
            {"id": "n1", "text": "灵感", "emoji": "💡"}]}]}
        self.assertIn("- 💡 灵感", mubu_markdown.tree_to_markdown(tree))

    def test_image_numbers_are_document_wide(self):
        """跨节点的图片说明要连续编号（image-1/2/3），而不是每个节点都从 1 开始。"""
        tree = {"nodes": [{"text": "T", "children": [
            {"id": "n1", "text": "第一张", "children": []},
            {"id": "n2", "text": "第二张", "children": []}]}]}
        records = [
            {"nodeId": "n1", "status": "ok", "local": "T.assets/001.jpg", "alt": None},
            {"nodeId": "n2", "status": "ok", "local": "T.assets/002.jpg", "alt": None},
        ]
        out = mubu_markdown.tree_to_markdown(
            tree, image_resolver=mubu_markdown.make_image_resolver(records))
        self.assertIn("![image-1](T.assets/001.jpg)", out)
        self.assertIn("![image-2](T.assets/002.jpg)", out)

    def test_repeated_image_in_same_document_gets_new_number(self):
        tree = {"nodes": [{"text": "T", "children": [
            {"id": "n1", "text": "A", "children": []},
            {"id": "n2", "text": "B", "children": []}]}]}
        records = [
            {"nodeId": "n1", "status": "ok", "local": "T.assets/001.jpg", "alt": None},
            {"nodeId": "n2", "status": "ok", "local": "T.assets/001.jpg", "alt": None},
        ]
        out = mubu_markdown.tree_to_markdown(
            tree, image_resolver=mubu_markdown.make_image_resolver(records))
        self.assertIn("![image-1](", out)
        self.assertIn("![image-2](", out)

    def test_checkbox_from_finish_field(self):
        tree = {"nodes": [{"text": "T", "children": [
            {"id": "n1", "text": "未完成", "finish": False},
            {"id": "n2", "text": "已完成", "finish": True}]}]}
        out = mubu_markdown.tree_to_markdown(tree)
        self.assertIn("- [ ] 未完成", out)
        self.assertIn("- [x] 已完成", out)

    def test_checkbox_from_task_status(self):
        """真实文档里 finish 恒为 false，勾选状态由 taskStatus 决定（1 未完成 / 2 已完成）。"""
        tree = {"nodes": [{"text": "T", "children": [
            {"id": "n1", "text": "勾选", "finish": False, "taskStatus": 1},
            {"id": "n2", "text": "已勾选", "finish": False, "taskStatus": 2}]}]}
        out = mubu_markdown.tree_to_markdown(tree)
        self.assertIn("- [ ] 勾选", out)
        self.assertIn("- [x] 已勾选", out)

    def test_images_are_emitted_at_node_position(self):
        tree = {"nodes": [{"id": "root", "text": "文档", "children": [
            {"id": "n1", "text": "第一节", "children": [
                {"id": "n1-1", "text": "子节点", "children": []}]}]}]}
        records = [{"nodeId": "n1", "status": "ok", "local": "文档.assets/001.png",
                    "alt": "配图"}]
        text = mubu_markdown.tree_to_markdown(
            tree, image_resolver=mubu_markdown.make_image_resolver(records))
        self.assertIn("![配图](文档.assets/001.png)", text)
        # 图片在该节点之后、其子节点之前
        self.assertLess(text.index("![配图]"), text.index("- 子节点"))

    def test_failed_image_becomes_placeholder(self):
        tree = {"nodes": [{"id": "root", "text": "文档", "children": [
            {"id": "n1", "text": "第一节", "children": []}]}]}
        records = [{"nodeId": "n1", "status": "failed", "local": None, "alt": None}]
        text = mubu_markdown.tree_to_markdown(
            tree, image_resolver=mubu_markdown.make_image_resolver(records))
        self.assertIn("图片备份失败", text)
        self.assertNotIn("![", text)

    def test_multiline_note_keeps_paragraphs(self):
        tree = {"nodes": [{"text": "T", "children": [
            {"id": "n1", "text": "A", "note": "第一段\n\n第二段", "children": []}]}]}
        text = mubu_markdown.tree_to_markdown(tree)
        self.assertIn("> 第一段", text)
        self.assertIn("> ", text)
        self.assertIn("> 第二段", text)
        quoted = [line for line in text.splitlines() if line.strip().startswith(">")]
        self.assertEqual(len(quoted), 3)

    def test_ordered_list_marker_when_flagged(self):
        tree = {"nodes": [{"text": "T", "children": [
            {"id": "n1", "text": "第一步", "listType": "ordered", "children": []},
            {"id": "n2", "text": "第二步", "children": []}]}]}
        text = mubu_markdown.tree_to_markdown(tree)
        self.assertIn("1. 第一步", text)
        self.assertIn("- 第二步", text)

    def test_tree_to_markdown_without_resolver_is_unchanged(self):
        tree = {"nodes": [{"text": "T", "children": [
            {"id": "n1", "text": "A", "children": []}]}]}
        self.assertEqual(mubu_markdown.tree_to_markdown(tree), "# T\n\n- A")

    def test_title_from_document_name(self):
        """给了 title 就用文档名做标题，第一个节点作为正文（与官方导出一致）。"""
        tree = {"nodes": [{"text": "作业思考", "children": []}]}
        out = mubu_markdown.tree_to_markdown(tree, title="🧭2.项目/钱/工作")
        self.assertEqual(out.splitlines()[0], "# 🧭2.项目/钱/工作")
        self.assertIn("- 作业思考", out)

    def test_renders_notes_and_checkboxes(self):
        tree = {"nodes": [{
            "id": "root", "text": "标题",
            "children": [
                {"id": "1", "text": "A", "note": "备注", "children": [
                    {"id": "2", "text": "A1", "finish": True, "children": []}]},
            ]}]}
        text = mubu_markdown.tree_to_markdown(tree)
        self.assertIn("# 标题", text)
        self.assertIn("- A", text)
        self.assertIn("  - [x] A1", text)
        self.assertIn("> 备注", text)

    def test_empty_tree(self):
        self.assertEqual(mubu_markdown.tree_to_markdown({}), "")

    def test_accepts_bare_node(self):
        text = mubu_markdown.tree_to_markdown({"text": "T", "children": []})
        self.assertIn("# T", text)

    def test_round_trip_is_stable(self):
        # 官方导出约定：备注紧跟节点行、缩进深一级，然后才是子节点
        sample = (
            "# 产品周会\n"
            "\n"
            "- 上周进展\n"
            "> 备注：记得同步给设计团队\n"
            "  - [x] 上线新版本\n"
            "  - [ ] 修复登录 bug\n"
            "- 本周计划\n"
            "  - 性能优化\n"
        )
        once = mubu_markdown.tree_to_markdown(
            {"nodes": [mubu_markdown.markdown_to_tree(sample)]})
        twice = mubu_markdown.tree_to_markdown(
            {"nodes": [mubu_markdown.markdown_to_tree(once)]})
        self.assertEqual(once, twice)
        self.assertEqual(once.strip(), sample.strip())


if __name__ == "__main__":
    unittest.main()
