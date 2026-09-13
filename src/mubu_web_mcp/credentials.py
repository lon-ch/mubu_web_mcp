"""凭据存储：按平台选择最安全的可用后端。

优先级（读取时从上到下，第一个拿得到的生效）：

1. 环境变量 ``MUBU_PHONE`` / ``MUBU_PASSWORD``
2. Windows：DPAPI 加密文件（密钥绑定当前 Windows 用户）
3. macOS：系统钥匙串（``security`` 命令）
4. Linux：Secret Service（``secret-tool``）
5. 回退：明文 JSON 文件，权限 0600（会在提示里明确告知）

**这个模块刻意不在顶层 import ``ctypes.wintypes``** —— 那个模块只在 Windows
存在，放在顶层会让整个包在 macOS/Linux 上 import 失败。DPAPI 用
``c_uint32`` / ``c_void_p`` 手写结构体，效果一样但不依赖 wintypes。
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_NAME = "mubu-web-mcp"

MUBU_HOME = Path(os.environ.get("MUBU_HOME") or (Path.home() / ".mubu"))
CRED_JSON = MUBU_HOME / "credentials.json"
CRED_DPAPI = MUBU_HOME / "credentials.dpapi"

KEYCHAIN_ACCOUNT = "mubu-web-mcp"
KEYCHAIN_SERVICE = "mubu-web-mcp"


class CredentialsError(RuntimeError):
    """凭据读写失败。"""


class MissingCredentials(CredentialsError):
    """本机找不到任何可用凭据。"""


def _is_windows() -> bool:
    return os.name == "nt"


def _is_macos() -> bool:
    return sys.platform == "darwin"


# ---------------------------------------------------------------------------
# Windows DPAPI
# ---------------------------------------------------------------------------

def _dpapi(protect: bool, data: bytes) -> bytes:
    import ctypes

    class Blob(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.c_uint32),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = Blob()

    crypt32 = ctypes.windll.crypt32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    # CryptProtectData(pDataIn, szDataDescr, pOptionalEntropy, pvReserved,
    #                  pPromptStruct, dwFlags, pDataOut)
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise CredentialsError(
            "DPAPI 操作失败：凭据文件可能不是当前 Windows 用户创建的"
        )
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


# ---------------------------------------------------------------------------
# macOS 钥匙串
# ---------------------------------------------------------------------------

def _keychain_available() -> bool:
    return _is_macos() and shutil.which("security") is not None


def _keychain_store(secret: str) -> None:
    result = subprocess.run(
        ["security", "add-generic-password", "-a", KEYCHAIN_ACCOUNT,
         "-s", KEYCHAIN_SERVICE, "-w", secret, "-U"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise CredentialsError(f"写入 macOS 钥匙串失败：{result.stderr.strip()}")


def _keychain_load() -> str | None:
    result = subprocess.run(
        ["security", "find-generic-password", "-a", KEYCHAIN_ACCOUNT,
         "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _keychain_delete() -> bool:
    result = subprocess.run(
        ["security", "delete-generic-password", "-a", KEYCHAIN_ACCOUNT,
         "-s", KEYCHAIN_SERVICE],
        capture_output=True, text=True,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Linux Secret Service
# ---------------------------------------------------------------------------

def _secret_tool_available() -> bool:
    return sys.platform.startswith("linux") and shutil.which("secret-tool") is not None


def _secret_tool_store(secret: str) -> None:
    result = subprocess.run(
        ["secret-tool", "store", f"--label={APP_NAME} credentials",
         "service", KEYCHAIN_SERVICE, "account", KEYCHAIN_ACCOUNT],
        input=secret, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise CredentialsError(f"写入 Secret Service 失败：{result.stderr.strip()}")


def _secret_tool_load() -> str | None:
    result = subprocess.run(
        ["secret-tool", "lookup", "service", KEYCHAIN_SERVICE,
         "account", KEYCHAIN_ACCOUNT],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip()


def _secret_tool_delete() -> bool:
    result = subprocess.run(
        ["secret-tool", "clear", "service", KEYCHAIN_SERVICE,
         "account", KEYCHAIN_ACCOUNT],
        capture_output=True, text=True,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# 明文文件（回退方案）
# ---------------------------------------------------------------------------

def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _load_plaintext() -> tuple[str, str] | None:
    if not CRED_JSON.exists():
        return None
    try:
        raw = json.loads(CRED_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    phone = str(raw.get("phone", "")).strip()
    password = str(raw.get("password", ""))
    return (phone, password) if phone and password else None


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------

def available_backends() -> list[str]:
    backends = ["env", "plaintext-file"]
    if _is_windows():
        backends.insert(1, "dpapi")
    if _keychain_available():
        backends.insert(1, "keychain")
    if _secret_tool_available():
        backends.insert(1, "secret-service")
    return backends


def default_backend() -> str:
    if _is_windows():
        return "dpapi"
    if _keychain_available():
        return "keychain"
    if _secret_tool_available():
        return "secret-service"
    return "plaintext-file"


def load_credentials() -> tuple[str, str]:
    """按优先级读取手机号和密码。"""
    phone = (os.environ.get("MUBU_PHONE") or "").strip()
    password = os.environ.get("MUBU_PASSWORD") or ""
    if phone and password:
        return phone, password

    if _is_windows() and CRED_DPAPI.exists():
        try:
            blob = bytes.fromhex(CRED_DPAPI.read_text(encoding="utf-8").strip())
            raw = json.loads(_dpapi(False, blob).decode("utf-16-le", "ignore"))
            stored_phone = str(raw.get("phone", "")).strip()
            stored_password = str(raw.get("password", ""))
            if stored_phone and stored_password:
                return (phone or stored_phone), (password or stored_password)
        except (OSError, ValueError, CredentialsError):
            pass

    if _keychain_available():
        secret = _keychain_load()
        if secret:
            try:
                raw = json.loads(secret)
                stored_phone = str(raw.get("phone", "")).strip()
                stored_password = str(raw.get("password", ""))
                if stored_phone and stored_password:
                    return (phone or stored_phone), (password or stored_password)
            except ValueError:
                pass

    if _secret_tool_available():
        secret = _secret_tool_load()
        if secret:
            try:
                raw = json.loads(secret)
                stored_phone = str(raw.get("phone", "")).strip()
                stored_password = str(raw.get("password", ""))
                if stored_phone and stored_password:
                    return (phone or stored_phone), (password or stored_password)
            except ValueError:
                pass

    plaintext = _load_plaintext()
    if plaintext:
        return plaintext

    raise MissingCredentials(
        "没有找到幕布凭据。请运行 `mubu-web-mcp login` 完成配置，"
        "或设置 MUBU_PHONE / MUBU_PASSWORD 环境变量。"
    )


def store_credentials(phone: str, password: str, backend: str | None = None) -> str:
    """保存凭据，返回实际使用的后端名。"""
    phone = phone.strip()
    if not phone or not password:
        raise CredentialsError("手机号和密码都不能为空")
    secret = json.dumps({"phone": phone, "password": password}, ensure_ascii=False)
    backend = backend or default_backend()

    if backend == "dpapi":
        if not _is_windows():
            raise CredentialsError("DPAPI 只在 Windows 上可用")
        blob = _dpapi(True, secret.encode("utf-16-le"))
        _write_private(CRED_DPAPI, blob.hex())
        return "dpapi"

    if backend == "keychain":
        if not _keychain_available():
            raise CredentialsError("当前系统没有可用的 macOS 钥匙串")
        _keychain_store(secret)
        return "keychain"

    if backend == "secret-service":
        if not _secret_tool_available():
            raise CredentialsError("当前系统没有可用的 secret-tool")
        _secret_tool_store(secret)
        return "secret-service"

    if backend == "plaintext-file":
        _write_private(CRED_JSON, json.dumps(
            {"phone": phone, "password": password}, ensure_ascii=False, indent=2))
        return "plaintext-file"

    raise CredentialsError(f"未知的凭据后端：{backend}")


def delete_credentials() -> list[str]:
    """删除本机保存的凭据，返回被删除的后端列表。"""
    removed: list[str] = []
    for path, name in ((CRED_DPAPI, "dpapi"), (CRED_JSON, "plaintext-file")):
        if path.exists():
            path.unlink()
            removed.append(name)
    if _keychain_available() and _keychain_delete():
        removed.append("keychain")
    if _secret_tool_available() and _secret_tool_delete():
        removed.append("secret-service")
    return removed


def prompt_credentials() -> tuple[str, str]:
    """交互式询问手机号和密码（密码不回显）。"""
    phone = input("幕布手机号: ").strip()
    password = getpass.getpass("幕布密码（输入时不显示）: ")
    return phone, password
