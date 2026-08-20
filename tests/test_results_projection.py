"""`/api/results` 的 `fields=` 投影与 `with_total=false`。

------------------------------------------------------------------------
它们冲的是什么
------------------------------------------------------------------------
`server/api/results.py` 头部那段实测记着「**82% 的账在 Python 序列化上**」。
这两个参数不优化 SQL，优化的是"要吐多大一坨"。

实测（100 万行、单页 50 行、long_description 等宽列有真实内容）：

    默认 SELECT d.*（56 列）+ count      60.9 ms   274.2 KB
    首屏：窄投影 + 算 total              52.1 ms    20.0 KB
    翻页：窄投影 + 不算 total             2.7 ms    20.0 KB

采集结果页只渲染 15 个字段，而 bullet_points(24KB) / image_urls(22KB) /
long_description 这三个它一个都不显示。

------------------------------------------------------------------------
本文件最重要的一条是 test_default_response_is_unchanged
------------------------------------------------------------------------
`items[]` 的列集是**对外契约**（docs/erpapi_contract.md §3.2：可以单方面
**加**字段，**不可以删**）。所以这两个开关必须默认关闭，不带参数时的响应
必须与改动前逐字段相同 —— 否则这就不是一次优化，是一次静默的契约破坏。

两个后端都要跑：`get_results` 是 `PUBLIC_API` 对等断言覆盖的方法。
"""
from __future__ import annotations

import unittest

from tests.golden.harness import isolated_server

#: 采集结果页真正渲染的列（与 server/templates/results.html 的 UI_FIELDS 一致）
UI_FIELDS = ("asin,title,brand,current_price,buybox_price,rating,review_count,"
             "seller_id,seller_name,stock_count,stock_status,is_fba,"
             "delivery_date,crawl_time,screenshot_path")

#: 服务端会强制补上的四列，即使调用方没点名
FORCED = {"id", "asin", "screenshot_path", "updated_at"}


def _seed(c, n=3, batch="proj"):
    asins = [f"B0PROJ{i:04d}" for i in range(n)]
    c.post("/api/upload",
           files={"file": ("s.txt", "\n".join(asins).encode(), "text/plain")},
           data={"batch_name": batch, "zip_code": "10001"})
    tasks = c.get("/api/tasks/pull",
                  params={"worker_id": "w-proj", "count": n + 5}).json()["tasks"]
    c.post("/api/tasks/result/batch", json={"results": [{
        "task_id": t["id"], "batch_id": t["batch_id"], "worker_id": "w-proj",
        "lease_epoch": t["lease_epoch"], "success": True, "asin": t["asin"],
        "title": f"Title {t['asin']}", "brand": "ProjBrand",
        "long_description": "x" * 3000,          # 大块头，正是要省掉的那种
        "bullet_points": "b" * 2000,
        "image_urls": "https://m.media-amazon.com/images/I/71A._AC_.jpg\n" * 7,
        "current_price": "19.99", "stock_status": "In Stock", "stock_count": "5",
        "crawl_time": "2026-08-19T10:00:00Z", "site": "US", "zip_code": "10001"}
        for t in tasks]})
    return tasks


class DefaultUnchangedTests(unittest.TestCase):
    """**契约面。** 不传参数时，行为必须与改动前一模一样。"""

    def test_default_response_is_unchanged(self):
        with isolated_server() as (c, _):
            _seed(c)
            body = c.get("/api/results").json()

        item = body["items"][0]
        self.assertIsInstance(body["total"], int,
                              "不传 with_total 时必须照算 total")
        # 宽列必须还在 —— 它们正是"能省"的那些，但默认不许省
        for wide in ("long_description", "bullet_points", "image_urls"):
            self.assertIn(wide, item, f"默认响应少了 {wide}，这是删字段，契约不允许")
        self.assertGreater(len(item), 50,
                           "默认应当是全部列（当前 56 个），不是窄投影")


class ProjectionTests(unittest.TestCase):

    def test_fields_narrows_the_payload(self):
        with isolated_server() as (c, _):
            _seed(c)
            full = c.get("/api/results").json()
            slim = c.get("/api/results", params={"fields": UI_FIELDS}).json()

        self.assertEqual([i["asin"] for i in full["items"]],
                         [i["asin"] for i in slim["items"]],
                         "投影不该改变行集或行序")
        for wide in ("long_description", "bullet_points", "image_urls"):
            self.assertNotIn(wide, slim["items"][0], f"{wide} 没被省掉")
        self.assertLess(len(str(slim["items"])), len(str(full["items"])) / 2,
                        "窄投影没有显著变小 —— 省的不是大块头？")

    def test_forced_columns_are_always_present(self):
        """`id`/`asin`/`screenshot_path`/`updated_at` 必须补上，即使没点名。

        少 `id` -> next_cursor 是 KeyError，翻页当场断；
        少 `asin`/`screenshot_path` -> 截图路径归一化拿不到键；
        少 `updated_at` -> 带 batch_id 时 batch_asin_data_updated_at 是空。
        这些都是"首屏看起来正常、翻页或点截图才炸"的坏法。
        """
        with isolated_server() as (c, _):
            _seed(c)
            body = c.get("/api/results", params={"fields": "title"}).json()

        keys = set(body["items"][0])
        self.assertTrue(FORCED <= keys, f"强制列缺了: {sorted(FORCED - keys)}")
        self.assertIn("title", keys)

    def test_paging_still_works_under_projection(self):
        """翻页游标来自 `id`，投影之后必须照样能翻。"""
        with isolated_server() as (c, _):
            _seed(c, n=5)
            p1 = c.get("/api/results",
                       params={"fields": "title", "limit": 2}).json()
            self.assertTrue(p1["has_more"])
            p2 = c.get("/api/results",
                       params={"fields": "title", "limit": 2,
                               "cursor": p1["next_cursor"]}).json()

        self.assertTrue(p1["next_cursor"], "投影之后 next_cursor 没了")
        first = {i["asin"] for i in p1["items"]}
        second = {i["asin"] for i in p2["items"]}
        self.assertFalse(first & second, "两页有重叠 —— 游标坏了")

    def test_unknown_field_is_rejected_not_silently_dropped(self):
        """422 拒绝。静默丢弃会让调用方把「没返回」读成「是空的」。"""
        with isolated_server() as (c, _):
            _seed(c)
            r = c.get("/api/results",
                      params={"fields": "title,no_such_column"})
            empty = c.get("/api/results", params={"fields": ""})
            toomany = c.get("/api/results",
                            params={"fields": ",".join(["title"] * 65)})

        self.assertEqual(r.status_code, 422, r.text)
        self.assertIn("no_such_column", r.text)
        self.assertEqual(empty.status_code, 422, "fields= 空串该拒绝")
        self.assertEqual(toomany.status_code, 422, "超过 MAX_FIELDS 该拒绝")

    def test_injection_attempt_is_rejected(self):
        """列名会拼进 SQL —— 白名单之外的一律 422，不是过滤后放行。"""
        with isolated_server() as (c, _):
            _seed(c)
            r = c.get("/api/results",
                      params={"fields": "title,(SELECT 1)"})
        self.assertEqual(r.status_code, 422, r.text)


class WithTotalTests(unittest.TestCase):

    def test_with_total_false_returns_null(self):
        with isolated_server() as (c, _):
            _seed(c)
            on = c.get("/api/results").json()
            off = c.get("/api/results", params={"with_total": "false"}).json()

        self.assertIsInstance(on["total"], int)
        self.assertIsNone(off["total"], "with_total=false 时 total 应当是 null")
        self.assertEqual([i["asin"] for i in on["items"]],
                         [i["asin"] for i in off["items"]],
                         "省掉 count 不该影响行集")

    def test_has_more_still_works_without_total(self):
        """`has_more` 才是翻页的终止条件，它**不依赖** total。

        如果省掉 count 顺手把 has_more 也弄坏了，前端会在最后一页多点一次
        或者提前停 —— 而 total 是 null 时没有第二个信号能兜底。
        """
        with isolated_server() as (c, _):
            _seed(c, n=5)
            page = c.get("/api/results",
                         params={"with_total": "false", "limit": 2}).json()
            self.assertTrue(page["has_more"])
            last = c.get("/api/results",
                         params={"with_total": "false", "limit": 100}).json()

        self.assertIsNone(page["total"])
        self.assertFalse(last["has_more"], "一页装得下时 has_more 应当是 false")


if __name__ == "__main__":
    unittest.main()
