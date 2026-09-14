"""F-010 关键词搜索采集的端到端串联 —— 与 `tests/test_seller_api.py` 同位。

------------------------------------------------------------------------
为什么需要这份文件
------------------------------------------------------------------------
这一族端点**黄金基线一步都没有**，而且补不进去：`POST /api/search-batches`
的响应含 `keywords_{%Y%m%d_%H%M%S}` 批次名，不被 `harness.py` 的 `_TS_RE`
覆盖（那条正则只擦"值"里的时间戳，擦不掉嵌在批次名中间的那一段），逐次不同。

替代网就是这里：显式传 `batch_name` 消掉那个唯一的非确定源，把
create → pull → search-result → progress → discoveries 的串联钉死，
两个后端都跑（与 test_seller_api.py 同一套夹具约定）。

------------------------------------------------------------------------
除了串联，这里还钉住三条**只会静默失效**的不变量
------------------------------------------------------------------------
1. **筛选参数必须到得了 worker。** 建批次时写进 `task_meta.search`、
   `pull_tasks` 必须把它原样带出来。断链了不会报错 —— worker 拿不到参数就
   退化成裸关键词搜索，批次名/进度/发现数全部正常，只有数据是错的。
2. **收结果时 `task_meta` 是合并不是覆盖。** 直接 SET 会把筛选参数抹掉，
   于是"这批数据当初按什么条件采的"永久丢失，而当下没有任何症状。
3. **广告位与自然位必须能分开。** `is_sponsored` 丢了的话 `rank` 这一列
   就变成一个看起来很正常、实际混了广告的数字。
"""
from __future__ import annotations

import json
import unittest

from tests.golden.harness import isolated_server


class SearchApiFlowTests(unittest.TestCase):
    """create → pull → search-result → progress → discoveries 全链路。

    ``isolated_server()`` 产出 ``(client, ctx)``，每个用例一套全新的库
    （与 test_seller_api.py 同约定）。用 setUp/tearDown 手工进出上下文，
    而不是每个用例里写一层 ``with`` —— 本文件的用例都要用到 client，
    没有"只用一半"的情况。
    """

    def setUp(self):
        self._cm = isolated_server()
        self.client, self._ctx = self._cm.__enter__()

    def tearDown(self):
        self._cm.__exit__(None, None, None)

    # ---------------------------------------------------------- 建批次

    def test_create_rejects_bad_params_before_any_write(self):
        """非法筛选参数必须在**建批次之前**被拒 —— 400，且不留下半个批次。

        顺序在这里是承重的：先建批次再校验的话，一次手滑的 max_pages=999
        会留下一个空批次挂在列表里，而调用方看到的是 400，以为什么都没发生。
        """
        bad_cases = [
            ({"keywords": ["x"], "min_price": 50, "max_price": 10}, "min_price"),
            ({"keywords": ["x"], "max_pages": 999}, "max_pages"),
            ({"keywords": ["x"], "delivery": "no_such_thing"}, "delivery"),
            ({"keywords": ["x"], "domain": "evil.example.com"}, "站点"),
            ({"keywords": ["x"], "rh_extra": "a&b=c"}, "rh_extra"),
            ({"keywords": ["   ", ""]}, "关键词"),
            ({}, "keywords"),
        ]
        before = self.client.get("/api/batches").json()
        n_before = len(before.get("batches", before) if isinstance(before, dict) else before)
        for body, needle in bad_cases:
            with self.subTest(body=body):
                r = self.client.post("/api/search-batches", json=body)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertIn(needle, r.text)
        after = self.client.get("/api/batches").json()
        n_after = len(after.get("batches", after) if isinstance(after, dict) else after)
        self.assertEqual(n_before, n_after, "被拒的请求不该留下批次")

    def test_keywords_accepts_string_and_dedupes_case_insensitively(self):
        r = self.client.post("/api/search-batches", json={
            "batch_name": "kwstr",
            "keywords": "wireless mouse\nWireless Mouse, usb  hub",
        })
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # "wireless mouse" 与 "Wireless Mouse" 是同一个词；内部连续空白被压成一个
        self.assertEqual(body["total_keywords"], 2, body)
        self.assertEqual(body["inserted_tasks"], 2, body)

    # ---------------------------------------------------------- 全链路

    def test_full_flow_carries_params_and_ranks(self):
        create = self.client.post("/api/search-batches", json={
            "batch_name": "kwflow",
            "keywords": ["wireless mouse"],
            "min_price": 10,
            "max_price": 50,
            "delivery": "prime",
            "max_pages": 3,
            "discover_mode": "with_detail",
            "zip_code": "90001",
        })
        self.assertEqual(create.status_code, 200, create.text)
        created = create.json()
        batch_id = created["batch_id"]
        self.assertEqual(created["inserted_tasks"], 1)
        self.assertEqual(created["zip_code"], "90001")
        self.assertEqual(created["search_params"]["delivery"], "prime")

        # ---- 不变量 1：筛选参数要能被 worker 拉到 ----
        pulled = self.client.get("/api/tasks/pull",
                                 params={"worker_id": "w-kw", "count": 5})
        self.assertEqual(pulled.status_code, 200, pulled.text)
        tasks = pulled.json()["tasks"]
        self.assertEqual(len(tasks), 1, tasks)
        task = tasks[0]
        self.assertEqual(task["task_type"], "discover_search")
        self.assertEqual(task["asin"], "wireless mouse", "关键词存在 tasks.asin 上")
        meta = json.loads(task["task_meta"])
        self.assertEqual(meta["search"]["min_price"], 10.0)
        self.assertEqual(meta["search"]["max_price"], 50.0)
        self.assertEqual(meta["search"]["delivery"], "prime")
        self.assertEqual(meta["search"]["max_pages"], 3)

        # 这份参数拼出来的 URL 必须真的带上筛选 —— 光断言 dict 里有键不够，
        # 键在但拼 URL 时被漏掉正是这条不变量要防的事。
        from common.core.searchurl import build_search_url
        url = build_search_url(task["asin"], 2, meta["search"])
        self.assertIn("p_36%3A1000-5000", url)      # 价格区间（分）
        self.assertIn("p_85%3A2470955011", url)     # Prime
        self.assertIn("page=2", url)

        # ---- 提交发现结果（含一个广告位） ----
        submit = self.client.post("/api/tasks/search-result", json={
            "task_id": task["id"],
            "batch_id": batch_id,
            "worker_id": "w-kw",
            "lease_epoch": task["lease_epoch"],
            "keyword": "wireless mouse",
            "items": [
                {"asin": "B0KW000001", "title": "Ad one", "price": "$11.00",
                 "image": "http://i/1.jpg", "page": 1, "rank": 1, "sponsored": True},
                {"asin": "B0KW000002", "title": "Real one", "price": "$22.00",
                 "image": "http://i/2.jpg", "page": 1, "rank": 2, "sponsored": False},
                {"asin": "B0KW000003", "title": "Page two", "price": "$33.00",
                 "image": "", "page": 2, "rank": 1, "sponsored": False},
            ],
            "meta": {"pages_scanned": 2, "truncated": False, "sponsored_skipped": 0},
        })
        self.assertEqual(submit.status_code, 200, submit.text)
        res = submit.json()
        self.assertTrue(res["accepted"])
        self.assertEqual(res["discovered"], 3)
        self.assertEqual(res["detail_tasks_created"], 3,
                         "with_detail 必须派生等量的详情任务")

        # ---- 重放同一条必须 stale（lease 门） ----
        replay = self.client.post("/api/tasks/search-result", json={
            "task_id": task["id"], "batch_id": batch_id, "worker_id": "w-kw",
            "lease_epoch": task["lease_epoch"], "keyword": "wireless mouse",
            "items": [{"asin": "B0KW000009"}], "meta": {},
        })
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["stale"])
        self.assertFalse(replay.json()["accepted"])

        # ---- progress ----
        prog = self.client.get(f"/api/search-batches/{batch_id}/progress")
        self.assertEqual(prog.status_code, 200, prog.text)
        p = prog.json()
        self.assertEqual(p["discover"]["done"], 1)
        self.assertEqual(p["discover"]["total"], 1)
        self.assertEqual(p["keywords"], 1)
        self.assertEqual(p["detail"]["pending"], 3)
        self.assertEqual(p["discovered_asins"], 3)
        self.assertEqual(p["discover_mode"], "with_detail")

        # ---- discoveries：默认按 (page, rank) 原始顺序 ----
        disc = self.client.get(f"/api/search-batches/{batch_id}/discoveries")
        self.assertEqual(disc.status_code, 200, disc.text)
        items = disc.json()["items"]
        self.assertEqual([i["asin"] for i in items],
                         ["B0KW000001", "B0KW000002", "B0KW000003"],
                         "必须是搜索结果里的原始顺序，不是入库时间序")
        self.assertEqual([(i["page_no"], i["rank"]) for i in items],
                         [(1, 1), (1, 2), (2, 1)])

        # ---- 不变量 3：广告位可分离 ----
        self.assertEqual([bool(i["is_sponsored"]) for i in items],
                         [True, False, False])
        natural = self.client.get(f"/api/search-batches/{batch_id}/discoveries",
                                  params={"include_sponsored": "false"}).json()["items"]
        self.assertEqual([i["asin"] for i in natural],
                         ["B0KW000002", "B0KW000003"])

        # ---- 不变量 2：task_meta 合并，不是覆盖 ----
        pulled2 = self.client.get("/api/tasks/pull",
                                  params={"worker_id": "w-kw2", "count": 10}).json()["tasks"]
        self.assertTrue(all(t["task_type"] == "asin" for t in pulled2),
                        "发现任务已 done，再拉只应拿到派生的详情任务")
        self.assertEqual({t["zip_code"] for t in pulled2}, {"90001"},
                         "派生的详情任务必须继承发现任务的邮编")

    def test_discover_only_creates_no_detail_tasks(self):
        create = self.client.post("/api/search-batches", json={
            "batch_name": "kwonly",
            "keywords": ["gaming keyboard"],
            "discover_mode": "discover_only",
        }).json()
        batch_id = create["batch_id"]
        task = self.client.get("/api/tasks/pull",
                               params={"worker_id": "w-only", "count": 5}).json()["tasks"][0]
        res = self.client.post("/api/tasks/search-result", json={
            "task_id": task["id"], "batch_id": batch_id, "worker_id": "w-only",
            "lease_epoch": task["lease_epoch"], "keyword": "gaming keyboard",
            "items": [{"asin": "B0KW00ONLY", "title": "t", "page": 1, "rank": 1}],
            "meta": {"pages_scanned": 1},
        }).json()
        self.assertTrue(res["accepted"])
        self.assertEqual(res["discovered"], 1)
        self.assertEqual(res["detail_tasks_created"], 0,
                         "discover_only 不该派生任何详情任务")
        prog = self.client.get(f"/api/search-batches/{batch_id}/progress").json()
        self.assertEqual(prog["detail"]["total"], 0)

    def test_same_name_appends_instead_of_409(self):
        """同名批次在这里是**追加**语义（与 /api/upload 的 409 有意不同）。"""
        first = self.client.post("/api/search-batches", json={
            "batch_name": "kwappend", "keywords": ["aaa"]}).json()
        second = self.client.post("/api/search-batches", json={
            "batch_name": "kwappend", "keywords": ["aaa", "bbb"]}).json()
        self.assertEqual(first["batch_id"], second["batch_id"])
        self.assertEqual(second["inserted_tasks"], 1,
                         "重复的 aaa 被 ON CONFLICT 吞掉，只新增 bbb")
        prog = self.client.get(
            f"/api/search-batches/{first['batch_id']}/progress").json()
        self.assertEqual(prog["discover"]["total"], 2)

    # ---------------------------------------------------------- 选项端点

    def test_search_options_lists_only_configured_delivery(self):
        r = self.client.get("/api/search-options")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["domain"], "www.amazon.com")
        self.assertIn("prime", body["delivery"])
        self.assertIn("price_asc", body["sort"])

        # 没有内置对照表的站点：delivery 必须是**空列表**而不是抄一份 .com 的。
        # 抄过去的话用户会选到一个在该站点根本不存在的 refinement 节点，
        # Amazon 静默忽略 -> 采回一批没筛过的数据，没有任何一侧会报错。
        de = self.client.get("/api/search-options",
                             params={"domain": "www.amazon.de"}).json()
        self.assertEqual(de["delivery"], [])

        bad = self.client.get("/api/search-options", params={"domain": "evil.com"})
        self.assertEqual(bad.status_code, 400)
