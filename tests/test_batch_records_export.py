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
    """让任务**真正**走到终态失败（retry 耗尽）。

    ⚠ 两个坑，都踩过，都很安静：

    1. **不能复用同一个 lease_epoch 反复提交。** 第一次被接受（非终态，
       任务释放回 pending 且 lease_epoch +1），之后每一次都是 `stale`。
       实测（PG，事件流）：

           同一 epoch 提交 4 次 -> 事件 [(1,'stale'), (2,'stale'), (3,'stale')]
                                   任务仍是 open，一次终态失败都没发生
           每轮重新 pull        -> 事件 [(1,'parse_failed')]，任务 failed

    2. **第一轮必须用传进来的那个 task。** 调用方的 `_push` 已经把任务
       pull 走了（状态 processing、租约在手），这时再 pull 拿不到它 ——
       直接 return 就等于一次失败都没提交。

    也就是说：**非终态失败不发事件，终态失败发一条 `parse_failed`，
    stale 提交每次发一条 `stale`**。拿 `outcome != "ok"` 当"终态失败"的判据
    会被 stale 满足 —— 那正是本仓库最怕的"因为错误的理由而变绿"。
    """
    cur = task
    for _ in range(6):
        c.post("/api/tasks/result", json={
            "task_id": cur["id"], "batch_id": cur["batch_id"],
            "worker_id": f"w-{batch}", "lease_epoch": cur["lease_epoch"],
            "success": False, "error_type": error_type,
            "error_detail": "synthetic failure for test"})
        got = c.get("/api/tasks/pull",
                    params={"worker_id": f"w-{batch}", "count": 20}).json()["tasks"]
        mine = [t for t in got if t["asin"] == task["asin"]]
        if not mine:
            return          # 拉不到了 = 已经终态
        cur = mine[0]


@unittest.skipUnless(is_postgres(), "事件流是 PostgreSQL 专属")
class StaleRowTests(unittest.TestCase):
    """本端点存在的理由。"""

    def _hdr(self):
        return {"X-Export-Token": TOKEN}

    def test_failed_asin_does_not_come_back_as_a_stale_row(self):
        """同一个 ASIN：第一批采成功（19.99），第二批采失败。

        `/api/results?batch_id=<第二批>` 仍然会返回 19.99 那一行 —— 内容是
        **上一批**的。它现在带 `batch_task_status=failed`，所以**看得出**
        不是本批采的（那三列是后来补的，见下面的注释）；但要拿到"这一批
        真正采到了什么"，只有本端点能给。
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
            # ⚠ 这里原本断言的是 `assertNotIn("batch_task_status", ...)` ——
            # 一条**故意设的绊线**：`/api/results` 一旦带上本次任务状态，
            # 这个洞就被堵上了一半，本端点的存在理由就得重写。
            # 绊线按预期响了（补三列那一轮），所以这里如实改成新的现状：
            #
            #   现在能做到的：`batch_task_status` 让你**看得出**这行是旧的。
            #   仍然做不到的：返回的**内容**还是上一次那行（19.99），
            #                 而且这批里一次都没采过的 ASIN **整行不出现**
            #                 （INNER JOIN asin_data，见
            #                  tests/test_results_batch_status.py 的同名用例）。
            #
            # 也就是说本端点的理由从"没法分辨"变成了"分辨得出、但拿不到
            # 这一批真正采到的东西"。
            self.assertEqual(
                hit[0]["batch_task_status"], "failed",
                "/api/results 现在应当带上本次任务状态（补三列那一轮加的）")

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

        ⚠ **本条曾经因为错误的理由而绿。** 旧的 `_fail` 复用同一个
        lease_epoch 反复提交，实际产生的是三条 `outcome='stale'` 事件、
        任务根本没进终态；而断言写的是 `outcome != "ok"`，stale 照样满足。
        修好 `_fail`（每轮重新 pull）之后才真的是 parse_failed。
        断言也收紧成等于 `parse_failed`，不再是"不等于 ok"。
        """
        with _server_with_relay() as (c, _):
            tasks = _push(c, "cov_fail", ["B0COVFAIL1"])
            _fail(c, "cov_fail", tasks[0])
            _drain(1)

            body = c.get(PATH.format("cov_fail"), headers=self._hdr()).json()

        self.assertEqual(body["coverage"], {"asin_total": 1,
                                            "asin_with_event": 1})
        self.assertTrue(body["records"], "终态失败该有一条事件")
        self.assertEqual(
            body["records"][0]["outcome"], "parse_failed",
            "终态失败的 outcome 必须是 parse_failed。写成 `!= \"ok\"` 是不够的："
            "stale 提交也满足，而那根本不是一次终态失败")


@unittest.skipUnless(is_postgres(), "事件流是 PostgreSQL 专属")
class RetryTimelineTests(unittest.TestCase):
    """`docs/incremental_export_contract.md` §5.1 那张"什么时候发事件"的表。

    文档里的每一行都在这里有断言，否则它就只是一段会过期的散文 ——
    而消费侧的告警判据是照着它写的。
    """

    def _hdr(self):
        return {"X-Export-Token": TOKEN}

    def _events(self, c, batch):
        return c.get(PATH.format(batch), headers=self._hdr()).json()["records"]

    def test_non_terminal_failure_emits_nothing(self):
        """还会重试的失败**不发事件**。

        发了的话，一个最终会成功的 ASIN 会在流里留下 2~3 条
        parse_failed 噪声，消费侧的失败率统计全错。
        """
        with _server_with_relay() as (c, _):
            tasks = _push(c, "tl_one", ["B0TLONE001"])
            t = tasks[0]
            # **只提交一次**失败：retry_count 1 < 3，非终态
            c.post("/api/tasks/result", json={
                "task_id": t["id"], "batch_id": t["batch_id"],
                "worker_id": "w-tl_one", "lease_epoch": t["lease_epoch"],
                "success": False, "error_type": "timeout", "error_detail": "x"})
            _drain(0)
            recs = self._events(c, "tl_one")

        self.assertEqual(recs, [], "非终态失败不该产生事件")

    def test_terminal_failure_emits_exactly_one_parse_failed(self):
        with _server_with_relay() as (c, _):
            tasks = _push(c, "tl_term", ["B0TLTERM01"])
            _fail(c, "tl_term", tasks[0])
            _drain(1)
            recs = self._events(c, "tl_term")

        self.assertEqual([r["outcome"] for r in recs], ["parse_failed"],
                         "终态失败应当恰好一条 parse_failed")

    def test_stale_submission_emits_stale_not_a_failure(self):
        """⚠ `stale` 也满足 `outcome != "ok"` —— 文档里专门警告过这一点。

        它既不代表采集失败，也不代表数据可用；拿 `!= "ok"` 当失败判据
        会把租约门挡掉的提交算成采集失败。
        """
        with _server_with_relay() as (c, _):
            tasks = _push(c, "tl_stale", ["B0TLSTAL01"])
            t = tasks[0]
            # 先用正确 epoch 失败一次（任务释放、epoch +1），
            # 再用**旧** epoch 提交 -> stale
            c.post("/api/tasks/result", json={
                "task_id": t["id"], "batch_id": t["batch_id"],
                "worker_id": "w-tl_stale", "lease_epoch": t["lease_epoch"],
                "success": False, "error_type": "timeout", "error_detail": "x"})
            r = c.post("/api/tasks/result", json={
                "task_id": t["id"], "batch_id": t["batch_id"],
                "worker_id": "w-tl_stale", "lease_epoch": t["lease_epoch"],
                "success": False, "error_type": "timeout", "error_detail": "x"})
            self.assertTrue(r.json()["stale"], "前置条件不成立：这条没被判 stale")
            _drain(1)
            recs = self._events(c, "tl_stale")

        self.assertEqual([r["outcome"] for r in recs], ["stale"],
                         "租约过期的提交应当是 stale，不是 parse_failed")
        self.assertNotEqual(recs[0]["outcome"], "ok")

    def test_failed_then_success_leaves_both_records(self):
        """终态失败 -> 自动重试 -> 成功：流里**两条**，后一条 cursor 更大。

        这就是消费侧必须"按 (asin, zipcode) 取 cursor 最大那条"的原因。

        重置用 `POST /api/batches/{name}/retry`（人工重试）而不是等
        `auto_retry_failed_tasks` 那个 30 秒一轮 + 冷却下界的循环：两者把
        任务放回 pending 的效果相同，而这里测的是**事件流的形状**，
        不是定时器的节奏。定时器另有用例。
        """
        with _server_with_relay() as (c, _):
            tasks = _push(c, "tl_both", ["B0TLBOTH01"])
            _fail(c, "tl_both", tasks[0])
            _drain(1)

            reset = c.post("/api/batches/tl_both/retry").json()
            self.assertEqual(reset.get("retried"), 1,
                             f"前置条件不成立，任务没被放回 pending: {reset}")

            # ⚠ worker_id 必须与 `_ok` 提交时用的一致（`w-<batch>`）：
            # 租约门比对的是 (task_id, worker_id, lease_epoch)，换个 worker
            # 提交会被判 stale —— 那时事件是 stale 而不是 ok，用例会以
            # 一种看起来像"重试没生效"的方式红掉。
            t2 = c.get("/api/tasks/pull",
                       params={"worker_id": "w-tl_both", "count": 5}
                       ).json()["tasks"][0]
            _ok(c, "tl_both", t2, "42.00")
            _drain(2)
            recs = self._events(c, "tl_both")

        self.assertEqual([r["outcome"] for r in recs], ["parse_failed", "ok"],
                         "应当先一条 parse_failed 再一条 ok")
        self.assertLess(recs[0]["cursor"], recs[1]["cursor"],
                        "后发生的那条 cursor 必须更大")
        self.assertAlmostEqual(recs[1]["fast"]["price"], 42.00, places=2)


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
