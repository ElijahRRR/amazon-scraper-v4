"""定时任务只有 `/api/schedules` 一族入口（旧 `auto-scrape` 族已删除）。

------------------------------------------------------------------------
背景
------------------------------------------------------------------------
曾经并存两族端点，**读写同一份** ``_runtime_settings["auto_scrape_schedules"]``：

    /api/schedules*              按 id 寻址，POST 收 multipart（必须带文件）
    /api/auto-scrape/schedules*  按数组下标寻址，POST 收 JSON（无文件=全库）

于是"定时采集全库"这一个能力只存在于旧族，而旧族又不收 ``name`` /
``interval_days``，前端被迫先 POST 旧端点、再立刻 PUT 新端点补字段——两次
请求之间那条定时任务是以错误的名字和间隔存在的。

下标寻址还有个更实际的隐患：列表是共享可变状态，别处删掉一条，剩下所有
下标整体前移，``PUT /api/auto-scrape/schedules/{index}`` 就会改到**另一条**
定时任务上。

现在合并成一族：``POST /api/schedules`` 的 ``file`` 改为可选，不带即全库模式。

写成 ``unittest.TestCase``：``unittest discover`` 只认 TestCase 子类。
"""
from __future__ import annotations

import unittest

from tests.golden.harness import isolated_server

_LEGACY = "/api/auto-scrape/schedules"


def _mk(client, **form):
    form.setdefault("time", "03:30")
    form.setdefault("interval_days", "1")
    form.setdefault("name", "")
    form.setdefault("needs_screenshot", "false")
    files = form.pop("_files", None)
    return client.post("/api/schedules", data=form, files=files)


class SchedulesSingleEndpointTests(unittest.TestCase):

    def test_legacy_family_is_gone(self):
        """四条旧端点必须全部不可达，否则"合并"只是又多了一族。"""
        with isolated_server() as (client, _ctx):
            self.assertEqual(client.get(_LEGACY).status_code, 404)
            self.assertEqual(client.post(_LEGACY, json={"time": "01:00"}).status_code, 404)
            self.assertEqual(client.put(f"{_LEGACY}/0", json={"enabled": False}).status_code, 404)
            self.assertEqual(client.delete(f"{_LEGACY}/0").status_code, 404)

    def test_no_file_means_whole_library(self):
        """旧族唯一的独有能力：不带文件 = 定时采集全库。

        ``source_file`` 为空串正是 ``_auto_scrape_scheduler`` 判定"走
        ``db.get_all_asins()``"的信号，所以这里钉的是空串而不是 None。
        """
        with isolated_server() as (client, _ctx):
            r = _mk(client, time="03:30", interval_days="2")
            self.assertEqual(r.status_code, 200, r.text)
            sched = r.json()["schedule"]

        self.assertEqual(sched["source_file"], "")
        self.assertEqual(sched["asin_count"], 0)
        # 旧端点写死 interval_days=1、name="全库采集-{time}"；现在两者都可控，
        # 但不给 name 时默认名要保持旧端点那个（UI 上认得出来）
        self.assertEqual(sched["interval_days"], 2)
        self.assertEqual(sched["name"], "全库采集-03:30")

    def test_no_file_still_honors_name_and_interval(self):
        """这正是旧端点做不到、前端才要补一次 PUT 的那两个字段。"""
        with isolated_server() as (client, _ctx):
            r = _mk(client, name="我的全库任务", interval_days="7")
            sched = r.json()["schedule"]

        self.assertEqual(sched["name"], "我的全库任务")
        self.assertEqual(sched["interval_days"], 7)

    def test_with_file_still_works(self):
        with isolated_server() as (client, _ctx):
            r = _mk(client, name="带文件的",
                    _files={"file": ("a.txt", b"B0TEST0001\nB0TEST0002\n", "text/plain")})
            self.assertEqual(r.status_code, 200, r.text)
            sched = r.json()["schedule"]

        self.assertTrue(sched["source_file"])
        self.assertEqual(sched["asin_count"], 2)
        self.assertEqual(sched["name"], "带文件的")

    def test_empty_filename_part_counts_as_no_file(self):
        """浏览器在"选过文件又清空"时仍会发一个 filename 为空的 part。

        只判 ``file is None`` 会把它当成"带了文件"，然后拿空内容去抽 ASIN、
        抽不到、回 400 —— 用户看到的是"创建失败"，而他其实想建的是全库任务。
        """
        with isolated_server() as (client, _ctx):
            r = _mk(client, _files={"file": ("", b"", "application/octet-stream")})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["schedule"]["source_file"], "")

    def test_real_file_without_valid_asins_is_still_rejected(self):
        """"文件可选"不等于"文件内容不再校验"——带了文件就得有 ASIN。"""
        with isolated_server() as (client, _ctx):
            r = _mk(client, _files={"file": ("bad.txt", b"no asins here\n", "text/plain")})
            self.assertEqual(r.status_code, 400, r.text)

    def test_run_now_works_for_whole_library_schedules(self):
        """全库定时任务点「立即执行」必须真能跑起来。

        修复前 ``api_run_schedule_now`` 无条件走
        ``_extract_asins_from_file(source_file)``，而全库任务的 ``source_file``
        是空串 -> 恒返回空列表 -> 400「ASIN 文件为空或不存在」。也就是说这类
        任务**自动触发是好的、手动触发永远失败**，两条路径对"空 source_file"
        的理解不一致。现在两边共用 ``_resolve_asins``。

        注意「全库」= ``asin_data`` 里已有结果的 ASIN（``db.get_all_asins()``），
        不是待采任务，所以这里得先真的跑完一个 ASIN 才有东西可采。
        """
        with isolated_server() as (client, _ctx):
            sched = _mk(client, time="03:30").json()["schedule"]

            # 库里还没有任何采集结果 -> 明确报"全库为空"，而不是"文件不存在"
            r = client.post(f"/api/schedules/{sched['id']}/run")
            self.assertEqual(r.status_code, 400, r.text)
            self.assertIn("全库", r.json()["detail"])

            # 走完整链路让 asin_data 里有一条结果
            up = client.post(
                "/api/upload",
                files={"file": ("s.txt", b"B0RUNNOW01\n", "text/plain")},
                data={"batch_name": "seed_run_now", "zip_code": "10001",
                      "needs_screenshot": "false"})
            self.assertEqual(up.status_code, 200, up.text)
            task = client.get("/api/tasks/pull",
                              params={"worker_id": "w-run-now", "count": 1}).json()["tasks"][0]
            sub = client.post("/api/tasks/result", json={
                "task_id": task["id"], "batch_id": task["batch_id"],
                "worker_id": "w-run-now", "lease_epoch": task["lease_epoch"],
                "success": True, "asin": task["asin"], "title": "Run Now Seed",
                "current_price": "$1.00",
            })
            self.assertEqual(sub.status_code, 200, sub.text)

            # 现在全库非空，立即执行应当成功并真的建出一个批次
            r2 = client.post(f"/api/schedules/{sched['id']}/run")
            self.assertEqual(r2.status_code, 200, r2.text)
            body = r2.json()

        self.assertTrue(body["ok"])
        self.assertGreaterEqual(body["asin_count"], 1)
        self.assertTrue(body["batch_name"].startswith("auto_"))

    def test_id_addressing_still_works_for_both_kinds(self):
        """合并后所有定时任务都按 id 寻址，不再有下标错位的问题。"""
        with isolated_server() as (client, _ctx):
            whole = _mk(client, time="03:30").json()["schedule"]
            withfile = _mk(client, time="04:00",
                           _files={"file": ("a.txt", b"B0TEST0001\n", "text/plain")}
                           ).json()["schedule"]

            # 先删前一条，再按 id 改后一条：下标寻址在这里就会改错对象
            self.assertEqual(client.delete(f"/api/schedules/{whole['id']}").status_code, 200)
            r = client.put(f"/api/schedules/{withfile['id']}", json={"name": "改过的"})
            self.assertEqual(r.status_code, 200, r.text)

            remaining = client.get("/api/schedules").json()["schedules"]

        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["id"], withfile["id"])
        self.assertEqual(remaining[0]["name"], "改过的")


if __name__ == "__main__":
    unittest.main()
