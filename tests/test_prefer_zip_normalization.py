"""``/api/tasks/pull?prefer_zip=`` 的归一化（Phase 4.6）。

------------------------------------------------------------------------
这份文件钉的是什么
------------------------------------------------------------------------
4.6 之前 ``server/api/worker_queue.py`` 里有一份**内联的第二份**邮编规则：

    if prefer_zip and re.match(r'^\\d{5}$', prefer_zip):
        pz = prefer_zip

它与 ``server/app.py:_normalize_zip``（同一个进程里、为同一件事写的那份）
分叉出两个后果，两个都是**静默**的：

1. **``"1001"`` 被丢弃。** 整数往返会丢掉前导零（Excel、JSON、任何把邮编
   当 number 的一环），``"01001"`` 于是变成 ``"1001"``。
   ``_normalize_zip`` 为此专门做了 zfill、``common/pgdb/relay.py:normalize_zip``
   也为同一个形状专门做了 zfill —— 只有这处内联的没有。
   worker 传 ``prefer_zip="1001"`` 时任务分配悄悄退回「不挑邮编」，
   worker 于是每个任务都要切一次 session。**没有任何一侧会报错。**
2. **``"10001\\n"`` 被原样透传进 SQL 谓词。** Python 的 ``$`` 在「末尾恰好一个
   换行」处也匹配，所以那条正则会放行它；随后 ``t.zip_code = '10001\\n'``
   一行都匹配不上，同样静默退化成「不挑邮编」。

用例形状：三个批次三个邮编，**故意让偏好的那个在默认排序里排在后面**
（无 prefer_zip 时 ``ORDER BY t.zip_code, t.id ASC``），于是
「归一化生效」与「静默退回不挑邮编」产出不同的任务 —— 这个差别正是回归网。

跟着 ``DB_BACKEND`` 走，门禁两列各跑一遍（C4：两个后端行为一致）。
写成 ``unittest.TestCase``：``python -m unittest discover -s tests``
的加载器只认 TestCase 子类，四道门才都覆盖得到。
"""
from __future__ import annotations

import unittest

from tests.golden.harness import isolated_server

#: (批次名, ASIN, zip)。zip 的字典序 = 默认派发序，所以 ``00001`` 恒排第一，
#: 它是「静默退回不挑邮编」时必然拿到的那个任务。
_SEEDS = (
    ("pz_a", "B00PZ00001", "00001"),
    ("pz_b", "B00PZ00002", "01001"),
    ("pz_c", "B00PZ00003", "10001"),
)

_FALLBACK_ASIN = "B00PZ00001"   # zip 00001，默认排序的第一个


def _seed(client):
    for name, asin, zc in _SEEDS:
        r = client.post(
            "/api/upload",
            files={"file": (f"{name}.txt", asin.encode(), "text/plain")},
            data={"batch_name": name, "zip_code": zc, "needs_screenshot": "false"},
        )
        assert r.status_code == 200, r.text


def _pull_one(client, worker_id, prefer_zip=None):
    params = {"worker_id": worker_id, "count": 1, "enable_screenshot": "false"}
    if prefer_zip is not None:
        params["prefer_zip"] = prefer_zip
    r = client.get("/api/tasks/pull", params=params)
    assert r.status_code == 200, r.text
    tasks = r.json()["tasks"]
    assert len(tasks) == 1, tasks
    return tasks[0]


class PreferZipNormalizationTests(unittest.TestCase):

    def _pulled(self, prefer_zip):
        """每条断言独立起一次服务：pull 会把任务置 processing，会互相污染。"""
        with isolated_server() as (client, _ctx):
            _seed(client)
            return _pull_one(client, "pz-w", prefer_zip)

    # ---------- 主修复：短数字邮编要补零，不能丢 ----------

    def test_short_numeric_prefer_zip_is_zfilled_not_dropped(self):
        """``prefer_zip="1001"`` 必须归一成 ``"01001"`` 并**生效**。

        改动前：内联正则判 None -> 退回「不挑邮编」-> 拿到 zip 00001 的任务。
        """
        t = self._pulled("1001")
        self.assertEqual(
            t["zip_code"], "01001",
            "prefer_zip='1001' 没有被 zfill 成 '01001' —— 任务分配静默退回"
            "「不挑邮编」（拿到的是 %r）" % (t["zip_code"],))
        self.assertEqual(t["asin"], "B00PZ00002")
        self.assertNotEqual(t["asin"], _FALLBACK_ASIN)

    def test_already_five_digits_behaves_identically(self):
        """``"01001"`` 与 ``"1001"`` 必须走到同一个结果 —— 补零是**归一**，
        不是另一条规则。"""
        self.assertEqual(self._pulled("01001")["asin"],
                         self._pulled("1001")["asin"])

    def test_widened_shapes_are_pinned(self):
        """三种**新放行**的形状 —— Phase 4.5-4.8 审计发现它们没被声明也没被覆盖。

        HEAD 的内联 ``re.match(r'^\d{5}$', ...)`` 把这三种全部丢弃（退回「不挑邮编」）；
        换成 ``_normalize_zip`` 之后它们都被归一成合法邮编并生效。
        这是「换实现」的必然副产物，不是额外加的逻辑 —— 但它是行为改动，
        所以钉在这里，不靠注释自证。

        安全性论证的落点也在这条：放行值必须**恰好 5 位数字**，
        否则就是把未净化的串喂进 SQL 谓词。
        """
        for raw, expected in (
            ("90001.0", "90001"),     # Excel 把邮编读成浮点
            ("10001-1234", "10001"),  # ZIP+4，取前段
            ("1", "00001"),           # 单个数字也补成合法邮编
        ):
            with self.subTest(raw=raw):
                from server import app as _srv_app
                got = _srv_app._normalize_zip(raw)
                self.assertEqual(got, expected)
                self.assertRegex(
                    got, r"^\d{5}$",
                    f"_normalize_zip({raw!r}) 放行了非 5 位数字的值 {got!r} —— "
                    "它会被直接拼进任务分配的 SQL 谓词")

    # ---------- 顺带堵掉的口子：尾随换行 ----------

    def test_trailing_newline_is_stripped_not_passed_through(self):
        """``"10001\\n"``：``$`` 放行、``\\Z`` 不放行。

        改动前那条 ``re.match(r'^\\d{5}$', ...)`` **匹配**它，于是
        ``t.zip_code = '10001\\n'`` 进了 SQL 谓词、一行都匹配不上，
        又是一次静默的「不挑邮编」。
        """
        t = self._pulled("10001\n")
        self.assertEqual(
            t["zip_code"], "10001",
            "prefer_zip='10001\\n' 没有被 strip —— 拿到 %r" % (t["zip_code"],))
        self.assertEqual(t["asin"], "B00PZ00003")

    # ---------- 既有行为不许变 ----------

    def test_garbage_prefer_zip_still_falls_back(self):
        """非法值仍然退回「不挑邮编」（而不是 500、也不是进 SQL）。"""
        for bad in ("abc", "123456", "1' OR '1'='1", "", "-"):
            with self.subTest(prefer_zip=bad):
                self.assertEqual(self._pulled(bad)["asin"], _FALLBACK_ASIN)

    def test_no_prefer_zip_is_unchanged(self):
        """不传 prefer_zip 时的派发序不受本改动影响。"""
        self.assertEqual(self._pulled(None)["asin"], _FALLBACK_ASIN)

    def test_prefer_zip_with_surrounding_space(self):
        """``" 01001 "``：``_normalize_zip`` 先 strip，所以它生效。

        改动前内联正则不放行（有空格），静默退回。
        """
        self.assertEqual(self._pulled(" 01001 ")["asin"], "B00PZ00002")


class ZfillRuleIsSharedTests(unittest.TestCase):
    """兑现 ``common/pgdb/relay.py:normalize_zip_observed`` 那条注释。

    原文：「补零规则与 ``normalize_zip`` **必须一致**：这两个值会被
    ``reconcile_zip_verify`` 直接比较，一边补零一边不补，就会凭空造出
    ``mismatch``（'01001' vs '1001'）。」

    4.6 之前这是一条**只写在注释里**的约束（两个函数各抄了一遍
    ``s.isdigit() and 0 < len(s) < 5``）。现在三处都调
    ``common/core/zipcode.py:_zfill_short_numeric``，本用例守住这条边。

    ⚠ 守的是**补零这一步**，不是三个函数的外层语义 —— 那三份刻意不合并，
    理由写在 ``common/core/zipcode.py`` 的 docstring 里。
    """

    def test_the_three_sites_share_one_object(self):
        """三处仍然共用同一个补零函数对象。

        ⚠ F-012 之后 ``server/app.py`` 的引用路径**多了一跳**：邮编归一化
        整体搬进了 ``common/core/marketplace.py``（它现在是站点感知的），
        app 调 ``marketplace.normalize_postal``，由后者调
        ``_zfill_short_numeric``。所以这里不再断言
        ``srv._zfill_short_numeric``（那个名字已经不在 app 里了），
        改成沿新链路断言 —— **守的东西一个字没变**：三处补零必须是同一个对象。
        """
        from common.core.zipcode import _zfill_short_numeric
        from common.core import marketplace as mkt
        import common.pgdb.relay as relay
        import server.app as srv
        self.assertIs(relay._zfill_short_numeric, _zfill_short_numeric)
        self.assertIs(mkt._zfill_short_numeric, _zfill_short_numeric)
        # app 那一跳：它引用的就是上面这个 marketplace 模块。
        self.assertIs(srv._marketplace, mkt)

    def test_relay_and_app_agree_on_short_numeric(self):
        import common.pgdb.relay as relay
        import server.app as srv
        for raw in ("1001", "1", "01001", "10001", "0"):
            with self.subTest(raw=raw):
                self.assertEqual(relay.normalize_zip(raw)[0], srv._normalize_zip(raw),
                                 "relay 与 app 对 %r 补零结果不同 —— "
                                 "reconcile_zip_verify 会凭空造出 mismatch" % (raw,))
                self.assertEqual(relay.normalize_zip_observed(raw)[0],
                                 relay.normalize_zip(raw)[0])

    def test_five_digits_is_not_counted_as_coerced(self):
        """``"10001"`` 已经是 5 位，不算「补过零」—— relay 拿这个布尔计
        ``zip_coerced``，把恒等变换算进去会让计数器永远在响。"""
        import common.pgdb.relay as relay
        self.assertEqual(relay.normalize_zip("10001"), ("10001", False))
        self.assertEqual(relay.normalize_zip("1001"), ("01001", True))


if __name__ == "__main__":
    unittest.main()
