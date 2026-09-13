"""本机秘密存储：按平台选择最安全的可用后端。

读取优先级（从上到下，第一个拿得到的生效）：

1. 环境变量（仅手机号/密码：``MUBU_PHONE`` / ``MUBU_PASSWORD``）
2. Windows：DPAPI 加密文件（密钥绑定当前 Windows 用户）
3. macOS：系统钥匙串（``security`` 命令）
4. Linux：Secret Service（``secret-tool``）
5. 回退：明文 JSON，权限 0600（写入时会明确提示用户）

存储按「槽位」（slot）区分：``credentials`` 放手机号口令，``token`` 放登录 JWT。
每个槽位在系统级后端里是独立条目，互不影响。

**本模块刻意不在顶层 import ``ctypes.wintypes``** —— 那个模块只在 Windows 存在，
放在顶层会让整个包在 macOS/Linux 上 import 失败。DPAPI 用 ``c_uint32`` /
``c_void_p`` 手写结构体，效果一样但不依赖 wintypes。
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

KEYCHAIN_ACCOUNT = "mubu-web-mcp"
KEYCHAIN_SERVICE = "mubu-web-mcp"

CREDENTIALS_SLOT = "credentials"
TOKEN_SLOT = "token"


class CredentialsError(RuntimeError):
    """凭据读写失败。"""


class MissingCredentials(CredentialsError):
    """本机找不到任何可用凭据。"""


def _home() -> Path:
    """每次调用都重新取，方便测试替换 ``MUBU_HOME``。"""
    return Path(MUBU_HOME)


def home_dir() -> Path:
    """公开访问器：其它模块一律通过它拿目录，避免缓存住旧常量。"""
    return _home()


def dpapi_path(slot: str) -> Path:
    return _home() / f"{slot}.dpapi"


def secrets_json_path() -> Path:
    return _home() / "secrets.json"


def legacy_credentials_json_path() -> Path:
    """旧版本使用的明文文件名，仍然兼容读取。"""
    return _home() / "credentials.json"


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
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise CredentialsError(
            "DPAPI 操作失败：凭据文件可能不是当前 Windows 用户创建的")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


# ---------------------------------------------------------------------------
# macOS 钥匙串
# ---------------------------------------------------------------------------

def _keychain_available() -> bool:
    return _is_macos() and shutil.which("security") is not None


def _keychain_store(slot: str, secret: str) -> None:
    result = subprocess.run(
        ["security", "add-generic-password", "-a", f"{KEYCHAIN_ACCOUNT}:{slot}",
         "-s", KEYCHAIN_SERVICE, "-w", secret, "-U"],
        capture_output=True, text=True)
    if result.returncode != 0:
        raise CredentialsError(f"写入 macOS 钥匙串失败：{result.stderr.strip()}")


def _keychain_load(slot: str) -> str | None:
    result = subprocess.run(
        ["security", "find-generic-password", "-a", f"{KEYCHAIN_ACCOUNT}:{slot}",
         "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _keychain_delete(slot: str) -> bool:
    result = subprocess.run(
        ["security", "delete-generic-password", "-a", f"{KEYCHAIN_ACCOUNT}:{slot}",
         "-s", KEYCHAIN_SERVICE],
        capture_output=True, text=True)
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Linux Secret Service
# ---------------------------------------------------------------------------

def _secret_tool_available() -> bool:
    return sys.platform.startswith("linux") and shutil.which("secret-tool") is not None


def _secret_tool_store(slot: str, secret: str) -> None:
    result = subprocess.run(
        ["secret-tool", "store", f"--label={APP_NAME} {slot}",
         "service", KEYCHAIN_SERVICE, "account", slot],
        input=secret, capture_output=True, text=True)
    if result.returncode != 0:
        raise CredentialsError(f"写入 Secret Service 失败：{result.stderr.strip()}")


def _secret_tool_load(slot: str) -> str | None:
    result = subprocess.run(
        ["secret-tool", "lookup", "service", KEYCHAIN_SERVICE, "account", slot],
        capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip()


def _secret_tool_delete(slot: str) -> bool:
    result = subprocess.run(
        ["secret-tool", "clear", "service", KEYCHAIN_SERVICE, "account", slot],
        capture_output=True, text=True)
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


def _read_secrets_json() -> dict:
    path = secrets_json_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_secrets_json(data: dict) -> None:
    _write_private(secrets_json_path(), json.dumps(data, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# 后端探测
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


# ---------------------------------------------------------------------------
# 通用槽位读写
# ---------------------------------------------------------------------------

def store_secret(slot: str, value: str, backend: str | None = None) -> str:
    """把任意一个字符串秘密写进指定后端，返回实际使用的后端名。"""
    if not value:
        raise CredentialsError("秘密内容不能为空")
    backend = backend or default_backend()

    if backend == "dpapi":
        if not _is_windows():
            raise CredentialsError("DPAPI 只在 Windows 上可用")
        blob = _dpapi(True, value.encode("utf-16-le"))
        _write_private(dpapi_path(slot), blob.hex())
        return "dpapi"

    if backend == "keychain":
        if not _keychain_available():
            raise CredentialsError("当前系统没有可用的 macOS 钥匙串")
        _keychain_store(slot, value)
        return "keychain"

    if backend == "secret-service":
        if not _secret_tool_available():
            raise CredentialsError("当前系统没有可用的 secret-tool")
        _secret_tool_store(slot, value)
        return "secret-service"

    if backend == "plaintext-file":
        data = _read_secrets_json()
        data[slot] = value
        _write_secrets_json(data)
        return "plaintext-file"

    raise CredentialsError(f"未知的凭据后端：{backend}")


def load_secret(slot: str) -> str | None:
    """读取槽位；找不到返回 None（不抛异常，便于调用方降级）。"""
    path = dpapi_path(slot)
    if _is_windows() and path.exists():
        try:
            blob = bytes.fromhex(path.read_text(encoding="utf-8").strip())
            return _dpapi(False, blob).decode("utf-16-le", "ignore")
        except (OSError, ValueError, CredentialsError):
            pass

    if _keychain_available():
        value = _keychain_load(slot)
        if value:
            return value

    if _secret_tool_available():
        value = _secret_tool_load(slot)
        if value:
            return value

    value = _read_secrets_json().get(slot)
    return value if isinstance(value, str) and value else None


def delete_secret(slot: str) -> list[str]:
    """删除槽位，返回被清掉的后端列表。"""
    removed: list[str] = []
    path = dpapi_path(slot)
    if path.exists():
        path.unlink()
        removed.append("dpapi")
    data = _read_secrets_json()
    if slot in data:
        del data[slot]
        _write_secrets_json(data)
        removed.append("plaintext-file")
    if _keychain_available() and _keychain_delete(slot):
        removed.append("keychain")
    if _secret_tool_available() and _secret_tool_delete(slot):
        removed.append("secret-service")
    return removed


# ---------------------------------------------------------------------------
# 手机号 / 密码
# ---------------------------------------------------------------------------

def load_credentials() -> tuple[str, str]:
    """按优先级读取手机号和密码。"""
    env_phone = (os.environ.get("MUBU_PHONE") or "").strip()
    env_password = os.environ.get("MUBU_PASSWORD") or ""
    if env_phone and env_password:
        return env_phone, env_password

    raw_text = load_secret(CREDENTIALS_SLOT)
    if not raw_text:
        legacy = legacy_credentials_json_path()
        if legacy.exists():
            try:
                raw_text = legacy.read_text(encoding="utf-8")
            except OSError:
                raw_text = None

    if raw_text:
        try:
            raw = json.loads(raw_text)
        except ValueError:
            raw = {}
        phone = (env_phone or str(raw.get("phone", ""))).strip()
        password = env_password or str(raw.get("password", ""))
        if phone and password:
            return phone, password

    raise MissingCredentials(
        "没有找到幕布凭据。请运行 `mubu-web-mcp login` 完成配置，"
        "或设置 MUBU_PHONE / MUBU_PASSWORD 环境变量。")


def store_credentials(phone: str, password: str, backend: str | None = None) -> str:
    """保存凭据，返回实际使用的后端名。"""
    phone = phone.strip()
    if not phone or not password:
        raise CredentialsError("手机号和密码都不能为空")
    secret = json.dumps({"phone": phone, "password": password}, ensure_ascii=False)
    return store_secret(CREDENTIALS_SLOT, secret, backend=backend)


def delete_credentials() -> list[str]:
    """删除本机保存的所有凭据（含登录 token）。"""
    removed = delete_secret(CREDENTIALS_SLOT)
    removed += delete_secret(TOKEN_SLOT)
    legacy = legacy_credentials_json_path()
    if legacy.exists():
        try:
            legacy.unlink()
            removed.append("legacy-plaintext-file")
        except OSError:
            pass
    return removed


def prompt_credentials() -> tuple[str, str]:
    """交互式询问手机号和密码（密码不回显）。"""
    phone = input("幕布手机号: ").strip()
    password = getpass.getpass("幕布密码（输入时不显示）: ")
    return phone, password
