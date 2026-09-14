"""F-011 浏览器插件对接端点的替代网 —— 与 test_seller_api / test_search_api 同位。

黄金基线同样覆盖不到（`ext_{%Y%m%d_%H%M%S}` 批次名逐次不同），所以这里是
唯一的网。显式传 `batch_name` 消掉那个非确定源。

------------------------------------------------------------------------
重点钉的是**这一族独有**的那条语义：同名批次 = 追加
------------------------------------------------------------------------
`/api/upload` 与 `/api/batches` 撞名返回 409，那是有意的；而插件翻页采集
必须一页一推、全部落进同一批次。两条语义相反，共存在同一个服务里 ——
所以两边都要有断言钉住，不然哪天有人"统一"一下，坏掉的是翻页采集，
而症状是"控制台里多出来 10 个批次"，不会有任何报错。
"""
from __future__ import annotations

import unittest

from tests.golden.harness import isolated_server

_SELLER_A = "A2L77EE7U53NWQ"
_SELLER_B = "A1PA6795UKMFR9"


class ExtensionCollectTests(unittest.TestCase):

    def setUp(self):
        self._cm = isolated_server()
        self.client, self._ctx = self._cm.__enter__()

    def tearDown(self):
        self._cm.__exit__(None, None, None)

    def _collect(self, **body):
        return self.client.post("/api/extension/collect", json=body)

    # ---------------------------------------------------------- ping

    def test_ping_reports_status_without_leaking_business_data(self):
        r = self.client.get("/api/extension/ping")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["service"], "amazon-scraper-v4")
        self.assertIn("default_zip_code", body)
        self.assertEqual(body["workers_online"], 0)
        # 这个端点会在插件"配置完成之前"就被调用，是唯一一个裸奔的探针。
        # 多回一个业务字段就多一份泄露面 —— 钉住它不回批次/ASIN 之类的东西。
        leaky = {"batches", "asins", "results", "tasks", "batch_list"}
        self.assertEqual(leaky & set(body), set(), f"ping 回了业务数据: {body}")

    # ---------------------------------------------------------- 追加语义

    def test_same_batch_name_appends_across_pages(self):
        """翻页采集的核心不变量：一页一推，全部落进同一批次。"""
        page1 = self._collect(batch_name="ext_pages",
                              asins=["B0EXT00001", "B0EXT00002"]).json()
        page2 = self._collect(batch_name="ext_pages",
                              asins=["B0EXT00003", "B0EXT00002"]).json()

        self.assertEqual(page1["asin_batch"]["batch_id"],
                         page2["asin_batch"]["batch_id"],
                         "同名必须命中同一个批次，而不是 409、也不是新建一个")
        self.assertTrue(page1["asin_batch"]["created"])
        self.assertFalse(page2["asin_batch"]["created"])
        self.assertEqual(page1["asin_batch"]["inserted_tasks"], 2)
        self.assertEqual(page2["asin_batch"]["inserted_tasks"], 1,
                         "重复的 B0EXT00002 被吞掉，只新增一个")

        status = self.client.get("/api/batches/ext_pages/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["stats"]["total"], 3)

    def test_upload_still_409s_on_duplicate_name(self):
        """反向断言：`/api/upload` 的撞名语义**没有**被这次改动带偏。"""
        files = {"file": ("a.txt", b"B0UPLOAD01\n", "text/plain")}
        first = self.client.post("/api/upload", files=files,
                                 data={"batch_name": "dup_check"})
        self.assertEqual(first.status_code, 200, first.text)
        second = self.client.post("/api/upload",
                                  files={"file": ("a.txt", b"B0UPLOAD02\n", "text/plain")},
                                  data={"batch_name": "dup_check"})
        self.assertEqual(second.status_code, 409, second.text)

    # ---------------------------------------------------------- 商品 + 卖家

    def test_sellers_go_into_a_separate_batch(self):
        """勾了"整店采集"时产生两个批次，不是一个。

        规模差三个数量级（勾的几个商品 vs 几家店的全部在售），混在一个批次里
        进度条会永远卡在个位数百分比，看不出自己勾的那几个采完没有。
        """
        r = self._collect(batch_name="ext_mixed",
                          asins=["B0EXT10001"],
                          seller_ids=[_SELLER_A, f"https://www.amazon.com/s?me={_SELLER_B}"])
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()

        ab, sb = body["asin_batch"], body["seller_batch"]
        self.assertEqual(ab["batch_name"], "ext_mixed")
        self.assertEqual(sb["batch_name"], "ext_mixed_sellers")
        self.assertNotEqual(ab["batch_id"], sb["batch_id"])
        self.assertEqual(ab["inserted_tasks"], 1)
        self.assertEqual(sb["inserted_tasks"], 2, "裸 ID 与店铺 URL 两种形态都要认")

        # 卖家批次要能用 F-009 的专属进度端点（batch_type 得是 seller_discovery）
        prog = self.client.get(f"/api/seller-batches/{sb['batch_id']}/progress")
        self.assertEqual(prog.status_code, 200, prog.text)
        self.assertEqual(prog.json()["discover"]["pending"], 2)
        self.assertEqual(prog.json()["discover_mode"], "with_detail")

    def test_asins_only_leaves_seller_batch_null(self):
        body = self._collect(batch_name="ext_asins", asins=["B0EXT20001"]).json()
        self.assertIsNone(body["seller_batch"])
        self.assertIsNotNone(body["asin_batch"])

    def test_sellers_only_leaves_asin_batch_null(self):
        body = self._collect(batch_name="ext_sellers", seller_ids=[_SELLER_A]).json()
        self.assertIsNone(body["asin_batch"])
        self.assertEqual(body["seller_batch"]["batch_name"], "ext_sellers_sellers")

    # ---------------------------------------------------------- 拒绝面

    def test_rejects_bad_input(self):
        cases = [
            ({}, 400, "不能都为空"),
            ({"asins": ["not-an-asin"]}, 400, "不能都为空"),
            ({"asins": "B0EXT30001"}, 400, "必须是数组"),
            ({"asins": ["B0EXT30001"], "zip_code": "abc"}, 400, "非法邮编"),
            ({"asins": ["B0EXT30001"], "seller_discover_mode": "nope"}, 400, "discover_mode"),
            ({"asins": ["B0EXT30001"] * 2001}, 400, "最多推送"),
            ({"seller_ids": [_SELLER_A] * 51}, 400, "最多推送"),
        ]
        for body, code, needle in cases:
            with self.subTest(body=str(body)[:60]):
                r = self._collect(**body)
                self.assertEqual(r.status_code, code, r.text)
                self.assertIn(needle, r.text)

    def test_batch_name_cannot_escape_the_screenshot_directory(self):
        """批次名会进截图落盘路径，路径穿越必须当场拒绝。

        `_safe_fs_component` 是**校验器不是净化器**（不合法返回 None）。
        这条用例同时守着"调用方没把 None 当字符串用"——那会是 500 而不是 400。
        """
        for bad in ["../../etc", "a/b", "a\\b", ".."]:
            with self.subTest(bad=bad):
                r = self._collect(batch_name=bad, asins=["B0EXT40001"])
                self.assertEqual(r.status_code, 400, r.text)
                self.assertIn("路径", r.text)

    def test_invalid_asins_are_counted_not_silently_dropped(self):
        """非法 ASIN 混在合法的里面时：合法的照采，非法的**计数回报**。

        静默丢掉的话，插件显示"已推送 10 个"而实际入队 7 个，
        差额没有任何地方能看出来。
        """
        body = self._collect(batch_name="ext_mixedvalid",
                             asins=["B0EXT50001", "garbage", "", "B0EXT50002"]).json()
        self.assertEqual(body["invalid_asins"], 2)
        self.assertEqual(body["asin_batch"]["submitted_asins"], 2)
        self.assertEqual(body["asin_batch"]["inserted_tasks"], 2)


class ResolveSellersTests(unittest.TestCase):

    def setUp(self):
        self._cm = isolated_server()
        self.client, self._ctx = self._cm.__enter__()

    def tearDown(self):
        self._cm.__exit__(None, None, None)

    def _collect_one(self, asin, seller_id, seller_name="Some Shop"):
        """把一个 ASIN 真采一遍，好让 asin_data 里有卖家信息。"""
        self.client.post("/api/extension/collect",
                         json={"batch_name": "rs_seed", "asins": [asin]})
        task = self.client.get("/api/tasks/pull",
                               params={"worker_id": "w-rs", "count": 10}).json()["tasks"][0]
        # ⚠ `/api/tasks/result` 的 body 是**平铺**的：handler 把 task_id /
        # batch_id / lease_epoch 三个 pop 掉，**剩下的整个 body 就是结果数据**。
        # 套一层 "data" 的话，落库的是一个只有 data 键的空壳，会被
        # `_is_parse_failure` 判成解析失败走 server_reject —— asin_data 里
        # 根本不会有这一行，下面 resolve-sellers 自然什么都查不到。
        #
        # title + brand 两个都得有效，也是 `_is_parse_failure` 的要求。
        r = self.client.post("/api/tasks/result", json={
            "task_id": task["id"], "batch_id": task["batch_id"],
            "worker_id": "w-rs", "lease_epoch": task["lease_epoch"], "success": True,
            "asin": asin, "title": "seeded product", "brand": "SeedBrand",
            "seller_id": seller_id, "seller_name": seller_name,
            "current_price": "1.00", "stock_status": "In Stock",
        })
        self.assertEqual(r.status_code, 200, r.text)
        return r

    def test_returns_only_known_sellers(self):
        self._collect_one("B0RS000001", "A1KNOWNSELLER")

        r = self.client.post("/api/extension/resolve-sellers",
                             json={"asins": ["B0RS000001", "B0RS000099"]})
        self.assertEqual(r.status_code, 200, r.text)
        sellers = r.json()["sellers"]
        self.assertEqual(sellers["B0RS000001"]["seller_id"], "A1KNOWNSELLER")
        # 库里没有的那个**不出现**，而不是回一个 null 占位 —— 这是补全，不是推测
        self.assertNotIn("B0RS000099", sellers)

    def test_na_seller_is_not_a_seller(self):
        """解析不出卖家时库里存的是字面量 "N/A"。它是"没采到"，不是卖家 ID。

        回给插件的话，用户会拿到一个采不出任何东西的整店任务，
        而界面上看起来一切正常。
        """
        self._collect_one("B0RS000002", "N/A", seller_name="N/A")
        r = self.client.post("/api/extension/resolve-sellers",
                             json={"asins": ["B0RS000002"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["sellers"], {})

    def test_rejects_oversized_and_malformed(self):
        r = self.client.post("/api/extension/resolve-sellers",
                             json={"asins": ["B0RS000001"] * 501})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("最多查询", r.text)

        r = self.client.post("/api/extension/resolve-sellers", json={"asins": "nope"})
        self.assertEqual(r.status_code, 400, r.text)

        # 全是非法 ASIN -> 空结果，不是报错（插件那边就是"没补上"，照常提交）
        r = self.client.post("/api/extension/resolve-sellers", json={"asins": ["junk"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"sellers": {}, "queried": 0})
