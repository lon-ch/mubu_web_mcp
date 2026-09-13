"""幕布接口的最小客户端（本仓库自行实现，非官方）。

设计约束：

1. 只用 Python 标准库，零第三方依赖，方便逐行审计。
2. 唯一的网络目标是 ``https://api2.mubu.com/v3/api``：主机名硬编码，
   发请求前用 ``urllib.parse`` 严格比对 hostname（不是字符串前缀），
   代码里没有任何"把数据发到别处"的分支。
3. 凭据与登录 token 都交给 :mod:`mubu_web_mcp.credentials` 存进系统安全存储。
4. 限速是跨进程的（文件锁 + 时间戳），所以两个 Agent 同时跑也不会叠加请求。
5. 支持取消：传入 ``cancel_event``（threading.Event）即可中止等待与重试。
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from threading import Event

from . import credentials
from .credentials import (
    TOKEN_SLOT,
    CredentialsError,
    MissingCredentials,
    delete_secret,
    load_credentials,
    load_secret,
    store_secret,
)

API_BASE = "https://api2.mubu.com/v3/api"
API_HOST = "api2.mubu.com"

TOKEN_TTL_SECONDS = 7200
TOKEN_REFRESH_MARGIN = 300

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 幕布网页端自己会带的头；缺了部分接口会直接返回 illegal request。
WEB_CLIENT_HEADERS = {
    "Origin": "https://mubu.com",
    "Referer": "https://mubu.com/",
    "x-reg-entrance": "https://mubu.com/app",
}

ENDPOINTS = {
    "login": "/user/phone_login",
    "list": "/list/get",
    "create_folder": "/list/create_folder",
    "create_doc": "/list/create_doc",
    "import_doc": "/list/import_doc",
    "get_doc": "/document/edit/get",
    "rename_doc": "/list/rename_doc",
    "move": "/list/custom/drag",
}

# 错误码 → 给人看的解释
ERROR_HINTS = {
    2: "登录态已失效，请重新登录",
    6: "没有权限访问该资源（可能是文档加密、已删除，或 id 不正确）",
    17: "请求被判定为非法（缺少必要的请求头，或接口已变更）",
    403: "被拒绝访问，账号可能触发了风控",
    1204: "手机号或密码不正确",
}

# 幕布可能在 HTTP 200 里返回的业务限流码。默认留空，可用环境变量补充。
DEFAULT_RATE_LIMIT_CODES: frozenset[int] = frozenset()
RATE_LIMIT_KEYWORDS = ("频繁", "过快", "限流", "稍后", "rate limit", "too many", "slow down")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _rate_limit_codes() -> frozenset[int]:
    raw = (os.environ.get("MUBU_RATE_LIMIT_CODES") or "").strip()
    if not raw:
        return DEFAULT_RATE_LIMIT_CODES
    codes = set()
    for part in raw.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            codes.add(int(part))
    return frozenset(codes)


class MubuError(RuntimeError):
    """幕布接口返回的业务错误。"""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class AuthError(MubuError):
    """登录态失效或凭据错误。"""


class RateLimitError(MubuError):
    """被限流。``retry_after`` 为服务端给出的建议等待秒数（可能为 None）。"""

    def __init__(self, message: str, code: int | None = None,
                 retry_after: float | None = None) -> None:
        super().__init__(message, code=code)
        self.retry_after = retry_after


class CancelledError(MubuError):
    """调用方主动取消。"""


__all__ = [
    "API_BASE",
    "ENDPOINTS",
    "AuthError",
    "CancelledError",
    "CredentialsError",
    "MissingCredentials",
    "MubuClient",
    "MubuError",
    "RateLimitError",
]


# ---------------------------------------------------------------------------
# 跨进程文件锁
# ---------------------------------------------------------------------------

class FileLock:
    """极简跨进程互斥锁：Windows 用 msvcrt，类 Unix 用 fcntl。"""

    def __init__(self, path: Path, timeout: float = 30.0,
                 cancel_event: Event | None = None) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.cancel_event = cancel_event
        self._fh = None

    def __enter__(self) -> FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+b")
        deadline = time.time() + self.timeout
        while True:
            try:
                self._acquire()
                return self
            except OSError:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    self.__exit__(None, None, None)
                    raise CancelledError("已取消") from None
                if time.time() > deadline:
                    self.__exit__(None, None, None)
                    raise MubuError(
                        f"等待文件锁超时：{self.path.name}") from None
                time.sleep(0.05)

    def _acquire(self) -> None:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release(self) -> None:
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass

    def __exit__(self, *_exc) -> bool:
        if self._fh is not None:
            self._release()
            self._fh.close()
            self._fh = None
        return False


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

class MubuClient:
    def __init__(self, phone: str | None = None, password: str | None = None,
                 timeout: float | None = None,
                 cancel_event: Event | None = None) -> None:
        self._phone = (phone or "").strip() or None
        self._password = password or None
        self.timeout = timeout if timeout is not None else _env_float("MUBU_TIMEOUT", 20.0)
        self.min_interval = _env_int("MUBU_MIN_INTERVAL_MS", 500) / 1000.0
        self.jitter = _env_int("MUBU_JITTER_MS", 150) / 1000.0
        self.max_retries = _env_int("MUBU_MAX_RETRIES", 2)
        self.max_backoff = _env_float("MUBU_MAX_BACKOFF_SECONDS", 60.0)
        self.use_process_lock = _env_bool("MUBU_PROCESS_LOCK", True)
        self.cancel_event = cancel_event
        self.rate_limit_codes = _rate_limit_codes()

        self._token: str | None = None
        self._member_id: str | None = None
        self._name: str | None = None
        self._user_id: str | None = None
        self._expires_at = 0.0
        self._unique_id = str(uuid.uuid4())
        self._session_id = str(uuid.uuid4())
        self._last_request_at = 0.0

        # 脱敏诊断计数（只记数字与错误码，不记正文）
        self.stats: dict[str, int] = {
            "requests": 0, "retries": 0, "rate_limit_waits": 0, "logins": 0,
        }
        self.last_error_code: int | None = None
        self._load_token()

    # ---- 取消 --------------------------------------------------------

    def _check_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise CancelledError("已取消")

    def _sleep(self, seconds: float) -> None:
        """可中断的 sleep。"""
        if seconds <= 0:
            return
        deadline = time.time() + seconds
        while True:
            self._check_cancelled()
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            time.sleep(min(0.25, remaining))

    # ---- token 缓存 --------------------------------------------------

    def _token_slot_value(self) -> str | None:
        return load_secret(TOKEN_SLOT)

    def _load_token(self) -> None:
        raw_text = self._token_slot_value()
        legacy = credentials.home_dir() / "token.json"
        if not raw_text and legacy.exists():
            try:
                raw_text = legacy.read_text(encoding="utf-8")
            except OSError:
                raw_text = None
        if not raw_text:
            return
        try:
            raw = json.loads(raw_text)
        except ValueError:
            return
        if not raw.get("token"):
            return
        if time.time() >= float(raw.get("expires_at", 0)) - TOKEN_REFRESH_MARGIN:
            return
        self._token = raw["token"]
        self._member_id = raw.get("member_id")
        self._name = raw.get("name")
        self._user_id = raw.get("user_id")
        self._expires_at = float(raw.get("expires_at", 0))

    def _save_token(self) -> None:
        payload = json.dumps({
            "token": self._token,
            "member_id": self._member_id,
            "name": self._name,
            "user_id": self._user_id,
            "expires_at": self._expires_at,
        }, ensure_ascii=False)
        try:
            store_secret(TOKEN_SLOT, payload)
        except CredentialsError:
            # 系统安全存储不可用时退回 0600 文件
            path = credentials.home_dir() / "token.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, path)

    def forget_token(self) -> list[str]:
        """清掉本机保存的登录 token。"""
        self._token = None
        self._expires_at = 0.0
        removed = delete_secret(TOKEN_SLOT)
        legacy = credentials.home_dir() / "token.json"
        if legacy.exists():
            legacy.unlink()
            removed.append("legacy-token-file")
        return removed

    # ---- 限速 --------------------------------------------------------

    def _rate_lock_path(self) -> Path:
        return credentials.home_dir() / "rate.lock"

    def _rate_stamp_path(self) -> Path:
        return credentials.home_dir() / "rate.stamp"

    def _throttle(self) -> None:
        """保证跨进程的最小请求间隔（含抖动）。"""
        target = self.min_interval + random.uniform(0, self.jitter)
        if target <= 0:
            return
        lock = (FileLock(self._rate_lock_path(), cancel_event=self.cancel_event)
                if self.use_process_lock else _NullLock())
        with lock:
            stamp_path = self._rate_stamp_path()
            try:
                last = float(stamp_path.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                last = 0.0
            wait = target - (time.time() - last)
            if wait > 0:
                self.stats["rate_limit_waits"] += 0  # 只统计被服务端限流的等待
                self._sleep(wait)
            try:
                stamp_path.parent.mkdir(parents=True, exist_ok=True)
                stamp_path.write_text(str(time.time()), encoding="utf-8")
            except OSError:
                pass

    @staticmethod
    def _parse_retry_after(value: str | None) -> float | None:
        if not value:
            return None
        value = value.strip()
        if value.isdigit():
            return float(value)
        try:
            from email.utils import parsedate_to_datetime
            when = parsedate_to_datetime(value)
            return max(0.0, when.timestamp() - time.time())
        except (TypeError, ValueError):
            return None

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        """指数退避 + 抖动；服务端给了 Retry-After 就听它的。"""
        if retry_after is not None:
            wait = min(retry_after, self.max_backoff)
        else:
            wait = min(2.0 ** attempt, self.max_backoff)
        return wait + random.uniform(0, min(self.jitter, 1.0))

    # ---- HTTP --------------------------------------------------------

    def _headers(self, token: str | None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "User-Agent": USER_AGENT,
            "data-unique-id": self._unique_id,
            "x-session-id": self._session_id,
            "x-request-id": str(uuid.uuid4()),
            **WEB_CLIENT_HEADERS,
        }
        if token:
            headers["Jwt-Token"] = token
        return headers

    @staticmethod
    def _assert_mubu_url(url: str) -> None:
        parts = urllib.parse.urlsplit(url)
        if (parts.scheme != "https" or parts.hostname != API_HOST
                or parts.port not in (None, 443)):
            raise MubuError("拒绝向非幕布域名发起请求")

    def _post(self, path: str, payload: dict,
              token: str | None = None) -> dict:
        url = API_BASE + path
        self._assert_mubu_url(url)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        attempt = 0
        while True:
            self._check_cancelled()
            self._throttle()
            request = urllib.request.Request(
                url, data=body, headers=self._headers(token), method="POST")
            retry_after: float | None = None
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    text = response.read().decode("utf-8", "replace")
                    status = response.status
                    retry_after = self._parse_retry_after(
                        response.headers.get("Retry-After"))
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")
                status = exc.code
                retry_after = self._parse_retry_after(
                    exc.headers.get("Retry-After") if exc.headers else None)
            except urllib.error.URLError as exc:
                if attempt < self.max_retries:
                    attempt += 1
                    self.stats["retries"] += 1
                    self._sleep(self._backoff(attempt, None))
                    continue
                raise MubuError(f"网络请求失败：{exc.reason}") from exc
            finally:
                self._last_request_at = time.time()
                self.stats["requests"] += 1

            # 限流：HTTP 429，或带 Retry-After 的 503
            if status == 429 or (status == 503 and retry_after is not None):
                if attempt < self.max_retries:
                    attempt += 1
                    self.stats["retries"] += 1
                    self.stats["rate_limit_waits"] += 1
                    self.last_error_code = status
                    self._sleep(self._backoff(attempt, retry_after))
                    continue
                raise RateLimitError(
                    f"被幕布限流（HTTP {status}）", code=status,
                    retry_after=retry_after)

            # 其他 5xx：指数退避重试
            if status >= 500:
                if attempt < self.max_retries:
                    attempt += 1
                    self.stats["retries"] += 1
                    self.last_error_code = status
                    self._sleep(self._backoff(attempt, retry_after))
                    continue
                raise MubuError(f"幕布服务器错误（HTTP {status}）", code=status)

            try:
                obj = json.loads(text)
            except ValueError:
                raise MubuError(
                    f"接口返回了非 JSON 内容（HTTP {status}）：{text[:200]}") from None

            code = obj.get("code")
            if code != 0:
                message = str(obj.get("msg") or (obj.get("data") or {}).get("message") or "")
                lowered = message.lower()
                self.last_error_code = code

                # 幕布会在 HTTP 200 里返回业务限流码
                if code in self.rate_limit_codes or any(
                        kw in lowered or kw in message for kw in RATE_LIMIT_KEYWORDS):
                    if attempt < self.max_retries:
                        attempt += 1
                        self.stats["retries"] += 1
                        self.stats["rate_limit_waits"] += 1
                        self._sleep(self._backoff(attempt, retry_after))
                        continue
                    raise RateLimitError(
                        f"被幕布限流（code={code}）：{message}", code=code,
                        retry_after=retry_after)

                is_auth = (
                    code in (2, 1204)
                    or "login expired" in lowered
                    or "未登录" in message
                )
                hint = ERROR_HINTS.get(code)
                detail = f"（{hint}）" if hint else ""
                text_message = (
                    f"幕布接口返回 code={code}{detail}：{message}"
                    if message else f"幕布接口返回 code={code}{detail}"
                )
                if is_auth:
                    raise AuthError(text_message, code=code)
                raise MubuError(text_message, code=code)
            return obj.get("data") or {}

    # ---- 登录 --------------------------------------------------------

    def login(self) -> dict:
        if not (self._phone and self._password):
            self._phone, self._password = load_credentials()
        # 多进程同时刷新 token 时串行化，避免并发登录
        lock = (FileLock(credentials.home_dir() / "login.lock",
                         cancel_event=self.cancel_event)
                if self.use_process_lock else _NullLock())
        with lock:
            data = self._post(ENDPOINTS["login"], {
                "phone": self._phone,
                "password": self._password,
                "callbackType": 0,
            })
            token = data.get("token")
            if not token:
                raise MubuError("登录响应里没有 token")
            self._token = token
            self._user_id = str(data.get("id") or "") or None
            self._name = data.get("name")
            self._member_id = str(data.get("memberId") or data.get("member_id") or "") or None
            self._expires_at = time.time() + TOKEN_TTL_SECONDS
            self.stats["logins"] += 1
            self._save_token()
        return {"name": self._name, "user_id": self._user_id}

    def ensure_token(self, force: bool = False) -> str:
        if not force and self._token and time.time() < self._expires_at - TOKEN_REFRESH_MARGIN:
            return self._token
        self.login()
        assert self._token
        return self._token

    def request(self, path: str, payload: dict | None = None,
                auth: bool = True, retry: bool = True) -> dict:
        token = self.ensure_token() if auth else None
        try:
            return self._post(path, payload or {}, token=token)
        except AuthError:
            if not (auth and retry):
                raise
            # 认证失败只重新登录一次，绝不循环
            self.forget_token()
            self.login()
            return self._post(path, payload or {}, token=self._token)

    # ---- 业务方法 ----------------------------------------------------

    def whoami(self) -> dict:
        self.ensure_token()
        return {
            "name": self._name,
            "user_id": self._user_id,
            "member_id": self._member_id,
            "token_expires_in_seconds": max(0, int(self._expires_at - time.time())),
        }

    def list_dir(self, folder_id: str = "0") -> dict:
        return self.request(ENDPOINTS["list"], {"folderId": str(folder_id)})

    def get_doc(self, doc_id: str) -> dict:
        return self.request(ENDPOINTS["get_doc"], {
            "docId": doc_id,
            "password": "",
            "isFromDocDir": True,
        })

    def create_folder(self, name: str, folder_id: str = "0") -> str:
        data = self.request(ENDPOINTS["create_folder"],
                            {"folderId": str(folder_id), "name": name})
        folder = data.get("folder") or {}
        return str(folder.get("id") or data.get("id") or "")

    def create_doc(self, name: str, folder_id: str = "0") -> str:
        """创建一篇**空**文档。幕布会忽略 content 参数，写正文请用 import_doc。"""
        data = self.request(ENDPOINTS["create_doc"],
                            {"folderId": str(folder_id), "name": name, "type": 0})
        doc = data.get("doc") or {}
        return str(doc.get("id") or data.get("id") or "")

    def import_doc(self, name: str, nodes: list, folder_id: str = "0") -> str:
        """用节点树创建一篇文档（幕布网页版「导入」走的接口）。"""
        def count(items: list) -> int:
            total = 0
            for item in items:
                total += 1 + count(item.get("children") or [])
            return total

        data = self.request(ENDPOINTS["import_doc"], {
            "name": name,
            "folderId": str(folder_id),
            "itemCount": count(nodes),
            "define": json.dumps({"nodes": nodes}, ensure_ascii=False),
        })
        return str(data.get("id") or "")

    # ---- 文档结构解析 ------------------------------------------------

    @staticmethod
    def doc_tree(doc_data: dict) -> dict:
        """把 get_doc 返回的 definition 字符串解析成节点树。"""
        raw = doc_data.get("definition")
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except ValueError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return raw if isinstance(raw, dict) else {}
