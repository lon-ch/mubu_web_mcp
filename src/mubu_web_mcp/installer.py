"""把 MCP 服务写进各个 AI 客户端的配置里。

设计原则：

* 只改配置文件，不猜、不覆盖别人的配置项；写之前先备份成 ``<file>.bak``。
* 幂等：重复执行只会把 ``mubu_web_mcp`` 这一项更新成最新值。
* 探测不到的客户端不硬写，只打印手动配置方法。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

SERVER_KEY = "mubu_web_mcp"

try:  # Python 3.11+ 自带；3.10 上只跳过 TOML 校验
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 取决于解释器版本
    tomllib = None


def _home() -> Path:
    return Path.home()


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or (_home() / "AppData" / "Roaming"))


def _package_src_dir() -> Path | None:
    """源码运行时返回 src 目录，装在 site-packages 时返回 None。"""
    here = Path(__file__).resolve()
    candidate = here.parents[1]  # .../src
    if (candidate / "mubu_web_mcp").is_dir() and "site-packages" not in str(candidate):
        return candidate
    return None


def python_command(read_only: bool = False) -> tuple[str, dict[str, str]]:
    """返回 (可执行文件, 需要额外设置的 env)。"""
    exe = sys.executable or shutil.which("python3") or shutil.which("python") or "python"
    env: dict[str, str] = {}

    # 如果这个解释器在没有 PYTHONPATH 的情况下 import 不到包，就把 src 目录带上
    try:
        probe = subprocess.run([exe, "-c", "import mubu_web_mcp"],
                               capture_output=True, timeout=20,
                               env={k: v for k, v in os.environ.items()
                                    if k.upper() not in ("PYTHONPATH",)})
        importable = probe.returncode == 0
    except (OSError, subprocess.SubprocessError):
        importable = False

    src = _package_src_dir()
    if not importable and src is not None:
        env["PYTHONPATH"] = str(src)
    if read_only:
        env["MUBU_READ_ONLY"] = "1"
    return exe, env


@dataclass
class Agent:
    key: str
    label: str
    # json-servers | json-mcpServers | toml-codex | claude-cli | manual
    kind: str
    config_paths: Callable[[], list[Path]] = field(default=lambda: [])
    manual_hint: str = ""


def _claude_desktop_paths() -> list[Path]:
    if os.name == "nt":
        return [_appdata() / "Claude" / "claude_desktop_config.json"]
    if sys.platform == "darwin":
        return [_home() / "Library" / "Application Support" / "Claude" /
                "claude_desktop_config.json"]
    return [_home() / ".config" / "Claude" / "claude_desktop_config.json"]


AGENTS: dict[str, Agent] = {
    "codex": Agent(
        "codex", "OpenAI Codex CLI",
        "toml-codex",
        lambda: [_home() / ".codex" / "config.toml"],
    ),
    "claude-desktop": Agent(
        "claude-desktop", "Claude 桌面版",
        "json-mcpServers", _claude_desktop_paths,
    ),
    "claude-code": Agent(
        "claude-code", "Claude Code",
        "claude-cli",
    ),
    "cursor": Agent(
        "cursor", "Cursor",
        "json-mcpServers",
        lambda: [_home() / ".cursor" / "mcp.json"],
    ),
    "windsurf": Agent(
        "windsurf", "Windsurf",
        "json-mcpServers",
        lambda: [_home() / ".codeium" / "windsurf" / "mcp_config.json"],
    ),
    "vscode": Agent(
        "vscode", "VS Code（项目级）",
        "json-servers",
        lambda: [Path.cwd() / ".vscode" / "mcp.json"],
    ),
    "cherry-studio": Agent(
        "cherry-studio", "Cherry Studio",
        "manual",
        manual_hint="设置 → MCP 服务器 → 添加服务器 → 从 JSON 导入，粘贴下面这段 JSON。",
    ),
}


def entry_for(agent: Agent, read_only: bool = False) -> dict[str, object]:
    exe, env = python_command(read_only=read_only)
    entry: dict[str, object] = {"command": exe, "args": ["-m", "mubu_web_mcp"]}
    if env:
        entry["env"] = env
    if agent.kind == "json-servers":
        entry["type"] = "stdio"
    return entry


def manual_json(read_only: bool = False) -> str:
    exe, env = python_command(read_only=read_only)
    return json.dumps(
        {"mcpServers": {SERVER_KEY: {"command": exe, "args": ["-m", "mubu_web_mcp"],
                                     **({"env": env} if env else {})}}},
        ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 写入实现
# ---------------------------------------------------------------------------

def _backup(path: Path) -> Path | None:
    """备份成带时间戳的文件，避免多次安装覆盖同一个 .bak。"""
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.{stamp}.bak")
    counter = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.{stamp}-{counter}.bak")
        counter += 1
    shutil.copy2(path, backup)
    return backup


def _replace_file(path: Path, text: str, backup: Path | None) -> str | None:
    """原子写入；失败时恢复备份。返回错误说明，成功返回 None。"""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return None
    except OSError as exc:
        try:
            if tmp.exists():
                tmp.unlink()
            if backup is not None and backup.exists():
                shutil.copy2(backup, path)
        except OSError:
            pass
        return f"写入失败（已尝试恢复原文件）：{exc}"


def _write_json(path: Path, key: str, entry: dict[str, object]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError:
            return f"跳过（已存在的文件不是合法 JSON，请手动修改）：{path}"
        if not isinstance(data, dict):
            return f"跳过（顶层不是 JSON 对象，请手动修改）：{path}"
    servers = data.setdefault(key, {})
    if not isinstance(servers, dict):
        return f"跳过（{key} 字段不是对象，请手动修改）：{path}"
    servers[SERVER_KEY] = entry
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    try:
        json.loads(text)
    except ValueError:
        return f"跳过（生成的内容不是合法 JSON）：{path}"
    backup = _backup(path)
    error = _replace_file(path, text, backup)
    if error:
        return f"{path} {error}"
    note = f"（已备份到 {backup.name}）" if backup else ""
    return f"已写入 {path}{note}"


def _toml_value(value: object) -> str:
    # JSON 的字符串转义规则和 TOML 基本字符串兼容
    return json.dumps(value, ensure_ascii=False)


def _toml_block(entry: dict[str, object]) -> str:
    lines = [f"[mcp_servers.{SERVER_KEY}]",
             f"command = {_toml_value(entry['command'])}",
             "args = [" + ", ".join(_toml_value(a) for a in entry["args"]) + "]"]
    env = entry.get("env") or {}
    if env:
        lines.append("")
        lines.append(f"[mcp_servers.{SERVER_KEY}.env]")
        for key, value in env.items():
            lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


def _strip_toml_section(text: str) -> str:
    """删掉已有的 [mcp_servers.<SERVER_KEY>] 及其子表。"""
    keep: list[str] = []
    skipping = False
    target = f"mcp_servers.{SERVER_KEY}"
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("["):
            # 精确匹配表名，避免误删名称相近的段（如 mubu_web_mcp_extra）
            name = stripped.strip("[]").strip()
            if name == target or name.startswith(f"{target}."):
                skipping = True
                continue
            skipping = False
        if not skipping:
            keep.append(line)
    return "".join(keep)


def _write_toml(path: Path, entry: dict[str, object]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    if original and tomllib is not None:
        try:
            tomllib.loads(original)
        except tomllib.TOMLDecodeError as exc:
            return f"跳过（已存在的文件不是合法 TOML：{exc}）：{path}"
    body = _strip_toml_section(original).rstrip("\n")
    text = (body + "\n\n" if body else "") + _toml_block(entry)
    if tomllib is not None:
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            return f"跳过（生成的内容不是合法 TOML：{exc}）：{path}"
    backup = _backup(path)
    error = _replace_file(path, text, backup)
    if error:
        return f"{path} {error}"
    note = f"（已备份到 {backup.name}）" if backup else ""
    return f"已写入 {path}{note}"


def _claude_cli(entry: dict[str, object]) -> str:
    claude = shutil.which("claude")
    if not claude:
        return ("没找到 claude 命令，请手动执行：\n"
                f"  claude mcp add {SERVER_KEY} -- \"{entry['command']}\" -m mubu_web_mcp")
    result = subprocess.run(
        [claude, "mcp", "add", SERVER_KEY, "--", str(entry["command"]), "-m", "mubu_web_mcp"],
        capture_output=True, text=True)
    if result.returncode == 0:
        return "已通过 claude mcp add 注册"
    return f"claude mcp add 失败：{result.stderr.strip() or result.stdout.strip()}"


def install(agent_keys: list[str], read_only: bool = False,
            dry_run: bool = False) -> list[str]:
    """把服务写进指定客户端的配置，返回每条结果说明。"""
    results: list[str] = []
    for key in agent_keys:
        agent = AGENTS.get(key)
        if agent is None:
            results.append(f"未知的客户端：{key}")
            continue
        entry = entry_for(agent, read_only=read_only)

        if agent.kind == "manual":
            results.append(
                f"{agent.label}：需要手动配置。{agent.manual_hint}\n"
                + manual_json(read_only))
            continue

        if agent.kind == "claude-cli":
            note = (f"（dry-run）将执行 claude mcp add {SERVER_KEY}"
                    if dry_run else _claude_cli(entry))
            results.append(f"{agent.label}：" + note)
            continue

        paths = agent.config_paths()
        for path in paths:
            if dry_run:
                results.append(f"{agent.label}：将写入 {path}")
            elif agent.kind == "toml-codex":
                results.append(f"{agent.label}：" + _write_toml(path, entry))
            else:
                key_name = "servers" if agent.kind == "json-servers" else "mcpServers"
                results.append(f"{agent.label}：" + _write_json(path, key_name, entry))
    return results


def detect_installed() -> list[str]:
    """猜测用户装了哪些客户端：有配置目录就算装了。"""
    found = []
    for key, agent in AGENTS.items():
        if key == "cherry-studio":
            continue
        if key == "claude-code":
            if shutil.which("claude"):
                found.append(key)
            continue
        for path in agent.config_paths():
            if path.parent.exists():
                found.append(key)
                break
    return found
