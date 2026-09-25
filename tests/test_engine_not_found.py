"""worker/engine.py 的 Phase 4：404 分支（P4-3）+ `_` 前缀采集元数据的管线。

**为什么这个文件必须存在**：黄金样本看不见这里的任何一个变化。
黄金夹具喂给 server 的是**合成的** result dict（从不 import worker.parser、
更不会走 worker/engine.py 的 404 分支），而 `crawl_time` 之类的键还被
`_VOLATILE_KEYS` 擦成 `<VOLATILE>`。所以 "64/64 仍然绿" 对本文件的断言
**一个字都证明不了**，证据只能自己带：真解析器 + 真 `_default_result` +
真 `_is_parse_failure` + 真 SQLite 写入语义。

覆盖：
  A. 404 提交体的形状（不带慢变字段、带 `_outcome=not_found`、能过 `_is_parse_failure`）
  B. `_` 键确实被服务端字段白名单丢弃（与 `_page_asin` 同理）
  C. 端到端：同一条 asin_data 上先写一次好数据，再写一次 404 结果，
     title/brand/category/UPC **必须**还在；并用**旧实现的 payload** 做配对对照，
     证明这个测试真的能测出差别（对照组必须把它们抹成占位符）
  D. `_process_task` 的 404 分支：只发一次请求、不轮换 session、提交体正确
  E. 纯函数 derive_zip_verify / sanitize_completeness / resolve_parse_engine 的值域
  F. 与 parser（P4-1/P4-2/P4-9）的接口对表，以及与 relay 的**已知缺口**哨兵
"""
import asyncio
import os
import sys
import tempfile
import types
import unittest

# 让本仓库根目录可导入（与 tests/test_session_slot.py 同一套推断）。
_REPO_ROOT = os.environ.get("REPO_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)
if not os.path.isdir(os.path.join(_REPO_ROOT, "worker")):
    _REPO_ROOT = os.getcwd()
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ── 只桩掉本环境真的装不上的那一个模块 ────────────────────────────────────
# worker/session.py 在模块级 `from curl_cffi import CurlHttpVersion`，本环境没有
# curl_cffi。**其余一律用真的**——尤其是 worker.parser：本文件的全部价值就在于
# 拿真实的 `_default_result()` 来验，桩掉它等于把被测对象换成了桩件。
# （tests/test_session_slot.py 无条件桩了 parser/metrics/adaptive，那是 D-27
#  装上 selectolax 之前的写法；这里不跟随。）
_SAVED_MODULES = {}


def _stub(name, **attrs):
    _SAVED_MODULES.setdefault(name, sys.modules.get(name))
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


try:  # pragma: no cover - 真装了 curl_cffi 的机器上走这一支
    import curl_cffi  # noqa: F401
except ImportError:
    _stub("worker.session", AmazonSession=object)

import logging  # noqa: E402

_SAVED_LOG_LEVEL = logging.root.manager.disable
logging.disable(logging.CRITICAL)


def tearDownModule():
    """还原 sys.modules 与 logging，避免污染同进程内的其他测试。"""
    for name, original in _SAVED_MODULES.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original
    logging.disable(_SAVED_LOG_LEVEL)


from common.core import ASIN_DATA_FIELDS, _is_parse_failure  # noqa: E402
from common.database import Database  # noqa: E402
from common.slowhash import SLOW_HASH_FIELDS  # noqa: E402
from worker import engine as eng  # noqa: E402
from worker.engine import Worker  # noqa: E402


def run(coro):
    """自持一个事件循环跑协程。

    **不要**改成 `asyncio.get_event_loop()`：那个进程级全局槽位会被任何
    `asyncio.run(...)` 置空，用例是否通过就成了收集顺序的函数（tests/conftest.py
    里记着这段历史，tests/test_runner_parity.py 在看守它）。
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def make_worker(**kw):
    kw.setdefault("worker_id", "w-test")
    kw.setdefault("zip_code", "10001")
    return Worker("http://server.invalid", **kw)


#: 旧实现（HEAD a34e0c6 的 worker/engine.py:1167-1174）的 404 提交体。
#: 配对对照组用它，证明本文件的断言确实能分辨新旧行为。
def legacy_not_found_payload(worker, asin, zip_code):
    data = worker.parser._default_result(asin, zip_code)
    data["title"] = "[商品不存在]"
    return data


# 一次"好采集"的最小 payload：慢变字段全都有真值。
GOOD = {
    "title": "Anker PowerCore 10000 Portable Charger",
    "brand": "Anker",
    "product_type": "Portable Power Bank",
    "manufacturer": "Anker Innovations",
    "model_number": "A1263",
    "part_number": "A1263011",
    "country_of_origin": "China",
    "is_customized": "No",
    "bullet_points": "One of the smallest 10000mAh chargers|PowerIQ",
    "long_description": "Ultra compact power bank.",
    "image_urls": "https://m.media-amazon.com/images/I/71ABC123._AC_SL1500_.jpg",
    "upc_list": "0848061071153,0848061071160",
    "ean_list": "0848061071153",
    "parent_asin": "B0BYP2E5PZ",
    "variation_asins": "B0BYP2E5PZ,B0BYP3RRJ9",
    "variant_attributes": "Color:Black|Size:10000mAh",
    "root_category_id": "2335752011",
    "category_ids": "2335752011,7073960011",
    "category_tree": "Electronics > Accessories > Portable Power Banks",
    "first_available_date": "2023-03-01",
    "package_dimensions": "3.6 x 2.4 x 0.9 inches",
    "package_weight": "6.4 ounces",
    "item_dimensions": "3.6 x 2.4 x 0.9 inches",
    "item_weight": "6.35 ounces",
    "best_sellers_rank": "#3 in Portable Power Banks",
    "current_price": "$21.99",
    "buybox_price": "$21.99",
    "original_price": "$25.99",
    "buybox_shipping": "FREE",
    "is_fba": "FBA",
    "stock_count": "37",
    "stock_status": "In Stock",
    "delivery_date": "Tomorrow",
    "delivery_time": "1 day",
    "product_url": "https://www.amazon.com/dp/B0GOODASIN",
    "site": "US",
    "zip_code": "10001",
    "crawl_time": "2026-08-01T00:00:00Z",
}


async def fresh_db(tmpdir):
    db = Database(os.path.join(tmpdir, "scraper_test.db"))
    await db.connect()
    return db


# ══════════════════════════════════════════════════════════════════════
# A. 404 提交体的形状
# ══════════════════════════════════════════════════════════════════════
class NotFoundPayloadShapeTests(unittest.TestCase):
    def setUp(self):
        self.w = make_worker()
        self.payload = self.w._build_not_found_result("B0DEADBEEF", "90210")

    def test_no_slow_field_is_submitted(self):
        """慢变/身份层字段一个都不提交 —— 这就是"不覆盖"在服务端写入语义下的写法。

        判据取自 common/slowhash.py 的 SLOW_HASH_FIELDS（哈希的输入全集），
        不是我在 engine.py 里手抄的那份常量，否则常量抄错时测试跟着一起错。
        """
        slow = (set(SLOW_HASH_FIELDS) - {"image_ids"}) | {"image_urls"}
        leaked = sorted(slow & set(self.payload))
        self.assertEqual(leaked, [], f"这些慢变字段被 404 提交体带上了: {leaked}")

    def test_the_four_headline_fields_are_absent(self):
        """验收口径逐字版：404 不得抹掉 title / brand / category / UPC。"""
        for f in ("title", "brand", "category_tree", "category_ids",
                  "root_category_id", "upc_list", "ean_list"):
            self.assertNotIn(f, self.payload)

    def test_outcome_is_not_found(self):
        self.assertEqual(self.payload[eng.META_OUTCOME], eng.OUTCOME_NOT_FOUND)

    def test_zip_requested_is_the_zip_we_actually_asked_for(self):
        self.assertEqual(self.payload[eng.META_ZIP_REQUESTED], "90210")
        self.assertEqual(self.payload["zip_code"], "90210")

    def test_no_page_means_nothing_measured(self):
        """404 没有商品页：观测邮编 None、位图 0、parse_engine None。"""
        self.assertIsNone(self.payload[eng.META_ZIP_OBSERVED])
        self.assertEqual(self.payload[eng.META_ZIP_VERIFY], eng.ZIP_VERIFY_UNVERIFIED)
        self.assertEqual(self.payload[eng.META_COMPLETENESS], 0)
        self.assertIsNone(self.payload[eng.META_PARSE_ENGINE])

    def test_completeness_zero_means_unmeasured_bit_is_clear(self):
        """契约 §6.4：completeness_ok ⟺ (c & 8) != 0 且 (c & 7) == 7。
        404 的 0 让消费侧**明确**知道"没测过"，而不是"测过且三项都缺"。"""
        self.assertEqual(self.payload[eng.META_COMPLETENESS] & 8, 0)

    def test_fast_fields_are_byte_identical_to_default_result(self):
        """快变字段照旧写占位值，且逐字等于 `_default_result`。

        这条钉住"最小 diff"：本次改动对 asin_data 的影响**只有**"慢变字段不再被
        覆盖"这一项，快变列写进去的字符串一个字都没变（含 stock_count='0'
        带来的既有 asin_changes 行为）。
        """
        default = self.w.parser._default_result("B0DEADBEEF", "90210")
        for k, v in self.payload.items():
            # `marketplace`（F-012）不参与比对：它**不是解析出来的字段**，
            # 而是 engine 从 task 挂上去的采集参数，parser 手里根本没有这个事实
            # （所以 _default_result 里没有它，也不该有）。
            # 它与 `_` 元数据的差别只在于「是不是 asin_data 的真列」，
            # 与「是不是解析产物」无关 —— 两者在这条用例里都该跳过。
            if k.startswith("_") or k in ("batch_name", "marketplace"):
                continue
            self.assertIn(k, default)
            self.assertEqual(v, default[k], f"{k} 与 _default_result 不一致")

    def test_survives_the_server_side_parse_failure_gate(self):
        """真 `_is_parse_failure`。这条一旦红，说明 404 记录会被降级成
        status='failed' / error_type='server_reject'，事件 outcome 变成
        parse_failed，且**一个字段都不落库**——比它想修的 bug 更糟。

        耦合点：title/brand 已经不提交了，唯一让 all_empty 为 False 的键是
        `_default_result` 的 stock_count='0'（'0' 不在 _NA_VALUES 里）。
        parser 若把这个默认值改成 '' / 'N/A'，这条测试会立刻变红。
        """
        self.assertFalse(_is_parse_failure(self.payload))

    def test_legacy_payload_would_have_wiped_everything(self):
        """反向哨兵：旧实现的 payload 里，慢变字段全在，且全是占位符。"""
        legacy = legacy_not_found_payload(self.w, "B0DEADBEEF", "90210")
        self.assertEqual(legacy["title"], "[商品不存在]")
        for f in ("brand", "category_tree", "upc_list", "manufacturer"):
            self.assertIn(f, legacy)
            self.assertIn(legacy[f], ("N/A", ""))


# ══════════════════════════════════════════════════════════════════════
# B. `_` 键与服务端字段白名单
# ══════════════════════════════════════════════════════════════════════
class UnderscoreKeyContractTests(unittest.TestCase):
    ALL_META = (eng.META_OUTCOME, eng.META_ZIP_REQUESTED, eng.META_ZIP_OBSERVED,
                eng.META_ZIP_VERIFY, eng.META_COMPLETENESS, eng.META_PARSE_ENGINE)

    def test_every_meta_key_is_underscore_prefixed(self):
        for k in self.ALL_META:
            self.assertTrue(k.startswith("_"), k)

    def test_no_meta_key_is_an_asin_data_column(self):
        """白名单丢弃的**前提**：这些键不在 ASIN_DATA_FIELDS 里。
        哪天有人给 asin_data 加了同名列，这条会立刻红。"""
        for k in self.ALL_META:
            self.assertNotIn(k, ASIN_DATA_FIELDS)

    def test_attach_meta_adds_exactly_six_meta_keys_plus_marketplace(self):
        """六个 `_` 元数据键 + 一个真列 `marketplace`，一个不多一个不少。

        ⚠ `marketplace`（F-012）**刻意不带** `_` 前缀，这不是笔误：
        那批 `_` 键的语义是「只进事件流、写库时被白名单丢弃」，而
        marketplace 是 asin_data 的真列（还是唯一键的一部分）。
        两者必须能被区分开 —— 上面 test_no_meta_key_is_an_asin_data_column
        守的就是这条界线，本用例在这里把 marketplace 摆在界线的另一侧。
        """
        result = {"asin": "B0X", "title": "t"}
        eng.Worker._attach_collection_meta(
            make_worker(), result, zip_requested="10001", outcome=eng.OUTCOME_OK)
        self.assertEqual(sorted(set(result) - {"asin", "title"}),
                         sorted((*self.ALL_META, "marketplace")))
        # 缺省参数下必须是美国站：老 server 不下发 marketplace 字段，
        # 而它们派的确实是美国站任务。
        self.assertEqual(result["marketplace"], "amazon.com")
        self.assertIn("marketplace", ASIN_DATA_FIELDS)

    def test_attach_meta_never_touches_business_fields(self):
        before = dict(GOOD)
        after = dict(GOOD)
        eng.Worker._attach_collection_meta(
            make_worker(), after, zip_requested="10001", outcome=eng.OUTCOME_OK)
        for k, v in before.items():
            self.assertEqual(after[k], v, f"{k} 被元数据挂载改动了")

    def test_outcome_is_clamped_to_the_worker_side_subset(self):
        """worker 只判定 ok / not_found。blocked / parse_failed / stale 是服务端的
        判定，worker 传越界值一律降级成 ok（服务端仍会自己判 parse_failed）。"""
        for bogus in ("blocked", "parse_failed", "stale", "", None, 7):
            r = {}
            eng.Worker._attach_collection_meta(
                make_worker(), r, zip_requested="1", outcome=bogus)
            self.assertEqual(r[eng.META_OUTCOME], eng.OUTCOME_OK)


# ══════════════════════════════════════════════════════════════════════
# C. 端到端：真 SQLite 写入语义 + 配对对照
# ══════════════════════════════════════════════════════════════════════
class NotFoundEndToEndTests(unittest.TestCase):
    """先写一次好数据，再写一次 404 结果，看 asin_data 上还剩什么。

    走的是 `save_result` → `_save_result_inner_unlocked`，也就是
    `val = data.get(f); if val is not None:` 那段真实的跨时间合并代码。
    """

    def _scrape_twice(self, second_payload_builder, asin):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                db = await fresh_db(tmp)
                try:
                    w = make_worker()
                    bid = await db.create_batch(f"b-{asin}")
                    good = dict(GOOD, asin=asin,
                                product_url=f"https://www.amazon.com/dp/{asin}")
                    self.assertTrue(await db.save_result(good, bid))

                    second = second_payload_builder(w, asin)
                    saved = await db.save_result(second, bid)
                    row = await db.get_result_by_asin(asin)
                    return saved, dict(row) if row else None
                finally:
                    await db.close()
        return run(go())

    def test_404_preserves_the_catalog_layer(self):
        saved, row = self._scrape_twice(
            lambda w, asin: w._build_not_found_result(asin, "10001"), "B0NEWPATH1")
        self.assertTrue(saved)
        # 慢变/目录层：原封不动
        self.assertEqual(row["title"], GOOD["title"])
        self.assertEqual(row["brand"], GOOD["brand"])
        self.assertEqual(row["category_tree"], GOOD["category_tree"])
        self.assertEqual(row["category_ids"], GOOD["category_ids"])
        self.assertEqual(row["root_category_id"], GOOD["root_category_id"])
        self.assertEqual(row["upc_list"], GOOD["upc_list"])
        self.assertEqual(row["ean_list"], GOOD["ean_list"])
        self.assertEqual(row["image_urls"], GOOD["image_urls"])
        self.assertEqual(row["bullet_points"], GOOD["bullet_points"])
        self.assertEqual(row["manufacturer"], GOOD["manufacturer"])
        self.assertEqual(row["variant_attributes"], GOOD["variant_attributes"])
        self.assertEqual(row["parent_asin"], GOOD["parent_asin"])
        # 快变层：如实写成"不可得"
        self.assertEqual(row["current_price"], "N/A")
        self.assertEqual(row["stock_count"], "0")
        # 采集参数：这一次采集的
        self.assertNotEqual(row["crawl_time"], GOOD["crawl_time"])

    def test_control_legacy_payload_wipes_the_catalog_layer(self):
        """配对对照：同一套断言点，旧 payload 必须**全部**被抹成占位符。
        没有这一组，上面那条测试无法证明自己测到了东西。"""
        saved, row = self._scrape_twice(
            lambda w, asin: legacy_not_found_payload(w, asin, "10001"), "B0OLDPATH1")
        self.assertTrue(saved)
        self.assertEqual(row["title"], "[商品不存在]")
        self.assertEqual(row["brand"], "N/A")
        self.assertEqual(row["category_tree"], "")
        self.assertEqual(row["upc_list"], "")
        self.assertEqual(row["image_urls"], "")
        self.assertEqual(row["manufacturer"], "N/A")

    def test_first_ever_scrape_404_inserts_a_row_without_catalog_columns(self):
        """从没见过的 ASIN 第一次就 404：INSERT 分支，慢变列留 NULL。

        这是本次改动**唯一**会改变既有端点返回内容的场景（审计里点名要单独决策的
        那一处）：以前这些列是占位符字符串，现在是 NULL。选 NULL 是因为
        "没观测到" 和 "观测到了是空" 必须可区分。
        """
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                db = await fresh_db(tmp)
                try:
                    w = make_worker()
                    bid = await db.create_batch("b-first")
                    payload = w._build_not_found_result("B0FIRST404", "10001")
                    self.assertTrue(await db.save_result(payload, bid))
                    row = await db.get_result_by_asin("B0FIRST404")
                    return dict(row)
                finally:
                    await db.close()
        row = run(go())
        self.assertIsNone(row["title"])
        self.assertIsNone(row["brand"])
        self.assertIsNone(row["upc_list"])
        self.assertEqual(row["stock_count"], "0")
        self.assertEqual(row["zip_code"], "10001")

    def test_underscore_keys_do_not_reach_asin_data(self):
        """`_` 键被 ASIN_DATA_FIELDS 白名单丢弃——与 `_page_asin` 同理。"""
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                db = await fresh_db(tmp)
                try:
                    w = make_worker()
                    bid = await db.create_batch("b-meta")
                    good = dict(GOOD, asin="B0METAKEY1")
                    w._attach_collection_meta(
                        good, zip_requested="10001", outcome=eng.OUTCOME_OK,
                        glow_zip_effective=True, session_zip="10001")
                    await db.save_result(good, bid)
                    row = await db.get_result_by_asin("B0METAKEY1")
                    return dict(row)
                finally:
                    await db.close()
        row = run(go())
        for k in UnderscoreKeyContractTests.ALL_META:
            self.assertNotIn(k, row)


# ══════════════════════════════════════════════════════════════════════
# D. _process_task 的 404 分支
# ══════════════════════════════════════════════════════════════════════
class FakeResponse:
    def __init__(self, status_code=404, text="<html>Dogs of Amazon</html>"):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()


class FakeSession:
    def __init__(self, zip_code="10001", status=404):
        self.zip_code = zip_code
        self.fetch_calls = 0
        self._status = status

    def is_ready(self):
        return True

    async def fetch_product_page(self, asin, max_recv_speed=0):
        self.fetch_calls += 1
        return FakeResponse(self._status)

    def is_blocked(self, resp):
        return False

    def is_captcha(self, resp):
        return False

    def is_404(self, resp):
        return resp.status_code == 404


class FakeSlot:
    def __init__(self, session):
        self.session = session
        self.rotations = []
        # F-012：记下每次 ensure_ready 收到的站点，供用例断言任务站点确实传到了 slot。
        self.ensure_ready_calls = []

    async def ensure_ready(self, marketplace=None, zip_code=None):
        """签名必须跟 worker/engine.py:SessionSlot.ensure_ready 一致。

        F-012 给真方法加了 (marketplace, zip_code) 两个参数；这个替身没跟上
        就会 TypeError，而 _process_* 里对 ensure_ready 的失败处理是
        「attempt += 1 后 continue」，于是表现成"一页都没采到"而不是报错。
        """
        self.ensure_ready_calls.append((marketplace, zip_code))
        return True

    async def ensure_zip(self, target_zip):
        return True

    async def repost_zip(self, target_zip):
        return False        # 逼真：重发失败 ⇒ 调用方回退到冷轮换

    async def rotate(self, reason="轮换"):
        self.rotations.append(reason)

    async def rotate_on_empty(self, asin, reason=""):
        self.rotations.append(reason)

    def note_success(self):
        pass

    def should_rotate_proactive(self):
        return False


class ProcessTask404Tests(unittest.TestCase):
    def _drive(self, task_zip="90210", worker_zip="10001"):
        async def go():
            w = make_worker(zip_code=worker_zip, enable_screenshot=False)
            w._result_queue = asyncio.Queue()
            w._rate_limiter = None          # 令牌桶与本用例无关
            sess = FakeSession(zip_code=task_zip)
            slot = FakeSlot(sess)
            task = {"id": 77, "asin": "B0DEADBEEF", "zip_code": task_zip,
                    "batch_id": 5, "batch_name": "nf-batch", "lease_epoch": 3,
                    "needs_screenshot": False}
            out = await w._process_task(task, slot)
            submitted = []
            while not w._result_queue.empty():
                submitted.append(w._result_queue.get_nowait())
            return w, sess, slot, out, submitted
        return run(go())

    def test_404_submits_once_without_retry_or_rotation(self):
        w, sess, slot, out, submitted = self._drive()
        success, blocked, resp_bytes = out
        self.assertTrue(success)          # 终态成功（任务不回队）
        self.assertFalse(blocked)         # 404 不是封锁
        self.assertGreater(resp_bytes, 0)
        self.assertEqual(sess.fetch_calls, 1, "404 不该重试")
        self.assertEqual(slot.rotations, [], "404 不该轮换 session / 换 IP")
        self.assertEqual(len(submitted), 1)

    def test_submitted_payload_is_the_not_found_shape(self):
        w, sess, slot, out, submitted = self._drive()
        p = submitted[0]
        self.assertEqual(p[eng.META_OUTCOME], eng.OUTCOME_NOT_FOUND)
        self.assertNotIn("title", p)
        self.assertNotIn("brand", p)
        self.assertNotIn("upc_list", p)
        self.assertEqual(p["asin"], "B0DEADBEEF")
        self.assertEqual(p["task_id"], 77)
        self.assertEqual(p["batch_id"], 5)
        self.assertEqual(p["lease_epoch"], 3)
        self.assertEqual(p["batch_name"], "nf-batch")
        self.assertEqual(p["worker_id"], w.worker_id)
        self.assertNotIn("success", p, "成功路径不带 success 键（server 默认 True）")

    def test_stats_count_it_as_success_and_as_not_found(self):
        w, sess, slot, out, submitted = self._drive()
        self.assertEqual(w._stats["success"], 1)
        self.assertEqual(w._stats["not_found"], 1)
        self.assertEqual(w._stats["total"], 1)
        self.assertEqual(w._stats["failed"], 0)

    def test_zip_requested_falls_back_to_worker_zip_when_task_has_none(self):
        """task.zip_code 为 None 时，真正请求用的是 worker 的默认邮编。
        提交体必须写这个值——不是 None，也不是空串。

        旧实现把 task 的原始 zip_code 交给 `_default_result`，于是
        `zip_code=None` ⇒ 服务端 `if val is not None` 跳过 ⇒ asin_data.zip_code
        **保留上一次采集的邮编**（可能完全是另一个邮编），而消费侧的分组键
        (asin, marketplace, zip_requested) 就此错位。
        """
        async def go():
            w = make_worker(zip_code="10001", enable_screenshot=False)
            w._result_queue = asyncio.Queue()
            w._rate_limiter = None
            slot = FakeSlot(FakeSession())
            task = {"id": 1, "asin": "B0NOZIP", "zip_code": None,
                    "batch_id": None, "batch_name": "", "lease_epoch": 0}
            await w._process_task(task, slot)
            return w._result_queue.get_nowait()
        p = run(go())
        self.assertEqual(p[eng.META_ZIP_REQUESTED], "10001")
        self.assertEqual(p["zip_code"], "10001")


# ══════════════════════════════════════════════════════════════════════
# E. 纯函数
# ══════════════════════════════════════════════════════════════════════
class ZipVerifyDerivationTests(unittest.TestCase):
    def test_parser_page_level_evidence_wins(self):
        for pv in (eng.ZIP_VERIFY_CONFIRMED, eng.ZIP_VERIFY_MISMATCH):
            self.assertEqual(
                eng.derive_zip_verify(pv, None, "10001", "10001"), pv)

    def test_engine_glow_when_parser_is_silent(self):
        self.assertEqual(eng.derive_zip_verify(None, True, None, "10001"),
                         eng.ZIP_VERIFY_CONFIRMED)
        self.assertEqual(eng.derive_zip_verify(None, False, None, "10001"),
                         eng.ZIP_VERIFY_MISMATCH)

    def test_out_of_band_evidence_is_assumed(self):
        self.assertEqual(eng.derive_zip_verify(None, None, "90210", "90210"),
                         eng.ZIP_VERIFY_ASSUMED)

    def test_no_evidence_at_all_is_unverified(self):
        self.assertEqual(eng.derive_zip_verify(None, None, "10001", "90210"),
                         eng.ZIP_VERIFY_UNVERIFIED)
        self.assertEqual(eng.derive_zip_verify(None, None, None, ""),
                         eng.ZIP_VERIFY_UNVERIFIED)

    def test_parser_assumed_is_kept(self):
        self.assertEqual(
            eng.derive_zip_verify(eng.ZIP_VERIFY_ASSUMED, None, None, "10001"),
            eng.ZIP_VERIFY_ASSUMED)

    def test_never_invents_confirmed_from_the_requested_zip(self):
        """请求值绝不能自证观测值。relay.py:223 的口径：
        一个错的 confirmed 比一个诚实的 unverified 更糟。"""
        for junk in ("", "  ", "yes", None, 7, True):
            v = eng.derive_zip_verify(junk, None, None, "10001")
            self.assertIn(v, eng.ZIP_VERIFY_DOMAIN)
            self.assertNotEqual(v, eng.ZIP_VERIFY_CONFIRMED)

    def test_always_in_domain(self):
        for pv in (None, "", "confirmed", "bogus", 3):
            for glow in (True, False, None):
                for sz in (None, "10001", "90210"):
                    self.assertIn(
                        eng.derive_zip_verify(pv, glow, sz, "10001"),
                        eng.ZIP_VERIFY_DOMAIN)


class CompletenessSanitizeTests(unittest.TestCase):
    def test_valid_bitmaps_pass_through(self):
        for v in range(16):
            self.assertEqual(eng.sanitize_completeness(v), v)

    def test_bool_is_rejected(self):
        """True 是 int 的子类；放行会变成 bit0=1 = "面包屑区块存在"，
        而那是一句没人测过的断言。"""
        self.assertEqual(eng.sanitize_completeness(True), 0)
        self.assertEqual(eng.sanitize_completeness(False), 0)

    def test_out_of_range_and_garbage_fall_back_to_unmeasured(self):
        for v in (-1, 16, 999, None, "", "abc", [], {}, object()):
            self.assertEqual(eng.sanitize_completeness(v), 0)

    def test_numeric_strings_are_accepted(self):
        self.assertEqual(eng.sanitize_completeness("15"), 15)


class ParseEngineResolutionTests(unittest.TestCase):
    def test_parser_value_wins(self):
        self.assertEqual(eng.resolve_parse_engine("lxml"), "lxml")
        self.assertEqual(eng.resolve_parse_engine("selectolax"), "selectolax")

    def test_falls_back_to_the_module_switch(self):
        import worker.parser as p
        expected = "selectolax" if p._USE_SELECTOLAX else "lxml"
        self.assertEqual(eng.resolve_parse_engine(None), expected)

    def test_unknown_value_is_not_passed_through(self):
        self.assertIn(eng.resolve_parse_engine("mystery-engine"),
                      ("selectolax", "lxml", None))


# ══════════════════════════════════════════════════════════════════════
# F. 与 parser / relay 的接口对表
# ══════════════════════════════════════════════════════════════════════
class ParserInterfaceTests(unittest.TestCase):
    """P4-1 / P4-2 / P4-9 的接口：engine 只是搬运工，键名和值域必须对得上。"""

    def test_key_names_match_the_parser(self):
        p = self.real_parser()
        default = p._default_result("B0IFACE001", "10001")
        for k in ("_zip_observed", "_zip_verify", "_completeness", "_parse_engine"):
            self.assertIn(k, default, f"parser 的 _default_result 少了 {k}")

    def test_zip_verify_domain_matches_the_parser(self):
        import worker.parser as p
        self.assertEqual(
            {p.ZIP_VERIFY_CONFIRMED, p.ZIP_VERIFY_ASSUMED,
             p.ZIP_VERIFY_MISMATCH, p.ZIP_VERIFY_UNVERIFIED},
            set(eng.ZIP_VERIFY_DOMAIN))

    def test_completeness_measured_bit_matches_the_contract(self):
        import worker.parser as p
        self.assertEqual(p.COMPLETENESS_MEASURED, 8)
        self.assertEqual(eng.COMPLETENESS_MAX, 15)

    def test_parse_engine_values_match_the_parser(self):
        import worker.parser as p
        self.assertEqual(set(eng.PARSE_ENGINE_VALUES),
                         {p.ENGINE_SELECTOLAX, p.ENGINE_LXML})

    def test_engine_passes_parser_values_through_untouched(self):
        """parser 给了值，engine 不许改写。"""
        r = {"_zip_observed": "16602", "_zip_verify": "mismatch",
             "_completeness": 15, "_parse_engine": "lxml"}
        make_worker()._attach_collection_meta(
            r, zip_requested="10001", outcome=eng.OUTCOME_OK,
            glow_zip_effective=None, session_zip="10001")
        self.assertEqual(r["_zip_observed"], "16602")
        self.assertEqual(r["_zip_verify"], "mismatch")
        self.assertEqual(r["_completeness"], 15)
        self.assertEqual(r["_parse_engine"], "lxml")

    @staticmethod
    def real_parser():
        import worker.parser as p
        return p.AmazonParser()


class RelayContractSentinelTests(unittest.TestCase):
    """**缺口已补**（哨兵在这里换岗成了回归门）。

    历史：`common/pgdb/relay.py` 的 `classify_success_outcome` 原先只认哨兵标题
    `title == '[商品不存在]'`。P4-3 把那个标题从 404 提交体里删掉了（它是慢变
    字段，写进去就是覆盖），于是**唯一**的 not_found 信号变成 `_outcome` 键，
    而当时 relay 还没读它 —— 这个类曾用一条 `expectedFailure` 把缺口钉成会自己
    响铃的形式，等接线的人回来收尾。

    接线已完成（relay 侧的 P4 提交，同一批还接上了 zip_observed / zip_verify /
    completeness / parse_engine 四列）：
      * `classify_success_outcome` 读 `_outcome`，哨兵标题仍然优先（页面级证据
        比自述强），老 worker 因此照旧被认出来；
      * `_build_event_row` 再兜一次底（`reconcile_outcome`），覆盖"升级前入队、
        升级后才被 relay 认领"的那批 outbox 行。
    实测（scratchpad/wire_probe.py，走真 HTTP + 真 relay + /api/v1/sync/records）：

        seq=3 asin='B0WIRE404A' outcome='not_found' review_hash=None slow_hash=None
              payload._outcome='not_found'  payload.title=None

    所以下面两条现在都是**正向断言**：新提交体判 not_found，老提交体（哨兵标题）
    也判 not_found。任何一条转红都意味着 404 的记录又开始算哈希了
    —— 好页→404→好页 = 两次 review_hash 翻转 = 两次误复审（契约 §6.5）。
    """

    def _payload(self):
        return make_worker()._build_not_found_result("B0DEADBEEF", "10001")

    @staticmethod
    def _classifier():
        try:
            from common.pgdb.relay import classify_success_outcome
        except Exception as e:  # pragma: no cover - asyncpg 没装的机器
            raise unittest.SkipTest(f"relay 不可导入: {e}")
        return classify_success_outcome

    def test_relay_still_recognizes_the_legacy_sentinel_title(self):
        """灰度期的另一半：老 worker 交的是 `_default_result` + 哨兵标题。

        它没有 `_outcome` 键，唯一的信号还是那个标题。worker 与 server 独立
        部署，所以这条路径在整个灰度窗口里都活着，不能顺手删掉。
        """
        legacy = legacy_not_found_payload(make_worker(), "B0DEADBEEF", "10001")
        self.assertNotIn(eng.META_OUTCOME, legacy)
        self.assertEqual(self._classifier()(legacy), "not_found")

    def test_relay_classifies_the_new_404_payload_as_not_found(self):
        """新提交体：没有 title，只有 `_outcome`。relay 必须据此判 not_found。"""
        self.assertEqual(self._classifier()(self._payload()), "not_found")

    def test_the_payload_carries_the_signal_relay_will_need(self):
        """worker 侧的承诺本身是无条件的：`_outcome` 一定在，且值合法。"""
        p = self._payload()
        self.assertEqual(p[eng.META_OUTCOME], "not_found")
        self.assertNotIn("title", p)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
