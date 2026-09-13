"""幕布接口的最小客户端（本仓库自行实现，非官方）。

设计约束：

1. 只用 Python 标准库，零第三方依赖，方便逐行审计。
2. 唯一的网络目标是 ``https://api2.mubu.com/v3/api``，主机名硬编码，
   发起请求前再校验一次，代码里没有任何"把数据发到别处"的分支。
3. 凭据只从本机读取，见 :mod:`mubu_web_mcp.credentials`。
4. 请求有最小间隔和退避重试，避免给幕布服务器造成压力。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from .credentials import CredentialsError, MissingCredentials, load_credentials

API_BASE = "https://api2.mubu.com/v3/api"
API_HOST = "api2.mubu.com"

MUBU_HOME = Path(os.environ.get("MUBU_HOME") or (Path.home() / ".mubu"))
TOKEN_FILE = MUBU_HOME / "token.json"

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


class MubuError(RuntimeError):
    """幕布接口返回的业务错误。"""

    def __init__(self, message: str, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code


class AuthError(MubuError):
    """登录态失效或凭据错误。"""


__all__ = [
    "API_BASE", "ENDPOINTS", "AuthError", "CredentialsError", "MissingCredentials",
    "MubuClient", "MubuError", "TOKEN_FILE",
]


class MubuClient:
    def __init__(self, phone: Optional[str] = None, password: Optional[str] = None,
                 timeout: Optional[float] = None) -> None:
        self._phone = (phone or "").strip() or None
        self._password = password or None
        self.timeout = timeout if timeout is not None else _env_float("MUBU_TIMEOUT", 20.0)
        self.min_interval = _env_int("MUBU_MIN_INTERVAL_MS", 200) / 1000.0
        self.max_retries = _env_int("MUBU_MAX_RETRIES", 2)
        self._token: Optional[str] = None
        self._member_id: Optional[str] = None
        self._name: Optional[str] = None
        self._user_id: Optional[str] = None
        self._expires_at = 0.0
        self._unique_id = str(uuid.uuid4())
        self._session_id = str(uuid.uuid4())
        self._last_request_at = 0.0
        self._load_token()

    # ---- token 缓存 --------------------------------------------------

    def _load_token(self) -> None:
        try:
            raw = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
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
        MUBU_HOME.mkdir(parents=True, exist_ok=True)
        payload = {
            "token": self._token,
            "member_id": self._member_id,
            "name": self._name,
            "user_id": self._user_id,
            "expires_at": self._expires_at,
        }
        tmp = TOKEN_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        # 原子替换，避免多个进程同时刷新 token 时写坏文件
        os.replace(tmp, TOKEN_FILE)

    # ---- 底层 HTTP ---------------------------------------------------

    def _headers(self, token: Optional[str]) -> Dict[str, str]:
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

    def _throttle(self) -> None:
        """保证两次请求之间至少有 min_interval 的间隔。"""
        if self.min_interval <= 0:
            return
        wait = self.min_interval - (time.time() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)

    def _post(self, path: str, payload: Dict[str, Any],
              token: Optional[str] = None) -> Dict[str, Any]:
        url = API_BASE + path
        if not url.startswith("https://" + API_HOST):
            raise MubuError("拒绝向非幕布域名发起请求")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        attempt = 0
        while True:
            self._throttle()
            request = urllib.request.Request(
                url, data=body, headers=self._headers(token), method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    text = response.read().decode("utf-8", "replace")
                    status = response.status
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")
                status = exc.code
            except urllib.error.URLError as exc:
                if attempt < self.max_retries:
                    attempt += 1
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise MubuError(f"网络请求失败：{exc.reason}") from exc
            finally:
                self._last_request_at = time.time()

            # 5xx / 429 退避重试
            if status >= 500 or status == 429:
                if attempt < self.max_retries:
                    attempt += 1
                    time.sleep(min(2 ** attempt, 8))
                    continue

            try:
                obj = json.loads(text)
            except ValueError:
                raise MubuError(f"接口返回了非 JSON 内容（HTTP {status}）：{text[:200]}")

            code = obj.get("code")
            if code != 0:
                message = str(obj.get("msg") or (obj.get("data") or {}).get("message") or "")
                lowered = message.lower()
                is_auth = (
                    code in (2, 1204)
                    or "login expired" in lowered
                    or "未登录" in message
                )
                hint = ERROR_HINTS.get(code)
                detail = f"（{hint}）" if hint else ""
                text_message = f"幕布接口返回 code={code}{detail}：{message}" if message else f"幕布接口返回 code={code}{detail}"
                if is_auth:
                    raise AuthError(text_message, code=code)
                raise MubuError(text_message, code=code)
            return obj.get("data") or {}

    # ---- 登录 --------------------------------------------------------

    def login(self) -> Dict[str, Any]:
        if not (self._phone and self._password):
            self._phone, self._password = load_credentials()
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
        self._save_token()
        return {"name": self._name, "user_id": self._user_id}

    def ensure_token(self, force: bool = False) -> str:
        if not force and self._token and time.time() < self._expires_at - TOKEN_REFRESH_MARGIN:
            return self._token
        self.login()
        assert self._token
        return self._token

    def request(self, path: str, payload: Optional[Dict[str, Any]] = None,
                auth: bool = True, retry: bool = True) -> Dict[str, Any]:
        token = self.ensure_token() if auth else None
        try:
            return self._post(path, payload or {}, token=token)
        except AuthError:
            if not (auth and retry):
                raise
            self.login()
            return self._post(path, payload or {}, token=self._token)

    # ---- 业务方法 ----------------------------------------------------

    def whoami(self) -> Dict[str, Any]:
        self.ensure_token()
        return {
            "name": self._name,
            "user_id": self._user_id,
            "member_id": self._member_id,
            "token_expires_in_seconds": max(0, int(self._expires_at - time.time())),
        }

    def list_dir(self, folder_id: str = "0") -> Dict[str, Any]:
        return self.request(ENDPOINTS["list"], {"folderId": str(folder_id)})

    def get_doc(self, doc_id: str) -> Dict[str, Any]:
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
        """用节点树创建一篇文档（幕布网页版「导入」走的接口）。

        ``nodes`` 形如 ``[{"id": ..., "text": 标题, "children": [...]}]``。
        这是唯一能真正写入正文的路径。
        """
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
    def doc_tree(doc_data: Dict[str, Any]) -> Dict[str, Any]:
        """把 get_doc 返回的 definition 字符串解析成节点树。"""
        raw = doc_data.get("definition")
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except ValueError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return raw if isinstance(raw, dict) else {}
