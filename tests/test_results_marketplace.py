"""F-012 blocker 3（C 组）：批次读写路径按**任务行的站点**钉住 asin_data。

------------------------------------------------------------------------
它补的洞
------------------------------------------------------------------------
asin_data 的唯一键是 ``(asin, marketplace)``：同一个 ASIN 在美国站和加拿大站
各一行。而批次与 asin_data 之间原来**只按 asin 关联**（batch_asins 没有站点列，
站点只记在 tasks 行上）。于是只要库里有一行加拿大数据：

  * 加拿大批次的结果 / 导出把同一 ASIN 的**美国行**也带出来，total 翻倍；
    没在加拿大采到的 ASIN 被导出成「历史产品库数据」（其实是美国的）；
  * **美国批次**的结果 / 导出也多出加拿大行 —— 美国站不变式当场破掉；
    导出的 keyset 是 ``ba.asin > ?``，页边界落在两行之间时第二行被静默跳过，
    导出内容随 batch_size 变；
  * ``GET /api/results/{asin}`` 不传站点时拿到的是**加拿大行**（索引序）；
  * 美国批次的截图落地会盖掉加拿大行的 screenshot_path（反之亦然），
    存结果时与读结果时的「不限批次」截图兜底也跨站点；
  * 加拿大批次的变体展开吃进美国家族，还把兄弟款建成 (amazon.com, 加拿大邮编)；
  * F-012 之前的 worker 不认识 tasks.marketplace，拉到加拿大任务照样去 amazon.com
    采、提交体不带站点 —— 服务端按「缺键 = 美国站」写进去，**覆盖真的美国行**；
  * 关键词批次对注册表之外的站点放行 discover_only，库层却又按 domain 推导站点，
    请求在库层炸成 500。

------------------------------------------------------------------------
怎么测
------------------------------------------------------------------------
``unittest.TestCase`` + ``tests.golden.harness.isolated_server``（跟着 DB_BACKEND
走，本机用 ``DB_BACKEND=sqlite``，CI 两个后端都跑）。加拿大批次直接调库建
（``client.portal.call`` 跑在 TestClient 自己的事件循环上，与 srv.db 同一个循环），
不依赖 ASIN 清单入口的站点参数 —— 那是另一组的改动，这里只测读写路径。
asin_data 行的站点来自**提交体**的 ``marketplace``（老 worker 不带 = 美国站）。

除两条美国站护栏（``test_matching_submissions_are_accepted_as_before`` 与
``test_registry_domains_keep_their_site``：改动前后都必须绿）之外，每条用例都在
改动前的代码上跑过、确认是红的（见提交说明）。
"""
from __future__ import annotations

import csv
import io
import unittest

import server.app as srv
from tests.golden.harness import isolated_server

US = "amazon.com"
CA = "amazon.ca"
CA_ZIP = "M5V 3L9"

X = "B0DUPE0001"     # 两个站点都采过
Y = "B0USONLY02"     # 两个批次都有，但只在美国站采到
Z = "B0CAONLY03"     # 只在加拿大批次

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000b49444154789c6360000200000500017a5eab3f0000"
    "000049454e44ae426082"
)


# ============================================================ 工具

def _mk_batch(client, name, asins, marketplace=None, zip_code="10001",
              needs_screenshot=False, expand_variants=False):
    """直接调库建批次 + 任务（站点由 create_tasks 的 marketplace 决定）。"""
    async def _go():
        bid = await srv.db.create_batch(name, needs_screenshot,
                                        expand_variants=expand_variants)
        await srv.db.create_tasks(bid, list(asins), zip_code, needs_screenshot,
                                  marketplace=marketplace)
        return bid
    return client.portal.call(_go)


def _pull(client, worker_id):
    """拉走当前全部可拉的任务。{asin: task}。"""
    r = client.get("/api/tasks/pull", params={"worker_id": worker_id, "count": 200,
                                              "enable_screenshot": "true"})
    assert r.status_code == 200, r.text
    return {t["asin"]: t for t in r.json()["tasks"]}


def _body(task, worker_id, price, marketplace="<omit>", **extra):
    body = {"task_id": task["id"], "batch_id": task["batch_id"],
            "worker_id": worker_id, "lease_epoch": task["lease_epoch"],
            "success": True, "asin": task["asin"], "title": f"P {task['asin']}",
            "brand": "Brand", "current_price": price, "stock_status": "In Stock",
            "crawl_time": "2026-10-01T10:00:00Z", "site": "US",
            "zip_code": task["zip_code"], **extra}
    if marketplace != "<omit>":
        body["marketplace"] = marketplace
    return body


def _submit(client, task, worker_id, price, marketplace="<omit>", **extra):
    r = client.post("/api/tasks/result",
                    json=_body(task, worker_id, price, marketplace, **extra))
    assert r.status_code == 200, r.text
    return r.json()


def _rows(client, sql, params=()):
    async def _go():
        async with srv.db.read() as rc, rc.execute(sql, params) as c:
            return [dict(r) for r in await c.fetchall()]
    return client.portal.call(_go)


def _iter(client, bid, batch_size):
    async def _go():
        return [dict(r) async for r in srv.db.iter_results(bid, batch_size=batch_size)]
    return client.portal.call(_go)


def _csv(client, batch_name, fields="asin,marketplace,current_price"):
    r = client.get(f"/api/export/{batch_name}",
                   params={"format": "csv", "fields": fields})
    assert r.status_code == 200, r.text
    return r.text


def _csv_rows(text):
    return list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))


def _results(client, **params):
    r = client.get("/api/results", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _seed_us_batch(client, needs_screenshot=False):
    """美国批次 us_b = [X, Y]，两个都按老 worker 的样子提交（不带 marketplace）。"""
    bid = _mk_batch(client, "us_b", [X, Y], needs_screenshot=needs_screenshot)
    tasks = _pull(client, "w-us")
    _submit(client, tasks[X], "w-us", "$10.00", variation_asins=f"{X},B0USFAM001")
    _submit(client, tasks[Y], "w-us", "$20.00")
    return bid


def _seed_ca_batch(client, needs_screenshot=False):
    """加拿大批次 ca_b = [X, Y, Z]：X、Z 在加拿大采到；Y 拉走了但没交（处理中）。"""
    bid = _mk_batch(client, "ca_b", [X, Y, Z], marketplace=CA, zip_code=CA_ZIP,
                    needs_screenshot=needs_screenshot)
    tasks = _pull(client, "w-ca")
    assert {t["marketplace"] for t in tasks.values()} == {CA}
    _submit(client, tasks[X], "w-ca", "$13.50", marketplace=CA)
    _submit(client, tasks[Z], "w-ca", "$30.00", marketplace=CA)
    return bid


def _key(items):
    return sorted((i["asin"], i["marketplace"]) for i in items)


# ============================================================ 结果列表 / 导出

class BatchResultsArePinnedToTheTaskSiteTests(unittest.TestCase):
    """C1 / C2：``/api/results?batch_id=`` 与批次导出只出本批任务那个站点的行。"""

    def test_ca_batch_results_only_ca_rows(self):
        with isolated_server() as (client, _ctx):
            _seed_us_batch(client)
            ca = _seed_ca_batch(client)
            for extra in ({}, {"change_filter": "new"},
                          {"search": "B0"},            # < 3 字符：LIKE 路径
                          {"search": "B0DUPE,B0CAONLY"}):   # >= 3 字符：FTS / bset 路径
                with self.subTest(**extra):
                    got = _results(client, batch_id=ca, **extra)
                    self.assertEqual(_key(got["items"]), sorted([(X, CA), (Z, CA)]), got["items"])
                    self.assertEqual(got["total"], 2)
                    # 每一行都挂着本批（加拿大）任务的状态，没有被借走的美国行
                    self.assertEqual({i["batch_task_status"] for i in got["items"]},
                                     {"done"})

    def test_us_batch_results_and_exports_do_not_change_when_ca_data_arrives(self):
        """美国批次的 JSON / CSV / iter_results 在加拿大数据入库前后**逐字相同**。"""
        with isolated_server() as (client, _ctx):
            us = _seed_us_batch(client)

            def snapshot():
                return {
                    "results": _results(client, batch_id=us),
                    "results_narrow": _results(client, batch_id=us,
                                               fields="asin,current_price"),
                    "results_new": _results(client, batch_id=us, change_filter="new"),
                    "results_search": _results(client, batch_id=us, search="B0DUPE"),
                    "csv": _csv(client, "us_b"),
                    "csv_all_fields": client.get("/api/export/us_b",
                                                 params={"format": "csv"}).text,
                    "iter_1": _iter(client, us, 1),
                    "iter_500": _iter(client, us, 500),
                }

            before = snapshot()
            _seed_ca_batch(client)
            after = snapshot()
            for k in before:
                with self.subTest(view=k):
                    self.assertEqual(after[k], before[k])
            self.assertEqual(_key(after["results"]["items"]), [(X, US), (Y, US)])

    def test_ca_batch_export_has_exactly_one_row_per_batch_asin(self):
        with isolated_server() as (client, _ctx):
            _seed_us_batch(client)
            _seed_ca_batch(client)
            rows = _csv_rows(_csv(client, "ca_b",
                                  fields="asin,marketplace,current_price"))
            got = [(r["ASIN (商品ID)"], r["采集站点"], r["当前价格"], r["数据来源"])
                   for r in rows]
            # 行序 = ba.asin 升序（导出的 keyset），即 Z < X < Y
            self.assertEqual(got, [
                (Z, CA, "$30.00", "本次采集更新"),
                (X, CA, "$13.50", "本次采集更新"),
                # Y 在加拿大没采到：不能拿美国那行冒充「历史产品库数据」
                (Y, "", "", "无产品库数据，本次未完成"),
            ])

    def test_iter_results_keyset_is_exact_for_any_batch_size(self):
        with isolated_server() as (client, _ctx):
            us = _seed_us_batch(client)
            ca = _seed_ca_batch(client)
            for bid, want in ((us, [(X, US), (Y, US)]),
                              (ca, [(Z, CA), (X, CA), (Y, None)])):   # ba.asin 升序
                for bs in (1, 2, 500):
                    with self.subTest(batch=bid, batch_size=bs):
                        got = [(r["asin"], r["marketplace"]) for r in _iter(client, bid, bs)]
                        self.assertEqual(got, want)

    def test_batch_without_task_rows_falls_back_to_us(self):
        """batch_asins 有行、tasks 没有（F-012 之前的老数据）-> 按美国站，与改动前一致。"""
        with isolated_server() as (client, _ctx):
            us = _seed_us_batch(client)
            _seed_ca_batch(client)

            async def _drop_task_rows():
                async with srv.db._write_lock:
                    await srv.db._db.execute("BEGIN")
                    await srv.db._db.execute("DELETE FROM tasks WHERE batch_id = ?", (us,))
                    await srv.db._db.execute("COMMIT")
            client.portal.call(_drop_task_rows)
            self.assertEqual(_key(_results(client, batch_id=us)["items"]),
                             [(X, US), (Y, US)])
            self.assertEqual([(r["asin"], r["marketplace"]) for r in _iter(client, us, 1)],
                             [(X, US), (Y, US)])


# ============================================================ 单 ASIN 详情

class ResultDetailTests(unittest.TestCase):
    """C3：``GET /api/results/{asin}`` 不传站点 -> 优先美国行；传了 -> 精确匹配。"""

    def test_detail_prefers_us_row_and_honours_marketplace(self):
        with isolated_server() as (client, _ctx):
            _seed_us_batch(client)
            _seed_ca_batch(client)

            def detail(asin, **params):
                return client.get(f"/api/results/{asin}", params=params)

            r = detail(X)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual((r.json()["data"]["marketplace"],
                              r.json()["data"]["current_price"]), (US, "$10.00"))
            for spelling in (CA, "AMAZON.CA", "www.amazon.ca", "https://www.amazon.ca/"):
                with self.subTest(marketplace=spelling):
                    r = detail(X, marketplace=spelling)
                    self.assertEqual(r.status_code, 200, r.text)
                    self.assertEqual(r.json()["data"]["marketplace"], CA)
                    self.assertEqual(r.json()["data"]["current_price"], "$13.50")
            # 只在加拿大才有的 ASIN：不传站点仍然 200（不是 404）
            r = detail(Z)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["data"]["marketplace"], CA)
            # 空串 = 没传
            self.assertEqual(detail(X, marketplace="").json()["data"]["marketplace"], US)
            # 指定站点没有那一行 -> 404
            self.assertEqual(detail(Y, marketplace=CA).status_code, 404)
            self.assertEqual(detail(Z, marketplace=US).status_code, 404)
            # 不认识的站点 -> 422 invalid_parameter，不静默回退美国站
            r = detail(X, marketplace="amazon.xx")
            self.assertEqual(r.status_code, 422, r.text)
            self.assertEqual(r.json()["detail"]["parameter"], "marketplace")
            # 不传站点的 404 消息逐字照旧
            r = detail("B0NOSUCH00")
            self.assertEqual((r.status_code, r.json()["detail"]),
                             (404, "ASIN B0NOSUCH00 不存在"))


# ============================================================ 截图

class ScreenshotsDoNotCrossSitesTests(unittest.TestCase):
    """C4：截图落地 / 存结果时的回填 / 读结果时的兜底，都按站点限定。"""

    def _shot(self, client, batch_name, asin, worker_id):
        r = client.post("/api/tasks/screenshot",
                        files={"file": (f"{asin}.png", _PNG, "image/png")},
                        data={"asin": asin, "batch_name": batch_name,
                              "worker_id": worker_id})
        self.assertEqual(r.status_code, 200, r.text)

    def _stored(self, client, asin, mkt):
        rows = _rows(client, "SELECT screenshot_path FROM asin_data "
                             "WHERE asin = ? AND marketplace = ?", (asin, mkt))
        return rows[0]["screenshot_path"] if rows else "<no row>"

    def _shown(self, client, asin, mkt):
        return client.get(f"/api/results/{asin}",
                          params={"marketplace": mkt}).json()["data"]["screenshot_path"]

    def test_upload_updates_only_the_batch_site_row(self):
        with isolated_server() as (client, _ctx):
            _seed_us_batch(client, needs_screenshot=True)
            _seed_ca_batch(client, needs_screenshot=True)
            us_path = f"/static/screenshots/us_b/{X}.png"
            ca_path = f"/static/screenshots/ca_b/{X}.png"

            self._shot(client, "us_b", X, "w-us")
            self.assertEqual(self._stored(client, X, US), us_path)
            self.assertIsNone(self._stored(client, X, CA))
            self.assertIsNone(self._shown(client, X, CA),
                              "加拿大行借到了美国批次的截图")

            self._shot(client, "ca_b", X, "w-ca")
            self.assertEqual(self._stored(client, X, CA), ca_path)
            self.assertEqual(self._stored(client, X, US), us_path,
                             "加拿大截图盖掉了美国行")

            # 全局视图（默认投影带 marketplace）里两行各是各的
            items = [i for i in _results(client)["items"] if i["asin"] == X]
            self.assertEqual(sorted((i["marketplace"], i["screenshot_path"]) for i in items),
                             [(CA, ca_path), (US, us_path)])

    def test_save_and_read_fallback_do_not_borrow_the_other_sites_screenshot(self):
        with isolated_server() as (client, _ctx):
            _seed_us_batch(client, needs_screenshot=True)
            self._shot(client, "us_b", X, "w-us")
            # 美国截图已经 done 之后才存加拿大结果：存的时候不能回填美国截图
            ca = _seed_ca_batch(client, needs_screenshot=True)
            self.assertIsNone(self._stored(client, X, CA))
            # 读的时候（截图表兜底）也不能借：详情、批次视图、全局视图都一样
            self.assertIsNone(self._shown(client, X, CA))
            for items in (_results(client, batch_id=ca)["items"],
                          _results(client)["items"]):
                ca_paths = [i["screenshot_path"] for i in items if i["marketplace"] == CA]
                self.assertEqual(ca_paths, [None, None], items)
            # 美国行不受影响
            self.assertEqual(self._shown(client, X, US),
                             f"/static/screenshots/us_b/{X}.png")


# ============================================================ 变体展开

class VariantExpansionStaysOnTheBatchSiteTests(unittest.TestCase):
    """C5：邮编与站点取自同一条任务行；家族只读同站点；新任务带同一个站点。"""

    def _tasks(self, client, bid):
        return _rows(client, "SELECT asin, zip_code, marketplace FROM tasks "
                             "WHERE batch_id = ? ORDER BY id", (bid,))

    def _expand(self, client, bid):
        return client.portal.call(srv.db.expand_batch_variants, bid)

    def test_ca_batch_expands_only_the_ca_family(self):
        with isolated_server() as (client, _ctx):
            _seed_us_batch(client)                     # 美国 X 的家族：B0USFAM001
            ca = _mk_batch(client, "ca_fam", [X], marketplace=CA, zip_code=CA_ZIP,
                           expand_variants=True)
            t = _pull(client, "w-ca")[X]
            _submit(client, t, "w-ca", "$13.50", marketplace=CA,
                    variation_asins=f"{X},B0CAFAM001")
            self.assertEqual(self._expand(client, ca), 1)
            self.assertEqual(self._tasks(client, ca), [
                {"asin": X, "zip_code": CA_ZIP, "marketplace": CA},
                {"asin": "B0CAFAM001", "zip_code": CA_ZIP, "marketplace": CA},
            ])
            self.assertEqual(self._expand(client, ca), 0)      # 收敛
            # 兄弟款在加拿大站是新的（is_new 按站点判）
            self.assertEqual(_rows(client, "SELECT is_new FROM batch_asins "
                                           "WHERE batch_id = ? AND asin = ?",
                                   (ca, "B0CAFAM001")), [{"is_new": 1}])

    def test_ca_row_without_family_does_not_borrow_the_us_family(self):
        with isolated_server() as (client, _ctx):
            _seed_us_batch(client)
            ca = _mk_batch(client, "ca_fam", [X], marketplace=CA, zip_code=CA_ZIP,
                           expand_variants=True)
            _submit(client, _pull(client, "w-ca")[X], "w-ca", "$13.50", marketplace=CA)
            self.assertEqual(self._expand(client, ca), 0)

    def test_us_batch_ignores_the_ca_family_and_is_otherwise_unchanged(self):
        with isolated_server() as (client, _ctx):
            ca = _mk_batch(client, "ca_seed", [X], marketplace=CA, zip_code=CA_ZIP)
            _submit(client, _pull(client, "w-ca")[X], "w-ca", "$13.50", marketplace=CA,
                    variation_asins=f"{X},B0CAFAM001")
            us = _mk_batch(client, "us_fam", [X], zip_code="90001", expand_variants=True)
            _submit(client, _pull(client, "w-us")[X], "w-us", "$10.00",
                    variation_asins=f"{X},B0USFAM001")
            self.assertEqual(self._expand(client, us), 1)
            self.assertEqual(self._tasks(client, us), [
                {"asin": X, "zip_code": "90001", "marketplace": US},
                {"asin": "B0USFAM001", "zip_code": "90001", "marketplace": US},
            ])
            self.assertEqual(self._tasks(client, ca),
                             [{"asin": X, "zip_code": CA_ZIP, "marketplace": CA}])


# ============================================================ 老 worker 防线

class LegacyWorkerCannotWriteACaTaskIntoTheUsRowTests(unittest.TestCase):
    """C6：提交体站点 ≠ 任务行站点 -> 不写 asin_data，任务 server_reject。"""

    def _setup(self, client):
        _seed_us_batch(client)
        bid = _mk_batch(client, "ca_b", [X], marketplace=CA, zip_code=CA_ZIP)
        return bid, _pull(client, "w-old")[X]

    def _task(self, client, task_id):
        return _rows(client, "SELECT status, error_type, error_detail FROM tasks "
                             "WHERE id = ?", (task_id,))[0]

    def _data_rows(self, client):
        return _rows(client, "SELECT marketplace, current_price FROM asin_data "
                             "WHERE asin = ? ORDER BY marketplace", (X,))

    def test_single_submit_without_marketplace_is_rejected(self):
        for payload_mkt, label in (("<omit>", US), (US, US), ("www.amazon.com", US)):
            with self.subTest(payload=payload_mkt), isolated_server() as (client, _ctx):
                _bid, t = self._setup(client)
                before = self._data_rows(client)
                r = _submit(client, t, "w-old", "$99.99", marketplace=payload_mkt)
                self.assertEqual(r, {"ok": True, "stale": False})
                self.assertEqual(self._data_rows(client), before,
                                 "加拿大任务的结果写进了美国行")
                self.assertEqual(self._task(client, t["id"]), {
                    "status": "failed", "error_type": "server_reject",
                    "error_detail": f"marketplace_mismatch task={CA} payload={label}"})

    def test_batch_submit_without_marketplace_is_rejected(self):
        with isolated_server() as (client, _ctx):
            _bid, t = self._setup(client)
            before = self._data_rows(client)
            body = _body(t, "w-old", "$99.99")
            r = client.post("/api/tasks/result/batch", json={"results": [body]})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json(), {"accepted": 0, "stale": 0, "failed": 1, "total": 1})
            self.assertEqual(self._data_rows(client), before)
            self.assertEqual(self._task(client, t["id"])["error_detail"],
                             f"marketplace_mismatch task={CA} payload={US}")

    def test_matching_submissions_are_accepted_as_before(self):
        with isolated_server() as (client, _ctx):
            _bid, t = self._setup(client)
            # 新 worker 交了正确的站点：照常写进加拿大行
            self.assertEqual(_submit(client, t, "w-old", "$13.50", marketplace=CA),
                             {"ok": True, "stale": False})
            self.assertEqual(self._data_rows(client), [
                {"marketplace": CA, "current_price": "$13.50"},
                {"marketplace": US, "current_price": "$10.00"}])
            self.assertEqual(self._task(client, t["id"])["status"], "done")
            # 美国任务 + 老 worker（不带站点）：与改动前一样被接受
            _mk_batch(client, "us_again", [X])
            t2 = _pull(client, "w-old2")[X]
            r = client.post("/api/tasks/result/batch",
                            json={"results": [_body(t2, "w-old2", "$11.00")]})
            self.assertEqual(r.json(), {"accepted": 1, "stale": 0, "failed": 0, "total": 1})
            self.assertEqual(self._data_rows(client)[1],
                             {"marketplace": US, "current_price": "$11.00"})


# ============================================================ 关键词批次

class SearchBatchSiteComesFromTheEndpointTests(unittest.TestCase):
    """C7：站点由 HTTP 边界解析一次，库层不再按 domain 重新推导。"""

    def _create(self, client, **body):
        return client.post("/api/search-batches", json={"keywords": ["usb hub"], **body})

    def _task_site(self, client, batch_id):
        return _rows(client, "SELECT marketplace FROM tasks WHERE batch_id = ?",
                     (batch_id,))

    def test_discover_only_on_a_non_registry_domain_is_not_a_500(self):
        with isolated_server() as (client, _ctx):
            r = self._create(client, domain="www.amazon.de", discover_mode="discover_only",
                             batch_name="kw_de")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(self._task_site(client, r.json()["batch_id"]),
                             [{"marketplace": US}])

    def test_registry_domains_keep_their_site(self):
        with isolated_server() as (client, _ctx):
            r = self._create(client, domain="www.amazon.ca", batch_name="kw_ca")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(self._task_site(client, r.json()["batch_id"]),
                             [{"marketplace": CA}])
            r = self._create(client, batch_name="kw_us")
            self.assertEqual(self._task_site(client, r.json()["batch_id"]),
                             [{"marketplace": US}])


if __name__ == "__main__":
    unittest.main()
