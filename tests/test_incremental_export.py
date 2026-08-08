"""catalog_sync 增量导出契约 v1 的用例。

每个用例对应契约里的一句话。契约副本：`docs/incremental_export_contract.md`。

**本文件最重要的一个用例是 test_route_order_is_load_bearing。** 它守的不是逻辑，
是一条隐式依赖：`/api/export/incremental` 落在 `/api/export/{batch_name}` 这条
catch-all 的前缀里，只有注册顺序在前才轮得到它。顺序一旦被挪，端点静默变 404，
而消费方会把 404 读成「暂无数据」并停止推进游标——两侧都不会报错。
"""
from __future__ import annotations

import contextlib
import os
import unittest

from tests.golden import harness
from tests.golden.harness import isolated_server

try:
    from common.dbfactory import is_postgres
except ImportError:  # pragma: no cover
    def is_postgres():
        return False


TOKEN = "test-export-token-12345"


# 展平逻辑的**唯一真源**在 server/routing.py。
#
# 这里以前是一份私有副本，tools/preflight.py 里还有第二份 —— 两份
# 逻辑相同、注释各写各的，守的却是同一条承重不变量。副本会独立腐坏：
# .agent/ARCH_PLAN.md 当年就记着「第二份副本今天是坏的」（catch 为 None
# 时落到 else 报绿）。现在两边都 import 同一份。
from server.routing import flatten_routes as _flatten_routes  # noqa: E402


class RouteOrderTests(unittest.TestCase):
    """路由顺序守卫。**两个后端都要跑**——它守的是注册顺序，与后端无关。

    三层，缺一层都不够：

      1. **结构**：递归展平后比索引。
      2. **行为**：真打一次，响应体不含「批次不存在」。结构断言会被下一次
         FastAPI 版本变化绕过（0.141 那次就绕过了一半），行为断言不会。
      3. **源码**：`server/api/export.py` 里 `include_router(_incr.router)`
         必须出现在第一个 `@router.get` 之前 —— Phase 3.7 把顺序局部化到了
         那个文件里，这一层直接钉住"局部化"本身还成立。
    """

    def test_route_order_is_load_bearing(self):
        """结构性守卫：注册位置必须在 catch-all 之前。

        写法特意做成**跨 FastAPI 版本**的：新形态（``_IncludedRouter`` 包装、
        ``path`` 是 ``None``）与旧形态（摊平的 ``APIRoute``）都要认，否则升个
        版本这条守卫就会静默失效——而它守的恰恰是一种静默失效。
        """
        from server.app import app

        flat = _flatten_routes(app.routes)
        paths = [getattr(r, "path", None) for r in flat]

        incr = next((i for i, p in enumerate(paths)
                     if p == "/api/export/incremental"), None)
        self.assertIsNotNone(
            incr, "端点没挂上——export.py 里的 include_router(_incr.router) 掉了？")

        catch_all = next((i for i, p in enumerate(paths)
                          if p == "/api/export/{batch_name}"), None)
        self.assertIsNotNone(catch_all, "catch-all 不见了，本守卫的前提变了")

        self.assertLess(
            incr, catch_all,
            "/api/export/incremental 必须注册在 /api/export/{batch_name} 之前。\n"
            "现在这个顺序下，它会被 catch-all 吞成 404「批次不存在: incremental」，\n"
            "而 catalog_sync 会把 404 读成「暂无数据」并永远不推进游标——两侧都不报错。\n"
            "修法：把 server/api/export.py 里的 router.include_router(_incr.router)\n"
            "移回该文件第一个 @router.get 之前，不要改成在 catch-all 之后 include。")

    def test_endpoint_is_reachable_not_swallowed(self):
        """端到端确认：打它拿到的不是 catch-all 的 404 文案。"""
        with isolated_server() as (c, _ctx):
            r = c.get("/api/export/incremental", params={"cursor": 0})
            self.assertNotEqual(r.status_code, 404, r.text)
            self.assertNotIn("批次不存在", r.text)

    def test_export_module_includes_incremental_before_first_route(self):
        """源码守卫：顺序局部化本身必须还成立。

        Phase 3.7 之后，「增量端点排在 catch-all 之前」这条不变量不再靠
        `app.py` 里 include 列表的次序，而是靠 `server/api/export.py`
        自上而下的阅读顺序。把那两行挪到任何一条 `@router.get` 之后，
        上面的结构断言当然也会红——但这一层能直接指出**是哪一行**动了，
        而且它在 import 之前就成立，不依赖 FastAPI 的任何内部形态。
        """
        import inspect
        import re as _re

        from server.api import export as _export

        src = inspect.getsource(_export)
        # 必须锚在行首：那份模块 docstring 里就写着 `@router.get` 与
        # `include_router(_incr.router)`（讲的正是这条约束），裸 str.find
        # 会先撞上文档里的那一处，把守卫变成对散文排版的断言。
        m_inc = _re.search(r"^router\.include_router\(_incr\.router\)", src, _re.M)
        self.assertIsNotNone(
            m_inc,
            "server/api/export.py 里找不到 router.include_router(_incr.router)——"
            "增量端点没被包进来，会被 /api/export/{batch_name} 吞成 404")
        m_route = _re.search(r"^@router\.get", src, _re.M)
        self.assertIsNotNone(
            m_route, "server/api/export.py 里一条 @router.get 都没有？")
        self.assertLess(
            m_inc.start(), m_route.start(),
            "router.include_router(_incr.router) 必须写在 export.py 第一个\n"
            "@router.get 之前。排在 @router.get(\"/api/export/{batch_name}\") 之后\n"
            "会让 /api/export/incremental 静默退化成 404「批次不存在: incremental」。")


@unittest.skipUnless(is_postgres(), "增量导出是 PostgreSQL 专属")
class ContractTests(unittest.TestCase):
    """契约语义。只在 PG 后端有意义。"""

    def setUp(self):
        self._saved = os.environ.get("EXPORT_TOKEN")
        os.environ["EXPORT_TOKEN"] = TOKEN

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("EXPORT_TOKEN", None)
        else:
            os.environ["EXPORT_TOKEN"] = self._saved

    def _hdr(self):
        return {"X-Export-Token": TOKEN}

    # ---- 鉴权 ----

    def test_missing_token_is_401(self):
        with _server_with_relay() as (c, _):
            r = c.get("/api/export/incremental", params={"cursor": 0})
            self.assertEqual(r.status_code, 401, r.text)

    def test_wrong_token_is_401(self):
        with _server_with_relay() as (c, _):
            r = c.get("/api/export/incremental", params={"cursor": 0},
                      headers={"X-Export-Token": "wrong"})
            self.assertEqual(r.status_code, 401, r.text)

    def test_unconfigured_token_serves_anonymously_per_contract(self):
        """契约 v1 说鉴权是**可选**的，所以没配 EXPORT_TOKEN 时必须放行。

        （我最初实现的是 fail closed；契约是权威，服从契约。
        想要 fail closed 见下一个用例的 EXPORT_REQUIRE_TOKEN。）
        """
        os.environ.pop("EXPORT_TOKEN", None)
        with _server_with_relay() as (c, _):
            r = c.get("/api/export/incremental", params={"cursor": 0})
            self.assertEqual(r.status_code, 200, r.text)

    def test_require_token_flag_restores_fail_closed(self):
        """运维想要「没配就关闭」时的开关。服务器是公网 IP，值得留这条路。"""
        os.environ.pop("EXPORT_TOKEN", None)
        os.environ["EXPORT_REQUIRE_TOKEN"] = "1"
        try:
            with _server_with_relay() as (c, _):
                r = c.get("/api/export/incremental", params={"cursor": 0})
                self.assertEqual(r.status_code, 503, r.text)
                self.assertEqual(r.json().get("error"),
                                 "export_token_not_configured")
        finally:
            os.environ.pop("EXPORT_REQUIRE_TOKEN", None)

    # ---- 空与边界 ----

    def test_empty_stream_is_200_never_404(self):
        """契约硬性规则：永不用 404 表达「没有数据」。"""
        with _server_with_relay() as (c, _):
            r = c.get("/api/export/incremental",
                      params={"cursor": 0}, headers=self._hdr())
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual(body["records"], [])
            self.assertFalse(body["has_more"])
            self.assertEqual(body["next_cursor"], 0, "空页不得推进游标")

    def test_cursor_zero_pulls_from_the_beginning(self):
        with _server_with_relay() as (c, _):
            _seed(c, n=3)
            r = c.get("/api/export/incremental",
                      params={"cursor": 0, "limit": 100}, headers=self._hdr())
            body = r.json()
            self.assertEqual(len(body["records"]), 3, body)
            self.assertEqual(body["records"][0]["cursor"],
                             min(x["cursor"] for x in body["records"]))

    def test_cursor_is_exclusive_and_ascending(self):
        with _server_with_relay() as (c, _):
            _seed(c, n=4)
            first = c.get("/api/export/incremental",
                          params={"cursor": 0, "limit": 2},
                          headers=self._hdr()).json()
            self.assertTrue(first["has_more"])
            cursors = [r["cursor"] for r in first["records"]]
            self.assertEqual(cursors, sorted(cursors), "必须按 cursor 升序")

            second = c.get("/api/export/incremental",
                           params={"cursor": first["next_cursor"], "limit": 2},
                           headers=self._hdr()).json()
            self.assertTrue(all(r["cursor"] > first["next_cursor"]
                                for r in second["records"]),
                            "cursor 是独占下界")

    def test_repeated_pull_is_harmless_and_stable(self):
        """契约验收项：重复返回无害。同一个 cursor 拉两次结果必须一致。"""
        with _server_with_relay() as (c, _):
            _seed(c, n=3)
            a = c.get("/api/export/incremental", params={"cursor": 0, "limit": 100},
                      headers=self._hdr()).json()
            b = c.get("/api/export/incremental", params={"cursor": 0, "limit": 100},
                      headers=self._hdr()).json()
            self.assertEqual([r["source_id"] for r in a["records"]],
                             [r["source_id"] for r in b["records"]])

    def test_source_id_is_unique_across_the_stream(self):
        """幂等键。消费侧靠它去重，重复推送必须无害。"""
        with _server_with_relay() as (c, _):
            _seed(c, n=5)
            recs = c.get("/api/export/incremental",
                         params={"cursor": 0, "limit": 100},
                         headers=self._hdr()).json()["records"]
            sids = [r["source_id"] for r in recs]
            self.assertEqual(len(sids), len(set(sids)))

    def test_cursor_values_are_unique_so_the_same_cursor_case_is_vacuous(self):
        """契约验收项「cursor 相同多条不丢」。

        我们的 cursor 是 bigserial 主键，**结构上不可能重复**，所以这条验收在
        本实现下是平凡成立的。用例把这个事实钉死：哪天有人把 cursor 换成
        时间戳之类可重复的东西，这里会立刻红。
        """
        with _server_with_relay() as (c, _):
            _seed(c, n=5)
            recs = c.get("/api/export/incremental",
                         params={"cursor": 0, "limit": 100},
                         headers=self._hdr()).json()["records"]
            cursors = [r["cursor"] for r in recs]
            self.assertEqual(len(cursors), len(set(cursors)))

    # ---- record 形状 ----

    def test_record_carries_every_mandatory_field(self):
        with _server_with_relay() as (c, _):
            _seed(c, n=1)
            rec = c.get("/api/export/incremental",
                        params={"cursor": 0}, headers=self._hdr()
                        ).json()["records"][0]
            for k in ("source_id", "cursor", "marketplace", "asin",
                      "scraped_at", "scrape_params", "slow", "fast"):
                self.assertIn(k, rec, f"缺必填字段 {k}")
            self.assertEqual(rec["marketplace"], "US")
            for k in ("title", "brand", "category_path", "images"):
                self.assertIn(k, rec["slow"])
            # 契约 v1 的可选慢变字段
            for k in ("bullet_points", "description", "weight",
                      "dimensions", "variant"):
                self.assertIn(k, rec["slow"])
            for k in ("price", "currency", "stock_state"):
                self.assertIn(k, rec["fast"])
            # 契约 v1 的可选快变字段
            for k in ("buybox_price", "buybox_seller", "coupon", "deal"):
                self.assertIn(k, rec["fast"])
            self.assertIn("slow_hash", rec)
            self.assertIn("raw", rec)
            # scrape_params 的键名是 zipcode（契约 v1），不是 zip
            self.assertIn("zipcode", rec["scrape_params"])

    def test_slow_hash_is_16_hex_per_contract(self):
        """契约 v1：slow_hash 是 sha256 前 16 位。内部存的是 'v1:<64 位>'。"""
        with _server_with_relay() as (c, _):
            _seed(c, n=1)
            rec = c.get("/api/export/incremental",
                        params={"cursor": 0}, headers=self._hdr()
                        ).json()["records"][0]
            h = rec["slow_hash"]
            self.assertIsNotNone(h)
            self.assertEqual(len(h), 16, f"契约要求 16 位，实际 {h!r}")
            self.assertRegex(h, r"^[0-9a-f]{16}$")

    def test_scraped_at_is_second_precision(self):
        """契约 v1：scraped_at 精确到秒。库里带小数秒，必须截掉。"""
        with _server_with_relay() as (c, _):
            _seed(c, n=1)
            rec = c.get("/api/export/incremental",
                        params={"cursor": 0}, headers=self._hdr()
                        ).json()["records"][0]
            self.assertRegex(rec["scraped_at"],
                             r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_stock_state_is_a_closed_three_value_set(self):
        """已确认（2026-08-06）：值域就是这三个，不许再冒出第四个值。"""
        import server.api.export_incremental as m
        allowed = {"in_stock", "out_of_stock", "unknown"}
        samples = [
            {"stock_status": "In Stock"}, {"stock_status": "Currently unavailable"},
            {"stock_status": "Out of Stock"}, {"stock_status": "N/A"},
            {"stock_status": ""}, {}, {"stock_status": "Only 3 left"},
            {"stock_status": "有货"}, {"stock_status": "缺货"},
        ]
        got = {m._stock_state(x) for x in samples}
        self.assertTrue(got <= allowed, f"冒出了值域外的取值: {got - allowed}")

    def test_slow_hash_is_not_recomputable_from_the_slow_object(self):
        """已确认：slow_hash 是**不透明值**。

        这个用例把「不可重算」钉成事实：拿消费侧唯一能拿到的东西（`slow` 对象）
        按契约文字描述的朴素算法（字段排序后 sha256 前 16 位）重算一遍，
        必须**对不上**。对上了反而说明有人把采集侧的算法悄悄换成了朴素版——
        那会丢掉归一化（NFKC / 空白折叠 / 哨兵值 / 图片 URL 归约），
        慢变哈希会重新变成噪声。
        """
        import hashlib
        import json as _json
        with _server_with_relay() as (c, _):
            _seed(c, n=1)
            rec = c.get("/api/export/incremental",
                        params={"cursor": 0}, headers=self._hdr()
                        ).json()["records"][0]
            naive = hashlib.sha256(
                _json.dumps(rec["slow"], sort_keys=True,
                            ensure_ascii=False).encode()).hexdigest()[:16]
            self.assertNotEqual(
                rec["slow_hash"], naive,
                "消费侧按 slow 对象重算竟然对上了——采集侧的归一化算法可能被换掉了")

    def test_default_limit_is_500(self):
        with _server_with_relay() as (c, _):
            import server.api.export_incremental as m
            self.assertEqual(m.DEFAULT_LIMIT, 500)

    def test_scraped_at_is_utc_iso8601(self):
        with _server_with_relay() as (c, _):
            _seed(c, n=1)
            rec = c.get("/api/export/incremental",
                        params={"cursor": 0}, headers=self._hdr()
                        ).json()["records"][0]
            self.assertTrue(rec["scraped_at"].endswith("Z"),
                            f"scraped_at 必须是带 Z 的 UTC: {rec['scraped_at']!r}")

    def test_source_marketplace_is_kept_separate_from_destination(self):
        """上架目的地（marketplace）与采集来源站点是两个概念，不许合并。"""
        with _server_with_relay() as (c, _):
            _seed(c, n=1)
            rec = c.get("/api/export/incremental",
                        params={"cursor": 0}, headers=self._hdr()
                        ).json()["records"][0]
            self.assertEqual(rec["marketplace"], "US")
            self.assertEqual(rec["scrape_params"]["source_marketplace"],
                             "amazon.com")

    def test_limit_is_capped_at_1000(self):
        with _server_with_relay() as (c, _):
            r = c.get("/api/export/incremental",
                      params={"cursor": 0, "limit": 1001}, headers=self._hdr())
            self.assertEqual(r.status_code, 422, r.text)


@contextlib.contextmanager
def _server_with_relay():
    """黄金夹具默认把 relay 也 no-op 掉（录制期必须静音）。契约用例需要它真的跑，
    否则 outbox 永远抽不干、scrape_events 恒空——那测的就不是这个端点。"""
    saved = harness._PATCHED_LOOPS
    harness._PATCHED_LOOPS = tuple(
        x for x in saved if x != "_scrape_event_relay")
    try:
        with isolated_server() as pair:
            yield pair
    finally:
        harness._PATCHED_LOOPS = saved


def _seed(client, n: int = 3):
    """跑一小段真实采集生命周期，并等 relay 把事件流抽干。"""
    import asyncio
    import asyncpg
    from common import config

    asins = [f"B0INCR{i:04d}" for i in range(n)]
    client.post("/api/upload",
                files={"file": ("s.txt", "\n".join(asins).encode(), "text/plain")},
                data={"batch_name": f"incr_{n}", "zip_code": "10001"})
    tasks = client.get("/api/tasks/pull",
                       params={"worker_id": "w-incr", "count": 100}).json()["tasks"]
    client.post("/api/tasks/result/batch", json={"results": [
        {"task_id": t["id"], "batch_id": t["batch_id"], "worker_id": "w-incr",
         "lease_epoch": t["lease_epoch"], "success": True,
         "asin": t["asin"], "title": f"Incr {t['asin']}", "brand": "IncrBrand",
         "category_tree": "Home > Tools > Wrenches",
         "image_urls": "https://m.media-amazon.com/images/I/71ABC._AC_SL1500_.jpg",
         "current_price": "19.99", "stock_status": "In Stock", "stock_count": "5",
         "crawl_time": "2026-08-05T10:00:00Z", "site": "US", "zip_code": "10001"}
        for t in tasks]})

    async def _wait():
        conn = await asyncpg.connect(config.PG_DSN)
        try:
            for _ in range(80):
                left = await conn.fetchval(
                    "SELECT count(*) FROM scraper.scrape_outbox")
                got = await conn.fetchval(
                    "SELECT count(*) FROM scraper.scrape_events")
                if left == 0 and got >= n:
                    return got
                await asyncio.sleep(0.25)
            return got
        finally:
            await conn.close()

    return asyncio.run(_wait())


if __name__ == "__main__":
    unittest.main()
