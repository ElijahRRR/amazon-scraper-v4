"""F-012 blocker 3：浏览器插件这条 ASIN 清单入口按站点采集。

两半，各管一侧：

* 服务端 —— `/api/extension/collect` 与 `/api/extension/resolve-sellers` 认请求体里
  **显式**的 `marketplace`（isolated_server + 当前 DB 后端；本机用 SQLite 跑）。
  任务行的 (zip_code, marketplace) 一律经 `GET /api/tasks/pull` 读出来 —— 那是
  worker 真正看到的东西，而且两个后端都能用，不用直连哪个库。
* 插件 —— `tests/extension_marketplace_spec.js`（纯 Node，不要 jsdom）：page.js 的站点
  纯函数，以及在假 `chrome` / 假 `fetch` 下真正加载 background.js、断言推给服务端的
  请求体。只在**没装 Node** 时 skip。

------------------------------------------------------------------------
钉得最死的是「老插件不带字段 = 美国站，逐字节不变」
------------------------------------------------------------------------
v1.1.0 的插件不会自己更新（解压安装，要用户去 chrome://extensions 点重新加载），
服务端上线后很长一段时间里推过来的都是不带 `marketplace` 的请求。它们的任务行、
响应的键集合、报错文案都必须与改造前完全一样 —— 下面 `UsInvariantTests` 那一组。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest

from tests.golden.harness import isolated_server

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SPEC = os.path.join(_REPO_ROOT, "tests", "extension_marketplace_spec.js")

_SELLER = "A2L77EE7U53NWQ"

#: v1.1.0 时代 `asin_batch` 的键集合（改造前 server/api/extension.py 的字面量）。
_US_ASIN_BATCH_KEYS = {"batch_id", "batch_name", "created", "submitted_asins",
                       "inserted_tasks", "zip_code", "status_url"}
_US_SELLER_BATCH_KEYS = {"batch_id", "batch_name", "discover_mode", "submitted_sellers",
                         "inserted_tasks", "status_url"}


class _ServerCase(unittest.TestCase):

    def setUp(self):
        self._cm = isolated_server()
        self.client, self._ctx = self._cm.__enter__()
        # pull 出来的任务就不再是 pending 了，所以拉过的都记在这里，按批次名取
        self._pulled = {}

    def tearDown(self):
        self._cm.__exit__(None, None, None)

    def _collect(self, **body):
        return self.client.post("/api/extension/collect", json=body)

    def _tasks(self, batch_name):
        """该批次的任务 [(asin, zip_code, marketplace, task_type)]，按 asin 排序。"""
        r = self.client.get("/api/tasks/pull", params={"worker_id": "w-mkt", "count": 500})
        self.assertEqual(r.status_code, 200, r.text)
        for t in r.json()["tasks"]:
            self._pulled.setdefault(t["batch_name"], []).append(
                (t["asin"], t["zip_code"], t["marketplace"], t["task_type"]))
        return sorted(self._pulled.get(batch_name, []))

    def _batch_exists(self, name):
        r = self.client.get(f"/api/batches/{name}/status")
        self.assertIn(r.status_code, (200, 404), r.text)
        return r.status_code == 200

    def _set_runtime_zip(self, zc):
        r = self.client.put("/api/settings", json={"zip_code": zc})
        self.assertEqual(r.status_code, 200, r.text)


class CollectMarketplaceTests(_ServerCase):
    """插件推加拿大站：任务按 amazon.ca 建、邮编按加拿大站取。"""

    def test_ca_push_creates_ca_tasks_with_site_default_zip(self):
        # 全局设置是美国邮编（默认 10001）：加拿大批次绝不能拿它去用
        r = self._collect(batch_name="ext_ca1", asins=["B0CA000001", "B0CA000002"],
                          marketplace="amazon.ca")
        self.assertEqual(r.status_code, 200, r.text)
        ab = r.json()["asin_batch"]
        self.assertEqual(ab["zip_code"], "K1V 7P8")
        self.assertEqual(ab["marketplace"], "amazon.ca")
        self.assertEqual(self._tasks("ext_ca1"), [
            ("B0CA000001", "K1V 7P8", "amazon.ca", "asin"),
            ("B0CA000002", "K1V 7P8", "amazon.ca", "asin"),
        ])

    def test_accepts_registry_spellings(self):
        for i, raw in enumerate(["AMAZON.CA", "www.amazon.ca", "https://www.amazon.ca/", " amazon.ca "]):
            with self.subTest(raw=raw):
                name = f"ext_spell{i}"
                r = self._collect(batch_name=name, asins=[f"B0SPELL00{i}"], marketplace=raw)
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(self._tasks(name), [(f"B0SPELL00{i}", "K1V 7P8", "amazon.ca", "asin")])

    def test_ca_zip_is_normalized_for_the_site(self):
        r = self._collect(batch_name="ext_ca_zip", asins=["B0CA000003"],
                          marketplace="amazon.ca", zip_code="m5v3l9")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["asin_batch"]["zip_code"], "M5V 3L9")
        self.assertEqual(self._tasks("ext_ca_zip"), [("B0CA000003", "M5V 3L9", "amazon.ca", "asin")])

    def test_ca_uses_runtime_zip_only_when_it_is_valid_for_the_site(self):
        self._set_runtime_zip("M5V 3L9")
        r = self._collect(batch_name="ext_ca_rt", asins=["B0CA000004"], marketplace="amazon.ca")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._tasks("ext_ca_rt"), [("B0CA000004", "M5V 3L9", "amazon.ca", "asin")])

    def test_us_zip_on_ca_push_is_400_and_writes_nothing(self):
        r = self._collect(batch_name="ext_ca_bad", asins=["B0CA000005"],
                          marketplace="amazon.ca", zip_code="10001")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("加拿大站", r.text)
        self.assertIn("K1V 7P8", r.text)
        self.assertFalse(self._batch_exists("ext_ca_bad"))

    def test_unsupported_or_malformed_site_is_400_and_creates_no_batch(self):
        cases = [
            ("amazon.de", "不支持的站点"),
            ("amazon.zz", "不支持的站点"),
            ("www.amazon.ca.evil.com", "不支持的站点"),
            (["amazon.ca"], "必须是字符串"),
            (1, "必须是字符串"),
        ]
        for i, (raw, needle) in enumerate(cases):
            with self.subTest(raw=raw):
                name = f"ext_bad_site{i}"
                r = self._collect(batch_name=name, asins=["B0BADSITE1"], seller_ids=[_SELLER],
                                  marketplace=raw)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertIn(needle, r.text)
                self.assertFalse(self._batch_exists(name), "站点非法时不能留下空批次")
                self.assertFalse(self._batch_exists(f"{name}_sellers"))
        self.assertEqual(self._tasks("ext_bad_site0"), [])

    def test_ca_seller_batch_crawls_the_ca_storefront(self):
        """加拿大店铺页上点"整店采集"：发现任务必须是 amazon.ca 的，不是同一卖家的美国店。"""
        r = self._collect(batch_name="ext_ca_s", seller_ids=[_SELLER], marketplace="amazon.ca")
        self.assertEqual(r.status_code, 200, r.text)
        sb = r.json()["seller_batch"]
        self.assertEqual(sb["marketplace"], "amazon.ca")
        prog = self.client.get(f"/api/seller-batches/{sb['batch_id']}/progress")
        self.assertEqual(prog.status_code, 200, prog.text)
        self.assertEqual(self._tasks("ext_ca_s_sellers"),
                         [(_SELLER, "K1V 7P8", "amazon.ca", "discover_seller")])


class CrossSiteAppendTests(_ServerCase):
    """同名 = 追加，但一个批次只能属于一个站点。"""

    def test_same_site_appends(self):
        a = self._collect(batch_name="ext_ca_pages", asins=["B0CAPG0001"], marketplace="amazon.ca")
        b = self._collect(batch_name="ext_ca_pages", asins=["B0CAPG0002"], marketplace="amazon.ca")
        self.assertEqual((a.status_code, b.status_code), (200, 200), b.text)
        self.assertFalse(b.json()["asin_batch"]["created"])
        self.assertEqual(self._tasks("ext_ca_pages"), [
            ("B0CAPG0001", "K1V 7P8", "amazon.ca", "asin"),
            ("B0CAPG0002", "K1V 7P8", "amazon.ca", "asin"),
        ])

    def test_ca_append_into_us_batch_is_409_and_inserts_nothing(self):
        self.assertEqual(self._collect(batch_name="ext_mix", asins=["B0MIX00001"]).status_code, 200)
        r = self._collect(batch_name="ext_mix", asins=["B0MIX00002"], marketplace="amazon.ca")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("amazon.com", r.text)
        self.assertIn("amazon.ca", r.text)
        self.assertEqual(self._tasks("ext_mix"), [("B0MIX00001", "10001", "amazon.com", "asin")])

    def test_us_append_into_ca_batch_is_409_even_without_the_field(self):
        """老插件（不带字段 = 美国站）往加拿大批次里追加，同样拒。"""
        self.assertEqual(self._collect(batch_name="ext_mix2", asins=["B0MIX00003"],
                                       marketplace="amazon.ca").status_code, 200)
        r = self._collect(batch_name="ext_mix2", asins=["B0MIX00004"])
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self._tasks("ext_mix2"), [("B0MIX00003", "K1V 7P8", "amazon.ca", "asin")])

    def test_rejection_is_checked_before_any_write(self):
        """卖家批次撞站点时，商品批次也不能先写进去（不留半截推送）。"""
        self.assertEqual(self._collect(batch_name="ext_half", seller_ids=[_SELLER]).status_code, 200)
        r = self._collect(batch_name="ext_half", asins=["B0HALF0001"], seller_ids=[_SELLER],
                          marketplace="amazon.ca")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("ext_half_sellers", r.text)
        self.assertFalse(self._batch_exists("ext_half"), "商品批次不该被建出来")
        self.assertEqual(self._tasks("ext_half_sellers"),
                         [(_SELLER, "10001", "amazon.com", "discover_seller")])


class UsInvariantTests(_ServerCase):
    """老插件不带 `marketplace` -> 美国站，与改造前逐字节一致。"""

    def test_absent_field_equals_explicit_us(self):
        bodies = {
            "absent": {},
            "null": {"marketplace": None},
            "empty": {"marketplace": ""},
            "explicit": {"marketplace": "amazon.com"},
            # page_url 指向加拿大站也**不**推导站点：老插件的队列提交根本不带 page_url，
            # 推导的话同一个老插件的两种推送会落到两个站点上
            "ca_page_url": {"page_url": "https://www.amazon.ca/s?k=mouse"},
        }
        for tag, extra in bodies.items():
            with self.subTest(tag=tag):
                name = f"ext_us_{tag}"
                r = self._collect(batch_name=name, asins=["B0USINV001", "B0USINV002"],
                                  seller_ids=[_SELLER], **extra)
                self.assertEqual(r.status_code, 200, r.text)
                body = r.json()
                self.assertEqual(set(body["asin_batch"]), _US_ASIN_BATCH_KEYS,
                                 "美国站响应的键集合必须与 v1.1.0 时一致")
                self.assertEqual(set(body["seller_batch"]), _US_SELLER_BATCH_KEYS)
                self.assertEqual(body["asin_batch"]["zip_code"], "10001")
                self.assertEqual(self._tasks(name), [
                    ("B0USINV001", "10001", "amazon.com", "asin"),
                    ("B0USINV002", "10001", "amazon.com", "asin"),
                ])
                self.assertEqual(self._tasks(f"{name}_sellers"),
                                 [(_SELLER, "10001", "amazon.com", "discover_seller")])

    def test_us_zip_rules_unchanged(self):
        r = self._collect(batch_name="ext_us_pad", asins=["B0USPAD001"], zip_code="1234")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._tasks("ext_us_pad"), [("B0USPAD001", "01234", "amazon.com", "asin")])

        r = self._collect(batch_name="ext_us_bad", asins=["B0USBAD001"], zip_code="abc")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["detail"], "非法邮编: 'abc'", "美国站报错文案逐字节不变")

    def test_us_runtime_zip_still_passes_through_unvalidated(self):
        """美国站缺省邮编 = 全局设置原样透传（改造前就不校验，golden 钉着同一口径）。"""
        self._set_runtime_zip("abcde")
        r = self._collect(batch_name="ext_us_rt", asins=["B0USRT0001"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._tasks("ext_us_rt"), [("B0USRT0001", "abcde", "amazon.com", "asin")])
        # 同一个全局值对加拿大站不合法 -> 加拿大批次用注册表默认，不吃它
        r = self._collect(batch_name="ext_ca_rt2", asins=["B0CART0001"], marketplace="amazon.ca")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._tasks("ext_ca_rt2"), [("B0CART0001", "K1V 7P8", "amazon.ca", "asin")])


class ResolveSellersMarketplaceTests(_ServerCase):
    """同一个 ASIN 两个站点不同卖家：按站点回，不串站。"""

    def _seed(self, asin, marketplace, seller_id):
        """把一个 ASIN 在某个站点上真采一遍（collect -> pull -> result）。

        ⚠ 结果体里要带 `marketplace`：worker 的结果按 payload 里的站点落 asin_data，
        真实 worker 会带（worker/engine.py），这里照做。
        """
        name = f"rs_{(marketplace or 'none').replace('.', '_')}_{asin}"
        body = {"batch_name": name, "asins": [asin]}
        if marketplace:
            body["marketplace"] = marketplace
        r = self._collect(**body)
        self.assertEqual(r.status_code, 200, r.text)
        tasks = self.client.get("/api/tasks/pull",
                                params={"worker_id": "w-rs", "count": 50}).json()["tasks"]
        task = [t for t in tasks if t["batch_name"] == name][0]
        payload = {
            "task_id": task["id"], "batch_id": task["batch_id"],
            "worker_id": "w-rs", "lease_epoch": task["lease_epoch"], "success": True,
            "asin": asin, "title": "seeded product", "brand": "SeedBrand",
            "seller_id": seller_id, "seller_name": f"shop {seller_id}",
            "current_price": "1.00", "stock_status": "In Stock",
        }
        if marketplace:
            payload["marketplace"] = task["marketplace"]
        r = self.client.post("/api/tasks/result", json=payload)
        self.assertEqual(r.status_code, 200, r.text)

    def _resolve(self, asins, **extra):
        return self.client.post("/api/extension/resolve-sellers", json={"asins": asins, **extra})

    def test_filters_by_site(self):
        self._seed("B0RSMKT001", None, "A1USSELLER0001")
        self._seed("B0RSMKT001", "amazon.ca", "A1CASELLER0001")
        self._seed("B0RSMKT002", "amazon.ca", "A1CASELLER0002")

        def sellers(**extra):
            r = self._resolve(["B0RSMKT001", "B0RSMKT002"], **extra)
            self.assertEqual(r.status_code, 200, r.text)
            return {a: v["seller_id"] for a, v in r.json()["sellers"].items()}

        # 缺省 = 美国站（老插件）；加拿大独有的 ASIN 不出现在美国站的答案里
        self.assertEqual(sellers(), {"B0RSMKT001": "A1USSELLER0001"})
        self.assertEqual(sellers(marketplace="amazon.com"), {"B0RSMKT001": "A1USSELLER0001"})
        self.assertEqual(sellers(marketplace="www.amazon.ca"),
                         {"B0RSMKT001": "A1CASELLER0001", "B0RSMKT002": "A1CASELLER0002"})

    def test_unsupported_site_is_400(self):
        for raw, needle in (("amazon.zz", "不支持的站点"), (["amazon.ca"], "必须是字符串")):
            with self.subTest(raw=raw):
                r = self._resolve(["B0RSMKT001"], marketplace=raw)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertIn(needle, r.text)
        # ASIN 全非法时也照样先拒站点：站点非法一律 400，不因为"反正查不到"而放过
        r = self._resolve(["junk"], marketplace="amazon.zz")
        self.assertEqual(r.status_code, 400, r.text)


def _node():
    return shutil.which("node") or (
        "/opt/node22/bin/node" if os.path.exists("/opt/node22/bin/node") else None)


class ExtensionMarketplaceJsTests(unittest.TestCase):
    """插件一侧（page.js 站点纯函数 + background.js 推送请求体），纯 Node 跑。

    写成 TestCase 而不是裸函数：`python -m unittest discover -s tests` 那一列只认
    TestCase 子类。只在没装 Node 时 skip —— 它不需要 jsdom，没有理由不跑。
    """

    def test_extension_marketplace_spec_passes(self):
        node = _node()
        if not node:
            self.skipTest("没装 Node，跳过插件站点用例")
        from common.core import marketplace as _marketplace

        registry = [{"id": i, "host": _marketplace.get(i).host} for i in _marketplace.all_ids()]
        env = dict(os.environ, AMZ_REGISTRY_JSON=json.dumps(registry))
        proc = subprocess.run([node, _SPEC], cwd=_REPO_ROOT, capture_output=True,
                              text=True, timeout=120, env=env)
        self.assertEqual(
            proc.returncode, 0,
            f"tests/extension_marketplace_spec.js 失败：\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("passed", proc.stdout)
