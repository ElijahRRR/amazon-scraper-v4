"""破坏性端点的可选管理员鉴权。

------------------------------------------------------------------------
为什么需要
------------------------------------------------------------------------
本服务此前**只有** `/api/export/incremental`（与可选的 `/api/v1/sync/*`）有
鉴权，其余一律裸奔——其中包括 `DELETE /api/database`：一次无认证的请求就能
清空整个商品库外加所有截图文件。如果服务只跑在内网，这没问题；但没有任何
地方写着"必须内网"，而 `EXPORT_TOKEN` 的 docstring 明说"服务器是公网 IP"。

------------------------------------------------------------------------
语义：与 EXPORT_TOKEN 一致的 opt-in
------------------------------------------------------------------------
    配了 ADMIN_TOKEN   -> 下面 _PROTECTED 里的端点强制校验，不匹配即 401
    没配               -> 全部放行（与本改动之前的行为逐字节相同），
                          但进程内首次命中受保护端点时打一条 WARNING

**默认不改变任何现有部署的行为**是硬要求：这个仓库已经在跑，突然要求令牌会
让在用的控制台和脚本全线 401。要强制开启就设 ADMIN_TOKEN。

------------------------------------------------------------------------
令牌怎么传：header 或 cookie，两者等价
------------------------------------------------------------------------
    X-Admin-Token: <token>        脚本 / curl / 运维用
    Cookie: admin_token=<token>   浏览器控制台用

之所以**同时**收 cookie：受保护的端点里有好几个是 Web 控制台在用的
（删批次、清空数据库、重启 worker）。只收 header 的话，控制台的每一处
`fetch` 都得包一层去塞 header——五个模板、几十个调用点，漏一个就是一个
点了没反应的按钮。cookie 由浏览器自动附带在同源请求上，一处设置全站生效
（设置页有个输入框写 cookie，见 `settings.html`）。

cookie 用 `SameSite=Strict` 写入，跨站请求不会带上它，因此不构成 CSRF 面。

------------------------------------------------------------------------
为什么是 ASGI 中间件而不是 Depends
------------------------------------------------------------------------
`/openapi.json` 是黄金基线的一步、**逐字节钉死**。`Depends(...)` 会把参数
/ 安全方案渲染进 schema，等于每加一个受保护端点就动一次基线；纯 ASGI
中间件在路由之外，对 schema 零影响。

同理，这里刻意**不是** `BaseHTTPMiddleware` 子类：与 `_RequestIDMiddleware`
保持同一种写法（那里有一条踩过的坑，见其 docstring）。
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

#: 受保护的 (HTTP 方法, 路径正则) 清单。
#:
#: 只收「破坏性 / 管理性」的那几条，**不收**日常读写（`PUT /api/settings`、
#: 建改定时任务、上传批次……）。过度设防的结果是运维图省事把 ADMIN_TOKEN
#: 整个关掉，那还不如只锁住真正要命的几扇门。
_PROTECTED: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    # 清空整个数据库 + 全部截图文件 —— 破坏力最大的一条
    ("DELETE", re.compile(r"^/api/database/?$")),
    # 删批次（连带任务、结果、截图）
    ("DELETE", re.compile(r"^/api/batches/[^/]+/?$")),
    ("POST",   re.compile(r"^/api/batches/delete-bulk/?$")),
    # 删结果
    ("DELETE", re.compile(r"^/api/results/?$")),
    ("POST",   re.compile(r"^/api/results/delete-by-file/?$")),
    # 恢复默认设置（会丢掉全部定时任务：它们就存在 settings 里）
    ("POST",   re.compile(r"^/api/settings/reset/?$")),
    # 机群运维
    ("DELETE", re.compile(r"^/api/workers/?$")),
    ("DELETE", re.compile(r"^/api/workers/[^/]+/?$")),
    ("POST",   re.compile(r"^/api/workers/[^/]+/restart/?$")),
)

_COOKIE_NAME = "admin_token"
_HEADER_NAME = b"x-admin-token"

#: 进程内只警告一次，不然每次删批次都刷一行
_warned_anonymous = False


def is_protected(method: str, path: str) -> bool:
    """这条请求是否落在受保护清单里。"""
    up = (method or "").upper()
    return any(up == m and rx.match(path or "") for m, rx in _PROTECTED)


def expected_token() -> str:
    """当前配置的管理员令牌；空串表示未启用鉴权。"""
    return os.environ.get("ADMIN_TOKEN", "").strip()


def _presented_token(scope) -> Optional[str]:
    """从 header 或 cookie 里取调用方给的令牌。"""
    for raw_key, raw_val in scope.get("headers") or ():
        if raw_key == _HEADER_NAME:
            return raw_val.decode("latin-1").strip()
    for raw_key, raw_val in scope.get("headers") or ():
        if raw_key == b"cookie":
            for part in raw_val.decode("latin-1").split(";"):
                name, _, val = part.strip().partition("=")
                if name == _COOKIE_NAME:
                    return val.strip()
    return None


class AdminTokenMiddleware:
    """纯 ASGI 中间件：对 `_PROTECTED` 里的端点校验 `ADMIN_TOKEN`。

    未配置 `ADMIN_TOKEN` 时**完全透明**——不读 body、不改 header、不碰
    send/receive，对所有响应字节零影响。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        global _warned_anonymous

        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        if not is_protected(scope.get("method", ""), scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        expected = expected_token()
        if not expected:
            if not _warned_anonymous:
                logger.warning(
                    "⚠ 破坏性端点正在【无鉴权】提供服务（如 DELETE /api/database "
                    "可清空全库 + 全部截图）：未配置 ADMIN_TOKEN。"
                    "若本服务可从不受信网络访问，请设置 ADMIN_TOKEN，"
                    "并在控制台「设置」页填入同一个令牌。")
                _warned_anonymous = True
            await self.app(scope, receive, send)
            return

        presented = _presented_token(scope)
        if not presented or not hmac.compare_digest(presented, expected):
            body = json.dumps({
                "error": "invalid_admin_token",
                "message": "该操作需要管理员令牌：请带 X-Admin-Token 头，"
                           "或在控制台「设置」页填入令牌后重试。",
            }, ensure_ascii=False).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)
