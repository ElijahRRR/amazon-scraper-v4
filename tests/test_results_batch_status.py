"""`/api/results?batch_id=` 的三列批次状态 —— 让 JSON 出口也能看出数据的年龄。

------------------------------------------------------------------------
它补的洞
------------------------------------------------------------------------
`get_results` 的 SQL 是

    SELECT d.* FROM asin_data d
    JOIN batch_asins ba ON ba.asin = d.asin AND ba.batch_id = ?

`asin_data` 是**每个 ASIN 一行的最新态**，`batch_id` 只回答「属不属于这批」，
**不参与取哪一行**。于是这批采失败的 ASIN —— 只要它以前采过 —— 照样命中
JOIN，返回**上一次的旧行**，而响应里一个字段都没有能看出它的年龄。
消费侧摄进自己的库、盖上一个新鲜的接收时间，陈旧数据就此看起来很新鲜。

CSV/xlsx 批次导出早就有防护（`data_source` 列写「历史产品库数据，本次未更新」），
JSON 没有。本轮把同名的三列补给 JSON。

两个后端都要跑：这是 `PUBLIC_API` 对等断言覆盖的一对补齐方法。
"""
from __future__ import annotations

import unittest

from tests.golden.harness import isolated_server


def _push(c, batch, asins, zip_code="10001"):
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
        "current_price": price, "stock_status": "In Stock",
        "crawl_time": "2026-08-19T10:00:00Z", "site": "US", "zip_code": "10001"})


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


def _items(c, batch_id):
    return c.get("/api/results", params={"batch_id": batch_id}).json()["items"]


class BatchStatusFieldsTests(unittest.TestCase):

    def test_stale_row_is_now_distinguishable(self):
        """**这一条对应那个洞。**

        同一个 ASIN：第一批采成功（19.99），第二批采失败。第二批查出来的
        仍然是 19.99 那行（这是既有行为，不改），但现在**能看出来**它不是
        本批采的 —— `batch_task_status == "failed"`。
        """
        asin = "B0STALE001"
        with isolated_server() as (c, _):
            first = _push(c, "st_first", [asin])
            _ok(c, "st_first", first[0], "19.99")
            second = _push(c, "st_second", [asin])
            _fail(c, "st_second", second[0])
            items = _items(c, second[0]["batch_id"])

        hit = [i for i in items if i["asin"] == asin]
        self.assertTrue(hit, "前置条件不成立：旧行都没返回")
        row = hit[0]
        self.assertEqual(row["current_price"], "19.99",
                         "前置条件不成立：拿到的不是上一批那行")
        self.assertEqual(
            row["batch_task_status"], "failed",
            "旧行仍然分辨不出来 —— 这正是本轮要补的字段")

    def test_fresh_row_says_done(self):
        with isolated_server() as (c, _):
            tasks = _push(c, "fresh", ["B0FRESH001"])
            _ok(c, "fresh", tasks[0], "24.50")
            items = _items(c, tasks[0]["batch_id"])

        self.assertEqual(items[0]["batch_task_status"], "done")
        self.assertEqual(items[0]["batch_has_asin_data"], 1)

    def test_all_three_fields_are_present(self):
        """字段名与 CSV 导出那条 SQL 逐字相同 —— 同一条信息不许有两个名字。"""
        with isolated_server() as (c, _):
            tasks = _push(c, "shape", ["B0SHAPE001"])
            _ok(c, "shape", tasks[0], "1.00")
            row = _items(c, tasks[0]["batch_id"])[0]

        for f in ("batch_task_status", "batch_has_asin_data",
                  "batch_asin_data_updated_at"):
            self.assertIn(f, row, f"少了 {f}")
        self.assertEqual(row["batch_asin_data_updated_at"], row["updated_at"],
                         "它就是这一行的 updated_at，别另算一个")

    def test_fields_absent_without_batch_id(self):
        """不带 batch_id 时**不加**这三列。

        没有批次就没有「本次任务」可言，给一个 null 只会让消费侧以为
        「这批没跑过」。而且这样一来既有的无 batch_id 调用方（黄金基线里
        那几步）响应逐字节不变。
        """
        with isolated_server() as (c, _):
            tasks = _push(c, "nobatch", ["B0NOBATC01"])
            _ok(c, "nobatch", tasks[0], "1.00")
            row = c.get("/api/results").json()["items"][0]

        for f in ("batch_task_status", "batch_has_asin_data",
                  "batch_asin_data_updated_at"):
            self.assertNotIn(f, row, f"不带 batch_id 时不该有 {f}")

    def test_never_scraped_asin_is_absent_entirely(self):
        """⚠ 这是本端点与 CSV 导出**真正的**差别，不是补三列能抹平的。

        `get_results` 以 `asin_data` 为驱动表走 INNER JOIN，所以这批里
        **一次都没采过**的 ASIN 整行都不会出现 —— 连「缺了一个」都看不出来。
        CSV 那条以 `batch_asins` 为驱动表 LEFT JOIN，那个 ASIN 会出现且
        `batch_has_asin_data = 0`。

        正因为如此，`batch_has_asin_data` 在本端点恒为 1。这条用例把这个
        差异钉住，免得有人看到「恒为 1」以为是写死的占位符而"顺手修掉"。
        """
        with isolated_server() as (c, _):
            tasks = _push(c, "gap", ["B0GAPOK001", "B0GAPNONE1"])
            by = {t["asin"]: t for t in tasks}
            _ok(c, "gap", by["B0GAPOK001"], "5.00")
            # B0GAPNONE1 永不提交结果
            items = _items(c, tasks[0]["batch_id"])

        got = {i["asin"] for i in items}
        self.assertEqual(got, {"B0GAPOK001"},
                         "从没采过的 ASIN 不该出现在 /api/results 里")
        self.assertTrue(all(i["batch_has_asin_data"] == 1 for i in items),
                        "INNER JOIN 决定了它恒为 1")


if __name__ == "__main__":
    unittest.main()
