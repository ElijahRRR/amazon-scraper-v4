"""【本分支说明】这是 main 上 ``tests/test_offer_price_seller_aod.py`` 的副本，只用美国站片段，在加拿大分支上守住
美国站的同一批行为（加拿大分支自己的 ``tests/test_offer_price_seller_aod.py`` 大多换成了加拿大片段）。
main 改那个文件时，把改动同步到这里。
与 main 原文的唯一差别：端到端用例的假 session 补了
``AMAZON_BASE``、假 slot 的 ``ensure_ready`` 收下站点 / 邮编参数 —— 本分支的 engine 按任务站点
拼 AOD 地址、按任务站点建会话，main 的替身没有这两样。

buybox 报价相关的三处修复，用美国站真实页面片段守（tests/fixtures/offer/README.md）。

  G2  accordion 布局的原价：只读选中那一行，不再落到二手行的 "New Condition Price"
  G6  卖家链接里没有 ``seller=``（Warehouse / Amazon Resale 的类目节点链接）时读
      **同一张加购表单**的 merchantID
  G7  无推荐报价页走 aodAjaxMain 兜底：回填第一个报价的卖家名 / ID，库存数用该报价的
      maxQty，不再写死 "5"

每条修复都有**反向**用例（真实的划线价不能丢、链接自带 ID 时不许被表单覆盖、
别的 offer 的表单不许借用、商品页已有卖家时不许拼对），因为这几处最容易修过头。

两条解析引擎都要跑：生产上 lxml 分支只在 selectolax 缺失时才走，靠模块级开关
永远测不到它。``parse_offer_listing`` 只有 selectolax 实现，见 G7 那组的说明。
"""
from __future__ import annotations

import asyncio
import gzip
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# worker/session.py 模块级 import curl_cffi；装不上的环境只桩这一个模块
# （与 tests/test_engine_not_found.py 同一做法），其余一律用真的。
_SAVED_SESSION_MODULE = sys.modules.get("worker.session")
try:  # pragma: no cover - 装了 curl_cffi 的机器走这一支
    import curl_cffi  # noqa: F401
    _STUBBED_SESSION = False
except ImportError:  # pragma: no cover
    _m = types.ModuleType("worker.session")
    _m.AmazonSession = object
    sys.modules["worker.session"] = _m
    _STUBBED_SESSION = True

import logging  # noqa: E402

from lxml import html as lxml_html  # noqa: E402
from selectolax.parser import HTMLParser  # noqa: E402

import worker.parser as parser_mod  # noqa: E402
from common.core import _is_parse_failure  # noqa: E402
from worker.parser import AmazonParser  # noqa: E402

_FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "offer")
_ZIP = "10001"


def tearDownModule():
    if _STUBBED_SESSION:  # pragma: no cover
        if _SAVED_SESSION_MODULE is None:
            sys.modules.pop("worker.session", None)
        else:
            sys.modules["worker.session"] = _SAVED_SESSION_MODULE


def _load(name: str) -> str:
    with gzip.open(os.path.join(_FIX, name), "rt", encoding="utf-8") as f:
        return f.read()


def _one_change(html: str, old: str, new: str, count: int = 1) -> str:
    """构造变体：``old`` 在片段里的出现次数必须正好是 ``count``，改动含义才确定。"""
    assert html.count(old) == count, (html.count(old), old)
    return html.replace(old, new)


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


def _parse_both(html: str, asin: str) -> dict:
    """``parse_product`` 两条引擎各跑一遍 -> {"slx": result, "lxml": result}。"""
    out = {}
    for name, slx in (("slx", True), ("lxml", False)):
        with _Engine(slx):
            out[name] = AmazonParser().parse_product(html, asin, _ZIP)
    return out


# ══════════════════════════════════════════════════════════════════════
# G2  accordion 原价
# ══════════════════════════════════════════════════════════════════════
class AccordionOriginalPriceTests(unittest.TestCase):
    """多 offer accordion 里每一行都有自己的 #corePriceDisplay_desktop_feature_div
    （id 重复）。原价只能从选中那一行（a-accordion-active）读。"""

    def _orig(self, html: str, asin: str) -> str:
        p = AmazonParser()
        direct = {p._slx_parse_original_price(HTMLParser(html)),
                  p._parse_original_price(lxml_html.fromstring(html))}
        self.assertEqual(len(direct), 1, f"两条引擎结论分叉: {direct}")
        full = {r["original_price"] for r in _parse_both(html, asin).values()}
        self.assertEqual(full, direct, "parse_product 出口与函数本身不一致")
        return direct.pop()

    def test_used_rows_new_condition_price_is_not_the_list_price(self):
        """实测症状：B0CQWZR6L8 导出 原价 $79.99 = 现价 $79.99。

        选中的是全新行，它自己没有划线价；那个 $79.99 来自**未选中的**二手行
        （"New Condition Price: $79.99"），是全新件现价，不是全新 offer 的原价。
        正确值 N/A。B0GGGQBJSB（$39.99）、B0GKNHN7Q5（$119.99）同形。
        """
        html = _load("dp_us_B0CQWZR6L8_accordion.html.gz")
        self.assertIn("New Condition Price", html, "夹具前提：二手行的参照价在页面上")
        self.assertEqual(self._orig(html, "B0CQWZR6L8"), "N/A")

    # ---- 反向：真实的划线价一个都不能丢 ----
    def test_selected_new_row_keeps_its_own_list_price(self):
        """B0CRGYZ4HV：accordion，全新行自己的中栏块就有 "List Price: $44.99"。"""
        html = _load("dp_us_B0CRGYZ4HV_accordion_list.html.gz")
        self.assertIn("$44.99", HTMLParser(html).css_first(
            "#apex_desktop_newAccordionRow").text(), "夹具前提：全新行自己的 List 价")
        self.assertEqual(self._orig(html, "B0CRGYZ4HV"), "$44.99")

    def test_selected_prime_row_reads_the_new_rows_strike_price(self):
        """B0D12731YT：选中的是会员价行，"List Price: $34.99" 在它自己的价格块里
        （#apex_desktop_primeSavingsUpsellAccordionRow），是相对会员价 $26.99 标的。
        用户口径（2026-10-14）：原价跟现价同一行 —— 全新行（现价 $35.99 就读它），
        而全新行自己没有划线价 -> N/A。原来报 $34.99，原价 < 现价。
        全新行自己标了划线价的页（B0F3CQT8G5）见 tests/test_accordion_buy_row.py。"""
        html = _load("dp_us_B0D12731YT_accordion_prime.html.gz")
        self.assertIn("$34.99", HTMLParser(html).css_first(
            "#apex_desktop_primeSavingsUpsellAccordionRow").text(), "夹具前提：会员价行有 List 价")
        self.assertEqual(self._orig(html, "B0D12731YT"), "N/A")

    def test_single_offer_layout_is_untouched(self):
        """非 accordion（B0CRH6FDD7，"List Price: $49.99"）：选择器与原来逐字节相同。"""
        html = _load("dp_us_B0CRH6FDD7_single_list.html.gz")
        self.assertIsNone(HTMLParser(html).css_first("#buyBoxAccordion"))
        self.assertEqual(self._orig(html, "B0CRH6FDD7"), "$49.99")

    def test_without_active_row_falls_back_to_the_new_row(self):
        """构造变体：去掉 a-accordion-active。没有展开行时取全新行（页面主推的就是它）
        —— 不许回落全页、读到二手行的 $79.99。"""
        html = _load("dp_us_B0CQWZR6L8_accordion.html.gz")
        stripped = html.replace("a-accordion-active", "")
        self.assertNotIn("a-accordion-active", stripped)
        self.assertEqual(self._orig(stripped, "B0CQWZR6L8"), "N/A")

    def test_layout_without_per_row_price_blocks_reads_the_single_centre_block(self):
        """较早的 accordion 形状：buybox 分行，中栏只有**一份**价格块（没有
        ``apex_desktop_<行名>``）。那一份就是主推 offer 的，真实的 List 价不能丢。

        用 B0CRGYZ4HV 模拟：删掉二手行的中栏块、去掉全新行中栏块的 id，只剩
        ``#apex_desktop`` 包着全新行那份 "List Price: $44.99"。buybox 里的全新行自己
        没有划线价，所以只读「分行块 + buybox 行」会丢成 N/A（改动前取得到）。
        """
        tree = HTMLParser(_load("dp_us_B0CRGYZ4HV_accordion_list.html.gz"))
        tree.css_first("#apex_desktop_usedAccordionRow").decompose()
        new_block = tree.css_first("#apex_desktop_newAccordionRow")
        del new_block.attrs["id"]
        html = tree.html
        self.assertNotRegex(html, r'id="apex_desktop_[^"]*AccordionRow')
        self.assertIn('id="apex_desktop"', html)
        self.assertIsNone(HTMLParser(html).css_first(
            '#newAccordionRow_0 span[data-a-strike="true"]'), "夹具前提：buybox 行自己没有划线价")
        self.assertEqual(self._orig(html, "B0CRGYZ4HV"), "$44.99")

    def test_unrecognisable_accordion_never_falls_back_to_the_whole_page(self):
        """accordion 在、但认不出任何行 -> N/A。回落全页就是这个 bug 本身。"""
        html = _load("dp_us_B0CQWZR6L8_accordion.html.gz")
        mangled = (html.replace("a-accordion-active", "")
                   .replace('id="newAccordionRow_0"', 'id="someOtherRow_0"'))
        self.assertIn('id="buyBoxAccordion"', mangled)
        self.assertEqual(self._orig(mangled, "B0CQWZR6L8"), "N/A")


# ══════════════════════════════════════════════════════════════════════
# G6  链接里没有 seller= 时的卖家 ID
# ══════════════════════════════════════════════════════════════════════
#: B0CQWZR6L8 全新行（Smibuy Direct）的两条卖家档案链接（链接 + 浮层各一条）。
_NEW_ROW_LINK = ('<a href="/gp/help/seller/at-a-glance.html/ref=dp_merchant_link?ie=UTF8'
                 '&amp;seller=A1IOUXTP7ANG7D&amp;asin=B0CQWZR6L8&amp;ref_=dp_merchant_link'
                 '&amp;isAmazonFulfilled=1" id="sellerProfileTriggerId"')
#: 同页二手行（Amazon Resale）表单里的 hidden input。
_USED_FORM_ANCHOR = '<input type="hidden" id="usedMerchantID" name="usedMerchantID" value="A2L77EE7U53NWQ">'
#: 同页二手行 #merchant-info 里 Amazon Resale 的真实链接（类目节点，没有 seller=）。
_RESALE_HREF = "https://www.amazon.com/b?node=10158976011"


class SellerIdFromFormTests(unittest.TestCase):

    def _seller(self, html: str):
        p = AmazonParser()
        got = {p._parse_seller(HTMLParser(html), html),
               p._parse_seller(lxml_html.fromstring(html), html)}
        self.assertEqual(len(got), 1, f"两条引擎结论分叉: {got}")
        return got.pop()

    def _resale_link_in_used_form(self, blank_merchant_id: bool = False) -> str:
        """构造变体：卖家链接是 Amazon Resale 的类目节点、且在二手行表单里。

        30 张美国页里没有一张的 buybox 选中 Warehouse / Amazon Resale 的二手 offer
        （真机采集里见过：卖家链接 ``/b?node=…``、ID 只在同一张加购表单的
        ``input#merchantID``）。这里在 B0CQWZR6L8 上把全新行的两条 sellerProfileTriggerId
        改名（让二手行的那条成为页面上第一条），再在二手行表单里放一条指向同页
        #merchant-info 里那个真实 Amazon Resale 链接的 sellerProfileTriggerId。
        """
        html = _load("dp_us_B0CQWZR6L8_accordion.html.gz")
        html = _one_change(html, 'id="sellerProfileTriggerId"', 'id="sellerProfileTriggerId-moved"', 2)
        self.assertIn(f'href="{_RESALE_HREF}"', html, "夹具前提：同页的 Amazon Resale 链接")
        html = _one_change(html, _USED_FORM_ANCHOR, _USED_FORM_ANCHOR
                           + f'<a id="sellerProfileTriggerId" href="{_RESALE_HREF}">Amazon Resale</a>')
        if blank_merchant_id:
            html = _one_change(html, 'id="merchantID" name="merchantID" value="A2L77EE7U53NWQ"',
                               'id="merchantID" name="merchantID" value=""')
        return html

    def test_resale_link_without_seller_param_reads_its_form(self):
        """基线：拿到名字没拿到 ID -> ("N/A", "Amazon Resale")。ID 在同一张加购表单里。
        这一条也钉住「读链接**所在**那张表单」：链接在第二张表单（二手行），读全页第一张
        （全新行 A1IOUXTP7ANG7D）就错了。"""
        html = self._resale_link_in_used_form()
        self.assertEqual(self._seller(html), ("A2L77EE7U53NWQ", "Amazon Resale"))
        for eng, r in _parse_both(html, "B0CQWZR6L8").items():
            self.assertEqual((r["seller_id"], r["seller_name"]),
                             ("A2L77EE7U53NWQ", "Amazon Resale"), eng)

    def test_used_merchant_id_is_the_second_choice(self):
        """``merchantID`` 没值时退到同表单的 ``usedMerchantID``（真实页面上两者同值）。"""
        html = self._resale_link_in_used_form(blank_merchant_id=True)
        self.assertEqual(self._seller(html), ("A2L77EE7U53NWQ", "Amazon Resale"))

    # ---- 反向 ----
    def test_seller_param_in_link_still_wins(self):
        """只在链接里**没有** seller= 时才回退；有就用链接的（真实片段原样）。"""
        html = _load("dp_us_B0CQWZR6L8_accordion.html.gz")
        self.assertEqual(self._seller(html), ("A1IOUXTP7ANG7D", "Smibuy Direct"))

    def test_never_borrows_another_offers_form(self):
        """accordion 每行一张 form#addToCart（id 重复），全新行与二手行的 merchantID 不同
        （A1IOUXTP7ANG7D / A2L77EE7U53NWQ）。构造变体：全新行的两条链接改成无 seller=
        的形状，必须拿到**自己表单**的 A1IOUXTP7ANG7D。基线 ("N/A", "Smibuy Direct")。"""
        html = _load("dp_us_B0CQWZR6L8_accordion.html.gz")
        self.assertIn('value="A2L77EE7U53NWQ"', html, "夹具前提：二手行表单在页面上")
        mangled = _one_change(html, _NEW_ROW_LINK, '<a href="/b?node=1" id="sellerProfileTriggerId"', 2)
        self.assertEqual(self._seller(mangled), ("A1IOUXTP7ANG7D", "Smibuy Direct"))

    def test_link_outside_any_form_stays_na(self):
        """链接不在任何表单里：宁可 N/A，也不拿全页第一张表单去配。"""
        html = ('<div id="buybox"><a id="sellerProfileTriggerId" href="/b?node=10158976011">'
                'Amazon Resale</a></div>'
                '<form id="addToCart"><input type="hidden" id="merchantID" '
                'name="merchantID" value="A2L77EE7U53NWQ"></form>')
        self.assertEqual(self._seller(html), ("N/A", "Amazon Resale"))

    def test_no_featured_offer_page_is_unchanged(self):
        """无推荐报价页（B07JN24QCB）：没有卖家链接，``merchantID`` 为空串 -> 仍是 (N/A, N/A)。"""
        html = _load("dp_us_B07JN24QCB_nfo.html.gz")
        self.assertIn('id="merchantID" name="merchantID" value=""', html)
        self.assertEqual(self._seller(html), ("N/A", "N/A"))


# ══════════════════════════════════════════════════════════════════════
# G7  parse_offer_listing：卖家 ID + maxQty
# ══════════════════════════════════════════════════════════════════════
def _aod_offer(price_whole: str, price_frac: str, seller: str, seller_id: str,
               ships_from: str, max_qty, delivery_price: str = "$12.99",
               delivery_time: str = "October 19 - 21", priced: bool = True) -> str:
    """构造：aodAjaxMain 响应里**一个** #aod-offer 节点。

    为什么是构造的：美国站 aodAjaxMain 在本沙箱里回 503（2026-10-14 试过两条 URL），
    30 张美国抽查页里也没有任何 AOD 标记，拿不到真实的美国站 AOD 响应。结构照
    2026-10 抽查抓到的真实 AOD 响应原样保留被测选择器读到的那几层
    （span.a-price 的 whole / fraction、data-csa-c-delivery-*、#aod-offer-shipsFrom
    .a-color-base、#aod-offer-soldBy a.a-link-normal 的 href、加购按钮外层
    data-aod-atc-action 的 JSON），其余装饰层略去；店名 / ID 是虚构的。

    ⚠ 照抄的那批真实 AOD 响应是**加拿大站**（amazon.ca）的：同一套 aodAjaxMain 模板，
    但美国站 amazon.com 的响应还没拿真实样本核对过 —— 这里守的是「解析器读这套结构」，
    不是「美国站的响应长这样」。
    """
    price = ('<span id="aod-price-1"><span class="a-price aok-align-center '
             'centralizedApexPricePriceToPayMargin apex-pricetopay-value" data-a-size="medium_plus">'
             '<span class="a-offscreen"> </span><span aria-hidden="true">'
             '<span class="a-price-symbol">$</span><span class="a-price-whole">%s'
             '<span class="a-price-decimal">.</span></span><span class="a-price-fraction">%s</span>'
             '</span></span></span>' % (price_whole, price_frac)) if priced else ""
    atc = ("" if max_qty is None else
           '<span class="a-declarative" data-action="aod-atc-action" data-aod-atc-action=\''
           '{"refTag":"aod_dpdsk_new_1","asin":"B07JN24QCB","isHdpOffer":false,'
           '"maxQty":%s,"offerIndex":1,"minQty":1,"sr":""}\'>'
           '<input name="submit.addToCart" class="a-button-input" type="submit"></span>' % max_qty)
    return (
        '<div id="aod-offer" class="a-section a-spacing-none a-padding-small aod-information-block '
        'aod-clear-float" role="listitem">'
        '<div id="aod-offer-heading"><span class="a-size-base a-text-bold"> New </span></div>'
        '<div id="aod-offer-price">' + price +
        '<div id="mir-layout-DELIVERY_BLOCK"><span data-csa-c-type="element" '
        'data-csa-c-content-id="DEXUnifiedCXPDM" data-csa-c-delivery-price="%s" '
        'data-csa-c-delivery-type="delivery" data-csa-c-delivery-time="%s">%s delivery '
        '<span class="a-text-bold">%s</span></span></div></div>' % (
            delivery_price, delivery_time, delivery_price, delivery_time) +
        '<form method="post" action="/gp/product/handle-buy-box/ref=aod_dpdsk_new_1">' + atc + '</form>'
        '<div class="a-section" role="list">'
        '<div id="aod-offer-shipsFrom" class="a-section a-spacing-none a-spacing-top-micro" role="listitem">'
        '<span class="a-size-small a-color-tertiary">Ships from</span>'
        '<span class="a-size-small a-color-base"> %s </span></div>'
        '<div id="aod-offer-soldBy" class="a-section a-spacing-none a-spacing-top-micro" role="listitem">'
        '<span class="a-size-small a-color-tertiary">Sold by</span>'
        '<a aria-label="%s. Opens a new page" class="a-size-small a-link-normal" target="_blank" '
        'rel="noopener" href="/gp/aag/main?ie=UTF8&amp;seller=%s&amp;isAmazonFulfilled=0'
        '&amp;asin=B07JN24QCB&amp;ref_=olp_merch_name_1" role="link">%s </a></div>'
        '</div></div>' % (ships_from, seller, seller_id, seller))


_PINNED_NFO = ('<div id="aod-pinned-offer" class="a-section a-spacing-none aod-pinned-offer">'
               '<div id="aod-asin-title"><h5 id="aod-asin-title-text"> HOMCOM 7FT Artificial '
               'Christmas Tree </h5></div><div class="a-row a-spacing-top-small">'
               '<span class="a-size-base"> No featured offers available </span></div></div>')

_FIRST = dict(price_whole="189", price_frac="99", seller="Store One", seller_id="AEXAMPLESTORE1",
              ships_from="Store One", max_qty=3)
_SECOND = dict(price_whole="205", price_frac="50", seller="Store Two", seller_id="AEXAMPLESTORE2",
               ships_from="Amazon.com", max_qty=30, delivery_price="FREE",
               delivery_time="October 17 - 18")


def _aod(*offers: str) -> str:
    return ('<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>'
            + _PINNED_NFO + '<div id="aod-offer-list">' + "".join(offers)
            + '</div></body></html>')


class OfferListingTests(unittest.TestCase):
    """``parse_offer_listing`` 只有 selectolax 实现（lxml 环境下直接返回 None，
    与 lxml 商品页路径本来就不产出 "No Featured Offer" 一致）。这里显式拨到
    selectolax 一侧，保证用例在本机真跑。"""

    def _offer(self, html: str):
        with _Engine(True):
            return AmazonParser().parse_offer_listing(html)

    def test_first_offer_carries_seller_id_and_max_qty(self):
        """导出里走兜底的行卖家两列都是 N/A、库存数都是写死的 5 —— 而报价节点上
        卖家链接带 seller=，加购按钮带 maxQty。"""
        o = self._offer(_aod(_aod_offer(**_FIRST), _aod_offer(**_SECOND)))
        self.assertEqual(o["seller"], "Store One")
        self.assertEqual(o["seller_id"], "AEXAMPLESTORE1")
        self.assertEqual(o["max_qty"], 3)

    def test_existing_keys_are_unchanged(self):
        o = self._offer(_aod(_aod_offer(**_FIRST), _aod_offer(**_SECOND)))
        self.assertEqual(
            {k: o[k] for k in ("price", "shipping", "delivery", "ships_from", "is_fba")},
            {"price": "$189.99", "shipping": "$12.99", "delivery": "October 19 - 21",
             "ships_from": "Store One", "is_fba": "FBM"})

    def test_every_field_comes_from_the_same_offer(self):
        """第一个报价没价格（如「加购后才显示价格」）时，价格、卖家、ID、maxQty、
        运费、配送必须**全部**来自第二个报价。

        原来退到整个 ``#aod-offer-list``：价格是列表里第一个价格（第二个报价的），
        卖家 / maxQty / 运费却仍是列表里第一个匹配（第一个报价的）—— 导出一行里
        拼了两个卖家。
        """
        o = self._offer(_aod(_aod_offer(priced=False, **_FIRST), _aod_offer(**_SECOND)))
        second = self._offer(_aod(_aod_offer(**_SECOND)))
        self.assertEqual(second["seller"], "Store Two", "前提：第二个报价单独解析")
        self.assertEqual((o["price"], o["seller"], o["seller_id"], o["max_qty"], o["shipping"]),
                         ("$205.50", "Store Two", "AEXAMPLESTORE2", 30, "FREE"))
        self.assertEqual(o, second)

    def test_missing_max_qty_is_absent_not_guessed(self):
        o = self._offer(_aod(_aod_offer(**dict(_FIRST, max_qty=None))))
        self.assertEqual(o["price"], "$189.99")
        self.assertNotIn("max_qty", o)

    def test_non_numeric_or_zero_max_qty_is_not_taken(self):
        for bad in ("0", '"x"', "true"):
            with self.subTest(maxQty=bad):
                o = self._offer(_aod(_aod_offer(**dict(_FIRST, max_qty=bad))))
                self.assertNotIn("max_qty", o)


# ══════════════════════════════════════════════════════════════════════
# G7  engine 合并：无推荐报价兜底
# ══════════════════════════════════════════════════════════════════════
def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _nfo_dp_result():
    """真实无推荐报价页（美国 B07JN24QCB）在 selectolax 路径上的解析结果（engine 拿到的就是它）。"""
    with _Engine(True):
        return AmazonParser().parse_product(
            _load("dp_us_B07JN24QCB_nfo.html.gz"), "B07JN24QCB", _ZIP)


class MergeOfferListingTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from worker.engine import Worker
        cls.Worker = Worker
        with _Engine(True):
            cls.offer = AmazonParser().parse_offer_listing(
                _aod(_aod_offer(**_FIRST), _aod_offer(**_SECOND)))

    def _merged(self, offer=None, **over):
        r = _nfo_dp_result()
        self.assertEqual(r["current_price"], "No Featured Offer", "夹具前提")
        self.assertEqual(r["stock_count"], "0", "夹具前提：商品页解析写的是 0")
        self.assertEqual((r["seller_id"], r["seller_name"]), ("N/A", "N/A"), "夹具前提")
        r.update(over)
        self.Worker._merge_offer_listing(r, dict(offer or self.offer))
        return r

    def test_stock_count_is_the_offers_max_qty_not_five(self):
        r = self._merged()
        self.assertEqual(r["stock_count"], "3")
        self.assertEqual(r["stock_status"], "In Stock (via offer-listing)")

    def test_unknown_qty_uses_the_parsers_in_stock_unknown_value(self):
        """读不到 maxQty：写 999 —— 两个解析引擎对「有货、页面没给数量」的同一个值。
        不能留 "0"：旁边写着 In Stock，导出里 0 就是确认缺货。"""
        offer = {k: v for k, v in self.offer.items() if k != "max_qty"}
        r = self._merged(offer)
        self.assertEqual(r["stock_count"], "999")
        with _Engine(True):
            parser_says = AmazonParser()._slx_parse_stock_count(
                "In Stock", HTMLParser("<div></div>"))
        self.assertEqual(r["stock_count"], str(parser_says))

    def test_seller_pair_is_backfilled_from_the_same_offer(self):
        r = self._merged()
        self.assertEqual((r["seller_id"], r["seller_name"]),
                         ("AEXAMPLESTORE1", "Store One"))

    def test_price_shipping_delivery_fba_still_merged(self):
        r = self._merged()
        self.assertEqual((r["current_price"], r["buybox_price"], r["buybox_shipping"],
                          r["delivery_date"], r["is_fba"]),
                         ("$189.99", "$189.99", "$12.99", "October 19 - 21", "FBM"))

    # ---- 反向 ----
    def test_never_mixes_a_product_page_seller_with_an_aod_seller(self):
        """商品页已经给了其中一列：整对不动（名字来自商品页、ID 来自 AOD 可能是两个人）。"""
        r = self._merged(seller_id="N/A", seller_name="Some Shop")
        self.assertEqual((r["seller_id"], r["seller_name"]), ("N/A", "Some Shop"))
        r = self._merged(seller_id="AXXXXXXXXXXXX", seller_name="N/A")
        self.assertEqual((r["seller_id"], r["seller_name"]), ("AXXXXXXXXXXXX", "N/A"))

    def test_a_real_count_is_not_overwritten(self):
        r = self._merged(stock_count="7")
        self.assertEqual(r["stock_count"], "7")

    def test_server_side_gates_are_not_tripped(self):
        """下游判定：两种取值都过 _is_parse_failure；增量导出读成整数而不是 0。"""
        from server.api.export_incremental import _int
        for offer in (self.offer, {k: v for k, v in self.offer.items() if k != "max_qty"}):
            r = self._merged(offer)
            self.assertFalse(_is_parse_failure(r))
            self.assertGreater(_int(r["stock_count"]), 0)


# ---- 端到端：_process_task 的无推荐报价分支 ----
class _Resp:
    def __init__(self, text, status=200):
        self.status_code = status
        self.text = text
        self.content = text.encode()


class _Session:
    AMAZON_BASE = "https://www.amazon.com"

    def __init__(self, dp_html, aod_html):
        self.zip_code = _ZIP
        self._dp = dp_html
        self._aod = aod_html
        self.urls = []

    def is_ready(self):
        return True

    async def fetch_product_page(self, asin, max_recv_speed=0):
        return _Resp(self._dp)

    async def fetch_product_page_by_url(self, url, *a, **kw):
        self.urls.append(url)
        return _Resp(self._aod)

    def is_blocked(self, resp):
        return False

    def is_captcha(self, resp):
        return False

    def is_404(self, resp):
        return False


class _Slot:
    def __init__(self, session):
        self.session = session
        self.rotations = []

    async def ensure_ready(self, *args, **kwargs):
        return True

    async def ensure_zip(self, target_zip):
        return True

    async def repost_zip(self, target_zip):
        return False

    async def rotate(self, reason=""):
        self.rotations.append(reason)

    async def rotate_on_empty(self, asin, reason=""):
        self.rotations.append(reason)

    def note_success(self):
        pass

    def should_rotate_proactive(self):
        return False


class NoFeaturedOfferEndToEndTests(unittest.TestCase):
    """真 Worker._process_task + 真解析器 + 真实美国无推荐报价商品页片段 + 构造的 AOD。"""

    def test_submitted_row_carries_aod_seller_and_qty(self):
        from worker.engine import Worker

        async def go():
            w = Worker("http://server.invalid", worker_id="w-test", zip_code=_ZIP,
                       enable_screenshot=False)
            w._result_queue = asyncio.Queue()
            w._rate_limiter = None
            sess = _Session(_load("dp_us_B07JN24QCB_nfo.html.gz"),
                            _aod(_aod_offer(**_FIRST), _aod_offer(**_SECOND)))
            slot = _Slot(sess)
            task = {"id": 9, "asin": "B07JN24QCB", "zip_code": _ZIP,
                    "batch_id": 1, "batch_name": "nfo",
                    "lease_epoch": 0, "needs_screenshot": False}
            saved = logging.root.manager.disable
            logging.disable(logging.CRITICAL)
            try:
                with _Engine(True):
                    out = await w._process_task(task, slot)
            finally:
                logging.disable(saved)
            return out, sess, slot, w._result_queue.get_nowait()

        (success, blocked, _), sess, slot, p = _run(go())
        self.assertTrue(success)
        self.assertFalse(blocked)
        self.assertEqual(slot.rotations, [])
        self.assertEqual(len(sess.urls), 1)
        self.assertTrue(sess.urls[0].startswith("https://www.amazon.com/gp/product/ajax/aodAjaxMain"))
        self.assertEqual(p["current_price"], "$189.99")
        self.assertEqual((p["seller_id"], p["seller_name"]), ("AEXAMPLESTORE1", "Store One"))
        self.assertEqual(p["stock_count"], "3")
        self.assertEqual(p["stock_status"], "In Stock (via offer-listing)")


if __name__ == "__main__":
    unittest.main()
