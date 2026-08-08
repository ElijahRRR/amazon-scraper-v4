"""破坏性端点的可选管理员鉴权（`ADMIN_TOKEN`）。

------------------------------------------------------------------------
最重要的一条在最前面
------------------------------------------------------------------------
**没配 `ADMIN_TOKEN` 时行为必须与本改动之前逐字节一致。** 这个仓库已经在跑，
默认就开始要令牌会让在用的控制台和脚本全线 401。所以 `test_default_is_wide_open`
是本文件的第一条，它红了就说明这次改动会打断现网部署。

其余用例守的是另一半：真配了令牌就得**真的**拦住。

写成 ``unittest.TestCase``：``unittest discover`` 只认 TestCase 子类。
断言与存储后端无关（纯中间件，不碰库），两个后端跑出来一样。
"""
from __future__ import annotations

import os
import unittest
from contextlib import contextmanager

from tests.golden.harness import isolated_server


@contextmanager
def _admin_token(value):
    """临时设/清 ADMIN_TOKEN，退出时恢复原值。"""
    old = os.environ.get("ADMIN_TOKEN")
    if value is None:
        os.environ.pop("ADMIN_TOKEN", None)
    else:
        os.environ["ADMIN_TOKEN"] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("ADMIN_TOKEN", None)
        else:
            os.environ["ADMIN_TOKEN"] = old


def _seed_batch(client, name):
    r = client.post(
        "/api/upload",
        files={"file": ("a.txt", b"B0AUTHZ001\n", "text/plain")},
        data={"batch_name": name, "zip_code": "10001", "needs_screenshot": "false"})
    assert r.status_code == 200, r.text


class AdminTokenAuthzTests(unittest.TestCase):

    def test_default_is_wide_open(self):
        """没配 ADMIN_TOKEN -> 破坏性端点照旧放行（不改变现网行为）。"""
        with _admin_token(None), isolated_server() as (client, _ctx):
            _seed_batch(client, "authz_default")
            self.assertEqual(client.delete("/api/batches/authz_default").status_code, 200)
            self.assertEqual(client.delete("/api/database").status_code, 200)

    def test_configured_token_blocks_anonymous_destructive_calls(self):
        with _admin_token("s3cret"), isolated_server() as (client, _ctx):
            _seed_batch(client, "authz_blocked")
            r = client.delete("/api/batches/authz_blocked")
            self.assertEqual(r.status_code, 401, r.text)
            self.assertEqual(r.json()["error"], "invalid_admin_token")
            # 401 之后批次必须还在 —— 中间件要在到达 handler **之前**拦住
            self.assertEqual(client.get("/api/batches/authz_blocked/status").status_code, 200)

    def test_wrong_token_is_rejected(self):
        with _admin_token("s3cret"), isolated_server() as (client, _ctx):
            _seed_batch(client, "authz_wrong")
            r = client.delete("/api/batches/authz_wrong",
                              headers={"X-Admin-Token": "not-it"})
            self.assertEqual(r.status_code, 401, r.text)

    def test_header_and_cookie_are_equivalent(self):
        """cookie 这条路是给 Web 控制台用的：浏览器自动附带，
        省掉给五个模板几十处 fetch 各包一层塞 header。"""
        with _admin_token("s3cret"), isolated_server() as (client, _ctx):
            _seed_batch(client, "authz_hdr")
            _seed_batch(client, "authz_cookie")

            self.assertEqual(
                client.delete("/api/batches/authz_hdr",
                              headers={"X-Admin-Token": "s3cret"}).status_code, 200)
            self.assertEqual(
                client.delete("/api/batches/authz_cookie",
                              cookies={"admin_token": "s3cret"}).status_code, 200)

    def test_routine_endpoints_stay_open_even_with_token_configured(self):
        """只锁破坏性操作。日常读写照常，否则运维会图省事把整个鉴权关掉。"""
        with _admin_token("s3cret"), isolated_server() as (client, _ctx):
            self.assertEqual(client.get("/api/batches").status_code, 200)
            self.assertEqual(client.get("/api/progress").status_code, 200)
            self.assertEqual(client.get("/api/results").status_code, 200)
            self.assertEqual(client.get("/api/settings").status_code, 200)
            # 上传新批次是日常操作，不该被拦
            r = client.post(
                "/api/upload",
                files={"file": ("a.txt", b"B0AUTHZ002\n", "text/plain")},
                data={"batch_name": "authz_open", "zip_code": "10001",
                      "needs_screenshot": "false"})
            self.assertEqual(r.status_code, 200, r.text)

    def test_the_protected_list_covers_the_scary_ones(self):
        """清单是纯函数，直接断言比逐个端点发请求快且稳。"""
        from server.authz import is_protected

        for method, path in (
            ("DELETE", "/api/database"),
            ("DELETE", "/api/batches/whatever"),
            ("POST", "/api/batches/delete-bulk"),
            ("DELETE", "/api/results"),
            ("POST", "/api/results/delete-by-file"),
            ("POST", "/api/settings/reset"),
            ("DELETE", "/api/workers"),
            ("DELETE", "/api/workers/w1"),
            ("POST", "/api/workers/w1/restart"),
        ):
            self.assertTrue(is_protected(method, path), f"{method} {path} 应受保护")

        for method, path in (
            ("GET", "/api/batches"),
            ("GET", "/api/database"),          # 只锁 DELETE
            ("POST", "/api/upload"),
            ("GET", "/api/results"),
            ("PUT", "/api/settings"),
            ("GET", "/api/workers"),
            ("GET", "/"),
        ):
            self.assertFalse(is_protected(method, path), f"{method} {path} 不该受保护")

    def test_openapi_is_untouched_by_the_middleware(self):
        """中间件不能进 schema —— `/openapi.json` 是黄金基线的一步、逐字节钉死。

        这也是这里用纯 ASGI 中间件而不是 `Depends(...)` 的原因。
        """
        with _admin_token("s3cret"), isolated_server() as (client, _ctx):
            schema = client.get("/openapi.json").json()

        self.assertNotIn("securitySchemes", schema.get("components", {}))
        for path, methods in schema["paths"].items():
            for method, op in methods.items():
                self.assertNotIn("security", op, f"{method} {path} 混进了 security")
                names = [p.get("name") for p in op.get("parameters", [])]
                self.assertNotIn("X-Admin-Token", names, f"{method} {path}")


if __name__ == "__main__":
    unittest.main()
