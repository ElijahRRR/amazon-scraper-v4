"""同一个 ASIN 采多个邮编：数据与截图分别怎么拿准。

------------------------------------------------------------------------
这份文件回答的问题
------------------------------------------------------------------------
「我推一个 ASIN，但要采多个不同邮编，怎么拿到我要的那份准确数据？开了截图
又怎么拿到对应那张图？」

答案受**库结构**约束，不是 API 层能自由决定的，所以先把三条硬事实摆出来：

  1. ``tasks`` 是 ``UNIQUE(batch_id, asin)`` —— **一个批次里，一个 ASIN 只能
     有一个邮编**。同 ASIN 多邮编这个诉求在单个批次内根本表达不了。
  2. ``asin_data.asin`` 是 ``UNIQUE`` —— 全库**每个 ASIN 只有一行快照**，
     后采的覆盖先采的。而 ``iter_results(batch_id=...)`` 是
     ``JOIN batch_asins ba ON ba.asin = d.asin``：batch_id 只用来**挑 ASIN**，
     数据仍然取那一行全局快照。所以 ``/api/results`` 和
     ``/api/export/{batch_name}`` 对同一个 ASIN 的两个邮编批次会返回
     **完全相同的行**（最后采的那次）。
  3. ``scrape_events`` 每次采集一行，各带自己的 ``source_id`` 与
     ``zip_requested`` —— **只有 ``/api/export/incremental`` 保留逐邮编的
     结果**。

于是唯一正确的用法是：**一个邮编推一个批次**，然后

  * 数据走 ``/api/export/incremental``，按 ``scrape_params.zipcode`` 分辨；
  * 截图走 ``/api/screenshots``，按批次分辨（截图落盘是
    ``<批次名>/<asin>.png``，**批次名就是隔离键**）。

本文件把这四条全部跑通、逐条断言。第 2 条那个「看起来能用、其实是最后一次
覆盖」的坑尤其要钉住：它不报错、值也长得像真的，只有拿两个邮编去对照才看得见。

------------------------------------------------------------------------
只在 PostgreSQL 上跑
------------------------------------------------------------------------
``scrape_events`` / 增量导出整条线是 PG-only（SQLite 那条回滚路径没有 relay）。
后端不是 PG 时整类 skip，与 ``tests/test_incremental_export.py`` 一致。
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


TOKEN = "test-multizip-token-12345"

_ASIN = "B0MZIP0001"
_ZIPS = ("10001", "90001")
#: 一个邮编一个批次，批次名带上邮编 —— 这既是隔离键，也让人一眼看出这批是哪个区。
_BATCHES = {z: f"mz_{z}" for z in _ZIPS}
#: 两个邮编采到**不同的价格**，这才分辨得出「拿到的是哪一份」。
_PRICES = {"10001": "19.99", "90001": "24.50"}

_PNG = {
    z: bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000b49444154789c6360000200000500017a5eab3f0000"
        "000049454e44ae426082"
    ) + f"\n<!-- zip {z} -->".encode()   # 尾部塞点字节，好证明两张图不是同一张
    for z in _ZIPS
}


@unittest.skipUnless(is_postgres(), "增量导出 / scrape_events 是 PG-only")
class MultiZipSameAsinTests(unittest.TestCase):

    def test_one_batch_per_zip_keeps_both_data_and_screenshots_apart(self):
        """一次跑完全程，四条结论一起验。

        写成一个用例而不是拆四个，是因为它们共享同一段昂贵的 setup（两轮真实
        采集 + relay 抽干），而且**结论之间是互相印证的**：第 3 条（快照被覆盖）
        只有和第 2 条（事件流两份都在）摆在一起才说明问题——否则会被读成
        「数据丢了」，而实际是「找错地方了」。
        """
        with _server_with_relay() as (client, _ctx):
            # ---------- 1. 一个邮编推一个批次 ----------
            for z in _ZIPS:
                r = client.post("/api/batches", json={
                    "asins": [_ASIN],
                    "zip_code": z,
                    "needs_screenshot": True,
                    "batch_name": _BATCHES[z],
                })
                self.assertEqual(r.status_code, 200, r.text)

            # 两个批次各自派了一条任务，邮编各是各的
            tasks = client.get("/api/tasks/pull", params={
                "worker_id": "w-mzip", "count": 100,
                "enable_screenshot": "true"}).json()["tasks"]
            by_zip = {t["zip_code"]: t for t in tasks if t["asin"] == _ASIN}
            self.assertEqual(set(by_zip), set(_ZIPS),
                             f"两个邮编各该有一条任务，实际 {tasks}")

            # ---------- 2. 采集：两个邮编采到不同的价格 ----------
            for z, t in by_zip.items():
                r = client.post("/api/tasks/result/batch", json={"results": [{
                    "task_id": t["id"], "batch_id": t["batch_id"],
                    "worker_id": "w-mzip", "lease_epoch": t["lease_epoch"],
                    "success": True,
                    "asin": _ASIN, "title": f"MultiZip {_ASIN}",
                    "brand": "MZBrand",
                    "category_tree": "Home > Tools > Wrenches",
                    "image_urls": "https://m.media-amazon.com/images/I/71ABC._AC_SL1500_.jpg",
                    "current_price": _PRICES[z],
                    "stock_status": "In Stock", "stock_count": "5",
                    "crawl_time": "2026-08-05T10:00:00Z",
                    "site": "US", "zip_code": z,
                }]})
                self.assertEqual(r.status_code, 200, r.text)

            # ---------- 3. 截图：两个批次各传一张 ----------
            for z in _ZIPS:
                r = client.post("/api/tasks/screenshot",
                                files={"file": (f"{_ASIN}.png", _PNG[z], "image/png")},
                                data={"asin": _ASIN, "batch_name": _BATCHES[z],
                                      "worker_id": "w-mzip"})
                self.assertEqual(r.status_code, 200, r.text)

            _drain(n=2)

            # ================================================================
            # 结论 A：截图按批次隔离，两张图各是各的
            # ================================================================
            got = {}
            for z in _ZIPS:
                r = client.get(f"/api/screenshots/{_BATCHES[z]}/{_ASIN}")
                self.assertEqual(r.status_code, 200, r.text)
                got[z] = r.content
            self.assertEqual(got["10001"], _PNG["10001"])
            self.assertEqual(got["90001"], _PNG["90001"])
            self.assertNotEqual(
                got["10001"], got["90001"],
                "两个邮编的截图内容相同 —— 说明它们落到了同一个文件、互相覆盖了。"
                "截图路径是 <批次名>/<asin>.png，批次名是唯一的隔离键，"
                "一旦两个邮编共用批次名就会撞。")

            # 列表端点也各报各的，url 指向自己那张
            for z in _ZIPS:
                items = client.get("/api/screenshots", params={
                    "batch_name": _BATCHES[z]}).json()["items"]
                self.assertEqual(len(items), 1, items)
                self.assertEqual(items[0]["status"], "done")
                self.assertTrue(items[0]["url"].endswith(
                    f"/api/screenshots/{_BATCHES[z]}/{_ASIN}"), items[0]["url"])

            # ================================================================
            # 结论 B：/api/export/incremental 保留**两份**，各带自己的邮编
            #         —— 这是拿准确数据的**唯一**入口
            # ================================================================
            records = _export_all(client)
            mine = [r for r in records if r["asin"] == _ASIN]
            self.assertEqual(
                len(mine), 2,
                f"同一 ASIN 的两次采集必须是事件流里的两条独立记录，实际 {len(mine)} 条")

            by_zipcode = {r["scrape_params"]["zipcode"]: r for r in mine}
            self.assertEqual(set(by_zipcode), set(_ZIPS),
                             "两条记录的 scrape_params.zipcode 必须分别是两个邮编")
            for z in _ZIPS:
                # 契约里 fast.price 是**数值**（不是采集侧那个字符串），
                # 所以按数值比 —— "24.50" 存进去、24.5 取出来是对的。
                self.assertAlmostEqual(
                    float(by_zipcode[z]["fast"]["price"]), float(_PRICES[z]),
                    places=2,
                    msg=f"邮编 {z} 那条记录带的价格不对 —— 逐邮编的准确性就靠这个字段对得上")

            # 两条记录的 source_id / cursor 互不相同：消费方按游标推进不会漏掉其中一条
            self.assertNotEqual(mine[0]["source_id"], mine[1]["source_id"])
            self.assertNotEqual(mine[0]["cursor"], mine[1]["cursor"])

            # ================================================================
            # 结论 C（**坑**）：快照类端点只有一行，后采的覆盖先采的
            # ================================================================
            detail = client.get(f"/api/results/{_ASIN}")
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertIn(
                str(detail.json()["data"]["current_price"]), set(_PRICES.values()),
                "快照里应当是两次采集中的某一次")

            # 关键：**两个批次导出的是同一行**。batch_id 只用来挑 ASIN，
            # 数据来自那一行全局 asin_data 快照。
            per_batch_prices = set()
            for z in _ZIPS:
                rows = client.get("/api/results", params={
                    "batch_id": _batch_id(client, _BATCHES[z])}).json()
                items = rows.get("results") or rows.get("items") or []
                hit = [i for i in items if i["asin"] == _ASIN]
                self.assertEqual(len(hit), 1, hit)
                per_batch_prices.add(str(hit[0]["current_price"]))

            self.assertEqual(
                len(per_batch_prices), 1,
                "本断言在描述**现状**：/api/results 按 batch_id 过滤时，两个邮编批次"
                "返回的是同一行全局快照（asin_data.asin 是 UNIQUE）。\n"
                "如果这里变成 2，说明快照表已经改成按邮编分行了 —— 那是个好改动，"
                "但契约变了，本文件与 docs 里「只有增量导出逐邮编准确」那句话要一起改。")

    def test_two_zips_for_one_asin_in_one_push_is_rejected(self):
        """一个批次里给同一 ASIN 两个邮编 -> 400，并告诉调用方正确做法。

        库里表达不了（``UNIQUE(batch_id, asin)``）。静默取第一个的话，调用方
        拿到 200、少采一个邮编，**响应里完全看不出来** —— 这正是最坏的一种失败。
        """
        with isolated_server() as (client, _ctx):
            r = client.post("/api/batches", json={
                "batch_name": "mz_conflict",
                "items": [{"asin": _ASIN, "zip_code": "10001"},
                          {"asin": _ASIN, "zip_code": "90001"}],
            })
            self.assertEqual(r.status_code, 400, r.text)
            detail = r.json()["detail"]
            self.assertEqual(detail["error"], "conflicting_zip_for_asin")
            self.assertEqual(detail["asin"], _ASIN)
            self.assertEqual(sorted(detail["zip_codes"]), ["10001", "90001"])
            self.assertIn("一个邮编推一个批次", detail["message"],
                          "报错必须给出正确做法，否则调用方只知道被拒了")

    def test_same_zip_twice_for_one_asin_is_fine(self):
        """重复写同一个邮编不是冲突 —— 上游拼列表时重复很常见，不该因此失败。"""
        with isolated_server() as (client, _ctx):
            r = client.post("/api/batches", json={
                "batch_name": "mz_same",
                "items": [{"asin": _ASIN, "zip_code": "10001"},
                          {"asin": _ASIN, "zip_code": "10001"}],
            })
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["total_asins"], 1)


def _batch_id(client, name):
    rows = client.get("/api/batches").json()["batches"]
    return next(b["id"] for b in rows if b["name"] == name)


def _export_all(client):
    """把增量导出整条流拉完（游标推进到底）。"""
    out, cursor = [], 0
    for _ in range(20):
        r = client.get("/api/export/incremental",
                       params={"cursor": cursor, "limit": 100},
                       headers={"X-Export-Token": TOKEN})
        assert r.status_code == 200, r.text
        body = r.json()
        recs = body.get("records") or []
        out += recs
        if not recs:
            break
        cursor = body.get("next_cursor") or recs[-1]["cursor"]
    return out


@contextlib.contextmanager
def _server_with_relay():
    """黄金夹具默认把 relay 也 no-op 掉（录制期必须静音）。这里需要它真的跑，
    否则 outbox 永远抽不干、scrape_events 恒空 —— 结论 B 就成了空断言。

    同时钉上 EXPORT_TOKEN：导出端点未配 token 时会拒绝服务。
    """
    saved = harness._PATCHED_LOOPS
    harness._PATCHED_LOOPS = tuple(
        x for x in saved if x != "_scrape_event_relay")
    saved_token = os.environ.get("EXPORT_TOKEN")
    os.environ["EXPORT_TOKEN"] = TOKEN
    try:
        with isolated_server() as pair:
            yield pair
    finally:
        harness._PATCHED_LOOPS = saved
        if saved_token is None:
            os.environ.pop("EXPORT_TOKEN", None)
        else:
            os.environ["EXPORT_TOKEN"] = saved_token


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


if __name__ == "__main__":
    unittest.main()
