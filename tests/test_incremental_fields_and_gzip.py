"""增量导出的 `fields=` 投影 + 全局 gzip —— 两个都是冲**传输**去的。

------------------------------------------------------------------------
账目（实测，500 条 record、每条内容各不相同的真实体量）
------------------------------------------------------------------------
消费侧跨机房拉一页实测 8~11s，其中服务端只占 ~1.1s：

    SQL 取 501 行         234.7 ms
    _to_record × 500      409.1 ms
    json.dumps            438.0 ms
    响应体              3429.4 KB      <- 剩下 7~10s 全在这上面

单条 record 的构成：`slow` 占 69%，而 `slow.description` 一个字段就占 47%。

所以两把刀都砍传输：`fields=` 让调用方别拉不需要的块，gzip 把剩下的压到
六分之一。SQL 和序列化一分钱没省 —— 这是**有意的**，见 `_prune` 的注释。

------------------------------------------------------------------------
本文件最重要的一条是 test_default_response_is_unchanged
------------------------------------------------------------------------
`/api/export/incremental` 是**契约 v1**，沃尔玛侧已按它实现（§3.2：可以单方面
**加**字段，不可以删）。所以 `fields=` 必须默认关闭，不传时逐字段与改动前相同。
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
INCR = "/api/export/incremental"


@contextlib.contextmanager
def _server_with_relay():
    saved = harness._PATCHED_LOOPS
    harness._PATCHED_LOOPS = tuple(
        x for x in saved if x != "_scrape_event_relay")
    try:
        with isolated_server() as pair:
            yield pair
    finally:
        harness._PATCHED_LOOPS = saved


def _drain(n=1):
    import asyncio
    import asyncpg
    from common import config

    async def _wait():
        conn = await asyncpg.connect(config.PG_DSN)
        try:
            for _ in range(80):
                left = await conn.fetchval("SELECT count(*) FROM scraper.scrape_outbox")
                got = await conn.fetchval("SELECT count(*) FROM scraper.scrape_events")
                if left == 0 and got >= n:
                    return got
                await asyncio.sleep(0.25)
            return 0
        finally:
            await conn.close()
    return asyncio.run(_wait())


def _seed(c, asin="B0FIELDS01", batch="fields_probe"):
    c.post("/api/upload",
           files={"file": ("s.txt", f"{asin}\n".encode(), "text/plain")},
           data={"batch_name": batch, "zip_code": "10001"})
    t = c.get("/api/tasks/pull",
              params={"worker_id": "w-f", "count": 1}).json()["tasks"][0]
    c.post("/api/tasks/result", json={
        "task_id": t["id"], "batch_id": t["batch_id"], "worker_id": "w-f",
        "lease_epoch": t["lease_epoch"], "success": True, "asin": asin,
        "title": "Fields Probe", "brand": "FieldsBrand",
        "category_tree": "Home > Tools",
        "long_description": "x" * 3000, "bullet_points": "b" * 800,
        "image_urls": "https://m.media-amazon.com/images/I/71A._AC_.jpg\n" * 7,
        "current_price": "19.99", "stock_status": "In Stock", "stock_count": "5",
        "crawl_time": "2026-08-20T10:00:00Z", "site": "US", "zip_code": "10001"})
    _drain(1)
    return batch


def _hdr():
    return {"X-Export-Token": TOKEN}


@unittest.skipUnless(is_postgres(), "事件流是 PostgreSQL 专属")
class DefaultUnchangedTests(unittest.TestCase):
    """契约面。不传 `fields=` 时必须与改动前一模一样。"""

    def test_default_response_is_unchanged(self):
        with _server_with_relay() as (c, _):
            _seed(c)
            rec = c.get(INCR, params={"cursor": 0}, headers=_hdr()
                        ).json()["records"][0]

        for block in ("scrape_params", "slow", "fast", "raw"):
            self.assertIn(block, rec, f"默认响应少了 {block} —— 这是删字段，契约不允许")
        # 大块头必须还在：它们正是"能省"的，但默认不许省
        self.assertTrue(rec["slow"]["description"], "默认不该裁掉 slow.description")
        self.assertTrue(rec["slow"]["images"], "默认不该裁掉 slow.images")
        for scalar in ("slow_hash", "review_hash", "completeness_ok", "recorded_at"):
            self.assertIn(scalar, rec)


@unittest.skipUnless(is_postgres(), "事件流是 PostgreSQL 专属")
class ProjectionTests(unittest.TestCase):

    def test_block_level_projection(self):
        with _server_with_relay() as (c, _):
            _seed(c)
            body = c.get(INCR, params={"cursor": 0, "fields": "fast"},
                         headers=_hdr()).json()
        rec = body["records"][0]
        self.assertIn("fast", rec)
        for gone in ("slow", "raw", "scrape_params", "slow_hash"):
            self.assertNotIn(gone, rec, f"{gone} 没被裁掉")

    def test_dotted_path_projection(self):
        with _server_with_relay() as (c, _):
            _seed(c)
            rec = c.get(INCR, params={"cursor": 0,
                                      "fields": "slow.title,fast.price"},
                        headers=_hdr()).json()["records"][0]
        self.assertEqual(set(rec["slow"]), {"title"})
        self.assertEqual(set(rec["fast"]), {"price"})
        self.assertEqual(rec["slow"]["title"], "Fields Probe")

    def test_identity_fields_survive_any_projection(self):
        """`source_id`/`cursor`/`asin`/`marketplace`/`scraped_at`/`outcome` 恒在。

        少 `cursor` -> 调用方不知道下一页从哪开始，同步当场断。
        少 `outcome` -> 一条失败记录会被当成功数据 upsert 进商品库
                        （契约正文：outcome != 'ok' 只进快照表）。
        这些加起来约 200 字节，省不出什么，却是唯一能防住"裁出一份不可用
        数据"的东西。
        """
        with _server_with_relay() as (c, _):
            _seed(c)
            rec = c.get(INCR, params={"cursor": 0, "fields": "fast.price"},
                        headers=_hdr()).json()["records"][0]
        for k in ("source_id", "cursor", "asin", "marketplace",
                  "scraped_at", "outcome"):
            self.assertIn(k, rec, f"身份字段 {k} 被裁掉了")

    def test_projection_shrinks_the_payload(self):
        import json
        with _server_with_relay() as (c, _):
            _seed(c)
            full = c.get(INCR, params={"cursor": 0}, headers=_hdr()).json()
            slim = c.get(INCR, params={"cursor": 0, "fields": "fast"},
                         headers=_hdr()).json()
        self.assertLess(len(json.dumps(slim["records"])),
                        len(json.dumps(full["records"])) / 3,
                        "窄投影没有显著变小 —— 省的不是大块头？")

    def test_cursor_still_advances_under_projection(self):
        with _server_with_relay() as (c, _):
            _seed(c)
            body = c.get(INCR, params={"cursor": 0, "fields": "fast"},
                         headers=_hdr()).json()
            self.assertGreater(body["next_cursor"], 0, "投影之后游标不推进了")
            tail = c.get(INCR, params={"cursor": body["next_cursor"],
                                       "fields": "fast"}, headers=_hdr()).json()
        self.assertEqual(tail["records"], [])
        self.assertEqual(tail["next_cursor"], body["next_cursor"])

    def test_unknown_field_is_rejected_not_silently_dropped(self):
        with _server_with_relay() as (c, _):
            _seed(c)
            bad = c.get(INCR, params={"cursor": 0, "fields": "slow,nope"},
                        headers=_hdr())
            empty = c.get(INCR, params={"cursor": 0, "fields": ""}, headers=_hdr())
            deep = c.get(INCR, params={"cursor": 0, "fields": "slow.weight.package"},
                         headers=_hdr())
        self.assertEqual(bad.status_code, 422, bad.text)
        self.assertEqual(bad.json()["error"], "invalid_parameter")
        self.assertIn("nope", bad.text)
        self.assertEqual(empty.status_code, 422, "fields= 空串该拒绝")
        self.assertEqual(deep.status_code, 422, "点号只支持一层，两层该拒绝")

    def test_batch_endpoint_shares_the_same_projection(self):
        """两个端点共用 `_to_record`，投影语义也必须共用。

        否则同一条记录在两条路上会裁出两种形状。
        """
        with _server_with_relay() as (c, _):
            batch = _seed(c)
            a = c.get(INCR, params={"cursor": 0, "fields": "slow.title"},
                      headers=_hdr()).json()["records"][0]
            b = c.get(f"/api/export/batch/{batch}/records",
                      params={"cursor": 0, "fields": "slow.title"},
                      headers=_hdr()).json()["records"][0]
        self.assertEqual(a, b, "同一条记录在两个端点上裁出了不同形状")


class GzipTests(unittest.TestCase):
    """gzip 对两个后端都生效，所以不 skip。"""

    def test_large_json_is_compressed(self):
        with isolated_server() as (c, _):
            # 造一页够大的响应：/api/results 默认全 56 列
            asins = [f"B0GZIP{i:04d}" for i in range(30)]
            c.post("/api/upload",
                   files={"file": ("s.txt", "\n".join(asins).encode(), "text/plain")},
                   data={"batch_name": "gzip_probe", "zip_code": "10001"})
            tasks = c.get("/api/tasks/pull",
                          params={"worker_id": "w-g", "count": 50}).json()["tasks"]
            c.post("/api/tasks/result/batch", json={"results": [{
                "task_id": t["id"], "batch_id": t["batch_id"], "worker_id": "w-g",
                "lease_epoch": t["lease_epoch"], "success": True, "asin": t["asin"],
                "title": "G" * 80, "long_description": "d" * 3000,
                "bullet_points": "b" * 800, "current_price": "1.00",
                "stock_status": "In Stock",
                "crawl_time": "2026-08-20T10:00:00Z", "site": "US",
                "zip_code": "10001"} for t in tasks]})

            gz = c.get("/api/results", params={"limit": 30},
                       headers={"Accept-Encoding": "gzip"})
            plain = c.get("/api/results", params={"limit": 30},
                          headers={"Accept-Encoding": "identity"})

        self.assertEqual(gz.headers.get("content-encoding"), "gzip",
                         "大 JSON 没有被压缩")
        # httpx 会自动解压，所以两边解出来的内容必须逐字节相同
        self.assertEqual(gz.json(), plain.json(),
                         "压缩改变了响应内容 —— 那不是压缩，是损坏")
        self.assertIsNone(plain.headers.get("content-encoding"),
                          "客户端明说不要压缩时不该压")

    def test_small_response_is_not_compressed(self):
        """小响应压了反而更大（gzip 有固定头开销），且白费 CPU。"""
        with isolated_server() as (c, _):
            r = c.get("/api/progress", headers={"Accept-Encoding": "gzip"})
        self.assertLess(len(r.content), 1024, "前置条件：这个响应本来就该很小")
        self.assertIsNone(r.headers.get("content-encoding"),
                          "小响应不该被压缩（minimum_size=1024）")

    def test_png_is_not_recompressed(self):
        """PNG 已经是压缩格式，再压一遍纯浪费 CPU 且几乎不变小。

        靠 Starlette 的 exclude_content_types 默认值（含 image/png）。
        """
        from starlette.middleware.gzip import GZipMiddleware
        import inspect
        excluded = inspect.signature(
            GZipMiddleware.__init__).parameters["exclude_content_types"].default
        for t in ("image/png", "image/jpeg", "application/zip"):
            self.assertIn(t, excluded, f"{t} 不在排除列表里，会被白压一遍")


if __name__ == "__main__":
    unittest.main()
