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
        sample = (
            "# 产品周会\n"
            "- 上周进展\n"
            "  - [x] 上线新版本\n"
            "  - [ ] 修复登录 bug\n"
            "> 备注：记得同步给设计团队\n"
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
