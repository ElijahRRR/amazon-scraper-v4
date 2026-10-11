"""F-012 blocker 3：三块改动之间的**接缝**，按用户真正要走的那条路端到端钉住。

blocker 3 分三块各自实现、各自有测试：

* 入口（``tests/test_asin_list_marketplace.py``）：上传 / JSON / 定时任务把站点写进任务行；
* 插件（``tests/test_extension_marketplace.py``）：按页面站点推送，跨站点追加 409；
* 读写路径（``tests/test_results_marketplace.py``）：结果 / 导出 / 详情 / 截图 /
  变体展开按任务行的站点取 ``asin_data``，老 worker 的错站点结果拒收。

每一块的测试都直接造另外两块的输入（例如读写路径的测试用 ``db.create_tasks(...,
marketplace=...)`` 建批次，而不是走 ``/api/upload``）。三块各自全绿，并不能说明
「用户上传一份加拿大清单 -> 采回来 -> 看结果 / 导出」这条路真的通 —— 那正是本文件
要钉的：每条用例都从 HTTP 入口进、从 HTTP 出口读，中间经过真的完成检测协程。

另外钉住最终审查补的几处：
* 「同名 = 合并」的入口（关键词批次、卖家文件上传、插件）共用一条跨站点 409 规则，
  并且已经混了站点的批次也拦得住（不是只看第一条任务）；
* 关键词批次的加拿大默认邮编与其它入口同一条规则（全局值归一成规范形）；
* 用加拿大邮编起的 worker，``prefer_zip`` 不再被美国规则丢掉。

全部走 ``isolated_server``，``DB_BACKEND=sqlite`` 下本机可跑。
"""
from __future__ import annotations

import asyncio
import csv
import io
import types
import unittest

import httpx

import server.app as srv
from tests.golden.harness import isolated_server

US = "amazon.com"
CA = "amazon.ca"

#: 导入时就抓住真的完成检测协程：isolated_server 会把后台循环换成桩
#: （tests/golden/harness.py 的 _PATCHED_LOOPS），用例里要手动跑它一轮。
_REAL_WATCHER = srv._completion_watcher


def _rows(client, sql, params=()):
    async def _go():
        async with srv.db.read() as rc, rc.execute(sql, params) as c:
            return [dict(r) for r in await c.fetchall()]
    return client.portal.call(_go)


def _pull(client, worker_id="w", **params):
    r = client.get("/api/tasks/pull",
                   params={"worker_id": worker_id, "count": 500, **params})
    assert r.status_code == 200, r.text
    return r.json()["tasks"]


def _submit(client, task, worker_id="w", send_marketplace=True, **extra):
    """按**新** worker 的形状提交成功结果：带 marketplace（= 任务的站点）。

    ``send_marketplace=False`` 模拟 F-012 之前的老 worker：提交体里没有这个键。
    """
    body = {"task_id": task["id"], "batch_id": task["batch_id"],
            "worker_id": worker_id, "lease_epoch": task["lease_epoch"],
            "success": True, "asin": task["asin"],
            "title": f"T {task['asin']} {task['marketplace']}", "brand": "B",
            "current_price": "$1.00", "stock_status": "In Stock", **extra}
    if send_marketplace:
        body["marketplace"] = task["marketplace"]
    r = client.post("/api/tasks/result", json=body)
    assert r.status_code == 200, r.text


def _tasks(client, batch_id):
    return _rows(client,
                 "SELECT asin, zip_code, marketplace, status, error_type, error_detail "
                 "FROM tasks WHERE batch_id = ? ORDER BY asin", (batch_id,))


def _batch_id(client, name):
    got = _rows(client, "SELECT id FROM batches WHERE name = ?", (name,))
    return got[0]["id"] if got else None


def _run_watcher_once(client):
    """真的完成检测协程跑一轮（第二次 sleep 时抛出去结束循环）。

    换的是 ``server.app`` 模块上的 ``asyncio`` 引用（一个只把 ``sleep`` 换掉的
    代理），不是 ``asyncio.sleep`` 本身 —— 后者是全进程的，同一个事件循环上别的
    协程一 sleep 就会吃掉这里的计数。与 test_asin_list_marketplace 的
    ``_one_auto_tick`` 同一个做法。
    """
    class _Stop(Exception):
        pass

    calls = {"n": 0}

    async def _sleep(_s):
        calls["n"] += 1
        if calls["n"] > 1:
            raise _Stop()

    async def _go():
        saved = srv.asyncio
        attrs = {k: getattr(saved, k) for k in dir(saved) if not k.startswith("__")}
        attrs["sleep"] = _sleep
        srv.asyncio = types.SimpleNamespace(**attrs)
        try:
            await _REAL_WATCHER()
        except _Stop:
            pass
        finally:
            srv.asyncio = saved
    client.portal.call(_go)


def _items(resp_json):
    return resp_json.get("items") or resp_json.get("results") or []


class CanadaAsinListEndToEndTests(unittest.TestCase):
    """上传加拿大清单（含变体展开）-> worker 采 -> 结果 / 导出，同一 ASIN 另有美国批次。"""

    def test_upload_collect_expand_view_and_export(self):
        with isolated_server() as (client, _ctx):
            # 加拿大上传：站点用别的写法，B 列小写无空格的加拿大邮编，开变体展开
            r = client.post(
                "/api/upload",
                data={"batch_name": "ca_up", "marketplace": "www.amazon.ca",
                      "expand_variants": "true"},
                files={"file": ("a.csv",
                                b"asin,zip\nB0SEED0001,m5v3l9\nB0SEED0002,\n",
                                "text/csv")})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["marketplace"], CA)
            # 同一个 ASIN 的美国批次（老调用方：不带站点）
            r = client.post("/api/batches", json={
                "batch_name": "us_json", "expand_variants": True,
                "items": [{"asin": "B0SEED0001"}]})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertNotIn("marketplace", r.json())
            ca_bid, us_bid = _batch_id(client, "ca_up"), _batch_id(client, "us_json")

            self.assertEqual(
                [(t["asin"], t["marketplace"], t["zip_code"]) for t in _tasks(client, ca_bid)],
                [("B0SEED0001", CA, "M5V 3L9"), ("B0SEED0002", CA, "K1V 7P8")])

            # worker 拿到的任务带着站点与该站点形状的邮编；两站各报一个变体家族
            for t in _pull(client):
                if t["batch_id"] == ca_bid:
                    fam = "B0CASIB001,B0SEED0001" if t["asin"] == "B0SEED0001" else ""
                    _submit(client, t, variation_asins=fam)
                else:
                    self.assertEqual((t["marketplace"], t["zip_code"]), (US, "10001"))
                    _submit(client, t, send_marketplace=False,
                            variation_asins="B0USSIB001,B0SEED0001")

            # 真的完成检测协程触发变体展开
            srv._completion_check_set.update({ca_bid, us_bid})
            _run_watcher_once(client)
            ca_tasks = {t["asin"]: t for t in _tasks(client, ca_bid)}
            us_tasks = {t["asin"]: t for t in _tasks(client, us_bid)}
            self.assertIn("B0CASIB001", ca_tasks, "加拿大批次没有展开加拿大的变体")
            self.assertEqual(ca_tasks["B0CASIB001"]["marketplace"], CA)
            self.assertIn(ca_tasks["B0CASIB001"]["zip_code"], ("M5V 3L9", "K1V 7P8"))
            self.assertNotIn("B0USSIB001", ca_tasks, "加拿大批次吃进了美国的变体家族")
            self.assertEqual(us_tasks["B0USSIB001"]["marketplace"], US)
            self.assertNotIn("B0CASIB001", us_tasks, "美国批次吃进了加拿大的变体家族")

            for t in _pull(client):
                _submit(client, t, send_marketplace=(t["marketplace"] != US))

            # 结果：每个批次只出自己站点的行
            got = client.get("/api/results", params={"batch_id": ca_bid, "per_page": 100})
            self.assertEqual(got.status_code, 200, got.text)
            self.assertEqual(
                sorted((x["asin"], x["marketplace"]) for x in _items(got.json())),
                [("B0CASIB001", CA), ("B0SEED0001", CA), ("B0SEED0002", CA)])
            got = client.get("/api/results", params={"batch_id": us_bid, "per_page": 100})
            self.assertEqual(
                sorted((x["asin"], x["marketplace"]) for x in _items(got.json())),
                [("B0SEED0001", US), ("B0USSIB001", US)])

            # 导出：加拿大批次恰好三行、全是加拿大行
            exp = client.get("/api/export/ca_up",
                             params={"format": "csv", "fields": "asin,marketplace,title"})
            self.assertEqual(exp.status_code, 200, exp.text)
            rows = list(csv.reader(io.StringIO(exp.text.lstrip("﻿"))))
            header, body = rows[0], rows[1:]
            self.assertEqual(len(body), 3, rows)
            site_col = header.index("采集站点") if "采集站点" in header else header.index("marketplace")
            self.assertEqual({r_[site_col] for r_ in body}, {CA})

            # 详情：不带站点只给美国行；带站点给加拿大行；加拿大独有的不带站点 -> 404
            r = client.get("/api/results/B0SEED0001")
            self.assertEqual(r.json()["data"]["marketplace"], US)
            r = client.get("/api/results/B0SEED0001", params={"marketplace": CA})
            self.assertEqual(r.json()["data"]["marketplace"], CA)
            self.assertEqual(client.get("/api/results/B0SEED0002").status_code, 404)

    def test_old_worker_cannot_write_a_canada_task_into_the_us_row(self):
        with isolated_server() as (client, _ctx):
            r = client.post("/api/batches", json={"batch_name": "us", "asins": ["B0SEED0001"]})
            self.assertEqual(r.status_code, 200, r.text)
            for t in _pull(client):
                _submit(client, t, send_marketplace=False, seller_id="A1USSELLER01")
            us_before = _rows(client, "SELECT * FROM asin_data WHERE asin = 'B0SEED0001'")

            r = client.post("/api/batches", json={
                "batch_name": "ca", "asins": ["B0SEED0001"], "marketplace": CA})
            ca_bid = r.json()["batch_id"]
            for t in _pull(client, worker_id="old"):
                # 老 worker：去美国站采、提交体不带 marketplace
                _submit(client, t, worker_id="old", send_marketplace=False,
                        title="old worker US page", seller_id="A1WRONG00001")
            t = _tasks(client, ca_bid)[0]
            self.assertEqual((t["status"], t["error_type"]), ("failed", "server_reject"))
            self.assertTrue(t["error_detail"].startswith("marketplace_mismatch"), t)
            self.assertEqual(_rows(client, "SELECT * FROM asin_data WHERE asin = 'B0SEED0001'"),
                             us_before, "老 worker 的结果写进了美国行")


class SameNameMergeEntriesRejectCrossSiteTests(unittest.TestCase):
    """「同名 = 合并」的入口共用一条规则：同名批次里有别的站点的任务 -> 409，什么都不写。"""

    def test_keyword_batch_cannot_merge_into_another_site(self):
        with isolated_server() as (client, _ctx):
            r = client.post("/api/batches", json={
                "batch_name": "shared", "asins": ["B0SEED0001"], "marketplace": CA})
            self.assertEqual(r.status_code, 200, r.text)
            before = _rows(client, "SELECT COUNT(*) AS n FROM tasks")[0]["n"]
            r = client.post("/api/search-batches", json={
                "batch_name": "shared", "keywords": ["mouse"],
                "domain": "www.amazon.com", "discover_mode": "discover_only"})
            self.assertEqual(r.status_code, 409, r.text)
            self.assertIn("一个批次只能属于一个站点", r.text)
            self.assertEqual(_rows(client, "SELECT COUNT(*) AS n FROM tasks")[0]["n"], before)
            # 同站点同名照旧合并（与改动前一致）
            r = client.post("/api/search-batches", json={
                "batch_name": "shared", "keywords": ["mouse"],
                "domain": "www.amazon.ca", "discover_mode": "discover_only"})
            self.assertEqual(r.status_code, 200, r.text)

    def test_seller_upload_cannot_merge_into_another_site(self):
        with isolated_server() as (client, _ctx):
            r = client.post("/api/extension/collect", json={
                "batch_name": "shop", "seller_ids": ["A1CASHOP00001"], "marketplace": CA})
            self.assertEqual(r.status_code, 200, r.text)
            r = client.post("/api/upload-sellers", data={"batch_name": "shop_sellers"},
                            files={"file": ("s.txt", b"A1USSHOP00001\n", "text/plain")})
            self.assertEqual(r.status_code, 409, r.text)
            # 美国卖家批次之间的同名合并不受影响
            r = client.post("/api/upload-sellers", data={"batch_name": "us_shop"},
                            files={"file": ("s.txt", b"A1USSHOP00001\n", "text/plain")})
            self.assertEqual(r.status_code, 200, r.text)
            r = client.post("/api/upload-sellers", data={"batch_name": "us_shop"},
                            files={"file": ("s.txt", b"A1USSHOP00002\n", "text/plain")})
            self.assertEqual(r.status_code, 200, r.text)

    def test_an_already_mixed_batch_is_still_rejected(self):
        """只看第一条任务的话，一个已经混了站点的批次会因为先读到同站点那条而放行。"""
        with isolated_server() as (client, _ctx):
            r = client.post("/api/extension/collect", json={
                "batch_name": "mixed", "asins": ["B0SEED0001"]})
            self.assertEqual(r.status_code, 200, r.text)
            bid = r.json()["asin_batch"]["batch_id"]

            async def _inject():
                # 造一个混合批次（今天的入口已经造不出来了，老数据 / 并发写才有）
                await srv.db.create_tasks(bid, ["B0SEED0002"], "K1V 7P8", False,
                                          marketplace=CA)
            client.portal.call(_inject)
            for site in (None, CA):
                with self.subTest(marketplace=site):
                    body = {"batch_name": "mixed", "asins": ["B0SEED0003"]}
                    if site:
                        body["marketplace"] = site
                    r = client.post("/api/extension/collect", json=body)
                    self.assertEqual(r.status_code, 409, r.text)


class ConcurrentSameNameDifferentSiteTests(unittest.TestCase):
    """同名、不同站点的两个请求**同时**到达：只能有一个成功，批次不能混站点。

    跨站点检查是先查后写；不把「查 + 写」放进同一把按批次名的锁，两边都会查到
    「没有别的站点的任务」然后都写进去（审查时 15/15 次复现）。
    """

    PAIRS = [
        ("extension CA + keyword US",
         ("/api/extension/collect", {"json": {"asins": ["B0RACE0001"], "marketplace": CA}}),
         ("/api/search-batches", {"json": {"keywords": ["yoga mat"], "domain": "www.amazon.com",
                                           "discover_mode": "discover_only"}})),
        ("upload CA + seller upload US",
         ("/api/upload", {"data": {"marketplace": CA},
                          "files": {"file": ("a.txt", b"B0RACE0002\n", "text/plain")}}),
         ("/api/upload-sellers", {"files": {"file": ("s.txt", b"A1RACESHOP001\n", "text/plain")}})),
        ("upload CA + keyword US",
         ("/api/upload", {"data": {"marketplace": CA},
                          "files": {"file": ("a.txt", b"B0RACE0003\n", "text/plain")}}),
         ("/api/search-batches", {"json": {"keywords": ["desk"], "domain": "www.amazon.com",
                                           "discover_mode": "discover_only"}})),
    ]

    def _fire(self, client, name, a, b):
        def with_name(spec):
            path, kw = spec
            kw = {k: (dict(v) if isinstance(v, dict) else v) for k, v in kw.items()}
            if "json" in kw:
                kw["json"]["batch_name"] = name
            else:
                kw.setdefault("data", {})
                kw["data"] = {**kw["data"], "batch_name": name}
            return path, kw

        async def _go():
            transport = httpx.ASGITransport(app=srv.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
                (pa, ka), (pb, kb) = with_name(a), with_name(b)
                return await asyncio.gather(ac.post(pa, **ka), ac.post(pb, **kb))
        return client.portal.call(_go)

    def test_only_one_of_two_concurrent_cross_site_requests_wins(self):
        with isolated_server() as (client, _ctx):
            for i, (label, a, b) in enumerate(self.PAIRS):
                for run in range(3):
                    name = f"race_{i}_{run}"
                    with self.subTest(pair=label, run=run):
                        r1, r2 = self._fire(client, name, a, b)
                        self.assertEqual(sorted((r1.status_code, r2.status_code)), [200, 409],
                                         (r1.text[:200], r2.text[:200]))
                        sites = {row["marketplace"] for row in _rows(
                            client, "SELECT t.marketplace FROM tasks t "
                                    "JOIN batches b ON b.id = t.batch_id WHERE b.name = ?",
                            (name,))}
                        self.assertEqual(len(sites), 1, f"批次 {name} 混了站点: {sites}")
            # 锁表用完即删，不随批次名增长
            self.assertEqual(srv._batch_name_locks, {})


class KeywordBatchNameTypeTests(unittest.TestCase):
    """关键词批次的 batch_name 可以是 JSON 数字：库层会把它存成文本，跨站点检查也
    必须按同一规则绑定（PG 上把 int 绑到 text 列是 DataError -> 500）。本机只有
    SQLite 能跑，PG 的那一半由 CI 的 PG 任务覆盖。"""

    def test_numeric_batch_name_still_creates_the_batch(self):
        with isolated_server() as (client, _ctx):
            for raw in (20261010, 1.5):
                with self.subTest(batch_name=raw):
                    r = client.post("/api/search-batches", json={
                        "batch_name": raw, "keywords": ["mouse"],
                        "domain": "www.amazon.com", "discover_mode": "discover_only"})
                    self.assertEqual(r.status_code, 200, r.text)


class KeywordBatchCanadaDefaultZipTests(unittest.TestCase):
    """关键词批次的加拿大默认邮编与上传 / JSON / 插件 / 定时任务同一条规则。"""

    def test_global_ca_zip_is_normalized_like_every_other_entry(self):
        with isolated_server() as (client, _ctx):
            srv._runtime_settings["zip_code"] = "k1v7p8"
            r = client.post("/api/search-batches", json={
                "batch_name": "kw_ca", "keywords": ["mouse"],
                "domain": "www.amazon.ca", "discover_mode": "discover_only"})
            self.assertEqual(r.status_code, 200, r.text)
            r = client.post("/api/batches", json={
                "batch_name": "json_ca", "asins": ["B0SEED0001"], "marketplace": CA})
            self.assertEqual(r.status_code, 200, r.text)
            zips = {row["name"]: row["zip_code"] for row in _rows(
                client, "SELECT b.name, t.zip_code FROM tasks t "
                        "JOIN batches b ON b.id = t.batch_id")}
            self.assertEqual(zips, {"kw_ca": "K1V 7P8", "json_ca": "K1V 7P8"})

    def test_us_keyword_default_zip_is_unchanged(self):
        with isolated_server() as (client, _ctx):
            srv._runtime_settings["zip_code"] = "90210"
            r = client.post("/api/search-batches", json={
                "batch_name": "kw_us", "keywords": ["mouse"],
                "domain": "www.amazon.com", "discover_mode": "discover_only"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(_rows(client, "SELECT zip_code FROM tasks")[0]["zip_code"], "90210")


class CanadaPreferZipTests(unittest.TestCase):
    """``--zip-code 'K1V 7P8'`` 起的 worker：prefer_zip 不再被美国规则丢掉。"""

    def test_canadian_prefer_zip_pulls_canadian_tasks_first(self):
        with isolated_server() as (client, _ctx):
            us = [f"B0USPZ{i:04d}" for i in range(5)]
            ca = [f"B0CAPZ{i:04d}" for i in range(5)]
            client.post("/api/batches", json={"batch_name": "us", "asins": us})
            client.post("/api/batches", json={"batch_name": "ca", "asins": ca,
                                              "marketplace": CA})
            for raw in ("K1V 7P8", "k1v7p8"):
                with self.subTest(prefer_zip=raw):
                    got = _pull(client, worker_id=f"w_{raw}", count=1, prefer_zip=raw)
                    self.assertEqual(len(got), 1)
                    self.assertEqual(got[0]["marketplace"], CA)

    def test_us_prefer_zip_is_unchanged(self):
        with isolated_server() as (client, _ctx):
            client.post("/api/batches", json={"batch_name": "ca",
                                              "asins": ["B0CAPZ0001"], "marketplace": CA})
            client.post("/api/batches", json={"batch_name": "us",
                                              "asins": ["B0USPZ0001"]})
            got = _pull(client, count=1, prefer_zip="10001")
            self.assertEqual(got[0]["marketplace"], US)


class TasksPageGuardsTests(unittest.TestCase):
    """网页表单与失败明细里本轮补的两处提示（模板层的静态钉子）。"""

    def test_upload_form_warns_when_a_letter_postal_code_is_sent_to_the_us_site(self):
        with isolated_server() as (client, _ctx):
            html = client.get("/tasks").text
        self.assertIn("不是美国邮编，但站点选的是美国站", html)
        self.assertIn("/[A-Za-z]/.test(upZipVal)", html)

    def test_failure_modal_explains_marketplace_mismatch_and_ca_zip_switch(self):
        with isolated_server() as (client, _ctx):
            html = client.get("/tasks").text
        self.assertIn("/^marketplace_mismatch task=(\\S+) payload=(\\S+)/", html)
        self.assertIn("需要升级到多站点版本", html)
        self.assertIn("/^current=(.*?)\\s+target=(.+)$/", html)


if __name__ == "__main__":
    unittest.main()
