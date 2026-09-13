"""测试公用小工具。"""

import os
import shutil
import tempfile
import uuid
from pathlib import Path


def temp_dir(case) -> Path:
    """创建临时目录，并在用例结束时尽力清理。

    不用 ``tempfile.TemporaryDirectory``：它在 Windows 上清理时会 chmod，
    在受限环境里可能直接抛 PermissionError，把测试结果搅乱。

    可以用环境变量 ``MUBU_TEST_TMPDIR`` 指定临时目录的父目录
    （在受限沙箱里跑测试时用得到）。
    """
    base = Path(os.environ.get("MUBU_TEST_TMPDIR") or tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"mubu-web-mcp-test-{uuid.uuid4().hex[:8]}"
    path.mkdir()
    case.addCleanup(lambda: shutil.rmtree(path, ignore_errors=True))
    return path
