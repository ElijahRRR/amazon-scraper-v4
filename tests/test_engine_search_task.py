"""worker 侧关键词发现任务（F-010，`_process_search_task`）的用例。

------------------------------------------------------------------------
为什么必须单独测
------------------------------------------------------------------------
`tests/test_search_api.py` 覆盖的是 server 那一半（建批次 → 收结果 → 查进度），
它喂给 `/api/tasks/search-result` 的 items 是**手写**的。也就是说：
worker 究竟有没有按筛选参数请求、翻页翻到哪停、广告位丢没丢、
失败时走的是哪条通道 —— 那份用例一个字都证明不了。

而这些恰恰全是**静默**失效：

  筛选参数没读出来 -> 退化成裸关键词搜索，批次/进度/发现数全部正常，只有数据是错的
  翻页上限没生效   -> 用户说"翻 3 页"，实际翻到 Amazon 给完为止（代理配额白烧）
  广告位没丢       -> 排名数据混进广告，且会挤掉自然位的采集配额
  一页都没成功     -> 该判失败进重试通道，判成"成功但 0 个"的话任务直接 done，
                      这个关键词就此永远缺数据，而批次显示 100% 完成

所以这里把 session / 限流 / 并发控制器全桩掉，只跑那个循环本身。
"""
import asyncio
import logging
import os
import sys
import types
import unittest

_REPO_ROOT = os.environ.get("REPO_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)
if not os.path.isdir(os.path.join(_REPO_ROOT, "worker")):
    _REPO_ROOT = os.getcwd()
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# 与 tests/test_engine_not_found.py 同一套：只桩本环境真装不上的那一个模块。
# worker.parser 一律用**真的** —— 广告位识别、翻页判定都在它里面，桩掉等于
# 把被测对象换成桩件。
_SAVED_MODULES = {}


def _stub(name, **attrs):
    _SAVED_MODULES.setdefault(name, sys.modules.get(name))
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


try:  # pragma: no cover - 装了 curl_cffi 的机器走这一支
    import curl_cffi  # noqa: F401
except ImportError:
    _stub("worker.session", AmazonSession=object)

_SAVED_LOG_LEVEL = logging.root.manager.disable
logging.disable(logging.CRITICAL)


def tearDownModule():
    for name, original in _SAVED_MODULES.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original
    logging.disable(_SAVED_LOG_LEVEL)


import json  # noqa: E402

from common.core import error_types  # noqa: E402
from worker.engine import Worker  # noqa: E402


def run(coro):
    """自持一个事件循环（理由见 tests/test_engine_not_found.py 的同名函数）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ────────────────────────────────────────────────────────────────
# 桩件
# ────────────────────────────────────────────────────────────────

def _page_html(asins, *, has_next=True, sponsored=()):
    """构造一页搜索结果的最小 HTML。结构照 worker/parser.py 认的那套。"""
    cards = []
    for a in asins:
        ad = '<div data-component-type="sp-sponsored-result"></div>' if a in sponsored else ''
        cards.append(
            f'<div class="s-result-item" data-asin="{a}">{ad}'
            f'<h2><span>Item {a}</span></h2>'
            f'<span class="a-price"><span class="a-offscreen">$1.00</span></span>'
            f'<img class="s-image" src="http://i/{a}.jpg"></div>'
        )
    nxt = ('<a class="s-pagination-next" href="#">Next</a>' if has_next
           else '<a class="s-pagination-next s-pagination-disabled">Next</a>')
    return f"<html><body>{''.join(cards)}{nxt}</body></html>"


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code
        self.content = text.encode()


class FakeSession:
    """记录每次请求的参数，按脚本返回页面。"""

    def __init__(self, pages):
        self.pages = pages          # [FakeResponse | None]
        self.calls = []             # [(keyword, page, params)]

    async def fetch_search_page(self, keyword, page=1, search_params=None, max_recv_speed=0):
        self.calls.append((keyword, page, search_params))
        idx = len(self.calls) - 1
        return self.pages[idx] if idx < len(self.pages) else None

    def is_blocked(self, resp):
        return resp is not None and resp.status_code in (403, 503)

    def is_captcha(self, resp):
        return False

    def is_404(self, resp):
        return resp is not None and resp.status_code == 404

    async def solve_captcha(self, resp):
        return False


class FakeSlot:
    def __init__(self, session):
        self.session = session
        self.rotations = []

    async def ensure_ready(self):
        return True

    def note_success(self):
        pass

    def should_rotate_proactive(self):
        return False

    async def rotate(self, reason=""):
        self.rotations.append(reason)


class FakeController:
    async def acquire(self):
        pass

    def release(self):
        pass

    def record_result(self, *a, **kw):
        pass


def make_worker(pages):
    """一个把网络与并发控制全桩掉的 Worker，只留下被测的那个循环。"""
    w = Worker("http://server.invalid", worker_id="w-kw", zip_code="10001")
    w._controller = FakeController()
    w._rate_limiter = None
    w._max_retries = 2
    w._calc_recv_speed = lambda: 0
    w._apply_jitter = lambda: asyncio.sleep(0)

    session = FakeSession(pages)
    slot = FakeSlot(session)

    # 两个提交口都拦下来，断言"提交了什么"而不是真发 HTTP
    w.submitted_search = []
    w.submitted_failures = []

    async def _fake_submit_search(task_id, keyword, items, meta, lease_epoch, batch_id):
        w.submitted_search.append({
            "task_id": task_id, "keyword": keyword, "items": items,
            "meta": meta, "lease_epoch": lease_epoch, "batch_id": batch_id,
        })
        return True

    async def _fake_submit_result(task_id, result_data, success, error_type=None,
                                  error_detail=None, batch_id=None, lease_epoch=0):
        w.submitted_failures.append({
            "task_id": task_id, "success": success,
            "error_type": error_type, "error_detail": error_detail,
        })
        return True

    w._submit_search_result = _fake_submit_search
    w._submit_result = _fake_submit_result
    return w, slot, session


def make_task(keyword="wireless mouse", **search):
    params = {
        "domain": "www.amazon.com", "min_price": None, "max_price": None,
        "delivery": None, "sort": None, "rh_extra": None,
        "max_pages": 7, "include_sponsored": False,
    }
    params.update(search)
    return {
        "id": 42, "batch_id": 7, "asin": keyword, "lease_epoch": 3,
        "task_meta": json.dumps({"search": params}),
    }


# ────────────────────────────────────────────────────────────────
# 用例
# ────────────────────────────────────────────────────────────────

class SearchTaskTests(unittest.TestCase):

    def test_paginates_until_no_next_and_dedupes(self):
        pages = [
            FakeResponse(_page_html(["B0PAGE0001", "B0PAGE0002"], has_next=True)),
            FakeResponse(_page_html(["B0PAGE0002", "B0PAGE0003"], has_next=False)),
        ]
        w, slot, session = make_worker(pages)
        run(w._process_search_task(make_task(), slot))

        self.assertEqual(len(session.calls), 2, "翻到没有下一页就该停")
        self.assertEqual([c[1] for c in session.calls], [1, 2])

        self.assertEqual(len(w.submitted_search), 1)
        sub = w.submitted_search[0]
        self.assertEqual([i["asin"] for i in sub["items"]],
                         ["B0PAGE0001", "B0PAGE0002", "B0PAGE0003"],
                         "跨页重复的 ASIN 只留一条")
        self.assertEqual([i["page"] for i in sub["items"]], [1, 1, 2],
                         "页号由 worker 记（parser 只给页内名次）")
        self.assertEqual(sub["meta"]["pages_scanned"], 2)
        self.assertFalse(sub["meta"]["truncated"])
        self.assertEqual(sub["lease_epoch"], 3)
        self.assertEqual(sub["batch_id"], 7)
        self.assertEqual(w.submitted_failures, [])

    def test_max_pages_is_honored_and_marked_truncated(self):
        """翻页上限来自任务本身，不是类常量 —— 这是用户的采集意图。"""
        pages = [FakeResponse(_page_html([f"B0MAX0000{i}"], has_next=True)) for i in range(9)]
        w, slot, session = make_worker(pages)
        run(w._process_search_task(make_task(max_pages=3), slot))

        self.assertEqual(len(session.calls), 3, "翻到第 3 页就必须停")
        sub = w.submitted_search[0]
        self.assertEqual(sub["meta"]["pages_scanned"], 3)
        self.assertTrue(sub["meta"]["truncated"],
                        "还有下一页却因为上限停下，必须标 truncated")

    def test_filter_params_reach_the_session(self):
        """筛选参数必须原样传到 fetch_search_page —— 断链是静默的。"""
        pages = [FakeResponse(_page_html(["B0FILT0001"], has_next=False))]
        w, slot, session = make_worker(pages)
        run(w._process_search_task(
            make_task(min_price=10.0, max_price=50.0, delivery="prime", max_pages=3), slot))

        _kw, _page, params = session.calls[0]
        self.assertEqual(params["min_price"], 10.0)
        self.assertEqual(params["max_price"], 50.0)
        self.assertEqual(params["delivery"], "prime")

        # 光断言 dict 里有键不够 —— 键在但拼 URL 时被漏掉正是要防的事
        from common.core.searchurl import build_search_url
        url = build_search_url(_kw, 2, params)
        self.assertIn("p_36%3A1000-5000", url)
        self.assertIn("p_85%3A2470955011", url)

    def test_sponsored_dropped_by_default_but_ranks_keep_the_gap(self):
        """广告位默认丢弃；被丢掉的位置在 rank 上留空档（那是有意义的信息）。"""
        pages = [FakeResponse(_page_html(
            ["B0SPON0001", "B0REAL0001", "B0REAL0002"],
            has_next=False, sponsored={"B0SPON0001"}))]
        w, slot, _ = make_worker(pages)
        run(w._process_search_task(make_task(), slot))

        sub = w.submitted_search[0]
        self.assertEqual([i["asin"] for i in sub["items"]], ["B0REAL0001", "B0REAL0002"])
        self.assertEqual([i["rank"] for i in sub["items"]], [2, 3],
                         "rank 是页内绝对位置，被丢掉的广告位留空档")
        self.assertEqual(sub["meta"]["sponsored_skipped"], 1)

    def test_sponsored_kept_when_asked(self):
        pages = [FakeResponse(_page_html(
            ["B0SPON0001", "B0REAL0001"], has_next=False, sponsored={"B0SPON0001"}))]
        w, slot, _ = make_worker(pages)
        run(w._process_search_task(make_task(include_sponsored=True), slot))

        sub = w.submitted_search[0]
        self.assertEqual([i["asin"] for i in sub["items"]], ["B0SPON0001", "B0REAL0001"])
        self.assertEqual([i["sponsored"] for i in sub["items"]], [True, False])
        self.assertEqual(sub["meta"]["sponsored_skipped"], 0)

    def test_zero_successful_pages_is_a_failure_not_an_empty_success(self):
        """一页都没成功 -> 判失败走重试通道。

        判成"成功但 0 个"的话任务直接 done，这个关键词就此永远缺数据，
        而批次进度显示 100% 完成 —— 最坏的一种失败形态。
        """
        w, slot, session = make_worker([None, None, None])
        run(w._process_search_task(make_task(), slot))

        self.assertEqual(w.submitted_search, [], "不该提交任何发现结果")
        self.assertEqual(len(w.submitted_failures), 1)
        fail = w.submitted_failures[0]
        self.assertFalse(fail["success"])
        self.assertEqual(fail["error_type"], error_types.TIMEOUT)
        self.assertEqual(fail["task_id"], 42)

    def test_blocked_rotates_session_and_eventually_fails(self):
        w, slot, session = make_worker([
            FakeResponse("blocked", status_code=503),
            FakeResponse("blocked", status_code=503),
        ])
        run(w._process_search_task(make_task(), slot))

        self.assertTrue(slot.rotations, "被封必须轮换 session 换 IP")
        self.assertEqual(len(w.submitted_failures), 1)
        self.assertEqual(w.submitted_failures[0]["error_type"], error_types.BLOCKED)

    def test_404_mid_pagination_is_a_boundary_not_an_error(self):
        """翻页途中拿到 404 = 没有更多结果，已抓到的照常提交。"""
        pages = [
            FakeResponse(_page_html(["B0OK000001"], has_next=True)),
            FakeResponse("not found", status_code=404),
        ]
        w, slot, _ = make_worker(pages)
        run(w._process_search_task(make_task(), slot))

        self.assertEqual(w.submitted_failures, [])
        self.assertEqual(len(w.submitted_search), 1)
        self.assertEqual([i["asin"] for i in w.submitted_search[0]["items"]], ["B0OK000001"])
        self.assertEqual(w.submitted_search[0]["meta"]["pages_scanned"], 1)

    def test_missing_task_meta_degrades_to_bare_search(self):
        """task_meta 缺失/损坏时退化成裸关键词搜索，而不是整条任务失败。

        （退化会打 warning —— 静默降级是这个功能最难查的故障形态。）
        """
        pages = [FakeResponse(_page_html(["B0BARE0001"], has_next=False))]
        w, slot, session = make_worker(pages)
        task = make_task()
        task["task_meta"] = "{{{ 不是合法 JSON"
        run(w._process_search_task(task, slot))

        self.assertEqual(len(w.submitted_search), 1)
        _kw, _page, params = session.calls[0]
        self.assertEqual(params, {}, "读不出来就用空 dict，靠 build_search_url 的缺省")


class UnknownTaskTypeTests(unittest.TestCase):
    """server 派下来一个本 worker 不认识的 task_type 时，必须当场判失败。

    落进 `_process_task` 的话，关键词会被当成 ASIN 去请求
    （`/dp/wireless%20mouse`），404 之后走 not_found 通道**写进 asin_data** ——
    一条以关键词为 ASIN 的垃圾行，而且看起来完全正常。
    """

    def test_dispatch_covers_the_three_known_types(self):
        import inspect
        src = inspect.getsource(Worker._worker_loop)
        for t in ("discover_seller", "discover_search", "asin"):
            self.assertIn(f'"{t}"', src, f"分派里少了 {t}")
        self.assertIn("未知 task_type", src,
                      "未知类型必须有显式分支，不能落进 else 当 ASIN 采")
