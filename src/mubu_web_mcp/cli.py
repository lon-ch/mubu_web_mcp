"""命令行入口：

    mubu-web-mcp                 以 MCP 服务方式运行（stdio，默认）
    mubu-web-mcp setup           一条命令完成：登录 + 写配置 + 自检
    mubu-web-mcp login           保存幕布账号凭据
    mubu-web-mcp logout          删除本机保存的凭据
    mubu-web-mcp install         把服务写进各 AI 客户端的配置
    mubu-web-mcp doctor          检查凭据、登录、目录是否能读到
    mubu-web-mcp selftest        离线自检（不联网、不需要账号）
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from . import __version__
from . import credentials, installer
from . import server as server_module


def _print_results(results: List[str]) -> None:
    for line in results:
        print("  " + line)


def cmd_serve(args: argparse.Namespace) -> int:
    return server_module.serve(read_only=args.read_only)


def cmd_login(args: argparse.Namespace) -> int:
    phone = (args.phone or "").strip()
    password = args.password or ""
    if not phone or not password:
        print(f"可用凭据后端：{', '.join(credentials.available_backends())}")
        print(f"默认后端：{credentials.default_backend()}")
        phone, password = credentials.prompt_credentials()
    try:
        backend = credentials.store_credentials(phone, password, backend=args.backend)
    except credentials.CredentialsError as exc:
        print(f"保存失败：{exc}", file=sys.stderr)
        return 1
    print(f"凭据已保存（后端：{backend}）")
    if backend == "plaintext-file":
        print("注意：这台机器上没有可用的系统级加密存储，凭据以明文保存在 "
              f"{credentials.CRED_JSON}（权限 600）。")
    return 0


def cmd_logout(_args: argparse.Namespace) -> int:
    removed = credentials.delete_credentials()
    print("已删除：" + (", ".join(removed) if removed else "（本机没有保存任何凭据）"))
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    keys = args.agent
    if "all" in keys:
        keys = [k for k in installer.AGENTS]
    results = installer.install(keys, read_only=args.read_only, dry_run=args.dry_run)
    _print_results(results)
    if not args.dry_run:
        print("\n完成。重启对应的 AI 客户端后即可使用（Codex / Claude Code 需要新开一个会话）。")
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    ok = True
    print(f"mubu_web_mcp v{__version__}")
    print(f"Python：{sys.executable}")
    print(f"凭据后端：{credentials.default_backend()}"
          f"（可用：{', '.join(credentials.available_backends())}）")
    try:
        phone, password = credentials.load_credentials()
        print(f"凭据：已找到（手机号 {phone[:3]}****{phone[-4:]}）")
    except credentials.CredentialsError as exc:
        print(f"凭据：未找到 —— {exc}")
        return 1

    from .mubu_client import MubuClient, MubuError
    client = MubuClient()
    try:
        info = client.whoami()
        print(f"登录：成功，账号 {info.get('name')}")
        root = client.list_dir("0")
        folders = len(root.get("folders") or [])
        docs = len(root.get("documents") or [])
        print(f"读取根目录：成功（{folders} 个文件夹、{docs} 篇文档）")
    except MubuError as exc:
        print(f"登录/读取失败：{exc}")
        ok = False
    print("自检结果：" + ("一切正常" if ok else "有问题，请看上面的输出"))
    return 0 if ok else 1


def cmd_selftest(_args: argparse.Namespace) -> int:
    from .selftest import run_selftest
    return run_selftest()


def cmd_setup(args: argparse.Namespace) -> int:
    print("== 第 1 步：检查凭据 ==")
    try:
        credentials.load_credentials()
        print("  已存在，跳过。")
    except credentials.CredentialsError:
        if args.phone and args.password:
            backend = credentials.store_credentials(args.phone, args.password)
            print(f"  已用命令行参数保存（后端：{backend}）")
        elif args.yes or sys.stdin.isatty():
            print("  需要你的幕布账号：")
            phone, password = credentials.prompt_credentials()
            backend = credentials.store_credentials(phone, password)
            print(f"  已保存（后端：{backend}）")
        else:
            print("  没有凭据，且当前不是交互式终端。请先运行 mubu-web-mcp login。")
            return 1

    print("== 第 2 步：写入 AI 客户端配置 ==")
    keys = args.agent
    if "all" in keys:
        detected = installer.detect_installed()
        keys = detected or list(installer.AGENTS)
        print(f"  检测到：{', '.join(keys) if detected else '没有已安装的客户端，将全部写入'}")
    _print_results(installer.install(keys, read_only=args.read_only, dry_run=args.dry_run))

    print("== 第 3 步：验证 ==")
    if args.dry_run:
        print("  dry-run，跳过验证。")
        return 0
    return cmd_doctor(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mubu-web-mcp",
        description="让 AI 智能体读写幕布（Mubu）大纲的 MCP 服务（非官方，miaoteam 维护）")
    parser.add_argument("--version", action="version", version=f"mubu_web_mcp {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="以 MCP 服务方式运行（stdio）")
    p_serve.add_argument("--read-only", action="store_true", help="禁用所有创建类工具")
    p_serve.set_defaults(func=cmd_serve)

    p_login = sub.add_parser("login", help="保存幕布账号凭据")
    p_login.add_argument("--phone")
    p_login.add_argument("--password")
    p_login.add_argument("--backend", choices=credentials.available_backends())
    p_login.set_defaults(func=cmd_login)

    p_logout = sub.add_parser("logout", help="删除本机保存的凭据")
    p_logout.set_defaults(func=cmd_logout)

    p_install = sub.add_parser("install", help="把服务写进 AI 客户端的 MCP 配置")
    p_install.add_argument("--agent", action="append", default=None,
                           choices=["all", *installer.AGENTS],
                           help="可重复指定；all 表示全部")
    p_install.add_argument("--read-only", action="store_true")
    p_install.add_argument("--dry-run", action="store_true")
    p_install.set_defaults(func=cmd_install)

    p_setup = sub.add_parser("setup", help="一条命令完成登录 + 写配置 + 验证")
    p_setup.add_argument("--agent", action="append", default=None,
                         choices=["all", *installer.AGENTS])
    p_setup.add_argument("--phone")
    p_setup.add_argument("--password")
    p_setup.add_argument("--read-only", action="store_true")
    p_setup.add_argument("--dry-run", action="store_true")
    p_setup.add_argument("--yes", action="store_true", help="非交互式，直接使用已有的环境变量凭据")
    p_setup.set_defaults(func=cmd_setup)

    p_doctor = sub.add_parser("doctor", help="检查凭据、登录、目录读取")
    p_doctor.set_defaults(func=cmd_doctor)

    p_self = sub.add_parser("selftest", help="离线自检（不联网）")
    p_self.set_defaults(func=cmd_selftest)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    # 不带子命令时当作 serve，这样 MCP 客户端可以直接用 `-m mubu_web_mcp`
    if not argv:
        args = parser.parse_args(["serve"])
    elif argv[0] in ("-h", "--help", "--version"):
        args = parser.parse_args(argv)
    elif argv[0].startswith("-"):
        args = parser.parse_args(["serve", *argv])
    else:
        args = parser.parse_args(argv)
    if args.command == "install" and not args.agent:
        args.agent = ["all"]
    if args.command == "setup" and not args.agent:
        args.agent = ["all"]
    return args.func(args)
