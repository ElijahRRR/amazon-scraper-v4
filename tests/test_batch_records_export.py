"""`GET /api/export/batch/{batch_name}/records` —— 按批次拿**这一批真正采到的**数据。

本文件最重要的一条是 `StaleRowTests.test_failed_asin_does_not_come_back_as_a_stale_row`。
它同时钉住两件事：

  1. 既有出口 `/api/results?batch_id=` **确实**会把上一批的旧行当本批结果返回，
     且响应里没有任何字段能让消费侧看出年龄 —— 这是本端点存在的理由，
     所以它必须被断言，而不是写在注释里。
  2. 新端点对同一个批次**不会**返回那一行。

只断言第 2 条是不够的：那样一来，哪天 `/api/results` 自己补上了状态列、
或者这个洞被别的方式堵上了，这里也不会有任何提示，而本端点的存在理由就变了。
两条一起断言，删掉任意一侧都会红。

事件流是 PostgreSQL 专属，所以整个文件在 SQLite 后端上 skip。
"""
from __future__ import annotations

import contextlib
import unittest

from tests.golden import harness
from tests.golden.harness import isolated_server

try:
    from common.dbfactory import is_postgres
except ImportError:                                    # pragma: no cover
    def is_postgres():
        return False


TOKEN = "test-export-token-12345"
PATH = "/api/export/batch/{}/records"


@contextlib.contextmanager
def _server_with_relay():
    """与 tests/test_incremental_export.py 同一个理由：黄金夹具默认把 relay
    no-op 掉（录制期必须静音），而事件流用例需要它真的跑，否则 outbox 永远
    抽不干、scrape_events 恒空 —— 那测的就不是这个端点。"""
    saved = harness._PATCHED_LOOPS
    harness._PATCHED_LOOPS = tuple(
        x for x in saved if x != "_scrape_event_relay")
    try:
        with isolated_server() as pair:
            yield pair
    finally:
        harness._PATCHED_LOOPS = saved


def _drain(n: int = 1):
    """等 relay 把 outbox 抽干、且 scrape_events 至少有 n 行。"""
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


def _push(c, batch, asins, zip_code="10001"):
    """建批次并返回它的任务列表。

    ⚠ **一次拉完**。pull_tasks 会给拉走的任务上租约，第二次调用拿到的是空列表。
    """
    c.post("/api/upload",
           files={"file": ("s.txt", "\n".join(asins).encode(), "text/plain")},
           data={"batch_name": batch, "zip_code": zip_code})
    return c.get("/api/tasks/pull",
                 params={"worker_id": f"w-{batch}", "count": len(asins) + 5}
                 ).json()["tasks"]


def _ok(c, batch, task, price):
    c.post("/api/tasks/result", json={
        "task_id": task["id"], "batch_id": task["batch_id"],
        "worker_id": f"w-{batch}", "lease_epoch": task["lease_epoch"],
        "success": True, "asin": task["asin"], "title": f"P {task['asin']}",
        "brand": "BatchBrand", "category_tree": "Home > Tools",
        "current_price": price, "buybox_price": price,
        "stock_status": "In Stock", "stock_count": "5",
        "crawl_time": "2026-08-05T10:00:00Z", "site": "US",
        "zip_code": task.get("zip_code") or "10001"})


def _fail(c, batch, task, error_type="timeout"):
    """让任务**终态失败**。retry_count 拉满，免得它被重试掉。"""
    for _ in range(4):
        c.post("/api/tasks/result", json={
            "task_id": task["id"], "batch_id": task["batch_id"],
            "worker_id": f"w-{batch}", "lease_epoch": task["lease_epoch"],
            "success": False, "error_type": error_type,
            "error_detail": "synthetic failure for batch-records test"})


@unittest.skipUnless(is_postgres(), "事件流是 PostgreSQL 专属")
class StaleRowTests(unittest.TestCase):
    """本端点存在的理由。"""

    def _hdr(self):
        return {"X-Export-Token": TOKEN}

    def test_failed_asin_does_not_come_back_as_a_stale_row(self):
        """同一个 ASIN：第一批采成功（19.99），第二批采失败。

        `/api/results?batch_id=<第二批>` 会返回 19.99 那一行，且**看不出**它
        不是本批采的；本端点对第二批则一条成功记录都不给。
        """
        asin = "B0STALE001"
        with _server_with_relay() as (c, _):
            first = _push(c, "stale_first", [asin])
            _ok(c, "stale_first", first[0], "19.99")
            _drain(1)

            second = _push(c, "stale_second", [asin])
            _fail(c, "stale_second", second[0])

            # —— 洞本身：旧行冒充本批结果 ——
            items = c.get("/api/results",
                          params={"batch_id": second[0]["batch_id"]}
                          ).json()["items"]
            hit = [i for i in items if i["asin"] == asin]
            self.assertTrue(
                hit, "前置条件不成立：/api/results 连旧行都没返回，本用例失去意义")
            self.assertEqual(
                hit[0]["current_price"], "19.99",
                "前置条件不成立：拿到的不是上一批那行")
            self.assertNotIn(
                "batch_task_status", hit[0],
                "/api/results 若已经带上本次任务状态，这个洞就被堵上了 —— "
                "那时本端点的存在理由需要重写，别让这条用例继续默默绿着")

            # —— 本端点：这一批没采成，就不给记录 ——
            body = c.get(PATH.format("stale_second"),
                         headers=self._hdr()).json()
            oks = [r for r in body["records"] if r["outcome"] == "ok"]
            self.assertEqual(
                oks, [],
                "第二批一条都没采成，却给出了成功记录 —— 事件流不该有这一行")

    def test_coverage_counts_expose_the_gap(self):
        """`coverage` 让调用方一眼看出"这批有 ASIN 一次事件都没有"。

        ⚠ 缺口用的是**一条从未提交过结果**的任务，不是一条失败的任务：
        终态失败**会**发事件（outcome != 'ok'），那时 `asin_with_event` 照样
        计数。这正是端点 docstring 里那句「`asin_total == asin_with_event`
        不等于这批都成功了」的由来 —— 覆盖率回答的是"有没有采过"，
        成功与否要看每条记录的 `outcome`。
        """
        with _server_with_relay() as (c, _):
            tasks = _push(c, "cov", ["B0COVOK001", "B0COVNON01"])
            by_asin = {t["asin"]: t for t in tasks}
            _ok(c, "cov", by_asin["B0COVOK001"], "9.99")
            # B0COVNON01 的结果**永不提交** -> 它一条事件都不会有
            _drain(1)

            cov = c.get(PATH.format("cov"), headers=self._hdr()
                        ).json()["coverage"]

        self.assertEqual(cov["asin_total"], 2,
                         "分母是 batch_asins（入过队的，含没采成的）")
        self.assertEqual(cov["asin_with_event"], 1,
                         "只有一个 ASIN 产生过事件，差额就是那个没采过的")

    def test_terminal_failure_still_counts_as_covered(self):
        """守住上一条注释里那个反直觉点：失败**也是**一次采集事件。

        把它写成用例而不是注释 —— 若哪天 coverage 改成"只数成功的"，
        端点 docstring 就变成谎言，而调用方的告警判据会跟着错。
        """
        with _server_with_relay() as (c, _):
            tasks = _push(c, "cov_fail", ["B0COVFAIL1"])
            _fail(c, "cov_fail", tasks[0])
            _drain(1)

            body = c.get(PATH.format("cov_fail"), headers=self._hdr()).json()

        self.assertEqual(body["coverage"], {"asin_total": 1,
                                            "asin_with_event": 1})
        self.assertTrue(body["records"], "终态失败该有一条事件")
        self.assertNotEqual(
            body["records"][0]["outcome"], "ok",
            "失败的记录 outcome 不能是 ok —— 覆盖率算它、成功判据不算它")


@unittest.skipUnless(is_postgres(), "事件流是 PostgreSQL 专属")
class ContractTests(unittest.TestCase):

    def _hdr(self):
        return {"X-Export-Token": TOKEN}

    def test_unknown_batch_is_404_but_empty_batch_is_200(self):
        """404 在这一族端点里**只能**有一个含义：批次名不存在。

        批次存在但一条事件都没有 -> 200 + []。两者若都回 404，消费方分不清
        "打错名字"和"还没采完"，而后者会被读成"暂无数据"并静默停摆。
        """
        with _server_with_relay() as (c, _):
            missing = c.get(PATH.format("no_such_batch_at_all"),
                            headers=self._hdr())
            self.assertEqual(missing.status_code, 404, missing.text)
            self.assertEqual(missing.json()["error"], "batch_not_found")

            _push(c, "empty_batch", ["B0EMPTY001"])   # 建了但不提交结果
            empty = c.get(PATH.format("empty_batch"), headers=self._hdr())

        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertEqual(empty.json()["records"], [])
        self.assertFalse(empty.json()["has_more"])

    def test_records_are_byte_identical_to_the_global_stream(self):
        """两个端点对同一行必须给出**同一个** record。

        它们共用 `_to_record`；这条用例守的是"别再抄一份"——那正是本仓库
        V2/V4 两次事故的形状（.agent/MIGRATION_STATUS.md §5.5）。
        """
        with _server_with_relay() as (c, _):
            tasks = _push(c, "same_shape", ["B0SAMESHP1"])
            _ok(c, "same_shape", tasks[0], "33.33")
            _drain(1)

            per_batch = c.get(PATH.format("same_shape"), headers=self._hdr()
                              ).json()["records"]
            globally = c.get("/api/export/incremental",
                             params={"cursor": 0, "limit": 1000},
                             headers=self._hdr()).json()["records"]

        self.assertEqual(len(per_batch), 1)
        mine = [r for r in globally if r["cursor"] == per_batch[0]["cursor"]]
        self.assertEqual(len(mine), 1, "全量流里找不到同一个 cursor")
        self.assertEqual(per_batch[0], mine[0],
                         "同一条事件在两个端点上给出了不同的 record")

    def test_records_are_scoped_to_the_batch(self):
        """别的批次的事件**一条都不能**混进来。

        单批次的用例发现不了这个：一个库里只有一批时，"漏了 WHERE batch_id"
        与"没漏"给出同一个答案。必须有第二批当诱饵。
        """
        with _server_with_relay() as (c, _):
            mine = _push(c, "scope_mine", ["B0SCOPEMN1"])
            other = _push(c, "scope_other", ["B0SCOPEOT1"])
            _ok(c, "scope_mine", mine[0], "11.11")
            _ok(c, "scope_other", other[0], "22.22")
            _drain(2)

            body = c.get(PATH.format("scope_mine"), headers=self._hdr()).json()

        self.assertEqual([r["asin"] for r in body["records"]], ["B0SCOPEMN1"],
                         "另一个批次的事件漏进来了 —— WHERE batch_id 没生效")
        self.assertEqual(body["coverage"], {"asin_total": 1,
                                            "asin_with_event": 1},
                         "coverage 也必须按批次算，不是全库")

    def test_cursor_pages_within_the_batch_and_stops(self):
        with _server_with_relay() as (c, _):
            asins = [f"B0PAGE{i:04d}" for i in range(3)]
            tasks = _push(c, "paging", asins)
            for t in tasks:
                _ok(c, "paging", t, "5.55")
            _drain(3)

            seen, cursor, pages = [], 0, 0
            while True:
                body = c.get(PATH.format("paging"),
                             params={"cursor": cursor, "limit": 2},
                             headers=self._hdr()).json()
                seen.extend(r["asin"] for r in body["records"])
                pages += 1
                cursor = body["next_cursor"]
                if not body["has_more"] or pages > 10:
                    break

        self.assertEqual(sorted(seen), sorted(asins))
        self.assertEqual(pages, 2, "3 条 / 每页 2 条 = 2 页")

    def test_empty_page_does_not_advance_the_cursor(self):
        """与全量流同一条纪律：游标只推进到**真正投递过的那一条**。

        空页推进游标是唯一会丢数据的方向。
        """
        with _server_with_relay() as (c, _):
            tasks = _push(c, "no_advance", ["B0NOADVN01"])
            _ok(c, "no_advance", tasks[0], "1.23")
            _drain(1)

            first = c.get(PATH.format("no_advance"), headers=self._hdr()).json()
            tail = c.get(PATH.format("no_advance"),
                         params={"cursor": first["next_cursor"]},
                         headers=self._hdr()).json()

        self.assertEqual(tail["records"], [])
        self.assertEqual(tail["next_cursor"], first["next_cursor"])

    def test_token_is_enforced_like_the_global_stream(self):
        with _server_with_relay() as (c, _):
            _push(c, "auth_probe", ["B0AUTHPR01"])
            bad = c.get(PATH.format("auth_probe"),
                        headers={"X-Export-Token": "wrong"})
            none = c.get(PATH.format("auth_probe"))

        self.assertEqual(bad.status_code, 401, bad.text)
        self.assertEqual(bad.json()["error"], "invalid_export_token")
        self.assertEqual(none.status_code, 401, none.text)

    def test_limit_is_capped_at_1000(self):
        with _server_with_relay() as (c, _):
            _push(c, "cap_probe", ["B0CAPPRB01"])
            r = c.get(PATH.format("cap_probe"),
                      params={"limit": 1001}, headers=self._hdr())
        self.assertEqual(r.status_code, 422, r.text)


@unittest.skipUnless(is_postgres(), "事件流是 PostgreSQL 专属")
class ConsumerScriptTests(unittest.TestCase):
    """`tools/consume_batch.py` 打的是真 app（TestClient 当传输层）。

    为什么不起一条真 socket：``isolated_server`` 的连接池绑在 TestClient 那条
    事件循环上，另起 uvicorn 会用第二条循环，asyncpg 连接跨循环即
    ``ConnectionDoesNotExistError`` —— 那测的是夹具，不是脚本。
    换传输层保留了脚本的**全部**逻辑：分页、去重、过滤、截图、退出码。
    """

    def _load(self, c, tmp):
        """import 脚本，把它的 HTTP 层换成 TestClient。"""
        import importlib.util
        import os

        path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "tools", "consume_batch.py")
        spec = importlib.util.spec_from_file_location("consume_batch", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        def _get(server, p, token, params=None, raw=False, timeout=60):
            r = c.get(p, params={k: v for k, v in (params or {}).items()
                                 if v is not None},
                      headers={"X-Export-Token": token} if token else {})
            return r.status_code, (r.content if raw else r.json())

        mod._get = _get
        return mod

    def _args(self, mod, tmp, **kw):
        import argparse
        base = dict(server="http://testserver", token=TOKEN, out=tmp,
                    limit=500, strict_zip=False, screenshots=False)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_batch_command_writes_only_the_usable_records(self):
        """脚本把「事件」变成「可用行」：失败的、邮编对不上的都不该落到输出里。"""
        import json
        import tempfile

        with _server_with_relay() as (c, _):
            tasks = _push(c, "script_demo", ["B0SCRPOK01", "B0SCRPBAD1"])
            by_asin = {t["asin"]: t for t in tasks}
            _ok(c, "script_demo", by_asin["B0SCRPOK01"], "24.50")
            _fail(c, "script_demo", by_asin["B0SCRPBAD1"])
            _drain(2)

            with tempfile.TemporaryDirectory() as tmp:
                mod = self._load(c, tmp)
                mod.cmd_batch(self._args(mod, tmp, batch_name="script_demo"))
                with open(f"{tmp}/batch_script_demo.jsonl", encoding="utf-8") as fh:
                    rows = [json.loads(x) for x in fh if x.strip()]
                import os as _os
                self.assertTrue(_os.path.exists(f"{tmp}/batch_script_demo.csv"))

        self.assertEqual([r["asin"] for r in rows], ["B0SCRPOK01"],
                         "只有 outcome=ok 且邮编可信的记录该落盘")
        self.assertAlmostEqual(rows[0]["fast"]["price"], 24.50, places=2)

    def test_mismatch_zip_is_dropped_but_unverified_is_kept(self):
        """`mismatch` 是"证据表明是错的"，`unverified` 只是"没证据"。

        两者若同等对待，要么白丢一大批能用的数据，要么把**别的邮编**的价格
        当成你要的那个邮编的 —— 后者是个算得出数、但数是错的错误。
        """
        with _server_with_relay() as (c, _):
            mod = self._load(c, ".")

        def rec(verify):
            return {"asin": "B0ZIPPROB1", "outcome": "ok",
                    "scrape_params": {"zipcode": "10001", "zip_verify": verify}}

        self.assertFalse(mod.usable(rec("mismatch")))
        self.assertTrue(mod.usable(rec("unverified")))
        self.assertTrue(mod.usable(rec("assumed")))
        self.assertTrue(mod.usable(rec("confirmed")))
        # --strict-zip 只留页面级证据
        self.assertFalse(mod.usable(rec("unverified"), strict_zip=True))
        self.assertTrue(mod.usable(rec("confirmed"), strict_zip=True))

    def test_group_key_is_asin_plus_zipcode(self):
        """同一 ASIN 的两个邮编是两条事实，不能归并成一条。"""
        with _server_with_relay() as (c, _):
            mod = self._load(c, ".")

        a = {"asin": "B0GRPKEY01", "scrape_params": {"zipcode": "10001"}}
        b = {"asin": "B0GRPKEY01", "scrape_params": {"zipcode": "90001"}}
        self.assertNotEqual(mod.key_of(a), mod.key_of(b),
                            "只按 asin 分组会让两个邮编互相覆盖")


if __name__ == "__main__":
    unittest.main()
