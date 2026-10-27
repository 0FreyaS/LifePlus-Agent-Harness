"""Password-protected, single-owner interview deployment; not multi-tenant IAM."""
from __future__ import annotations

import base64
import binascii
import hashlib
import secrets
import time
from collections import OrderedDict
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse


def validate_showcase(settings) -> None:
    if settings.app_mode != "showcase":
        return
    password = settings.showcase_password.get_secret_value() if settings.showcase_password else ""
    if not settings.showcase_username or ":" in settings.showcase_username or len(password) < 24:
        raise ValueError("showcase requires a username without ':' and SHOWCASE_PASSWORD of at least 24 characters")
    if settings.public_origin:
        origin = urlsplit(settings.public_origin)
        if origin.scheme != "https" or not origin.netloc or origin.path not in {"", "/"} or origin.query or origin.fragment or origin.username:
            raise ValueError("PUBLIC_ORIGIN must be an HTTPS origin without credentials or a path")


def showcase_owner(username: str) -> str:
    return "showcase:" + hashlib.sha256(username.encode()).hexdigest()[:32]


class ShowcaseAccess:
    def __init__(self, app, settings):
        self.app, self.settings = app, settings
        self.failures: OrderedDict[str, tuple[int, float]] = OrderedDict()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.settings.app_mode != "showcase" or scope["path"] == "/healthz":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        try:
            validate_showcase(self.settings)
        except ValueError:
            await JSONResponse({"detail": "展示访问认证未配置，服务拒绝访问"}, 503)(scope, receive, send)
            return
        # Local HTTP acceptance must be explicitly enabled; never infer trust from Host alone.
        local_http = self.settings.showcase_allow_http and request.url.hostname in {"localhost", "127.0.0.1", "::1"}
        if request.url.scheme != "https" and not local_http:
            await JSONResponse({"detail": "展示站点必须使用 HTTPS"}, 426)(scope, receive, send)
            return
        now = time.monotonic()
        ip = request.client.host if request.client else "unknown"
        count, deadline = self.failures.get(ip, (0, now + 60))
        if deadline <= now:
            count, deadline = 0, now + 60
        if count >= 10:
            await JSONResponse({"detail": "认证尝试过多，请稍后重试"}, 429,
                               headers={"Retry-After": str(max(1, int(deadline - now)))})(scope, receive, send)
            return
        supplied_user, supplied_password = "", ""
        header = request.headers.get("authorization", "")
        try:
            scheme, token = header.split(" ", 1)
            if scheme.lower() == "basic" and len(token) <= 4096:
                supplied_user, supplied_password = base64.b64decode(token, validate=True).decode("utf-8").split(":", 1)
        except (ValueError, UnicodeDecodeError, binascii.Error):
            pass
        expected_password = self.settings.showcase_password.get_secret_value()
        valid_user = secrets.compare_digest(hashlib.sha256(supplied_user.encode()).digest(),
                                            hashlib.sha256(self.settings.showcase_username.encode()).digest())
        valid_password = secrets.compare_digest(hashlib.sha256(supplied_password.encode()).digest(),
                                                hashlib.sha256(expected_password.encode()).digest())
        if not (valid_user and valid_password):
            if header:
                self.failures[ip] = (count + 1, deadline)
                self.failures.move_to_end(ip)
                if len(self.failures) > 4096:
                    self.failures.popitem(last=False)
            await JSONResponse({"detail": "请输入展示账号与访问密码"}, 401, headers={
                "WWW-Authenticate": 'Basic realm="LifePlus showcase", charset="UTF-8"',
                "Cache-Control": "no-store",
            })(scope, receive, send)
            return
        self.failures.pop(ip, None)
        expected_origin = self.settings.public_origin.rstrip("/") or str(request.base_url).rstrip("/")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("origin") not in {None, expected_origin}:
            await JSONResponse({"detail": "拒绝跨站写入请求"}, 403)(scope, receive, send)
            return
        scope.setdefault("state", {})["showcase_owner"] = showcase_owner(self.settings.showcase_username)
        await self.app(scope, receive, send)
