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
            for k in ("buybox_price", "buybox_seller", "coupon", "deal",
                      "stock_count", "delivery_days", "shipping", "shipping_raw"):
                self.assertIn(k, rec["fast"])
            self.assertIn("slow_hash", rec)
            self.assertIn("raw", rec)
            # scrape_params 的键名是 zipcode（契约 v1），不是 zip
            self.assertIn("zipcode", rec["scrape_params"])

    def test_stock_count_and_delivery_days_are_ints(self):
        """`_seed` 提交的是字符串 "5"，导出必须给 int 5 而不是 "5"。"""
        with _server_with_relay() as (c, _):
            _seed(c, n=1)
            fast = c.get("/api/export/incremental",
                         params={"cursor": 0}, headers=self._hdr()
                         ).json()["records"][0]["fast"]

        self.assertIsInstance(fast["stock_count"], int)
        self.assertEqual(fast["stock_count"], 5)
        self.assertNotIsInstance(fast["stock_count"], str)

    def test_zero_stock_count_is_not_null(self):
        """**这条是这两个字段最容易做错的地方。**

        `stock_count=0` 是个合法值（缺货），绝不能被当成「没采到」变 null——
        消费侧若拿 null 当 0 处理、或拿 0 当 null 处理，两种情况刚好互相掩盖。
        与 `_price` 同一条原则：0 是数据，不是哨兵。
        """
        with _server_with_relay() as (c, _):
            c.post("/api/upload",
                   files={"file": ("z.txt", b"B0ZEROSTK1\n", "text/plain")},
                   data={"batch_name": "zero_stock", "zip_code": "10001"})
            t = c.get("/api/tasks/pull",
                      params={"worker_id": "w-zero", "count": 1}).json()["tasks"][0]
            c.post("/api/tasks/result", json={
                "task_id": t["id"], "batch_id": t["batch_id"],
                "worker_id": "w-zero", "lease_epoch": t["lease_epoch"],
                "success": True, "asin": t["asin"], "title": "Zero Stock",
                "current_price": "9.99", "stock_status": "Currently unavailable",
                "stock_count": "0", "delivery_time": "0",
                "crawl_time": "2026-08-05T10:00:00Z", "site": "US",
                "zip_code": "10001"})
            _drain(c)
            recs = c.get("/api/export/incremental",
                         params={"cursor": 0, "limit": 500}, headers=self._hdr()
                         ).json()["records"]

        hit = [r for r in recs if r["asin"] == "B0ZEROSTK1"]
        self.assertTrue(hit, "样本没进事件流")
        fast = hit[0]["fast"]
        self.assertEqual(fast["stock_count"], 0)
        self.assertIsNotNone(fast["stock_count"], "0 被当成「没采到」吞成了 null")
        self.assertEqual(fast["delivery_days"], 0)

    def test_missing_values_are_null_not_zero(self):
        """反过来也要成立：真的没采到时给 null，不能给 0。"""
        with _server_with_relay() as (c, _):
            c.post("/api/upload",
                   files={"file": ("m.txt", b"B0NOSTOCK1\n", "text/plain")},
                   data={"batch_name": "missing_stock", "zip_code": "10001"})
            t = c.get("/api/tasks/pull",
                      params={"worker_id": "w-miss", "count": 1}).json()["tasks"][0]
            c.post("/api/tasks/result", json={
                "task_id": t["id"], "batch_id": t["batch_id"],
                "worker_id": "w-miss", "lease_epoch": t["lease_epoch"],
                "success": True, "asin": t["asin"], "title": "No Stock Info",
                "current_price": "9.99", "stock_status": "In Stock",
                # 采集侧「没取到」的哨兵就是 "N/A"
                "stock_count": "N/A", "delivery_time": "N/A",
                "crawl_time": "2026-08-05T10:00:00Z", "site": "US",
                "zip_code": "10001"})
            _drain(c)
            recs = c.get("/api/export/incremental",
                         params={"cursor": 0, "limit": 500}, headers=self._hdr()
                         ).json()["records"]

        hit = [r for r in recs if r["asin"] == "B0NOSTOCK1"]
        self.assertTrue(hit, "样本没进事件流")
        fast = hit[0]["fast"]
        self.assertIsNone(fast["stock_count"])
        self.assertIsNone(fast["delivery_days"])

    # ---------------- 运费（fast.shipping / fast.shipping_raw） ----------------
    #
    # 采集侧存的是**字符串**，三种形态："FREE" / "N/A" / "$5.99"。
    # 对外要拆成两个字段，而三者必须映射到**三个互不相同**的结果：
    #
    #     FREE   -> shipping 0.0    确认免运费，落地价 = price + 0
    #     N/A    -> shipping null   没采到，落地价**算不出来**
    #     $5.99  -> shipping 5.99
    #
    # 把 N/A 也当成 0（UI 导出的「总价」列现在就是这么干的）是这里最容易犯、
    # 也最难发现的错：落地价照样算得出来、数值看着正常，只是**偏小**。
    def _submit_shipping(self, c, asin, shipping_value, batch):
        """提交一条只在 buybox_shipping 上有差别的采集结果，返回它的 fast 块。"""
        c.post("/api/upload",
               files={"file": ("s.txt", f"{asin}\n".encode(), "text/plain")},
               data={"batch_name": batch, "zip_code": "10001"})
        t = c.get("/api/tasks/pull",
                  params={"worker_id": f"w-{batch}", "count": 1}).json()["tasks"][0]
        c.post("/api/tasks/result", json={
            "task_id": t["id"], "batch_id": t["batch_id"],
            "worker_id": f"w-{batch}", "lease_epoch": t["lease_epoch"],
            "success": True, "asin": t["asin"], "title": "Shipping Probe",
            "current_price": "19.99", "buybox_price": "19.99",
            "buybox_shipping": shipping_value,
            "stock_status": "In Stock", "stock_count": "5",
            "crawl_time": "2026-08-05T10:00:00Z", "site": "US",
            "zip_code": "10001"})
        _drain(c)
        recs = c.get("/api/export/incremental",
                     params={"cursor": 0, "limit": 500}, headers=self._hdr()
                     ).json()["records"]
        hit = [r for r in recs if r["asin"] == asin]
        self.assertTrue(hit, f"样本 {asin} 没进事件流")
        return hit[0]["fast"]

    def test_numeric_shipping_is_a_number_and_raw_keeps_the_string(self):
        with _server_with_relay() as (c, _):
            fast = self._submit_shipping(c, "B0SHIPNUM1", "$5.99", "ship_num")

        self.assertAlmostEqual(fast["shipping"], 5.99, places=2)
        self.assertNotIsInstance(fast["shipping"], str,
                                 "shipping 要能直接参与落地价计算，不能是字符串")
        self.assertEqual(fast["shipping_raw"], "$5.99",
                         "shipping_raw 要原样留住采集侧那个串，供消费侧复核")

    def test_free_shipping_is_zero_not_null(self):
        """`FREE` 是**一条真信息**（确认免运费），丢成 null 就和「没采到」混了。"""
        with _server_with_relay() as (c, _):
            fast = self._submit_shipping(c, "B0SHIPFRE1", "FREE", "ship_free")

        self.assertEqual(fast["shipping"], 0.0)
        self.assertIsNotNone(fast["shipping"],
                             "FREE 被吞成 null —— 「确认免运费」变成了「不知道」")
        self.assertEqual(fast["shipping_raw"], "FREE")

    def test_unknown_shipping_is_null_not_zero(self):
        """**这条是运费最容易做错的地方。**

        采集侧的 `N/A` 是「这次没采到」。当成 0 的话落地价照样算得出来、
        看着也正常，只是**偏小**——没有任何一侧会报错。
        与 `stock_count` 同一条不变量（3b）：null ≠ 0，消费端不能写 `or 0`。
        """
        with _server_with_relay() as (c, _):
            fast = self._submit_shipping(c, "B0SHIPNA01", "N/A", "ship_na")

        self.assertIsNone(fast["shipping"],
                          "没采到的运费被当成 0 —— 落地价会静默偏小")
        self.assertIsNone(fast["shipping_raw"],
                          "N/A 是哨兵不是值，_clean 应当把它归一到 null")

    def test_free_and_unknown_do_not_collapse_into_the_same_value(self):
        """把三种形态摆在一起 —— 这才是真正要守的不变量。

        单独看，每条都可能被「统一成 0 更好算」或「统一成 null 更保守」改掉而
        只红一条；摆在一起，任何两者塌成同一个值都会被这条直接指出来。
        """
        with _server_with_relay() as (c, _):
            got = {
                "free": self._submit_shipping(
                    c, "B0SHIPMX01", "FREE", "ship_mx_free")["shipping"],
                "unknown": self._submit_shipping(
                    c, "B0SHIPMX02", "N/A", "ship_mx_na")["shipping"],
                "priced": self._submit_shipping(
                    c, "B0SHIPMX03", "$5.99", "ship_mx_num")["shipping"],
            }

        self.assertEqual(got["free"], 0.0)
        self.assertIsNone(got["unknown"])
        self.assertAlmostEqual(got["priced"], 5.99, places=2)
        # 0.0 == False、None != 0 —— 用 repr 比对，避免 Python 的真值坑
        # 把「塌成同一个值」这件事本身给掩盖过去。
        self.assertEqual(
            len({repr(v) for v in got.values()}), 3,
            f"FREE / 没采到 / 具体金额 必须是三个不同的值，实际 {got}")

    # ---------------- slow.variant.theme（变体维度名） ----------------
    #
    # 这一族守的是一个**曾经恒为 null** 的字段。`_variant` 原先自带一份
    # 格式写错的解析：按 `:` 切，而采集侧 `worker/parser.py:_parse_twister`
    # 拼的是 `"; ".join("%s=%s" % (dim, val))` —— 分隔符是 `=`。
    # 于是 `if ":" in seg` 对每一段都为假，theme 对**所有真实记录**恒为 null，
    # 而 parent_asin 照常有值、`variant` 对象不是 None，从响应上看不出任何异常。
    #
    # 用真实格式做输入是这几条用例的全部意义：夹具里只要写成 "Color:Red"，
    # 这个 bug 就能一直绿着活下去（原先的夹具正是这么写的）。
    def _submit_variant(self, c, asin, variant_attributes, batch, parent="B0PARENT01"):
        c.post("/api/upload",
               files={"file": ("v.txt", f"{asin}\n".encode(), "text/plain")},
               data={"batch_name": batch, "zip_code": "10001"})
        t = c.get("/api/tasks/pull",
                  params={"worker_id": f"w-{batch}", "count": 1}).json()["tasks"][0]
        c.post("/api/tasks/result", json={
            "task_id": t["id"], "batch_id": t["batch_id"],
            "worker_id": f"w-{batch}", "lease_epoch": t["lease_epoch"],
            "success": True, "asin": t["asin"], "title": "Variant Probe",
            "brand": "B", "category_tree": "Home > Tools",
            "image_urls": "https://m.media-amazon.com/images/I/71A._AC_SL1500_.jpg",
            "current_price": "19.99", "stock_status": "In Stock",
            "parent_asin": parent, "variant_attributes": variant_attributes,
            "crawl_time": "2026-08-05T10:00:00Z", "site": "US",
            "zip_code": "10001"})
        _drain(c)
        recs = c.get("/api/export/incremental",
                     params={"cursor": 0, "limit": 500}, headers=self._hdr()
                     ).json()["records"]
        hit = [r for r in recs if r["asin"] == asin]
        self.assertTrue(hit, f"样本 {asin} 没进事件流")
        return hit[0]["slow"]["variant"]

    def test_theme_uses_the_real_producer_format(self):
        """**这条对应 theme 恒为 null 那个 bug。**

        输入是 `worker/parser.py:_parse_twister` 真正会产出的串。
        """
        with _server_with_relay() as (c, _):
            v = self._submit_variant(
                c, "B0VARTHM01", "color_name=Red; size_name=L", "var_theme")

        self.assertEqual(v["parent_asin"], "B0PARENT01")
        self.assertEqual(
            v["theme"], "color_name/size_name",
            "theme 没解析出来 —— 采集侧用的是 `=` 分隔（color_name=Red），"
            "别再按 `:` 切；正确的解析在 common/slowhash.parse_variant_attributes")

    def test_theme_keeps_the_producer_dimension_order(self):
        """维度顺序按采集侧的 `dimensions` 数组来，不排序。

        `color/size` 与 `size/color` 描述同一族，但前者才是页面上的次序；
        排序会把这个信息抹掉。
        """
        with _server_with_relay() as (c, _):
            v = self._submit_variant(
                c, "B0VARORD01", "size_name=L; color_name=Red", "var_order")
        self.assertEqual(v["theme"], "size_name/color_name")

    def test_single_dimension_variant(self):
        with _server_with_relay() as (c, _):
            v = self._submit_variant(c, "B0VARONE01", "color_name=Blue", "var_one")
        self.assertEqual(v["theme"], "color_name")

    def test_values_without_dimension_names_give_null_theme(self):
        """采集侧拿不到维度名时只产出裸值（`"Red; L"`）——那时 theme 必须是 null。

        编一个维度名出来比留空更糟：下游会拿它当真的维度去分组。
        """
        with _server_with_relay() as (c, _):
            v = self._submit_variant(c, "B0VARBAR01", "Red; L", "var_bare")
        self.assertEqual(v["parent_asin"], "B0PARENT01")
        self.assertIsNone(
            v["theme"],
            "只有值没有维度名时不该编出 theme —— 那些裸值落在 slowhash 的无键槽里")

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


def _drain(client=None, n: int = 1):
    """等 relay 把 outbox 抽干、且 scrape_events 至少有 n 行。

    从 `_seed` 里抽出来的：只提交结果、不用 `_seed` 那套固定字段的用例
    （比如库存 0 / 缺值那两条）同样需要等 relay，否则查出来恒空。
    """
    import asyncio
    import asyncpg
    from common import config

    async def _wait():
        conn = await asyncpg.connect(config.PG_DSN)
        try:
            got = 0
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
