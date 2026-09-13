"""离线自检：不联网、不需要账号，用于安装后确认基本功能正常。"""

from __future__ import annotations

import sys

from . import mubu_markdown, server

SAMPLE = (
    "# 产品周会\n"
    "- 上周进展\n"
    "  - [x] 上线新版本\n"
    "  - [ ] 修复登录 bug\n"
    "> 备注：记得同步给设计团队\n"
    "- 本周计划\n"
    "  - 性能优化\n"
)


def run_selftest(verbose: bool = True) -> int:
    def say(msg: str) -> None:
        if verbose:
            print(msg, file=sys.stderr)

    tree = mubu_markdown.markdown_to_tree(SAMPLE)
    assert tree["text"] == "产品周会", tree["text"]
    assert [c["text"] for c in tree["children"]] == ["上周进展", "本周计划"]
    assert tree["children"][0]["children"][0]["finish"] is True
    assert tree["children"][0]["children"][1].get("finish") is False
    assert tree["children"][0].get("note") == "备注：记得同步给设计团队"

    once = mubu_markdown.tree_to_markdown({"nodes": [tree]})
    twice = mubu_markdown.tree_to_markdown(
        {"nodes": [mubu_markdown.markdown_to_tree(once)]})
    assert once == twice, once + "\n---\n" + twice
    assert "- [x] 上线新版本" in once

    # 文档名和 Markdown 首个标题同名时不应该重复
    titled = mubu_markdown.markdown_to_tree(SAMPLE, title="产品周会")
    assert [c["text"] for c in titled["children"]] == ["上周进展", "本周计划"]

    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["serverInfo"]["name"] == server.SERVER_NAME
    listing = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert len(listing["result"]["tools"]) == len(server.all_tools())
    readonly = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
                             read_only=True)
    assert all(not t["name"].startswith("mubu_create") for t in readonly["result"]["tools"])
    unknown = server.handle({"jsonrpc": "2.0", "id": 4, "method": "no/such"})
    assert unknown["error"]["code"] == -32601

    say("自检通过：Markdown 往返、只读模式、MCP 握手与工具清单都正常")
    print("selftest: OK")
    return 0
