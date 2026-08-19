"""每一个"可能让批次完成"的写点都要给完成检测入队。

------------------------------------------------------------------------
它补的洞
------------------------------------------------------------------------
一个批次算完成要**任务采完 + 截图也完**（`get_batch_completion_status`）。
在这之前只有 `POST /api/tasks/result/batch` 一处入队，于是「最后一张截图
上传完成」这个真正让批次完成的时刻**没有人入队**，只能等
`server/app.py:_timeout_loop` 那个 **30 秒一轮、`LIMIT 30`** 的兜底扫描。

兜底扫描的第二个问题是真的会丢：`LIMIT 30` 是**硬截断**，按
`updated_at DESC, id DESC` 取前 30。同时 running 的批次超过 30 个时，
一个长期 running 的老批次会被新批次一直挤在窗口外 —— **饿死**，不是慢。

本文件对每个写点各钉一条。`_completion_watcher` 在黄金夹具里是 no-op
（`tests/golden/harness.py:_PATCHED_LOOPS`），所以 set 里的东西不会被消费掉，
可以直接观察。
"""
from __future__ import annotations

import unittest

from tests.golden.harness import isolated_server


def _srv():
    from server import app as _s
    return _s


def _fresh_set():
    """清空并返回当前那个 set 对象。

    ⚠ 取的是 `server.app` 上的**属性**，不是 from-import 的快照 ——
    夹具会整体替换这个属性（见 server/api/worker_queue.py 承重约束 2）。
    """
    s = _srv()._completion_check_set
    s.clear()
    return s


def _push(c, batch, asins, screenshot=False):
    c.post("/api/upload",
           files={"file": ("s.txt", "\n".join(asins).encode(), "text/plain")},
           data={"batch_name": batch, "zip_code": "10001",
                 "needs_screenshot": "true" if screenshot else "false"})
    return c.get("/api/tasks/pull",
                 params={"worker_id": f"w-{batch}", "count": len(asins) + 5}
                 ).json()["tasks"]


_PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)


class CompletionEnqueueTests(unittest.TestCase):

    def test_screenshot_upload_enqueues(self):
        """**这一条对应那个洞。** 最后一张截图落地 = 批次真正完成的那一刻。"""
        with isolated_server() as (c, _):
            tasks = _push(c, "ss_up", ["B0SSUP0001"], screenshot=True)
            bid = tasks[0]["batch_id"]
            c.post("/api/tasks/result", json={
                "task_id": tasks[0]["id"], "batch_id": bid,
                "worker_id": "w-ss_up", "lease_epoch": tasks[0]["lease_epoch"],
                "success": True, "asin": "B0SSUP0001", "title": "T",
                "current_price": "1.00", "stock_status": "In Stock",
                "crawl_time": "2026-08-19T10:00:00Z", "site": "US",
                "zip_code": "10001"})
            s = _fresh_set()          # 清掉结果提交那次的入队，只看截图这一次
            r = c.post("/api/tasks/screenshot",
                       files={"file": ("x.png", _PNG, "image/png")},
                       data={"asin": "B0SSUP0001", "batch_name": "ss_up",
                             "worker_id": "w-ss_up"})
            self.assertEqual(r.status_code, 200, r.text)
            got = set(s)

        self.assertIn(bid, got,
                      "截图上传成功没有入队 —— 批次要等 30 秒兜底扫描才收尾")

    def test_screenshot_fail_enqueues(self):
        """`failed` 与 `done` 一样是**终态**，同样能让"截图还没完"变成"完了"。"""
        with isolated_server() as (c, _):
            tasks = _push(c, "ss_fail", ["B0SSFAI001"], screenshot=True)
            bid = tasks[0]["batch_id"]
            s = _fresh_set()
            r = c.post("/api/tasks/screenshot/fail",
                       json={"asin": "B0SSFAI001", "batch_name": "ss_fail",
                             "error": "render timeout"})
            self.assertEqual(r.status_code, 200, r.text)
            got = set(s)

        self.assertIn(bid, got, "截图失败没有入队")

    def test_single_result_endpoint_enqueues(self):
        """worker 的**回退路径**（批量接口连续失败时走它）。

        恰恰是批量接口不好使的时候才走到这条 —— 那时更不该让完成检测
        只剩兜底扫描。
        """
        with isolated_server() as (c, _):
            tasks = _push(c, "single", ["B0SINGLE01"])
            bid = tasks[0]["batch_id"]
            s = _fresh_set()
            c.post("/api/tasks/result", json={
                "task_id": tasks[0]["id"], "batch_id": bid,
                "worker_id": "w-single", "lease_epoch": tasks[0]["lease_epoch"],
                "success": True, "asin": "B0SINGLE01", "title": "T",
                "current_price": "1.00", "stock_status": "In Stock",
                "crawl_time": "2026-08-19T10:00:00Z", "site": "US",
                "zip_code": "10001"})
            got = set(s)

        self.assertIn(bid, got, "单条结果端点没有入队")

    def test_batch_result_endpoint_still_enqueues(self):
        """既有行为不许回退 —— 收口成助手时最容易顺手改坏的就是它。"""
        with isolated_server() as (c, _):
            tasks = _push(c, "bulk", ["B0BULK0001"])
            bid = tasks[0]["batch_id"]
            s = _fresh_set()
            c.post("/api/tasks/result/batch", json={"results": [{
                "task_id": tasks[0]["id"], "batch_id": bid,
                "worker_id": "w-bulk", "lease_epoch": tasks[0]["lease_epoch"],
                "success": True, "asin": "B0BULK0001", "title": "T",
                "current_price": "1.00", "stock_status": "In Stock",
                "crawl_time": "2026-08-19T10:00:00Z", "site": "US",
                "zip_code": "10001"}]})
            got = set(s)

        self.assertIn(bid, got, "批量结果端点不再入队了")

    def test_enqueue_failure_never_breaks_the_write_path(self):
        """入队炸了也不许影响采集。

        worker 提交失败会触发重试，而重试解决不了"入队失败"这种问题 ——
        只会把一次内存错误放大成一轮重复采集。
        """
        srv = _srv()
        with isolated_server() as (c, _):
            tasks = _push(c, "boom", ["B0BOOM0001"])

            class _Exploding(set):
                def add(self, item):
                    raise RuntimeError("入队爆炸（用例故意的）")

            saved = srv._completion_check_set
            srv._completion_check_set = _Exploding()
            try:
                r = c.post("/api/tasks/result", json={
                    "task_id": tasks[0]["id"], "batch_id": tasks[0]["batch_id"],
                    "worker_id": "w-boom", "lease_epoch": tasks[0]["lease_epoch"],
                    "success": True, "asin": "B0BOOM0001", "title": "T",
                    "current_price": "1.00", "stock_status": "In Stock",
                    "crawl_time": "2026-08-19T10:00:00Z", "site": "US",
                    "zip_code": "10001"})
            finally:
                srv._completion_check_set = saved

        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"], "入队异常把结果提交带崩了")

    def test_helper_reads_the_attribute_not_a_snapshot(self):
        """助手必须走 `_s._completion_check_set` 属性访问。

        夹具会**整体替换**这个属性；from-import 拿到的旧 set 之后没人读，
        完成通知静默失效 —— 一个不会报错、只会"通知不来"的故障。
        本条把替换后仍然写得进去这件事钉住。
        """
        srv = _srv()
        with isolated_server() as (c, _):
            tasks = _push(c, "swap", ["B0SWAP0001"])
            bid = tasks[0]["batch_id"]
            replacement = set()
            saved = srv._completion_check_set
            srv._completion_check_set = replacement      # 整体替换
            try:
                c.post("/api/tasks/result", json={
                    "task_id": tasks[0]["id"], "batch_id": bid,
                    "worker_id": "w-swap", "lease_epoch": tasks[0]["lease_epoch"],
                    "success": True, "asin": "B0SWAP0001", "title": "T",
                    "current_price": "1.00", "stock_status": "In Stock",
                    "crawl_time": "2026-08-19T10:00:00Z", "site": "US",
                    "zip_code": "10001"})
                got = set(replacement)
            finally:
                srv._completion_check_set = saved

        self.assertIn(bid, got, "写进了旧 set —— 助手里用了 from-import 的快照")


if __name__ == "__main__":
    unittest.main()
