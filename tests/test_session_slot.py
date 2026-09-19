"""
SessionSlot 单元测试（纯 stdlib/asyncio 桩件）。

worker.engine 依赖 aiofiles/httpx/curl_cffi 等（本环境未安装），故在导入前把
engine.py 的模块级依赖用最小 fake 注入 sys.modules，从而加载**真实**的
SessionSlot 代码，再用 FakeWorker + FakeSession 驱动它验证：
  - ensure_ready：懒建 session、epoch 变化强制重建、session 失活自动重建
  - ensure_zip：仅在邮编不同时 POST，且只影响本 slot
  - note_success / should_rotate_proactive：主动轮换计数（阈值下沉到 slot）
  - rotate：冷轮换、5s 防抖、重置计数、设置宽限期、上报被封
  - rotate_on_empty：空标题累计到阈值触发轮换；宽限期内不重复轮换
  - 隔离性：一个 slot 轮换只换它自己的 session
"""
import os
import sys
import types
import asyncio
import unittest

# 让本仓库根目录可导入（真实的 worker / common 包，均为空 __init__ + 纯净 config）。
# 优先按本文件位置推断仓库根（tests/ 的上一级），回退到 REPO_ROOT / cwd。
_REPO_ROOT = os.environ.get("REPO_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)
if not os.path.isdir(os.path.join(_REPO_ROOT, "worker")):
    _REPO_ROOT = os.getcwd()
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ── 只桩掉 engine 模块级需要、但本环境未装的重依赖子模块 ────────────────────
# 记录被我们改动过的 sys.modules 条目，模块结束时原样还原。
# 不还原会让整个测试进程被污染：本文件的桩件对后续用例仍然生效，
# 谁先被 import 谁说了算，测试结果变成收集顺序的函数。
# （test_delivery_parse.py 里那段 `sys.modules.pop("worker.parser")` 就是被这个
#  坑过一次之后的手工绕行；桩件泄漏还会让黄金样本夹具拿到假的 httpx。）
_SAVED_MODULES = {}


def _stub(name, **attrs):
    _SAVED_MODULES.setdefault(name, sys.modules.get(name))
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


def _stub_if_missing(name, **attrs):
    """真依赖装了就用真的——桩件的用意是「补上没装的」，不是「顶掉装了的」。"""
    try:
        __import__(name)
    except ImportError:
        _stub(name, **attrs)


_stub_if_missing("aiofiles")
_stub_if_missing("httpx", AsyncClient=object)
# worker / common 用真实包（空 __init__）；common.config 只依赖 os，直接用真实的。
#
# ⚠ 这五行以前是无条件 `_stub(...)`，理由写的是「在不拉起 curl_cffi/selectolax 的
# 前提下加载真实的 engine.py」。**D-27 之后那个理由不再成立**（selectolax 与
# dateparser 已经装进 venv，就是为了让解析器测试跑生产路径），而无条件桩有一个
# 它当初没有的代价：pytest 在**收集期**就 import 全部测试文件，所以这几行会在
# 任何用例开始跑之前先把 `worker.parser` 换成桩件；`worker/engine.py:325` 的
# `from worker.parser import AmazonParser` 是**模块级绑定**，一旦绑到 `object`，
# 本文件的 `tearDownModule` 再怎么还原 sys.modules 也换不回来。
#
# 实测（Phase 4 收口，见 D-53）：
#   pytest tests/test_session_slot.py tests/test_engine_not_found.py  -> 25 failed
#   pytest tests/test_engine_not_found.py tests/test_session_slot.py  -> 75 passed
# 默认字母序恰好是安全的那一种，所以六道门全绿、缺陷不可见。
#
# 改成 `_stub_if_missing` 之后，桩件回到它的本意——「补上没装的」，而不是
# 「顶掉装了的」。本环境里只有 worker.session 真的缺依赖（curl_cffi）。
_stub_if_missing("worker.proxy", get_proxy_manager=lambda: object())
_stub_if_missing("worker.session", AmazonSession=object)
_stub_if_missing("worker.parser", AmazonParser=object)
_stub_if_missing("worker.metrics", MetricsCollector=object)
_stub_if_missing("worker.adaptive", AdaptiveController=object, TokenBucket=object)

# 屏蔽 basicConfig 噪声
import logging
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

from worker.engine import SessionSlot, Worker  # noqa: E402


# ── Fakes ───────────────────────────────────────────────────────────────────
class FakeSession:
    _counter = 0

    def __init__(self, zip_code="10001"):
        FakeSession._counter += 1
        self.id = FakeSession._counter
        self.zip_code = zip_code
        self._ready = True
        self.closed = False
        self.change_zip_calls = []
        self.change_zip_ok = True
        self.resend_calls = 0
        self.resend_ok = True

    def is_ready(self):
        return self._ready and not self.closed

    async def change_zip_code(self, target, verify=True):
        self.change_zip_calls.append((target, verify))
        if self.change_zip_ok:
            self.zip_code = target
        return self.change_zip_ok

    async def resend_zip_code(self):
        self.resend_calls += 1
        return self.resend_ok

    async def close(self):
        self.closed = True


class FakeProxyManager:
    def __init__(self):
        self.blocked_reports = 0

    async def report_blocked(self):
        self.blocked_reports += 1


class FakeWorker:
    """只暴露 SessionSlot 用到的接口。"""
    def __init__(self, rotate_every=1000, empty_threshold=15, zip_code="10001"):
        self._restart_epoch = 0
        self._rotate_every = rotate_every
        self._empty_title_rotate_threshold = empty_threshold
        self.zip_code = zip_code
        self.proxy_manager = FakeProxyManager()
        self.created_sessions = []
        self.create_returns_none = False
        self._create_delay_seen = []
        # F-012：每次建 session 时 slot 传下来的站点，供用例断言"换站点会重建"。
        self._create_marketplace_seen = []

    async def _create_session_with_retry(self, delay=5, marketplace=None,
                                         zip_code=None):
        """签名必须跟 worker/engine.py 里的真实方法一致。

        ⚠ 这是一个测试替身：它与被替身对象之间**没有任何机制保证签名同步**，
        真实签名加参数时这里会 TypeError。那正是 F-012 改造时发生的事
        （40 条用例同时红），所以把这条写下来：改 _create_session_with_retry
        的签名时，这里要跟着改。
        """
        self._create_delay_seen.append(delay)
        self._create_marketplace_seen.append(marketplace)
        if self.create_returns_none:
            return None
        s = FakeSession(zip_code=zip_code or self.zip_code)
        s.marketplace = marketplace
        self.created_sessions.append(s)
        return s


# 本模块自持的事件循环。**不要**改回 asyncio.get_event_loop()。
#
# 原来这里是 `asyncio.get_event_loop().run_until_complete(coro)`，依赖的是
# 「当前线程的事件循环」这个**进程级全局槽位**。任何一句 asyncio.run(...) 结束时
# 都会关闭自己的循环并把该槽位置空，而本仓库里有好几处正当的 asyncio.run：
#   * tests/golden/harness.py:206-207   _pg_scratch_db 建库/删库
#   * tests/test_golden_with_relay.py:86 抽干事件流
#   * pytest-asyncio 每个 async 用例（跑完关闭并置空）
# 这些文件按字母序排在 tests/test_session_slot.py **之前**，于是本模块 31 个用例
# 是否通过，变成了「收集顺序 × 后端 × runner」的函数。实测（HEAD fea7395）：
#
#   DB_BACKEND=postgres python -m unittest discover -s tests
#     -> Ran 51 tests ... FAILED (errors=26, skipped=4)
#        26 × RuntimeError: There is no current event loop in thread 'MainThread'
#
# SQLite 下那两个文件走 skipTest、不跑 asyncio.run，所以只有 Postgres 侧翻红。
# pytest 侧看不见，是因为 tests/conftest.py 有一个 autouse 夹具把槽位补回来——
# 而 **unittest 根本不读 conftest.py**，同一份代码两个 runner 两种结果（B6）。
#
# 修法是拿掉这个依赖本身，而不是继续在别处打补丁：本模块自己持有一个循环，
# 谁把全局槽位置空都伤不到它。仍然调用 set_event_loop()，一是保持与原来完全一致的
# 语义（被测代码里若有 get_event_loop() 仍拿到同一个循环），二是顺手把槽位补上。
# 复用同一个循环（而不是每次 asyncio.run）也是刻意的：31 个用例共享一个循环是
# 原有行为，跨循环复用 asyncio 同步原语在 3.10+ 会抛 "bound to a different
# event loop"，换成 per-call asyncio.run 等于自找一类新故障。
_LOOP = None


def run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


class SessionSlotTests(unittest.TestCase):
    def setUp(self):
        FakeSession._counter = 0

    # ── ensure_ready ────────────────────────────────────────────────────────
    def test_ensure_ready_lazy_build(self):
        w = FakeWorker()
        slot = SessionSlot(w)
        self.assertIsNone(slot.session)
        ok = run(slot.ensure_ready())
        self.assertTrue(ok)
        self.assertIsNotNone(slot.session)
        self.assertEqual(len(w.created_sessions), 1)
        # 再次调用应复用，不新建
        run(slot.ensure_ready())
        self.assertEqual(len(w.created_sessions), 1)

    def test_ensure_ready_rebuild_on_dead_session(self):
        w = FakeWorker()
        slot = SessionSlot(w)
        run(slot.ensure_ready())
        first = slot.session
        first._ready = False  # session 失活
        ok = run(slot.ensure_ready())
        self.assertTrue(ok)
        self.assertIsNot(slot.session, first)
        self.assertTrue(first.closed)  # 旧的被关闭
        self.assertEqual(len(w.created_sessions), 2)

    def test_ensure_ready_epoch_change_forces_rebuild(self):
        w = FakeWorker()
        slot = SessionSlot(w)
        run(slot.ensure_ready())
        first = slot.session
        w._restart_epoch += 1  # 软重启
        run(slot.ensure_ready())
        self.assertIsNot(slot.session, first)
        self.assertTrue(first.closed)
        self.assertEqual(slot._restart_epoch, w._restart_epoch)

    def test_ensure_ready_create_fail_returns_false(self):
        w = FakeWorker()
        w.create_returns_none = True
        slot = SessionSlot(w)
        ok = run(slot.ensure_ready())
        self.assertFalse(ok)
        self.assertIsNone(slot.session)

    # ── ensure_zip ──────────────────────────────────────────────────────────
    def test_ensure_zip_same_zip_no_post(self):
        w = FakeWorker(zip_code="10001")
        slot = SessionSlot(w)
        run(slot.ensure_ready())
        ok = run(slot.ensure_zip("10001"))
        self.assertTrue(ok)
        self.assertEqual(slot.session.change_zip_calls, [])  # 无 POST

    def test_ensure_zip_different_zip_posts(self):
        # 显式固定 standalone，不依赖全局默认（默认已改为 on_fetch）
        import common.config as appcfg
        old = getattr(appcfg, "ZIP_VERIFY_MODE", "on_fetch")
        appcfg.ZIP_VERIFY_MODE = "standalone"
        try:
            w = FakeWorker(zip_code="10001")
            slot = SessionSlot(w)
            run(slot.ensure_ready())
            ok = run(slot.ensure_zip("90210"))
            self.assertTrue(ok)
            # standalone 模式：change_zip_code(verify=True)
            self.assertEqual(slot.session.change_zip_calls, [("90210", True)])
            self.assertEqual(slot.session.zip_code, "90210")
        finally:
            appcfg.ZIP_VERIFY_MODE = old

    def test_ensure_zip_on_fetch_mode_skips_verify(self):
        import common.config as appcfg
        old = getattr(appcfg, "ZIP_VERIFY_MODE", "standalone")
        appcfg.ZIP_VERIFY_MODE = "on_fetch"
        try:
            w = FakeWorker(zip_code="10001")
            slot = SessionSlot(w)
            run(slot.ensure_ready())
            ok = run(slot.ensure_zip("90210"))
            self.assertTrue(ok)
            # on_fetch 模式：change_zip_code(verify=False)，不发独立验证 GET
            self.assertEqual(slot.session.change_zip_calls, [("90210", False)])
        finally:
            appcfg.ZIP_VERIFY_MODE = old

    def test_ensure_zip_post_failure(self):
        w = FakeWorker(zip_code="10001")
        slot = SessionSlot(w)
        run(slot.ensure_ready())
        slot.session.change_zip_ok = False
        ok = run(slot.ensure_zip("90210"))
        self.assertFalse(ok)

    def test_ensure_zip_empty_target_noop(self):
        w = FakeWorker()
        slot = SessionSlot(w)
        run(slot.ensure_ready())
        ok = run(slot.ensure_zip(""))
        self.assertTrue(ok)
        self.assertEqual(slot.session.change_zip_calls, [])

    # ── repost_zip（on_fetch 未生效时同 session 重发 POST）─────────────────────
    def test_repost_zip_success_reuses_session(self):
        w = FakeWorker(zip_code="90210")
        slot = SessionSlot(w)
        run(slot.ensure_ready())
        old = slot.session
        ok = run(slot.repost_zip("90210"))
        self.assertTrue(ok)
        self.assertIs(slot.session, old)          # 不换 session
        self.assertEqual(slot.session.resend_calls, 1)
        self.assertEqual(w.proxy_manager.blocked_reports, 0)  # 不上报被封

    def test_repost_zip_failure_returns_false(self):
        w = FakeWorker(zip_code="90210")
        slot = SessionSlot(w)
        run(slot.ensure_ready())
        slot.session.resend_ok = False
        ok = run(slot.repost_zip("90210"))
        self.assertFalse(ok)
        self.assertEqual(slot.session.resend_calls, 1)

    def test_repost_zip_no_session_returns_false(self):
        w = FakeWorker()
        slot = SessionSlot(w)  # 未 ensure_ready，session=None
        ok = run(slot.repost_zip("90210"))
        self.assertFalse(ok)

    def test_repost_zip_empty_target_returns_false(self):
        w = FakeWorker()
        slot = SessionSlot(w)
        run(slot.ensure_ready())
        ok = run(slot.repost_zip(""))
        self.assertFalse(ok)
        self.assertEqual(slot.session.resend_calls, 0)

    # ── 主动轮换计数 ─────────────────────────────────────────────────────────
    def test_note_success_proactive_threshold(self):
        w = FakeWorker(rotate_every=3)
        slot = SessionSlot(w)
        run(slot.ensure_ready())
        self.assertFalse(slot.should_rotate_proactive())
        slot.note_success()
        slot.note_success()
        self.assertFalse(slot.should_rotate_proactive())
        slot.note_success()
        self.assertTrue(slot.should_rotate_proactive())

    def test_rotate_resets_success_counter(self):
        w = FakeWorker(rotate_every=3)
        slot = SessionSlot(w)
        run(slot.ensure_ready())
        slot.note_success(); slot.note_success(); slot.note_success()
        self.assertTrue(slot.should_rotate_proactive())
        run(slot.rotate(reason="主动轮换"))
        self.assertFalse(slot.should_rotate_proactive())

    # ── rotate ──────────────────────────────────────────────────────────────
    def test_rotate_swaps_session_and_reports_blocked(self):
        w = FakeWorker()
        slot = SessionSlot(w)
        run(slot.ensure_ready())
        old = slot.session
        run(slot.rotate(reason="被封锁"))
        self.assertIsNot(slot.session, old)
        self.assertTrue(old.closed)
        self.assertEqual(w.proxy_manager.blocked_reports, 1)
        self.assertGreater(slot._grace_until, 0)

    def test_rotate_debounce_5s(self):
        w = FakeWorker()
        slot = SessionSlot(w)
        run(slot.ensure_ready())
        run(slot.rotate(reason="第一次"))
        after_first = slot.session
        # 立即再次轮换应被 5s 防抖跳过
        run(slot.rotate(reason="第二次"))
        self.assertIs(slot.session, after_first)  # 未变
        self.assertEqual(w.proxy_manager.blocked_reports, 1)

    def test_rotate_debounce_expires(self):
        w = FakeWorker()
        slot = SessionSlot(w)
        run(slot.ensure_ready())
        run(slot.rotate(reason="第一次"))
        first_rotated = slot.session
        # 把上次轮换时间往前拨 6s，绕过防抖
        slot._last_rotate_time -= 6
        run(slot.rotate(reason="第二次"))
        self.assertIsNot(slot.session, first_rotated)
        self.assertEqual(w.proxy_manager.blocked_reports, 2)

    # ── rotate_on_empty（空标题机制1+2）──────────────────────────────────────
    def test_rotate_on_empty_single_trigger(self):
        w = FakeWorker(empty_threshold=15)
        slot = SessionSlot(w)
        run(slot.ensure_ready())
        old = slot.session
        # 单次空标题（未到累计阈值）：机制1 立即轮换
        run(slot.rotate_on_empty("B0TEST", reason="标题为空"))
        self.assertIsNot(slot.session, old)
        self.assertEqual(slot._empty_title_count, 0)  # 轮换后清零

    def test_rotate_on_empty_grace_period_no_double_rotate(self):
        w = FakeWorker(empty_threshold=15)
        slot = SessionSlot(w)
        run(slot.ensure_ready())
        run(slot.rotate_on_empty("B0TEST", reason="标题为空"))  # 设置宽限期
        rotated = slot.session
        reports = w.proxy_manager.blocked_reports
        # 宽限期内再来一次：只累计计数，不轮换
        run(slot.rotate_on_empty("B0TEST2", reason="标题为空"))
        self.assertIs(slot.session, rotated)
        self.assertEqual(w.proxy_manager.blocked_reports, reports)
        self.assertEqual(slot._empty_title_count, 1)

    def test_rotate_on_empty_accumulate_to_threshold(self):
        w = FakeWorker(empty_threshold=3)
        slot = SessionSlot(w)
        run(slot.ensure_ready())
        # 制造"过了宽限期 + 未达防抖"环境：直接调低阈值并绕过防抖/宽限
        # 第一次触发机制1轮换
        run(slot.rotate_on_empty("A1", reason="空"))
        # 绕过防抖与宽限期，让后续 empty 能累计并在阈值处轮换
        slot._last_rotate_time -= 10
        slot._grace_until -= 10
        run(slot.rotate_on_empty("A2", reason="空"))  # count=1
        slot._last_rotate_time -= 10
        slot._grace_until -= 10
        run(slot.rotate_on_empty("A3", reason="空"))  # count=2
        slot._last_rotate_time -= 10
        slot._grace_until -= 10
        pre = slot.session
        run(slot.rotate_on_empty("A4", reason="空"))  # count=3 → 达阈值轮换
        self.assertIsNot(slot.session, pre)

    # ── 隔离性 ───────────────────────────────────────────────────────────────
    def test_two_slots_independent_rotation(self):
        w = FakeWorker()
        a = SessionSlot(w)
        b = SessionSlot(w)
        run(a.ensure_ready())
        run(b.ensure_ready())
        a_sess, b_sess = a.session, b.session
        run(a.rotate(reason="被封锁"))
        # a 换了，b 不受影响
        self.assertIsNot(a.session, a_sess)
        self.assertIs(b.session, b_sess)
        self.assertFalse(b_sess.closed)

    # ── close ───────────────────────────────────────────────────────────────
    def test_close(self):
        w = FakeWorker()
        slot = SessionSlot(w)
        run(slot.ensure_ready())
        s = slot.session
        run(slot.close())
        self.assertTrue(s.closed)
        self.assertIsNone(slot.session)


class LooksDegradedDeliveryTests(unittest.TestCase):
    """_looks_degraded_delivery：判定"可售但无配送区"的软降级页（纯 dict 逻辑，
    不用 self，故用 Worker._looks_degraded_delivery(None, r) 直接调）。"""

    def _f(self, r):
        return Worker._looks_degraded_delivery(None, r)

    def _base(self, **kw):
        r = {"delivery_date": "N/A", "current_price": "$9.99",
             "is_fba": "FBA", "stock_status": "In Stock"}
        r.update(kw)
        return r

    def test_degraded_fba_in_stock_no_delivery(self):
        self.assertTrue(self._f(self._base()))
        self.assertTrue(self._f(self._base(delivery_date=None)))
        self.assertTrue(self._f(self._base(delivery_date="")))
        self.assertTrue(self._f(self._base(stock_status="Only 3 left in stock")))

    def test_not_degraded_when_delivery_present(self):
        self.assertFalse(self._f(self._base(delivery_date="July 20")))

    def test_not_degraded_when_not_fba(self):
        self.assertFalse(self._f(self._base(is_fba="第三方")))
        self.assertFalse(self._f(self._base(is_fba="N/A")))

    def test_not_degraded_when_not_sellable(self):
        for price in ("N/A", "No Featured Offer", "不可售", "See price in cart", ""):
            self.assertFalse(self._f(self._base(current_price=price)), price)

    def test_not_degraded_when_unavailable_or_empty_stock(self):
        self.assertFalse(self._f(self._base(stock_status="Currently unavailable")))
        self.assertFalse(self._f(self._base(stock_status="")))
        self.assertFalse(self._f(self._base(stock_status="N/A")))


class DumpDegradedHtmlTests(unittest.TestCase):
    """_dump_degraded_html 门控（用 SimpleNamespace 作 self，避免实例化 Worker）。"""

    def test_disabled_is_noop(self):
        fake = types.SimpleNamespace(
            _dump_degraded=False, _degraded_dump_count=0,
            _degraded_dump_max=300, _degraded_dump_dir="/nonexistent")
        run(Worker._dump_degraded_html(fake, "B0X", "10001", "<html>x</html>"))
        self.assertEqual(fake._degraded_dump_count, 0)  # 关闭时直接返回、不写盘

    def test_cap_reached_is_noop(self):
        fake = types.SimpleNamespace(
            _dump_degraded=True, _degraded_dump_count=300,
            _degraded_dump_max=300, _degraded_dump_dir="/nonexistent")
        run(Worker._dump_degraded_html(fake, "B0X", "10001", "<html>x</html>"))
        self.assertEqual(fake._degraded_dump_count, 300)  # 达上限不再写

    def test_empty_html_is_noop(self):
        fake = types.SimpleNamespace(
            _dump_degraded=True, _degraded_dump_count=0,
            _degraded_dump_max=300, _degraded_dump_dir="/nonexistent")
        run(Worker._dump_degraded_html(fake, "B0X", "10001", ""))
        self.assertEqual(fake._degraded_dump_count, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
