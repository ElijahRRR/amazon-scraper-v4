"""buybox 报价相关的三处修复，全部用真实页面片段守（tests/fixtures/offer/README.md）。

  G2  accordion 布局的原价：只读选中那一行，不再落到二手行的 "New condition price"
  G6  Warehouse Deals 卖家 ID：链接里没有 ``seller=`` 时读**同一张加购表单**的 merchantID
  G7  无推荐报价页走 aodAjaxMain 兜底：回填第一个报价的卖家名 / ID，库存数用该报价的
      maxQty，不再写死 "5"

每条修复都有**反向**用例（真实的划线价不能丢、链接自带 ID 时不许被表单覆盖、
别的 offer 的表单不许借用、商品页已有卖家时不许拼对），因为这几处最容易修过头。

两条解析引擎都要跑：生产上 lxml 分支只在 selectolax 缺失时才走，靠模块级开关
永远测不到它（P4-6 那四个字段就是这样丢的）。``parse_offer_listing`` 只有
selectolax 实现，见 G7 那组的说明。
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
from common.core import marketplace as M  # noqa: E402
from worker.parser import AmazonParser  # noqa: E402

_FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "offer")
_CA = ("K1V 7P8", "amazon.ca")
_US = ("10001", None)


def tearDownModule():
    if _STUBBED_SESSION:  # pragma: no cover
        if _SAVED_SESSION_MODULE is None:
            sys.modules.pop("worker.session", None)
        else:
            sys.modules["worker.session"] = _SAVED_SESSION_MODULE


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


# ══════════════════════════════════════════════════════════════════════
# G2  accordion 原价
# ══════════════════════════════════════════════════════════════════════
class AccordionOriginalPriceTests(unittest.TestCase):
    """多 offer accordion 里每一行都有自己的 #corePriceDisplay_desktop_feature_div
    （id 重复）。原价只能从选中那一行（a-accordion-active）读；选中的是会员价行时
    读全新行（与现价同一行）。"""

    def _orig(self, html: str, asin: str, site) -> str:
        p = AmazonParser()
        direct = {p._slx_parse_original_price(HTMLParser(html)),
                  p._parse_original_price(lxml_html.fromstring(html))}
        self.assertEqual(len(direct), 1, f"两条引擎结论分叉: {direct}")
        full = {r["original_price"] for r in _parse_both(html, asin, site).values()}
        self.assertEqual(full, direct, "parse_product 出口与函数本身不一致")
        return direct.pop()

    def test_ca_used_rows_new_condition_price_is_not_the_list_price(self):
        """实测症状：B09Y3DKJKQ 导出 原价 $112.99 = 现价 $112.99。

        选中的是全新行，它自己没有划线价；那个 $112.99 来自**未选中的**二手行
        （"Used – Good $97.19 … New condition price: $112.99"），是全新件现价，
        不是全新 offer 的原价。正确值 N/A。
        """
        html = _load("dp_ca_B09Y3DKJKQ_accordion.html.gz")
        self.assertIn("New condition price", html, "夹具前提：二手行的参照价在页面上")
        self.assertEqual(self._orig(html, "B09Y3DKJKQ", _CA), "N/A")

    def test_us_same_shape_with_capitalised_label(self):
        """美国站同形：B0CQWZR6L8 原价 $79.99 = 现价（二手行 "New Condition Price"）。"""
        html = _load("dp_us_B0CQWZR6L8_accordion.html.gz")
        self.assertEqual(self._orig(html, "B0CQWZR6L8", _US), "N/A")

    # ---- 反向：真实的划线价一个都不能丢 ----
    def test_selected_new_row_keeps_its_own_was_price(self):
        """B0B9R8R8NC：accordion，全新行自己就有 "Was: $169.99"。"""
        html = _load("dp_ca_B0B9R8R8NC_accordion_was.html.gz")
        self.assertEqual(self._orig(html, "B0B9R8R8NC", _CA), "$169.99")

    def test_selected_prime_row_reads_the_new_rows_strike_price(self):
        """US B0D12731YT：选中的是会员价行，"List Price: $34.99" 在它自己的价格块里
        （#apex_desktop_primeSavingsUpsellAccordionRow），是相对会员价 $26.99 标的。
        用户口径（2026-10-14）：原价跟现价同一行 —— 全新行（现价 $35.99 就读它），
        而全新行自己没有划线价 -> N/A。原来报 $34.99，原价 < 现价。
        全新行自己标了划线价的页（B0F3CQT8G5）见 tests/test_accordion_buy_row.py。"""
        html = _load("dp_us_B0D12731YT_accordion_prime.html.gz")
        self.assertIn("$34.99", HTMLParser(html).css_first(
            "#apex_desktop_primeSavingsUpsellAccordionRow").text(), "夹具前提：会员价行有 List 价")
        self.assertEqual(self._orig(html, "B0D12731YT", _US), "N/A")

    def test_single_offer_layout_is_untouched(self):
        """非 accordion（B0CRH6FDD7，"Was: $64.99"）：选择器与原来逐字节相同。"""
        html = _load("dp_ca_B0CRH6FDD7_single_was.html.gz")
        self.assertEqual(self._orig(html, "B0CRH6FDD7", _CA), "$64.99")

    def test_without_active_row_falls_back_to_the_new_row(self):
        """没有展开行时取全新行（页面主推的就是它）：去掉 active 后 Was 价仍在。"""
        html = _load("dp_ca_B0B9R8R8NC_accordion_was.html.gz")
        stripped = html.replace("a-accordion-active", "")
        self.assertNotIn("a-accordion-active", stripped)
        self.assertEqual(self._orig(stripped, "B0B9R8R8NC", _CA), "$169.99")

    def test_layout_without_per_row_price_blocks_reads_the_single_centre_block(self):
        """较早的 accordion 形状：buybox 分行，中栏只有**一份**价格块（没有
        ``apex_desktop_<行名>``）。那一份就是主推 offer 的，真实的 Was 价不能丢。

        用 B0B9R8R8NC 模拟：删掉二手行的中栏块、去掉全新行中栏块的 id，只剩
        ``#apex_desktop`` 包着全新行那份 "Was: $169.99"。buybox 里的全新行自己
        没有划线价，所以只读「分行块 + buybox 行」会丢成 N/A（基线取得到）。
        """
        tree = HTMLParser(_load("dp_ca_B0B9R8R8NC_accordion_was.html.gz"))
        tree.css_first("#apex_desktop_usedAccordionRow").decompose()
        new_block = tree.css_first("#apex_desktop_newAccordionRow")
        del new_block.attrs["id"]
        html = tree.html
        self.assertNotRegex(html, r'id="apex_desktop_[^"]*AccordionRow')
        self.assertIn('id="apex_desktop"', html)
        self.assertIsNone(HTMLParser(html).css_first(
            '#newAccordionRow_0 span[data-a-strike="true"]'), "夹具前提：buybox 行自己没有划线价")
        self.assertEqual(self._orig(html, "B0B9R8R8NC", _CA), "$169.99")

    def test_unrecognisable_accordion_never_falls_back_to_the_whole_page(self):
        """accordion 在、但认不出任何行 -> N/A。回落全页就是这个 bug 本身。"""
        html = _load("dp_ca_B09Y3DKJKQ_accordion.html.gz")
        mangled = (html.replace("a-accordion-active", "")
                   .replace('id="newAccordionRow_0"', 'id="someOtherRow_0"'))
        self.assertIn('id="buyBoxAccordion"', mangled)
        self.assertEqual(self._orig(mangled, "B09Y3DKJKQ", _CA), "N/A")


# ══════════════════════════════════════════════════════════════════════
# G6  Warehouse Deals 卖家 ID
# ══════════════════════════════════════════════════════════════════════
class WarehouseSellerIdTests(unittest.TestCase):

    def _seller(self, html: str, site=_CA):
        p = AmazonParser()
        spec = M.get(site[1])
        got = {p._parse_seller(HTMLParser(html), html, spec),
               p._parse_seller(lxml_html.fromstring(html), html, spec)}
        self.assertEqual(len(got), 1, f"两条引擎结论分叉: {got}")
        return got.pop()

    def test_warehouse_link_without_seller_param_reads_its_form(self):
        """实测症状：B0GKNHN7Q5 导出 卖家店铺ID = N/A、卖家店铺名 = Warehouse Deals。

        卖家链接是 ``/b?node=8929975011``（Amazon Resale 的类目节点），没有
        ``seller=``；ID 在同一张加购表单的 ``input#merchantID`` 里。
        """
        html = _load("dp_ca_B0GKNHN7Q5_warehouse.html.gz")
        self.assertIn('href="/b?node=8929975011"', html, "夹具前提")
        self.assertEqual(self._seller(html), ("AUJBRIGYRJ3Q8", "Warehouse Deals"))
        for eng, r in _parse_both(html, "B0GKNHN7Q5", _CA).items():
            self.assertEqual((r["seller_id"], r["seller_name"]),
                             ("AUJBRIGYRJ3Q8", "Warehouse Deals"), eng)

    def test_used_merchant_id_is_the_second_choice(self):
        """``merchantID`` 没值时退到同表单的 ``usedMerchantID``（实测两者同值）。"""
        html = _load("dp_ca_B0GKNHN7Q5_warehouse.html.gz")
        html = html.replace('id="merchantID" name="merchantID" value="AUJBRIGYRJ3Q8"',
                            'id="merchantID" name="merchantID" value=""')
        self.assertIn('id="usedMerchantID"', html)
        self.assertEqual(self._seller(html), ("AUJBRIGYRJ3Q8", "Warehouse Deals"))

    # ---- 反向 ----
    def test_seller_param_in_link_still_wins(self):
        """只在链接里**没有** seller= 时才回退；有就用链接的。"""
        html = _load("dp_ca_B0GKNHN7Q5_warehouse.html.gz").replace(
            'href="/b?node=8929975011"', 'href="/gp/help/seller/at-a-glance.html?seller=AZZZZZZZZZZZZ"')
        self.assertEqual(self._seller(html), ("AZZZZZZZZZZZZ", "Warehouse Deals"))

    def test_never_borrows_another_offers_form(self):
        """accordion 每行一张 form#addToCart（id 重复），全新行与二手行的 merchantID 不同。

        B09Y3DKJKQ：卖家链接在全新行的表单里（MoKitDora / ADUC3URPUSR5N），二手行表单是
        Warehouse 的 AUJBRIGYRJ3Q8。把链接改成无 seller= 的形状后，必须拿到**自己表单**的
        ADUC3URPUSR5N —— 全页第一个、或二手行的都不行。
        """
        html = _load("dp_ca_B09Y3DKJKQ_accordion.html.gz")
        self.assertIn('value="AUJBRIGYRJ3Q8"', html, "夹具前提：二手行表单在页面上")
        mangled = html.replace(
            "/gp/help/seller/at-a-glance.html/ref=dp_merchant_link?ie=UTF8&amp;seller=ADUC3URPUSR5N",
            "/b?node=1")
        self.assertNotEqual(mangled, html)
        self.assertEqual(self._seller(mangled), ("ADUC3URPUSR5N", "MoKitDora"))

    def test_link_in_the_second_form_reads_the_second_form(self):
        """上一条的镜像：无 seller= 的链接在**第二张**表单（二手行，Warehouse）里，
        必须拿二手行表单的 AUJBRIGYRJ3Q8，而不是全页第一张表单（全新行）的
        ADUC3URPUSR5N。上一条里链接恰好在第一张表单，「读全页第一张」也能过，
        靠这一条把它分开。"""
        html = _load("dp_ca_B09Y3DKJKQ_accordion.html.gz")
        moved = html.replace('id="sellerProfileTriggerId"', 'id="sellerProfileTriggerId-moved"')
        self.assertEqual(moved.count('id="sellerProfileTriggerId-moved"'), 2, "夹具前提：全新行两条链接")
        anchor = '<input type="hidden" id="usedMerchantID" name="usedMerchantID" value="AUJBRIGYRJ3Q8">'
        self.assertEqual(moved.count(anchor), 1, "夹具前提：二手行表单")
        moved = moved.replace(
            anchor, anchor + '<a id="sellerProfileTriggerId" href="/b?node=8929975011">Warehouse Deals</a>')
        self.assertEqual(self._seller(moved), ("AUJBRIGYRJ3Q8", "Warehouse Deals"))

    def test_link_outside_any_form_stays_na(self):
        """链接不在任何表单里：宁可 N/A，也不拿全页第一张表单去配。"""
        html = ('<div id="buybox"><a id="sellerProfileTriggerId" href="/b?node=8929975011">'
                'Warehouse Deals</a></div>'
                '<form id="addToCart"><input type="hidden" id="merchantID" '
                'name="merchantID" value="AUJBRIGYRJ3Q8"></form>')
        self.assertEqual(self._seller(html), ("N/A", "Warehouse Deals"))

    def test_no_featured_offer_page_is_unchanged(self):
        """无推荐报价页：没有卖家链接，``merchantID`` 为空串 -> 仍是 (N/A, N/A)。"""
        html = _load("dp_ca_B0D45YH9FL_nfo.html.gz")
        self.assertIn('id="merchantID" name="merchantID" value=""', html)
        self.assertEqual(self._seller(html), ("N/A", "N/A"))


# ══════════════════════════════════════════════════════════════════════
# G7  parse_offer_listing：卖家 ID + maxQty
# ══════════════════════════════════════════════════════════════════════
class OfferListingTests(unittest.TestCase):
    """``parse_offer_listing`` 只有 selectolax 实现（lxml 环境下直接返回 None，
    与 lxml 商品页路径本来就不产出 "No Featured Offer" 一致）。这里显式拨到
    selectolax 一侧，保证用例在本机真跑。"""

    def _offer(self, name: str):
        with _Engine(True):
            return AmazonParser().parse_offer_listing(_load(name), M.get("amazon.ca"))

    def test_first_offer_carries_seller_id_and_max_qty(self):
        """B0D45YH9FL：用户导出那一行的价格 / 运费 / 配送全对上，只丢了卖家；
        库存数写成了常量 5，而这个报价的 maxQty 是 3。"""
        o = self._offer("aod_ca_B0D45YH9FL.html.gz")
        self.assertEqual(o["seller"], "Botley Store")
        self.assertEqual(o["seller_id"], "A15CBTP5BTSNTZ")
        self.assertEqual(o["max_qty"], 3)

    def test_all_three_exported_fallback_rows(self):
        """用户导出里走兜底的三行（库存状态 "In Stock (via offer-listing)"）：
        价格与 AOD 第一个报价逐分相同，卖家两列都是 N/A、库存数都是 5。
        B0D4QGVN6F 的 maxQty 恰好就是 5 —— 只看「是不是 5」分不出新旧，
        所以逐行断言卖家 ID 与数量上限的具体值。"""
        want = {
            # ASIN: (导出的当前价格, 卖家名, 卖家 ID, maxQty)
            "B0D45YH9FL": ("$246.62", "Botley Store", "A15CBTP5BTSNTZ", 3),
            "B098QJXZLS": ("$503.56", "GiantRun LLC", "ADHQ1EIRR50GJ", 20),
            "B0D4QGVN6F": ("$238.23", "Amazon US", "ADQ8PPI9HA4SX", 5),
        }
        for asin, (price, seller, sid, qty) in want.items():
            o = self._offer(f"aod_ca_{asin}.html.gz")
            self.assertEqual((o["price"], o["seller"], o.get("seller_id"), o.get("max_qty")),
                             (price, seller, sid, qty), asin)

    def test_existing_keys_are_unchanged(self):
        o = self._offer("aod_ca_B0D45YH9FL.html.gz")
        self.assertEqual(
            {k: o[k] for k in ("price", "shipping", "delivery", "ships_from", "is_fba")},
            {"price": "$246.62", "shipping": "$7.05", "delivery": "October 19 - 21",
             "ships_from": "Botley Store", "is_fba": "FBM"})

    def test_max_qty_equals_the_product_page_dropdown(self):
        """为什么取 maxQty 而不是 AOD 的数量下拉：同一时刻抓的同一个卖家，
        商品页 select#quantity 是 1..5（正常路径的 stock_count 取它），
        AOD 的 maxQty 是 5，而 AOD 自己的静态下拉只到 4。"""
        o = self._offer("aod_ca_B0DJ231D92.html.gz")
        self.assertEqual(o["seller_id"], "A2JYEGZW5E2V7Z")
        self.assertEqual(o["max_qty"], 5)
        aod_opts = [int(n.text(strip=True)) for n in
                    HTMLParser(_load("aod_ca_B0DJ231D92.html.gz")).css(".aod-qty-option")]
        self.assertEqual(max(aod_opts), 4, "夹具前提：AOD 静态下拉比 maxQty 少 1")
        dp = _load("dp_ca_B0DJ231D92_qty.html.gz")
        p = AmazonParser()
        self.assertEqual(p._slx_parse_stock_count("In Stock", HTMLParser(dp)), o["max_qty"])
        self.assertEqual(p._parse_stock_count("In Stock", lxml_html.fromstring(dp)), o["max_qty"])

    def test_every_field_comes_from_the_same_offer(self):
        """第一个报价没价格（如「加购后才显示价格」）时，价格、卖家、ID、maxQty、
        运费、配送必须**全部**来自第二个报价。

        原来退到整个 ``#aod-offer-list``：价格是列表里第一个价格（第二个报价的），
        卖家 / maxQty / 运费却仍是列表里第一个匹配（第一个报价的）—— 导出一行里
        拼了两个卖家。真实响应里 offer 都包在 ``#aod-offer-list`` 里；夹具只留了
        offer 节点，这里用原样的节点把列表外壳补回来。
        """
        tree = HTMLParser(_load("aod_ca_B0D45YH9FL.html.gz"))
        offers = tree.css("#aod-offer")
        self.assertEqual(len(offers), 2, "夹具前提：两个报价")
        for n in offers[0].css("span.a-price"):
            n.decompose()
        pinned = tree.css_first("#aod-pinned-offer")
        html = (pinned.html + '<div id="aod-offer-list">'
                + "".join(o.html for o in offers) + "</div>")
        with _Engine(True):
            o = AmazonParser().parse_offer_listing(html, M.get("amazon.ca"))
            second = AmazonParser().parse_offer_listing(
                pinned.html + offers[1].html, M.get("amazon.ca"))
        self.assertEqual(second["seller"], "Arjinstore", "夹具前提：第二个报价")
        self.assertEqual((o["seller"], o["seller_id"], o["max_qty"], o["shipping"]),
                         ("Arjinstore", "A13DS8IVXZFZHA", 30, "FREE"))
        self.assertEqual(o, second)

    def test_missing_max_qty_is_absent_not_guessed(self):
        html = _load("aod_ca_B0D45YH9FL.html.gz").replace(
            "data-aod-atc-action", "data-removed-for-test")
        with _Engine(True):
            o = AmazonParser().parse_offer_listing(html, M.get("amazon.ca"))
        self.assertEqual(o["price"], "$246.62")
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
    """真实无推荐报价页在 selectolax 路径上的解析结果（engine 拿到的就是它）。"""
    with _Engine(True):
        return AmazonParser().parse_product(
            _load("dp_ca_B0D45YH9FL_nfo.html.gz"), "B0D45YH9FL", *_CA)


class MergeOfferListingTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from worker.engine import Worker
        cls.Worker = Worker
        with _Engine(True):
            cls.offer = AmazonParser().parse_offer_listing(
                _load("aod_ca_B0D45YH9FL.html.gz"), M.get("amazon.ca"))

    def _merged(self, offer=None, **over):
        r = _nfo_dp_result()
        self.assertEqual(r["current_price"], "No Featured Offer", "夹具前提")
        self.assertEqual(r["stock_count"], "0", "夹具前提：商品页解析写的是 0")
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
                         ("A15CBTP5BTSNTZ", "Botley Store"))

    def test_price_shipping_delivery_fba_still_merged(self):
        r = self._merged()
        self.assertEqual((r["current_price"], r["buybox_price"], r["buybox_shipping"],
                          r["delivery_date"], r["is_fba"]),
                         ("$246.62", "$246.62", "$7.05", "October 19 - 21", "FBM"))

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
    AMAZON_BASE = "https://www.amazon.ca"

    def __init__(self, dp_html, aod_html):
        self.zip_code = _CA[0]
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

    async def ensure_ready(self, marketplace=None, zip_code=None):
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
    """真 Worker._process_task + 真解析器 + 真实商品页 / AOD 片段。"""

    def test_submitted_row_carries_aod_seller_and_qty(self):
        from worker.engine import Worker

        async def go():
            w = Worker("http://server.invalid", worker_id="w-test", zip_code="10001",
                       enable_screenshot=False)
            w._result_queue = asyncio.Queue()
            w._rate_limiter = None
            sess = _Session(_load("dp_ca_B0D45YH9FL_nfo.html.gz"),
                            _load("aod_ca_B0D45YH9FL.html.gz"))
            slot = _Slot(sess)
            task = {"id": 9, "asin": "B0D45YH9FL", "zip_code": _CA[0],
                    "marketplace": "amazon.ca", "batch_id": 1, "batch_name": "nfo",
                    "lease_epoch": 0, "needs_screenshot": False}
            saved = logging.root.manager.disable
            logging.disable(logging.CRITICAL)
            try:
                with _Engine(True):
                    out = await w._process_task(task, slot)
            finally:
                logging.disable(saved)
            return out, sess, w._result_queue.get_nowait()

        (success, blocked, _), sess, p = _run(go())
        self.assertTrue(success)
        self.assertFalse(blocked)
        self.assertEqual(len(sess.urls), 1)
        self.assertTrue(sess.urls[0].startswith("https://www.amazon.ca/gp/product/ajax/aodAjaxMain"))
        self.assertEqual(p["current_price"], "$246.62")
        self.assertEqual((p["seller_id"], p["seller_name"]), ("A15CBTP5BTSNTZ", "Botley Store"))
        self.assertEqual(p["stock_count"], "3")
        self.assertEqual(p["stock_status"], "In Stock (via offer-listing)")


if __name__ == "__main__":
    unittest.main()
