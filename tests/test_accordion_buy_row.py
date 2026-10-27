"""accordion buybox 的配送 / 运费只读下单那一行，会员价行被选中时现价取普通价。

  H1  配送日期 / 配送天数 / 运费：原来是在**整个购买框**里取 —— 日期取全页最快、
      运费取全页第一个配送价。未选中的二手行更快时就报了二手行的日期
      （实测 CA B0CG19X7RN：选中的全新行只有 October 16，导出 October 12；用户实测
      US B0CQWZR6L8：二手行 Prime「Tomorrow」入库）。现在只读普通买家下单的那一行：
      选中行；选中的是会员价行（它不带配送信息）时读全新行 newAccordionRow_N。
      **语义不变**：仍是那一行里能看到的最快日期（含 Prime 会员专享）。
  H2  选中会员价行（"Prime Member Price"）时 current_price / buybox_price 取全新行的
      普通价。用户口径：现价 = 普通买家能买到的价格，会员价不保留。
  P1  同一情形下原价也跟全新行取（用户口径 2026-10-14：与现价同一行，那一行没有
      划线价就是 N/A）。原来读会员价行的 List / Typical —— 那是相对会员价标的，
      配上普通价就成了 原价 ≤ 现价（US B0D12731YT $34.99 / $35.99）。

夹具是 tests/fixtures/offer/ 下的真实页面片段（来源见那里的 README）。凡是在片段上
改过东西的用例，docstring 里写明改了什么、为什么。

时钟钉在美国夹具的抓取时刻（``_CAPTURED_AT``，见 ``_Base.setUp``）。配送天数是「解析当天
到页面日期」的差，不钉的话同一个用例换个日子跑就换答案：US B0D12731YT 的全新行有
Friday, October 16 和 Prime「Tomorrow」，到了 10 月 15 日两者都是 1 天，``_pick_delivery``
取先出现的那个，就成了 October 16（每年 10 月 15 日都会红）。钉住之后天数可以直接写死。
CA B0CG19X7RN 是早一天（2026-10-10 13:52 UTC）抓的，页面上的日期（October 12 / 14 / 16）
都晚于钉住的那一刻，共用同一个时钟不影响它的结论。

两条引擎都要跑：生产上 lxml 分支只在 selectolax 缺失时才走，靠模块级开关永远测不到它。
"""
from __future__ import annotations

import gzip
import os
import re
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging  # noqa: E402

from lxml import html as lxml_html  # noqa: E402
from selectolax.parser import HTMLParser  # noqa: E402

import worker.parser as parser_mod  # noqa: E402
from common.core import marketplace as M  # noqa: E402
from worker.parser import AmazonParser  # noqa: E402

try:
    import dateparser  # noqa: E402
except ImportError:  # 没装时解析器换算不了「月 + 日」，断言这类日期的用例照样会红，不在这里掩盖
    dateparser = None

_FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "offer")
_CA = ("K1V 7P8", "amazon.ca")
_US = ("10001", None)

#: 美国夹具页面的抓取时刻（2026-10-11 05:49 UTC，见 tests/fixtures/offer/README.md）。
#: 解析器里「今天」有两个来源，都要钉：``worker.parser`` 模块里的 ``datetime.now()``
#: （``_pick_delivery`` 拿它当减数），以及 dateparser 自己的「现在」（``PREFER_DATES_FROM
#: future`` 靠它判断一个月 + 日是今年还是明年，经 ``RELATIVE_BASE`` 设定）。
_CAPTURED_AT = datetime(2026, 10, 11, 5, 49)


class _CapturedDatetime(datetime):
    """``worker.parser.datetime`` 的替身：``now()`` 恒为 ``_CAPTURED_AT``（按 UTC 解释）。"""

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return _CAPTURED_AT
        return _CAPTURED_AT.replace(tzinfo=timezone.utc).astimezone(tz)


def _dateparser_at_capture(real_parse):
    def parse(date_string, *args, settings=None, **kwargs):
        settings = dict(settings or {})
        settings.setdefault("RELATIVE_BASE", _CAPTURED_AT)
        return real_parse(date_string, *args, settings=settings, **kwargs)
    return parse


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


def _select_used_row(html: str) -> str:
    """B0CQWZR6L8 的变体：把 a-accordion-active 从全新行挪到二手行（模拟二手 offer
    赢得 buybox）。其余原样。"""
    html = _edit_row(html, "newAccordionRow_0",
                     'class="a-box a-accordion-active celwidget"', 'class="a-box celwidget"', 1)
    return _edit_row(html, "usedAccordionRow",
                     'class="a-box celwidget" data-a-accordion-row-name="usedAccordionRow"',
                     'class="a-box a-accordion-active celwidget" '
                     'data-a-accordion-row-name="usedAccordionRow"', 1)


def _edit_between(html: str, start: str, end: str, old: str, new: str, expect: int) -> str:
    """只在 ``start`` 与其后第一个 ``end`` 之间把 ``old`` 换成 ``new``，并核对次数。
    用来改中栏的某一个分行价格块（它们在 #apex_desktop 里按 DOM 顺序相邻）。"""
    i = html.index(start)
    j = html.index(end, i + len(start))
    seg = html[i:j]
    assert seg.count(old) == expect, (start, old, seg.count(old))
    return html[:i] + seg.replace(old, new) + html[j:]


def _row_times(html: str, row_id: str) -> list:
    """某一行里全部 data-csa-c-delivery-time 的值（夹具前提断言用）。"""
    return [n.attributes.get("data-csa-c-delivery-time")
            for n in HTMLParser(html).css(f"#{row_id} [data-csa-c-delivery-time]")]


class _Base(unittest.TestCase):

    def setUp(self):
        self.p = AmazonParser()
        # 时钟钉在抓取时刻（为什么见模块说明）。整个文件都钉：价格类用例不看日期，
        # 钉了也无妨，省得哪天有人往这里加配送断言时忘了。
        patches = [mock.patch.object(parser_mod, "datetime", _CapturedDatetime)]
        if dateparser is not None:
            patches.append(mock.patch.object(
                dateparser, "parse", _dateparser_at_capture(dateparser.parse)))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

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
        self.assertEqual(self.p._slx_parse_original_price(HTMLParser(html)),
                         res["slx"]["original_price"])
        self.assertEqual(self.p._parse_original_price(lxml_html.fromstring(html)),
                         res["lxml"]["original_price"])
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
        self.assertEqual((date, days), ("October 16", "5"))  # 10-11 -> 10-16

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
        self.assertEqual((date, days), ("October 16", "5"))  # 10-11 -> 10-16

    def test_member_price_row_selected_reads_the_new_row(self):
        """US B0D12731YT（未改动）：选中的是会员价行，它**没有任何**配送信息；
        全新行有 Friday, October 16 + Prime「Tomorrow, October 12」。

        这一条在基线上也是 Tomorrow（全页最快恰好也是它），守的是「会员价行 -> 全新行」
        这一步：只读会员价行本身会恒为 N/A。

        依赖时钟钉在抓取时刻：不钉的话每年 10 月 15 日 October 16 也是 1 天，并列时取先
        出现的那个，就成了 ('October 16', '1')。
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
        self.assertEqual((date, days), ("October 16", "5"))  # 10-11 -> 10-16

    def test_selected_used_row_reads_the_used_row(self):
        """读的是「选中行」，不是「全新行」。B0CQWZR6L8 的变体：**改动**是把
        a-accordion-active 从全新行挪到二手行（模拟二手 offer 赢得 buybox）。
        二手行只有 Saturday, October 17，全新行的 October 16 更快也不能报。

        已知的跨 offer 混拼（不在本用例断言里，记下来免得被当成已修）：这个变体下
        配送 / 运费 / 品相 / 原价读的是二手行，现价与 buybox 价却仍是全新行的 $79.99
        —— 现价只在**会员价行**被选中时换行（用户批的范围）。语料 66 张里没有一张是
        二手行被选中的；要扩成「现价也跟下单那一行走」得用户另行同意。"""
        html = _select_used_row(_load("dp_us_B0CQWZR6L8_accordion.html.gz"))
        date, days = self._delivery(html, "B0CQWZR6L8", _US)
        self.assertEqual((date, days), ("October 17", "6"))  # 10-11 -> 10-17

    def test_text_fallback_is_scoped_to_the_purchase_row_too(self):
        """页面不带 data-csa-c-delivery-time 属性时，配送走「整段配送文本 + 正则」的
        回退；那一步同样只读下单那一行。

        CA B0CG19X7RN 的变体：**改动**是删掉全部 data-csa-c-delivery-time 属性
        （5 处；节点与正文原样），逼解析走文本回退。回退要是读整页，未选中二手行
        正文里的 "Monday, October 12" 就会被当成最快日期报出来（基线即如此）。
        """
        html, n = re.subn(r'\sdata-csa-c-delivery-time="[^"]*"', "",
                          _load("dp_ca_B0CG19X7RN_accordion_delivery.html.gz"))
        self.assertEqual(n, 5)
        self.assertEqual(_row_times(html, "newAccordionRow_0"), [])
        self.assertIn("Monday, October 12",
                      HTMLParser(html).css_first("#usedAccordionRow").text(),
                      "夹具前提：二手行正文里有更快的日期")
        date, days = self._delivery(html, "B0CG19X7RN", _CA)
        self.assertEqual((date, days), ("October 16", "5"))  # 10-11 -> 10-16

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
        self.assertEqual(times, ["Wednesday, October 14", "Monday, October 12"])
        self.assertEqual(self._delivery(html, "B0CRH6FDD7", _CA), self.p._pick_delivery(times))
        self.assertEqual(self._delivery(html, "B0CRH6FDD7", _CA), ("October 12", "1"))


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
        html = _select_used_row(_load("dp_us_B0CQWZR6L8_accordion.html.gz"))
        html = _edit_row(html, "usedAccordionRow", 'data-csa-c-delivery-price="FREE"',
                         'data-csa-c-delivery-price="$6.99"', 2)
        self.assertEqual(self._shipping(html, "$42.61", _US), "$6.99")
        for eng, r in _parse_both(html, "B0CQWZR6L8", _US).items():
            self.assertEqual(r["buybox_shipping"], "$6.99", eng)

    def test_free_over_threshold_text_of_another_row_is_ignored(self):
        """满额免邮的文案判定（"FREE delivery … on orders over $35"）同样只看下单那一行。
        原来把**所有行**的 #deliveryBlockMessage 文案拼成一段再判：别的行里有这句，
        选中行自己的运费就被盖成 FREE。

        B0CQWZR6L8 的变体，**改动**三处：前两处同上一条（active 挪到二手行、二手行
        配送价 FREE -> $6.99）；第三处在**全新行**的 #deliveryBlockMessage 开头插一句
        "FREE delivery on orders over $35.00 shipped by Amazon"。这句是合成的：66 张
        真实页的购买框里都没有这种措辞（只在商品轮播里有 "FREE Shipping on orders over
        $35"），句式取自这个分支要匹配的文案（见 _slx_parse_buybox_shipping 的 F-012 注释）。
        """
        html = _select_used_row(_load("dp_us_B0CQWZR6L8_accordion.html.gz"))
        html = _edit_row(html, "usedAccordionRow", 'data-csa-c-delivery-price="FREE"',
                         'data-csa-c-delivery-price="$6.99"', 2)
        block = '<div id="deliveryBlockMessage" class="a-section a-spacing-none">'
        html = _edit_row(html, "newAccordionRow_0", block,
                         block + "<span>FREE delivery on orders over $35.00 shipped by Amazon</span>", 1)
        self.assertEqual(self._shipping(html, "$42.61", _US), "$6.99")
        for eng, r in _parse_both(html, "B0CQWZR6L8", _US).items():
            self.assertEqual(r["buybox_shipping"], "$6.99", eng)

    def test_free_over_threshold_with_the_amount_in_its_own_tag(self):
        """满额免邮的金额包在子标签里时，两条引擎得出同一个结论。

        selectolax 一侧把 #deliveryBlockMessage 下每个节点的 ``text(strip=True)`` 用空格
        拼起来，而父节点的 ``text(strip=True)`` 是无分隔符拼接：``"…on orders over$35.00
        shipped…"``。门槛正则是 ``over\\s*<币种符号>\\s*``，两条引擎都认得出（main 上
        原来写死 ``over \\$``，selectolax 认不出、lxml 认得出，两条引擎分叉）。

        B0CQWZR6L8 的变体（全新行选中，未挪动），**改动**一处：在全新行的
        #deliveryBlockMessage 开头插一句 "FREE delivery on orders over <span
        class="a-text-bold">$35.00</span> shipped by Amazon"。合成的句式；
        金额加粗用的是页面上配送日期那种 ``a-text-bold`` 标签。
        """
        html = _load("dp_us_B0CQWZR6L8_accordion.html.gz")
        block = '<div id="deliveryBlockMessage" class="a-section a-spacing-none">'
        html = _edit_row(html, "newAccordionRow_0", block,
                         block + '<span>FREE delivery on orders over '
                                 '<span class="a-text-bold">$35.00</span> shipped by Amazon</span>', 1)
        self.assertEqual(self._shipping(html, "$20.00", _US), "N/A")
        self.assertEqual(self._shipping(html, "$42.61", _US), "FREE")

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
        # P1：原价跟现价同一行（全新行），全新行没有划线价 -> N/A。会员价行自己的
        # "List Price: $34.99" 是相对会员价标的，原来报的就是它（原价 < 现价）。
        self.assertEqual(got["original_price"], "N/A")

    def test_member_price_row_without_a_new_row_gives_na(self):
        """选中会员价行、却找不到全新行 -> 现价 / buybox 价 / 原价 N/A，配送 / 运费也 N/A。
        拿会员价（及相对它标的 List 价）顶上正是用户否掉的那个值。

        B0D12731YT 的变体：**改动**是把全新行的 id 改名（<div id="newAccordionRow_1"
        -> someOtherRow_1）。
        """
        html = _load("dp_us_B0D12731YT_accordion_prime.html.gz").replace(
            '<div id="newAccordionRow_1"', '<div id="someOtherRow_1"')
        self.assertIsNone(HTMLParser(html).css_first('#buyBoxAccordion div[id^="newAccordionRow"]'))
        got = self._prices(html, "B0D12731YT", _US)
        self.assertEqual(got["current_price"], "N/A")
        self.assertEqual(got["buybox_price"], "N/A")
        self.assertEqual(got["original_price"], "N/A")
        self.assertEqual(self._delivery(html, "B0D12731YT", _US), ("N/A", "N/A"))
        self.assertEqual(self._shipping(html, "N/A", _US), "N/A")

    # ---- P1  原价跟全新行取 ----
    def test_new_rows_own_list_price_is_kept(self):
        """US B0F3CQT8G5（未改动的真实页面）：选中会员价行（$79.99），全新行普通价 $84.99，
        全新行**自己**的中栏价格块（apex_desktop_newAccordionRow，页面上是隐藏的那份）
        标了 "List Price: $99.99"。原价跟全新行取就是它，结果不变。

        会员价行碰巧也标 $99.99，所以这一条在基线上也绿；下一条把两者拆开。
        """
        html = _load("dp_us_B0F3CQT8G5_accordion_prime_list.html.gz")
        tree = HTMLParser(html)
        self.assertIn("a-accordion-active",
                      tree.css_first("#primeSavingsUpsellAccordionRow").attributes.get("class", ""))
        self.assertEqual(
            [n.text(strip=True) for n in tree.css(
                '#apex_desktop_newAccordionRow span[data-a-strike="true"] span.a-offscreen')],
            ["$99.99"], "夹具前提：全新行自己有划线价")
        got = self._prices(html, "B0F3CQT8G5", _US)
        self.assertEqual((got["current_price"], got["buybox_price"]), ("$84.99", "$84.99"))
        self.assertEqual(got["original_price"], "$99.99")

    def test_original_price_comes_from_the_new_row_not_the_member_row(self):
        """B0F3CQT8G5 的变体：**改动**是把会员价行中栏块
        （apex_desktop_primeSavingsUpsellAccordionRow）里的 "$99.99" 换成 "$89.99"
        （3 处："List Price:" 文案、a-offscreen、aria-hidden），全新行原样。
        两行的划线价不同，必须报全新行的 $99.99 —— 读会员价行的话是 $89.99（基线即如此）。
        """
        html = _edit_between(_load("dp_us_B0F3CQT8G5_accordion_prime_list.html.gz"),
                             '<div id="apex_desktop_primeSavingsUpsellAccordionRow"',
                             '<div id="apex_desktop_newAccordionRow"', "$99.99", "$89.99", 3)
        got = self._prices(html, "B0F3CQT8G5", _US)
        self.assertEqual(got["current_price"], "$84.99")
        self.assertEqual(got["original_price"], "$99.99")

    def test_single_centre_block_is_not_read_when_member_row_selected(self):
        """较早的 accordion 形状（buybox 分行、中栏只有一份价格块）上，原价会多读整个
        ``#apex_desktop``（G2 的兜底，那一份是主推 offer 的）。选中会员价行时主推的就是
        会员价，那一份就是会员价的，不读。

        B0D12731YT 的变体，**改动**同 test_offer_price_seller_aod 里那条单中栏用例的做法：
        删掉中栏的全新行块与二手行块，去掉会员价行块的 id —— ``#apex_desktop`` 里只剩
        会员价那份 "List Price: $34.99"。buybox 的全新行自己没有划线价 -> N/A
        （基线经 ``#apex_desktop`` 兜底报 $34.99）。
        """
        tree = HTMLParser(_load("dp_us_B0D12731YT_accordion_prime.html.gz"))
        tree.css_first("#apex_desktop_newAccordionRow").decompose()
        tree.css_first("#apex_desktop_usedAccordionRow").decompose()
        del tree.css_first("#apex_desktop_primeSavingsUpsellAccordionRow").attrs["id"]
        html = tree.html
        self.assertNotRegex(html, r'id="apex_desktop_[^"]*AccordionRow')
        self.assertIn("$34.99", HTMLParser(html).css_first("#apex_desktop").text(),
                      "夹具前提：单中栏里是会员价那份 List 价")
        self.assertIsNone(HTMLParser(html).css_first(
            '#newAccordionRow_1 span[data-a-strike="true"]'), "夹具前提：buybox 全新行没有划线价")
        got = self._prices(html, "B0D12731YT", _US)
        self.assertEqual(got["current_price"], "$35.99")
        self.assertEqual(got["original_price"], "N/A")

    def test_new_rows_own_price_block_unreadable_gives_na_not_the_center_column(self):
        """全新行**行内**的价格块读不到 -> 现价 / buybox 价 N/A，不去中栏
        apex_desktop_newAccordionRow 里捞：那一块「要付的价」的 a-offscreen 是空的，
        第一个有字的是单价 $1.80 / count（B0F3CQT8G5 上是 List Price $99.99）。

        B0D12731YT 的变体：**改动**是把全新行里的 #corePrice_feature_div 与
        #apex_offerDisplay_desktop 两个 id 改名（各 1 处），模拟 Amazon 改了行内价格块。
        """
        html = _load("dp_us_B0D12731YT_accordion_prime.html.gz")
        for block in ("corePrice_feature_div", "apex_offerDisplay_desktop"):
            html = _edit_row(html, "newAccordionRow_1", f'<div id="{block}"',
                             f'<div id="{block}_renamed"', 1)
        self.assertEqual(
            [n.text(strip=True) for n in HTMLParser(html).css(
                "#apex_desktop_newAccordionRow #corePriceDisplay_desktop_feature_div span.a-offscreen")],
            ["", "$1.80"], "夹具前提：中栏全新行块里首个有字的 a-offscreen 是单价")
        got = self._prices(html, "B0D12731YT", _US)
        self.assertEqual(got["current_price"], "N/A")
        self.assertEqual(got["buybox_price"], "N/A")

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
