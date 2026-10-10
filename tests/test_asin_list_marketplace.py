"""F-012 blocker 3 —— ASIN 清单入口按站点建批次（上传 / JSON 推送 / 定时任务 / 网页表单）。

------------------------------------------------------------------------
钉的是什么
------------------------------------------------------------------------
用户的目标是「上传一份加拿大 ASIN 清单 -> 按 amazon.ca 采集」。在这之前，
所有 ASIN 清单入口都**没有站点这个参数**：加拿大清单照样 200，任务全部建成
``amazon.com`` + 全局美国邮编 —— 采回来的是美国站的数据，批次名、进度、
完成回调全都正常，两侧都不会响。

本文件按入口逐条钉：

* ``POST /api/upload``（xlsx / csv / txt）与 ``POST /api/batches``（JSON）：
  ``marketplace`` 决定任务行的站点；邮编（B 列 / items / 批次级）按该站点的
  形状校验与归一；非美国站没给邮编时用该站点默认（全局设置是美国邮编时
  加拿大站用 ``K1V 7P8``）；不在注册表里的站点 400 且**不留下批次**。
* 定时任务：创建（只在给了时才存站点 / 邮编；写文件之前校验）、立即执行、
  自动调度三条路径；「全库」= 该站点的全库；存储值每次重新校验，自动调度里
  一条坏的不能拖垮同一轮后面的。
* 网页表单：站点下拉由服务端从注册表渲染，切站点时邮编框的处理。

还有一组 :class:`UsInvariantTests` —— **不传站点时美国站行为逐字节不变**。
它们在改造前后都是绿的（这正是它们要证明的），有效性靠变异验证：把批次级
邮编校验放宽到美国站、或把美国站报错原文改掉，它们当场红。

------------------------------------------------------------------------
怎么读结果
------------------------------------------------------------------------
一律走 ``GET /api/tasks/pull`` 读 ``(zip_code, marketplace)``：那是采集侧
真正会看到的值，而且不碰 PG 连接池的事件循环（同 ``test_json_submit_endpoint``
的 ``_task_zips``）。⚠ pull 会上租约，**一次拉完再查**。

写成 ``unittest.TestCase``：``unittest discover`` 只认 TestCase 子类。
跟着 ``DB_BACKEND`` 走 ``isolated_server``，两个后端都盖得到。
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import types
import unittest

import openpyxl

import server.app as srv
from tests.golden.harness import isolated_server

#: ``isolated_server()`` 会把它换成 no-op（``harness._PATCHED_LOOPS``），
#: 所以在导入期（还没有任何夹具生效时）先抓住真身。
_REAL_AUTO_SCHEDULER = srv._auto_scrape_scheduler

US = "amazon.com"
CA = "amazon.ca"
#: 加拿大站注册表默认邮编（``marketplace.get('amazon.ca').default_postal``）。
CA_DEFAULT = "K1V 7P8"
#: 夹具钉住的全局默认邮编（``harness._ENV_DERIVED_SETTINGS``）。
US_GLOBAL = "10001"

#: 改造前 ``/api/upload`` 200 响应的键集合（黄金 upload_batch_a 与 erpAPI 契约钉着）。
_LEGACY_BATCH_KEYS = {
    "batch_id", "batch_name", "external_id", "total_asins", "inserted",
    "per_asin_zip_count", "invalid_zip_rows", "callback_url", "status_url",
}
#: 改造前 ``POST /api/schedules`` 存下来的定时任务 dict 的键集合。
_LEGACY_SCHED_KEYS = {
    "id", "name", "time", "interval_days", "source_file", "asin_count",
    "needs_screenshot", "enabled", "last_run_date", "created_at",
}

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: ``<prefix>_YYYYMMDD_HHMMSS``（``server.app._batch_name``）。
_TS_SUFFIX = re.compile(r"_\d{8}_\d{6}$")


# ---------------------------------------------------------------- helpers

def _pulled(client, worker="mkt-probe"):
    """一次拉完全部待采任务：``{(batch_name, asin): (zip_code, marketplace)}``。"""
    r = client.get("/api/tasks/pull", params={"worker_id": worker, "count": 500})
    assert r.status_code == 200, r.text
    return {(t["batch_name"], t["asin"]): (t["zip_code"], t["marketplace"])
            for t in r.json()["tasks"]}


def _upload(client, name, body=b"B0MKTUP001\n", fname="a.txt", ctype="text/plain", **form):
    data = {k: str(v) for k, v in form.items()}
    if name is not None:
        data["batch_name"] = name
    return client.post("/api/upload", files={"file": (fname, body, ctype)}, data=data)


def _xlsx(rows) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


def _batch_names(client):
    r = client.get("/api/batches")
    assert r.status_code == 200, r.text
    return {b["name"] for b in r.json()["batches"]}


def _global_zip(client):
    return client.get("/api/settings").json()["settings"]["zip_code"]


def _seed_library(client, us=(), ca=()):
    """往 ``asin_data`` 里放美国站 / 加拿大站的结果行（定时任务「全库」读的就是它）。

    种子任务**不走**本组改的入口，直接调 ``db.create_tasks(marketplace=...)``：
    种子在改造前后必须一模一样，否则「基线上红」红在种子上而不是被测行为上。
    结果行的站点跟着任务行走（提交时带上任务的 marketplace）。
    """
    async def _mk(name, asins, mkt, zc):
        bid = await srv.db.create_batch(name, False)
        await srv.db.create_tasks(bid, list(asins), zc, False, marketplace=mkt)

    if us:
        client.portal.call(_mk, "seed_us", us, US, US_GLOBAL)
    if ca:
        client.portal.call(_mk, "seed_ca", ca, CA, CA_DEFAULT)
    tasks = client.get("/api/tasks/pull",
                       params={"worker_id": "seed", "count": 500}).json()["tasks"]
    assert len(tasks) == len(us) + len(ca), tasks
    for t in tasks:
        r = client.post("/api/tasks/result", json={
            "task_id": t["id"], "batch_id": t["batch_id"], "worker_id": "seed",
            "lease_epoch": t["lease_epoch"], "success": True, "asin": t["asin"],
            "title": f"seed {t['asin']}", "current_price": "$1.00",
            "marketplace": t["marketplace"],
        })
        assert r.status_code == 200, r.text


def _mk_sched(client, **form):
    form = {"time": "00:00", "interval_days": "1", "name": "",
            "needs_screenshot": "false", **form}
    files = form.pop("_files", None)
    return client.post("/api/schedules", data=form, files=files)


def _stored_schedules():
    return srv._runtime_settings["auto_scrape_schedules"]


class _Stop(Exception):
    """让自动调度跑完一轮就停。不能是 BaseException：那会掀翻 anyio portal。"""


def _one_auto_tick(client):
    """在 TestClient 的事件循环上把**真的** ``_auto_scrape_scheduler`` 跑一轮。

    第 1 次 ``asyncio.sleep(60)`` 立即返回、循环体跑一遍，第 2 次抛 ``_Stop``
    跳出 ``while True``（sleep 在循环体那层 try 之外，不会被它吞掉）。
    """
    assert _REAL_AUTO_SCHEDULER.__name__ == "_auto_scrape_scheduler", (
        "导入期抓到的是夹具的 no-op，不是真的自动调度")
    calls = {"n": 0}

    async def _sleep(_secs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise _Stop()

    saved = srv.asyncio
    srv.asyncio = types.SimpleNamespace(sleep=_sleep)
    try:
        client.portal.call(_REAL_AUTO_SCHEDULER)
    except _Stop:
        pass
    finally:
        srv.asyncio = saved


def _by_batch(pulled):
    """``{batch_name: {asin: (zip, marketplace)}}``"""
    out = {}
    for (b, a), v in pulled.items():
        out.setdefault(b, {})[a] = v
    return out


# ---------------------------------------------------------------- /api/upload

class UploadMarketplaceTests(unittest.TestCase):

    def test_ca_xlsx_column_b_follows_ca_postal_rules(self):
        """B 列按加拿大站规则归一；美国 5 位邮编在加拿大批次里算非法行、退回批次邮编。"""
        rows = [("B0CAUP0001", "K1V 7P8"), ("B0CAUP0002", "m5v3l9"),
                ("B0CAUP0003", None), ("B0CAUP0004", "10001")]
        with isolated_server() as (client, _ctx):
            self.assertEqual(_global_zip(client), US_GLOBAL)
            r = _upload(client, "ca_xlsx", body=_xlsx(rows), fname="a.xlsx",
                        ctype=_XLSX, marketplace=CA)
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            got = _pulled(client)

        self.assertEqual(body["invalid_zip_rows"], 1)
        self.assertEqual(body["per_asin_zip_count"], 2)
        self.assertEqual(body["marketplace"], CA)
        self.assertEqual(got, {
            ("ca_xlsx", "B0CAUP0001"): ("K1V 7P8", CA),
            ("ca_xlsx", "B0CAUP0002"): ("M5V 3L9", CA),
            ("ca_xlsx", "B0CAUP0003"): (CA_DEFAULT, CA),   # 空 B 列 -> 站点默认
            ("ca_xlsx", "B0CAUP0004"): (CA_DEFAULT, CA),   # 美国邮编 -> 非法行 -> 站点默认
        })

    def test_ca_csv_batch_zip_is_normalized(self):
        with isolated_server() as (client, _ctx):
            r = _upload(client, "ca_csv", body=b"B0CAUP0005\nB0CAUP0006,h0h0h0\n",
                        fname="a.csv", ctype="text/csv", marketplace=CA, zip_code="m5v 3l9")
            self.assertEqual(r.status_code, 200, r.text)
            got = _pulled(client)
        self.assertEqual(got, {
            ("ca_csv", "B0CAUP0005"): ("M5V 3L9", CA),
            ("ca_csv", "B0CAUP0006"): ("H0H 0H0", CA),
        })

    def test_ca_without_zip_gets_the_ca_default_not_the_global_us_zip(self):
        """全局设置是 10001 —— 拿它去加拿大站不会报错，只会静默落到默认地区。"""
        with isolated_server() as (client, _ctx):
            self.assertEqual(_global_zip(client), US_GLOBAL)
            self.assertEqual(_upload(client, "ca_txt", marketplace=CA).status_code, 200)
            # 纯空白邮编 = 没给，也不能原样落库
            self.assertEqual(_upload(client, "ca_blank", body=b"B0CAUP0007\n",
                                     marketplace=CA, zip_code="   ").status_code, 200)
            got = _pulled(client)
        self.assertEqual(got[("ca_txt", "B0MKTUP001")], (CA_DEFAULT, CA))
        self.assertEqual(got[("ca_blank", "B0CAUP0007")], (CA_DEFAULT, CA))

    def test_ca_default_follows_the_global_zip_when_it_is_a_valid_ca_code(self):
        with isolated_server() as (client, _ctx):
            client.put("/api/settings", json={"zip_code": "h0h0h0"})
            self.assertEqual(_upload(client, "ca_glob", marketplace=CA).status_code, 200)
            got = _pulled(client)
        self.assertEqual(got[("ca_glob", "B0MKTUP001")], ("H0H 0H0", CA))

    def test_ca_with_a_us_batch_zip_is_400_and_leaves_no_batch(self):
        with isolated_server() as (client, _ctx):
            before = _batch_names(client)
            r = _upload(client, "ca_bad_zip", marketplace=CA, zip_code="10001")
            self.assertEqual(r.status_code, 400, r.text)
            detail = r.json()["detail"]
            self.assertIn("加拿大站", detail)
            self.assertIn(repr(CA_DEFAULT), detail)
            self.assertEqual(_batch_names(client), before)
            self.assertEqual(client.get("/api/batches/ca_bad_zip/status").status_code, 404)

    def test_unknown_marketplace_is_400_and_leaves_no_batch(self):
        """不在注册表里 -> 400，绝不静默回退到美国站（那正是 blocker 3 本身）。"""
        with isolated_server() as (client, _ctx):
            before = _batch_names(client)
            for i, mk in enumerate(("amazon.de", "www.amazon.co.uk", "amazon.xx", "CA")):
                with self.subTest(marketplace=mk):
                    r = _upload(client, f"bad_mkt_{i}", marketplace=mk)
                    self.assertEqual(r.status_code, 400, r.text)
            self.assertEqual(_batch_names(client), before)
            self.assertEqual(_pulled(client), {})

    def test_accepted_marketplace_spellings(self):
        spellings = ("AMAZON.CA", "www.amazon.ca", "https://www.amazon.ca/", " amazon.ca ")
        with isolated_server() as (client, _ctx):
            for i, mk in enumerate(spellings):
                r = _upload(client, f"spell_{i}", body=f"B0SPELL00{i}\n".encode(), marketplace=mk)
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.json()["marketplace"], CA)
            got = _pulled(client)
        self.assertEqual(got, {(f"spell_{i}", f"B0SPELL00{i}"): (CA_DEFAULT, CA)
                               for i in range(len(spellings))})


# ---------------------------------------------------------------- 美国站不变式

class UsInvariantTests(unittest.TestCase):
    """调用方不说站点 = 美国站，与改造前逐字节一致。

    这几条在改造前也是绿的 —— 它们证明的就是「没变」。有效性靠变异：
    把批次级邮编校验放宽到美国站 / 改掉美国站报错原文 / 给美国站响应加键，
    它们当场红。
    """

    def test_no_marketplace_keeps_the_raw_batch_zip_and_the_legacy_keys(self):
        with isolated_server() as (client, _ctx):
            r = _upload(client, "us_raw", zip_code="abcde")
            self.assertEqual(r.status_code, 200, r.text)
            keys = set(r.json())
            got = _pulled(client)
        self.assertEqual(keys, _LEGACY_BATCH_KEYS)
        self.assertEqual(got[("us_raw", "B0MKTUP001")], ("abcde", US))

    def test_no_marketplace_column_b_still_zero_fills(self):
        with isolated_server() as (client, _ctx):
            r = _upload(client, "us_zfill", body=b"B0USPIN002,1234\nB0USPIN003,90001.0\n",
                        fname="a.csv", ctype="text/csv")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["invalid_zip_rows"], 0)
            got = _pulled(client)
        self.assertEqual(got[("us_zfill", "B0USPIN002")], ("01234", US))
        self.assertEqual(got[("us_zfill", "B0USPIN003")], ("90001", US))

    def test_blank_or_explicit_us_marketplace_is_the_same_as_omitting_it(self):
        """新 UI 会显式发 amazon.com：它必须与老客户端不发完全同路（含不校验批次邮编）。"""
        with isolated_server() as (client, _ctx):
            for i, mk in enumerate(("", "   ", "amazon.com", "www.amazon.com")):
                r = _upload(client, f"us_explicit_{i}", body=f"B0USEXP00{i}\n".encode(),
                            marketplace=mk, zip_code="abcde")
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(set(r.json()), _LEGACY_BATCH_KEYS)
            r = client.post("/api/batches", json={"asins": ["B0USEXP009"],
                                                  "batch_name": "us_json_null",
                                                  "marketplace": None})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(set(r.json()), _LEGACY_BATCH_KEYS)
            got = _pulled(client)
        for i in range(4):
            self.assertEqual(got[(f"us_explicit_{i}", f"B0USEXP00{i}")], ("abcde", US))
        self.assertEqual(got[("us_json_null", "B0USEXP009")], (US_GLOBAL, US))

    def test_json_us_bad_zip_message_is_byte_identical(self):
        """黄金 json_submit_bad_zip 钉着这句原文；显式传 amazon.com 也一样。"""
        with isolated_server() as (client, _ctx):
            for extra in ({}, {"marketplace": "amazon.com"}):
                with self.subTest(**extra):
                    r = client.post("/api/batches", json={
                        "asins": ["B0USPIN004"], "zip_code": "abcde", **extra})
                    self.assertEqual(r.status_code, 400, r.text)
                    self.assertEqual(r.json(), {"detail": "非法邮编: abcde"})


# ---------------------------------------------------------------- /api/batches

class JsonBatchMarketplaceTests(unittest.TestCase):

    def test_ca_items_and_batch_zip(self):
        with isolated_server() as (client, _ctx):
            r = client.post("/api/batches", json={
                "batch_name": "ca_js", "marketplace": CA, "zip_code": "m5v 3l9",
                "items": [{"asin": "B0CAJS0001", "zip_code": "k1v7p8"},
                          {"asin": "B0CAJS0002", "zip_code": "90001"},
                          {"asin": "B0CAJS0003"}]})
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            got = _pulled(client)
        self.assertEqual(body["invalid_zip_rows"], 1)
        self.assertEqual(body["marketplace"], CA)
        self.assertEqual(got, {
            ("ca_js", "B0CAJS0001"): ("K1V 7P8", CA),
            ("ca_js", "B0CAJS0002"): ("M5V 3L9", CA),   # 非法逐 ASIN 邮编 -> 批次邮编
            ("ca_js", "B0CAJS0003"): ("M5V 3L9", CA),
        })

    def test_ca_without_zip_gets_the_ca_default(self):
        with isolated_server() as (client, _ctx):
            r = client.post("/api/batches", json={
                "batch_name": "ca_js_dflt", "marketplace": "www.amazon.ca",
                "asins": ["B0CAJS0004"]})
            self.assertEqual(r.status_code, 200, r.text)
            got = _pulled(client)
        self.assertEqual(got, {("ca_js_dflt", "B0CAJS0004"): (CA_DEFAULT, CA)})

    def test_ca_top_level_us_zip_is_400_with_a_site_message_and_no_batch(self):
        with isolated_server() as (client, _ctx):
            before = _batch_names(client)
            r = client.post("/api/batches", json={
                "batch_name": "ca_js_bad", "marketplace": CA,
                "asins": ["B0CAJS0005"], "zip_code": "90001"})
            self.assertEqual(r.status_code, 400, r.text)
            self.assertEqual(
                r.json()["detail"], "非法邮编: '90001'（加拿大站需要 'K1V 7P8' 这种形状）")
            self.assertEqual(_batch_names(client), before)

    def test_ca_zips_equal_after_normalization_are_not_a_conflict(self):
        with isolated_server() as (client, _ctx):
            r = client.post("/api/batches", json={
                "batch_name": "ca_js_same", "marketplace": CA,
                "items": [{"asin": "B0CAJS0006", "zip_code": "k1v7p8"},
                          {"asin": "B0CAJS0006", "zip_code": "K1V 7P8"}]})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["invalid_zip_rows"], 0)
            got = _pulled(client)
        self.assertEqual(got, {("ca_js_same", "B0CAJS0006"): ("K1V 7P8", CA)})

    def test_bad_marketplace_values_are_400_and_leave_no_batch(self):
        with isolated_server() as (client, _ctx):
            before = _batch_names(client)
            for i, mk in enumerate((123, "amazon.de", ["amazon.ca"], {"id": "amazon.ca"}, True)):
                with self.subTest(marketplace=mk):
                    r = client.post("/api/batches", json={
                        "batch_name": f"js_bad_{i}", "asins": ["B0CAJS0007"],
                        "marketplace": mk})
                    self.assertEqual(r.status_code, 400, r.text)
            self.assertEqual(_batch_names(client), before)

    def test_upload_and_json_agree_for_ca(self):
        """两个入口共用一个核心：同样的输入，同样的任务行、同样的响应键集合。"""
        with isolated_server() as (client, _ctx):
            a = _upload(client, "par_file", body=b"B0CAPAR001\n", marketplace=CA)
            b = client.post("/api/batches", json={
                "batch_name": "par_json", "asins": ["B0CAPAR001"], "marketplace": CA})
            self.assertEqual(a.status_code, 200, a.text)
            self.assertEqual(b.status_code, 200, b.text)
            got = _pulled(client)
        self.assertEqual(set(a.json()), set(b.json()))
        self.assertEqual(set(a.json()), _LEGACY_BATCH_KEYS | {"marketplace"})
        self.assertEqual(got[("par_file", "B0CAPAR001")], got[("par_json", "B0CAPAR001")])
        self.assertEqual(got[("par_file", "B0CAPAR001")], (CA_DEFAULT, CA))


# ---------------------------------------------------------------- 定时任务：创建

class ScheduleCreateTests(unittest.TestCase):

    def test_site_and_zip_are_stored_only_when_given(self):
        """老客户端建出来的 dict 一个键都不多；给了才存，存的是规范形。"""
        with isolated_server() as (client, _ctx):
            legacy = _mk_sched(client, time="03:30")
            ca = _mk_sched(client, time="03:30", marketplace="www.amazon.ca")
            ca_zip = _mk_sched(client, time="03:30", marketplace=CA, zip_code="m5v3l9",
                               _files={"file": ("a.txt", b"B0SCHED001\n", "text/plain")})
            for r in (legacy, ca, ca_zip):
                self.assertEqual(r.status_code, 200, r.text)
            legacy, ca, ca_zip = (r.json()["schedule"] for r in (legacy, ca, ca_zip))

        self.assertEqual(set(legacy), _LEGACY_SCHED_KEYS)
        self.assertEqual(legacy["name"], "全库采集-03:30")
        self.assertEqual(set(ca), _LEGACY_SCHED_KEYS | {"marketplace"})
        self.assertEqual(ca["marketplace"], CA)
        # 默认名带站点名：同一时刻的美国 / 加拿大定时任务默认名不能一模一样
        self.assertEqual(ca["name"], "加拿大站-全库采集-03:30")
        self.assertEqual(ca_zip["zip_code"], "M5V 3L9")
        self.assertEqual(ca_zip["name"], "加拿大站-定时任务-03:30")

    def test_bad_site_or_zip_is_400_before_the_file_is_written(self):
        cases = (
            {"marketplace": "amazon.xx"},
            {"marketplace": CA, "zip_code": "10001"},
            {"zip_code": "abcde"},      # 新字段，美国站也校验
        )
        with isolated_server() as (client, _ctx):
            for form in cases:
                with self.subTest(**form):
                    r = _mk_sched(client, **form,
                                  _files={"file": ("a.txt", b"B0SCHED002\n", "text/plain")})
                    self.assertEqual(r.status_code, 400, r.text)
            sched_dir = srv._SCHEDULES_DIR
            leftovers = os.listdir(sched_dir) if os.path.isdir(sched_dir) else []
            stored = list(_stored_schedules())
        self.assertEqual(leftovers, [], "400 之后留下了孤儿 ASIN 文件")
        self.assertEqual(stored, [])


# ---------------------------------------------------------------- 定时任务：立即执行

class ScheduleRunNowTests(unittest.TestCase):

    def test_ca_file_schedule_runs_on_ca(self):
        with isolated_server() as (client, _ctx):
            s = _mk_sched(client, marketplace=CA,
                          _files={"file": ("a.txt", b"B0RUNCA001\nB0RUNCA002\n", "text/plain")}
                          ).json()["schedule"]
            r = client.post(f"/api/schedules/{s['id']}/run")
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            got = _by_batch(_pulled(client))

        self.assertEqual(body["marketplace"], CA)
        self.assertEqual(body["asin_count"], 2)
        self.assertEqual(list(got), [body["batch_name"]])
        self.assertTrue(body["batch_name"].startswith("auto_加拿大站-定时任务-00:00_amazon.ca_"),
                        body["batch_name"])
        self.assertRegex(body["batch_name"], _TS_SUFFIX)
        self.assertEqual(got[body["batch_name"]],
                         {"B0RUNCA001": (CA_DEFAULT, CA), "B0RUNCA002": (CA_DEFAULT, CA)})

    def test_whole_library_is_scoped_to_the_schedule_site(self):
        """「全库」= 本定时任务那个站点的全库；asin_count 与实际建出的任务数一致。"""
        with isolated_server() as (client, _ctx):
            _seed_library(client, us=["B0USONLY01", "B0BOTH0001"],
                          ca=["B0CAONLY01", "B0BOTH0001"])
            us = _mk_sched(client, name="us_lib").json()["schedule"]
            ca = _mk_sched(client, name="ca_lib", marketplace=CA).json()["schedule"]
            r_us = client.post(f"/api/schedules/{us['id']}/run")
            r_ca = client.post(f"/api/schedules/{ca['id']}/run")
            self.assertEqual(r_us.status_code, 200, r_us.text)
            self.assertEqual(r_ca.status_code, 200, r_ca.text)
            got = _by_batch(_pulled(client))

        b_us, b_ca = r_us.json()["batch_name"], r_ca.json()["batch_name"]
        self.assertEqual(got[b_us], {"B0USONLY01": (US_GLOBAL, US), "B0BOTH0001": (US_GLOBAL, US)})
        self.assertEqual(got[b_ca], {"B0CAONLY01": (CA_DEFAULT, CA), "B0BOTH0001": (CA_DEFAULT, CA)})
        self.assertEqual(r_us.json()["asin_count"], 2)
        self.assertEqual(r_ca.json()["asin_count"], 2)

    def test_empty_ca_library_message_names_the_site(self):
        with isolated_server() as (client, _ctx):
            _seed_library(client, us=["B0USONLY02"])
            s = _mk_sched(client, marketplace=CA).json()["schedule"]
            r = client.post(f"/api/schedules/{s['id']}/run")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["detail"], "加拿大站全库没有任何 ASIN 可采")

    def test_stored_zip_is_used_and_a_bad_stored_zip_falls_back_to_the_site_default(self):
        with isolated_server() as (client, _ctx):
            s = _mk_sched(client, name="ca_zip", marketplace=CA, zip_code="m5v 3l9",
                          _files={"file": ("a.txt", b"B0RUNCA003\n", "text/plain")}
                          ).json()["schedule"]
            r1 = client.post(f"/api/schedules/{s['id']}/run")
            self.assertEqual(r1.status_code, 200, r1.text)
            first = _pulled(client)
            # PUT /api/settings 能把存储值改成任何东西 —— 执行时必须重新校验。
            # 顺手改名：同一秒内第二次执行会拼出同一个批次名（秒精度），
            # create_batch 撞名返回既有批次、任务被 UNIQUE(batch_id, asin) 吃掉。
            _stored_schedules()[0]["zip_code"] = "10001"
            _stored_schedules()[0]["name"] = "ca_zip_2"
            r2 = client.post(f"/api/schedules/{s['id']}/run")
            self.assertEqual(r2.status_code, 200, r2.text)
            second = _pulled(client, worker="mkt-probe-2")
        self.assertEqual(first[(r1.json()["batch_name"], "B0RUNCA003")], ("M5V 3L9", CA))
        self.assertEqual(second[(r2.json()["batch_name"], "B0RUNCA003")], (CA_DEFAULT, CA))

    def test_bad_stored_marketplace_is_400_and_creates_no_batch(self):
        with isolated_server() as (client, _ctx):
            s = _mk_sched(client, _files={"file": ("a.txt", b"B0RUNBAD01\n", "text/plain")}
                          ).json()["schedule"]
            _stored_schedules()[0]["marketplace"] = "amazon.xx"
            before = _batch_names(client)
            r = client.post(f"/api/schedules/{s['id']}/run")
            self.assertEqual(r.status_code, 400, r.text)
            self.assertIn("amazon.xx", r.json()["detail"])
            self.assertEqual(_batch_names(client), before)
            self.assertEqual(_stored_schedules()[0]["last_run_date"], s["last_run_date"])

    def test_legacy_schedule_without_the_new_keys_runs_exactly_as_before(self):
        """不变式：老定时任务 -> 美国站、全局邮编原样、批次名前缀不带站点。"""
        with isolated_server() as (client, _ctx):
            client.put("/api/settings", json={"zip_code": "abcde"})   # 美国站不校验全局值
            s = _mk_sched(client, name="legacy",
                          _files={"file": ("a.txt", b"B0RUNUS001\n", "text/plain")}
                          ).json()["schedule"]
            self.assertEqual(set(s), _LEGACY_SCHED_KEYS)
            r = client.post(f"/api/schedules/{s['id']}/run")
            self.assertEqual(r.status_code, 200, r.text)
            got = _pulled(client)
        body = r.json()
        self.assertEqual(set(body), {"ok", "batch_id", "batch_name", "asin_count"})
        self.assertRegex(body["batch_name"], r"^auto_legacy_\d{8}_\d{6}$")
        self.assertEqual(got[(body["batch_name"], "B0RUNUS001")], ("abcde", US))


# ---------------------------------------------------------------- 定时任务：自动调度

class AutoSchedulerMarketplaceTests(unittest.TestCase):
    """驱动**真的** ``_auto_scrape_scheduler`` 跑一轮（不是复刻它的逻辑）。

    ⚠ 不要先点「立即执行」：它会把 last_run_date 设成今天，自动调度就跳过了。
    定时任务时间都用 00:00 —— 任何时刻都已过点。
    """

    def test_a_bad_site_schedule_is_skipped_without_blocking_the_rest(self):
        with isolated_server() as (client, _ctx):
            bad = _mk_sched(client, name="bad",
                            _files={"file": ("a.txt", b"B0AUTOBAD1\n", "text/plain")}
                            ).json()["schedule"]
            good = _mk_sched(client, name="good", marketplace=CA,
                             _files={"file": ("b.txt", b"B0AUTOGOOD\n", "text/plain")}
                             ).json()["schedule"]
            _stored_schedules()[0]["marketplace"] = "amazon.xx"   # 坏的排在前面
            _one_auto_tick(client)
            got = _by_batch(_pulled(client))
            stored = {s["id"]: s for s in _stored_schedules()}

        self.assertEqual(stored[bad["id"]]["last_run_date"], bad["last_run_date"])
        self.assertNotEqual(stored[good["id"]]["last_run_date"], good["last_run_date"])
        self.assertFalse([b for b in got if b.startswith("auto_bad")], got)
        (name, tasks), = got.items()
        self.assertTrue(name.startswith("auto_good_amazon.ca_"), name)
        self.assertEqual(tasks, {"B0AUTOGOOD": (CA_DEFAULT, CA)})

    def test_same_named_us_and_ca_schedules_get_separate_single_site_batches(self):
        """同名同时刻的美国 / 加拿大全库定时任务：同一轮、同一秒，必须是两个批次。

        批次名撞了的话 create_batch 返回既有批次 id —— 两站任务并进一个批次，
        两站都有的 ASIN 还会被 UNIQUE(batch_id, asin) 吃掉一边。
        """
        with isolated_server() as (client, _ctx):
            _seed_library(client, us=["B0USONLY03", "B0BOTH0003"],
                          ca=["B0CAONLY03", "B0BOTH0003"])
            _mk_sched(client, name="daily")                    # 老形状：不带站点
            _mk_sched(client, name="daily", marketplace=CA)
            _one_auto_tick(client)
            got = _by_batch(_pulled(client))

        auto = {b: t for b, t in got.items() if b.startswith("auto_")}
        self.assertEqual(len(auto), 2, auto)
        for b, tasks in auto.items():
            with self.subTest(batch=b):
                self.assertEqual(len({m for _z, m in tasks.values()}), 1, "一个批次混了两个站点")
        us_b = next(b for b in auto if re.match(r"^auto_daily_\d{8}_\d{6}$", b))
        ca_b = next(b for b in auto if re.match(r"^auto_daily_amazon\.ca_\d{8}_\d{6}$", b))
        self.assertEqual(auto[us_b], {"B0USONLY03": (US_GLOBAL, US), "B0BOTH0003": (US_GLOBAL, US)})
        self.assertEqual(auto[ca_b], {"B0CAONLY03": (CA_DEFAULT, CA), "B0BOTH0003": (CA_DEFAULT, CA)})

    def test_legacy_schedule_tick_is_unchanged(self):
        """不变式：老定时任务（没有 marketplace / zip_code 键）自动触发与改造前一样。"""
        with isolated_server() as (client, _ctx):
            _seed_library(client, us=["B0USONLY04"], ca=["B0CAONLY04"])
            s = _mk_sched(client, name="old").json()["schedule"]
            self.assertEqual(set(s), _LEGACY_SCHED_KEYS)
            _one_auto_tick(client)
            got = _by_batch(_pulled(client))
            stored = _stored_schedules()[0]
        (name, tasks), = got.items()
        self.assertRegex(name, r"^auto_old_\d{8}_\d{6}$")
        self.assertEqual(tasks, {"B0USONLY04": (US_GLOBAL, US)})
        self.assertEqual(set(stored), _LEGACY_SCHED_KEYS, "老定时任务被回填了新键")


# ---------------------------------------------------------------- 网页表单

_TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "server", "templates")


def _select_options(html, select_id):
    m = re.search(rf'<select[^>]*id="{select_id}"[^>]*>(.*?)</select>', html, re.S)
    assert m, f"页面里没有 #{select_id}"
    return [(v, p, bool(sel)) for v, p, sel in re.findall(
        r'<option value="([^"]+)" data-postal="([^"]+)"( selected)?>', m.group(1))]


class PageFormTests(unittest.TestCase):

    def _assert_registry_options(self, opts):
        self.assertEqual([v for v, _p, _s in opts][0], US, "美国站必须排第一个")
        self.assertEqual({v: p for v, p, _s in opts}, {US: "10001", CA: CA_DEFAULT})
        self.assertEqual([v for v, _p, s in opts if s], [US], "默认选中的必须是美国站")

    def test_pages_render_the_site_selects_from_the_registry(self):
        with isolated_server() as (client, _ctx):
            tasks_html = client.get("/tasks").text
            settings_html = client.get("/settings").text

        self._assert_registry_options(_select_options(tasks_html, "upMarketplace"))
        self.assertIn('name="marketplace" id="upMarketplace"', tasks_html)
        self.assertRegex(tasks_html, r'name="zip_code" id="upZip"[^>]*\s+value="10001"')
        self._assert_registry_options(_select_options(settings_html, "newSchedMarketplace"))
        self.assertIn('id="newSchedZip"', settings_html)

        # 模板源码里不许有第二份站点表：站点只从注册表来
        for tpl in ("tasks.html", "settings.html"):
            with open(os.path.join(_TEMPLATES, tpl), encoding="utf-8") as f:
                src = f.read()
            with self.subTest(template=tpl):
                self.assertNotIn("amazon.ca", src)
                self.assertNotIn("K1V", src)

    def test_switching_site_clears_the_prefilled_us_zip(self):
        """切到加拿大站时清掉预填的美国邮编（否则必 400），切回来再填回；用户手填的不碰。

        跑的是页面里**真的**那段 JS（从渲染结果里截出来），站点表也取自渲染出的
        <option>。只需要裸 Node，不需要 jsdom。
        """
        node = shutil.which("node")
        if not node:
            self.skipTest("没装 Node")
        with isolated_server() as (client, _ctx):
            html = client.get("/tasks").text
        opts = _select_options(html, "upMarketplace")
        m = re.search(r"(const SERVER_DEFAULT_ZIP.*?)\ndocument\.getElementById\('upMarketplace'\)",
                      html, re.S)
        self.assertIsNotNone(m, "页面里没有站点 / 邮编联动脚本")
        self.assertIn("applySiteZip(document.getElementById('kwZip'), "
                      "e.target.value.replace(/^www\\./, ''))", html)

        stub = ("const document = {getElementById: () => ({options: %s}), };\n"
                % json.dumps([{"value": v, "dataset": {"postal": p}} for v, p, _s in opts]))
        scenario = r"""
const out = [];
const snap = (i) => out.push([i.value, i.placeholder]);
const up = {value: '10001', placeholder: '10001'};
applySiteZip(up, 'amazon.ca'); snap(up);          // 预填美国邮编 -> 清空
applySiteZip(up, 'amazon.com'); snap(up);         // 切回 -> 填回全局默认
up.value = 'M5V 3L9';
applySiteZip(up, 'amazon.ca'); snap(up);          // 手填的不碰
const kw = {value: '10001', placeholder: '10001'};
const host = (h) => h.replace(/^www\./, '');
applySiteZip(kw, host('www.amazon.ca')); snap(kw);
applySiteZip(kw, host('www.amazon.de')); snap(kw); // 不在注册表 -> 按美国站
console.log(JSON.stringify(out));
"""
        proc = subprocess.run([node, "-e", stub + m.group(1) + "\n" + scenario],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [
            ["", CA_DEFAULT], ["10001", "10001"], ["M5V 3L9", CA_DEFAULT],
            ["", CA_DEFAULT], ["10001", "10001"],
        ])


if __name__ == "__main__":
    unittest.main()
