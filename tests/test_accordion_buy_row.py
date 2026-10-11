"""accordion buybox 的配送 / 运费只读下单那一行，会员价行被选中时现价取普通价。

  H1  配送日期 / 配送天数 / 运费：原来是在**整个购买框**里取 —— 日期取全页最快、
      运费取全页第一个配送价。未选中的二手行更快时就报了二手行的日期
      （实测 CA B0CG19X7RN：选中的全新行只有 October 16，导出 October 12；用户实测
      US B0CQWZR6L8：二手行 Prime「Tomorrow」入库）。现在只读普通买家下单的那一行：
      选中行；选中的是会员价行（它不带配送信息）时读全新行 newAccordionRow_N。
      **语义不变**：仍是那一行里能看到的最快日期（含 Prime 会员专享）。
  H2  选中会员价行（"Prime Member Price"）时 current_price / buybox_price 取全新行的
      普通价。用户口径：现价 = 普通买家能买到的价格，会员价不保留。原价仍按 G2
      （读选中行）不变。

夹具是 tests/fixtures/offer/ 下的真实页面片段（来源见那里的 README）。凡是在片段上
改过东西的用例，docstring 里写明改了什么、为什么。

日期断言只用与运行日期无关的形状：行里只剩一个日期时选中的就是它，"Tomorrow" 恒为
1 天。天数不写死，用同一行的候选日期经 ``_pick_delivery`` 现算。

两条引擎都要跑：生产上 lxml 分支只在 selectolax 缺失时才走，靠模块级开关永远测不到它。
"""
from __future__ import annotations

import gzip
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging  # noqa: E402

from lxml import html as lxml_html  # noqa: E402
from selectolax.parser import HTMLParser  # noqa: E402

import worker.parser as parser_mod  # noqa: E402
from common.core import marketplace as M  # noqa: E402
from worker.parser import AmazonParser  # noqa: E402

_FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "offer")
_CA = ("K1V 7P8", "amazon.ca")
_US = ("10001", None)


def _load(name: str) -> str:
    with gzip.open(os.path.join(_FIX, name), "rt", encoding="utf-8") as f:
        return f.read()


class _Engine:
    """把模块级引擎开关临时拨到指定一侧，用完还原。"""

    def __init__(self, slx: bool):
        self.slx = slx

    def __enter__(self):
        self.saved = parser_mod._USE_SELECTOLAX
        parser_mod._USE_SELECTOLAX = self.slx
        return self

    def __exit__(self, *exc):
        parser_mod._USE_SELECTOLAX = self.saved


def _parse_both(html: str, asin: str, site) -> dict:
    """``parse_product`` 两条引擎各跑一遍 -> {"slx": result, "lxml": result}。"""
    out = {}
    for name, slx in (("slx", True), ("lxml", False)):
        with _Engine(slx):
            out[name] = AmazonParser().parse_product(html, asin, site[0], site[1])
    return out


#: buybox 里各 offer 行的起始标签（行在 #buyBoxAccordion 里按 DOM 顺序相邻）。
_ROW_START = re.compile(
    r'<div id="(?:newAccordionRow_\d+|usedAccordionRow|primeSavingsUpsellAccordionRow)"')


def _edit_row(html: str, row_id: str, old: str, new: str, expect: int) -> str:
    """只在 buybox 里 ``row_id`` 那一行的范围内把 ``old`` 换成 ``new``，并核对次数。"""
    start = html.index(f'<div id="{row_id}"')
    nxt = _ROW_START.search(html, start + 1)
    end = nxt.start() if nxt else len(html)
    seg = html[start:end]
    assert seg.count(old) == expect, (row_id, old, seg.count(old))
    return html[:start] + seg.replace(old, new) + html[end:]


def _row_times(html: str, row_id: str) -> list:
    """某一行里全部 data-csa-c-delivery-time 的值（夹具前提断言用）。"""
    return [n.attributes.get("data-csa-c-delivery-time")
            for n in HTMLParser(html).css(f"#{row_id} [data-csa-c-delivery-time]")]


class _Base(unittest.TestCase):

    def setUp(self):
        self.p = AmazonParser()

    def _delivery(self, html: str, asin: str, site) -> tuple:
        """(delivery_date, delivery_time)：两条引擎的函数本身 + parse_product 出口必须一致。"""
        direct = {self.p._slx_parse_delivery(HTMLParser(html)),
                  self.p._parse_delivery(lxml_html.fromstring(html))}
        self.assertEqual(len(direct), 1, f"两条引擎结论分叉: {direct}")
        full = {(r["delivery_date"], r["delivery_time"])
                for r in _parse_both(html, asin, site).values()}
        self.assertEqual(full, direct, "parse_product 出口与函数本身不一致")
        return direct.pop()

    def _shipping(self, html: str, current: str, site) -> str:
        spec = M.get(site[1])
        got = {self.p._slx_parse_buybox_shipping(HTMLParser(html), current, spec),
               self.p._parse_buybox_shipping(lxml_html.fromstring(html), current, spec)}
        self.assertEqual(len(got), 1, f"两条引擎结论分叉: {got}")
        return got.pop()

    def _prices(self, html: str, asin: str, site) -> dict:
        """{"current_price", "buybox_price", "original_price"}，两条引擎必须一致。"""
        keys = ("current_price", "buybox_price", "original_price")
        res = {name: {k: r[k] for k in keys}
               for name, r in _parse_both(html, asin, site).items()}
        self.assertEqual(res["slx"], res["lxml"], "两条引擎结论分叉")
        spec = M.get(site[1])
        self.assertEqual(self.p._slx_parse_current_price(HTMLParser(html), spec),
                         res["slx"]["current_price"])
        self.assertEqual(self.p._parse_current_price(lxml_html.fromstring(html), spec),
                         res["lxml"]["current_price"])
        return res["slx"]


# ══════════════════════════════════════════════════════════════════════
# H1  配送日期 / 天数
# ══════════════════════════════════════════════════════════════════════
class AccordionDeliveryTests(_Base):

    def test_ca_unselected_used_rows_faster_date_is_not_reported(self):
        """实测症状（未改动的真实页面）：CA B0CG19X7RN 导出 delivery_date='October 12'。

        选中的 newAccordionRow_0 只有 Friday, October 16；October 14 / Monday October 12
        都在**未选中**的二手行（Warehouse）里。正确值 October 16。
        """
        html = _load("dp_ca_B0CG19X7RN_accordion_delivery.html.gz")
        self.assertEqual(set(_row_times(html, "newAccordionRow_0")), {"Friday, October 16"},
                         "夹具前提：选中行只有 October 16")
        self.assertIn("Monday, October 12", _row_times(html, "usedAccordionRow"),
                      "夹具前提：二手行更快")
        self.assertIn("a-accordion-active", HTMLParser(html).css_first(
            "#newAccordionRow_0").attributes.get("class", ""))
        date, days = self._delivery(html, "B0CG19X7RN", _CA)
        self.assertEqual(date, "October 16")
        self.assertEqual(days, self.p._pick_delivery(["Friday, October 16"])[1])

    def test_us_used_row_prime_tomorrow_is_not_reported(self):
        """用户实测：US B0CQWZR6L8 的 Worker 留档页里二手行有 Prime「Tomorrow」，入库
        delivery_date='Tomorrow'；选中的全新行是 10 月 16 日。

        我们保存的那份（tests/fixtures/offer/dp_us_B0CQWZR6L8_accordion.html.gz，
        2026-10-11 05:49 UTC 抓的 www.amazon.com 商品页）二手行是 Saturday, October 17，
        触发不了。**改动**：只把二手行里的 "Saturday, October 17"（属性 2 处、正文 2 处）
        换成 "Tomorrow, October 12"，其余原样。
        """
        html = _edit_row(_load("dp_us_B0CQWZR6L8_accordion.html.gz"),
                         "usedAccordionRow", "Saturday, October 17", "Tomorrow, October 12", 4)
        self.assertIn("Tomorrow, October 12", _row_times(html, "usedAccordionRow"))
        self.assertEqual(set(_row_times(html, "newAccordionRow_0")), {"Friday, October 16"})
        date, days = self._delivery(html, "B0CQWZR6L8", _US)
        self.assertEqual(date, "October 16")
        self.assertEqual(days, self.p._pick_delivery(["Friday, October 16"])[1])

    def test_member_price_row_selected_reads_the_new_row(self):
        """US B0D12731YT（未改动）：选中的是会员价行，它**没有任何**配送信息；
        全新行有 Friday, October 16 + Prime「Tomorrow, October 12」。

        这一条在基线上也是 Tomorrow（全页最快恰好也是它），守的是「会员价行 -> 全新行」
        这一步：只读会员价行本身会恒为 N/A。
        """
        html = _load("dp_us_B0D12731YT_accordion_prime.html.gz")
        self.assertEqual(_row_times(html, "primeSavingsUpsellAccordionRow"), [],
                         "夹具前提：会员价行不带配送信息")
        self.assertEqual(self._delivery(html, "B0D12731YT", _US), ("Tomorrow", "1"))

    def test_member_price_row_selected_ignores_the_used_row(self):
        """B0D12731YT 的变体：**改动**是删掉全新行里那条 Prime「Tomorrow, October 12」
        次级配送消息（data-csa-c-delivery-time 以 Tomorrow 开头的节点），全新行只剩
        Friday, October 16；二手行原样（仍有 Tomorrow）。

        对上用户报告的形状：下单那一行慢、未选中的二手行快。必须报 October 16。
        """
        tree = HTMLParser(_load("dp_us_B0D12731YT_accordion_prime.html.gz"))
        removed = 0
        for n in tree.css("#newAccordionRow_1 [data-csa-c-delivery-time]"):
            if (n.attributes.get("data-csa-c-delivery-time") or "").startswith("Tomorrow"):
                n.decompose()
                removed += 1
        self.assertEqual(removed, 1)
        html = tree.html
        self.assertEqual(set(_row_times(html, "newAccordionRow_1")), {"Friday, October 16"})
        self.assertIn("Tomorrow, October 12", _row_times(html, "usedAccordionRow"))
        date, days = self._delivery(html, "B0D12731YT", _US)
        self.assertEqual(date, "October 16")
        self.assertEqual(days, self.p._pick_delivery(["Friday, October 16"])[1])

    def test_selected_used_row_reads_the_used_row(self):
        """读的是「选中行」，不是「全新行」。B0CQWZR6L8 的变体：**改动**是把
        a-accordion-active 从全新行挪到二手行（模拟二手 offer 赢得 buybox）。
        二手行只有 Saturday, October 17，全新行的 October 16 更快也不能报。"""
        html = _load("dp_us_B0CQWZR6L8_accordion.html.gz")
        html = _edit_row(html, "newAccordionRow_0",
                         'class="a-box a-accordion-active celwidget"', 'class="a-box celwidget"', 1)
        html = _edit_row(html, "usedAccordionRow",
                         'class="a-box celwidget" data-a-accordion-row-name="usedAccordionRow"',
                         'class="a-box a-accordion-active celwidget" '
                         'data-a-accordion-row-name="usedAccordionRow"', 1)
        date, days = self._delivery(html, "B0CQWZR6L8", _US)
        self.assertEqual(date, "October 17")
        self.assertEqual(days, self.p._pick_delivery(["Saturday, October 17"])[1])

    def test_unrecognisable_accordion_never_falls_back_to_the_whole_page(self):
        """accordion 在、但认不出任何行 -> N/A。回落全页就是这个 bug 本身。

        B0CG19X7RN 的变体：去掉 a-accordion-active，并把全新行改名（与 G2 那条同法）。
        """
        html = (_load("dp_ca_B0CG19X7RN_accordion_delivery.html.gz")
                .replace("a-accordion-active", "")
                .replace('<div id="newAccordionRow_0"', '<div id="someOtherRow_0"'))
        self.assertIn('id="buyBoxAccordion"', html)
        self.assertEqual(self._delivery(html, "B0CG19X7RN", _CA), ("N/A", "N/A"))
        self.assertEqual(self._shipping(html, "$74.99", _CA), "N/A")

    def test_single_offer_layout_still_reads_the_whole_page(self):
        """非 accordion（CA B0CRH6FDD7，传统单 offer buybox）：不收窄，仍是全页最快。"""
        html = _load("dp_ca_B0CRH6FDD7_single_was.html.gz")
        self.assertNotIn("buyBoxAccordion", html)
        for tree in (HTMLParser(html), lxml_html.fromstring(html)):
            self.assertIsNone(self.p._accordion_purchase_row(tree))
            self.assertIsNone(self.p._member_price_scopes(tree))
        times = [n.attributes.get("data-csa-c-delivery-time")
                 for n in HTMLParser(html).css("[data-csa-c-delivery-time]")]
        self.assertEqual(len(times), 2)
        self.assertEqual(self._delivery(html, "B0CRH6FDD7", _CA), self.p._pick_delivery(times))


# ══════════════════════════════════════════════════════════════════════
# H1  运费
# ══════════════════════════════════════════════════════════════════════
class AccordionShippingTests(_Base):

    def test_selected_rows_delivery_price_not_the_first_rows(self):
        """原来取**全页第一个** data-csa-c-delivery-price：选中行不排在 DOM 第一时
        （二手 offer 赢得 buybox），读到的是全新行的运费。

        B0CQWZR6L8 的变体，**改动**两处：把 a-accordion-active 从全新行挪到二手行；
        二手行的 data-csa-c-delivery-price="FREE" -> "$6.99"（2 处；带金额的取值形状
        真实存在，如 CA B0D12731YT 页上的 "$11.61"）。必须报选中行的 $6.99。
        """
        html = _load("dp_us_B0CQWZR6L8_accordion.html.gz")
        html = _edit_row(html, "newAccordionRow_0",
                         'class="a-box a-accordion-active celwidget"', 'class="a-box celwidget"', 1)
        html = _edit_row(html, "usedAccordionRow",
                         'class="a-box celwidget" data-a-accordion-row-name="usedAccordionRow"',
                         'class="a-box a-accordion-active celwidget" '
                         'data-a-accordion-row-name="usedAccordionRow"', 1)
        html = _edit_row(html, "usedAccordionRow", 'data-csa-c-delivery-price="FREE"',
                         'data-csa-c-delivery-price="$6.99"', 2)
        self.assertEqual(self._shipping(html, "$42.61", _US), "$6.99")
        for eng, r in _parse_both(html, "B0CQWZR6L8", _US).items():
            self.assertEqual(r["buybox_shipping"], "$6.99", eng)

    def test_member_price_row_selected_reads_the_new_rows_block(self):
        """会员价行的 #deliveryBlockMessage 是空的，必须读全新行。

        B0D12731YT 的变体，**改动**：全新行的 data-csa-c-delivery-price="FREE" -> "$6.99"
        （3 处）。要它不是 FREE 才分得出来：只读会员价行时什么都读不到，会落到
        「什么都没有 -> FREE」的兜底。（基线全页取第一个恰好也是全新行的，这一条守的是
        「会员价行 -> 全新行」这一步。）
        """
        html = _edit_row(_load("dp_us_B0D12731YT_accordion_prime.html.gz"), "newAccordionRow_1",
                         'data-csa-c-delivery-price="FREE"', 'data-csa-c-delivery-price="$6.99"', 3)
        self.assertEqual(self._shipping(html, "$35.99", _US), "$6.99")


# ══════════════════════════════════════════════════════════════════════
# H2  会员价行被选中时，现价取普通价
# ══════════════════════════════════════════════════════════════════════
class MemberPriceTests(_Base):

    def test_member_price_row_selected_reports_the_regular_price(self):
        """实测症状（未改动的真实页面）：US B0D12731YT 导出 现价 = buybox 价 = $26.99，
        那是选中的会员价行（"Prime Member Price"）的价格；全新行的普通价是 $35.99。
        美国站另外三张同形页面（B0DGLHZBJR / B0F3CQT8G5 / B0FFGFT343）见 PR 说明的回归表。
        """
        html = _load("dp_us_B0D12731YT_accordion_prime.html.gz")
        member = HTMLParser(html).css_first("#primeSavingsUpsellAccordionRow")
        self.assertIn("a-accordion-active", member.attributes.get("class", ""))
        self.assertIn("Prime Member Price", member.text())
        self.assertIn("$26.99", member.text(), "夹具前提：会员价在选中行里")
        got = self._prices(html, "B0D12731YT", _US)
        self.assertEqual(got["current_price"], "$35.99")
        self.assertEqual(got["buybox_price"], "$35.99")
        # 原价仍按 G2 读选中行（会员价行自己的 "List Price: $34.99"），不因 H2 改变。
        self.assertEqual(got["original_price"], "$34.99")

    def test_member_price_row_without_a_new_row_gives_na(self):
        """选中会员价行、却找不到全新行 -> 现价 / buybox 价 N/A，配送 / 运费也 N/A。
        拿会员价顶上正是用户否掉的那个值。

        B0D12731YT 的变体：**改动**是把全新行的 id 改名（<div id="newAccordionRow_1"
        -> someOtherRow_1）。
        """
        html = _load("dp_us_B0D12731YT_accordion_prime.html.gz").replace(
            '<div id="newAccordionRow_1"', '<div id="someOtherRow_1"')
        self.assertIsNone(HTMLParser(html).css_first('#buyBoxAccordion div[id^="newAccordionRow"]'))
        got = self._prices(html, "B0D12731YT", _US)
        self.assertEqual(got["current_price"], "N/A")
        self.assertEqual(got["buybox_price"], "N/A")
        self.assertEqual(self._delivery(html, "B0D12731YT", _US), ("N/A", "N/A"))
        self.assertEqual(self._shipping(html, "N/A", _US), "N/A")

    # ---- 反向：没选中会员价行的 accordion 页，现价一分不动 ----
    def test_new_row_selected_price_is_untouched(self):
        """B0CQWZR6L8：选中的是全新行，现价 $79.99 照旧（二手行 $42.61 不相干）。"""
        html = _load("dp_us_B0CQWZR6L8_accordion.html.gz")
        for tree in (HTMLParser(html), lxml_html.fromstring(html)):
            self.assertIsNone(self.p._member_price_scopes(tree))
        got = self._prices(html, "B0CQWZR6L8", _US)
        self.assertEqual((got["current_price"], got["buybox_price"]), ("$79.99", "$79.99"))


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main(verbosity=2)
