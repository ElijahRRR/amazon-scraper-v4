"""详情表 / 卖点 / 卖家挂件的几个解析缺陷（与站点无关，美国站同样成立）。

    BSR        th/td 详情表取值无分隔符拼接 -> "...(See Top 100 in Home & Kitchen)#244 inBookcases"；
               detailBullets 版只取到最里层的子类排名，大类丢了
    详情值空白  双 td / th-td / li 三处取值：&nbsp;（\\xa0）原样留下、跨标签的词粘连
    卖点        "See more" 展开区副本让卖点翻倍；lxml 的 ``@class="a-list-item"`` 要求
               class **完全等于**一个词，多 class 的 span 一个都匹配不上
    重量 / 尺寸  ``Shipping Weight`` 整行走空；不带分号的重量值存成 N/A；
               ``尺寸; 重量`` 那一行的重量被扔掉；``ManufacturerPartNumber`` 无空格标签
    卖家        2026 版 buybox 的卖家挂件 #merchantInfoFeature_feature_div 上自营认不出

夹具是美国站真实页面的原文片段（tests/fixtures/parser_text/us_*.html，来历见 README）。
页面上没有现成例子的边界，在真实片段上做**一处**标明的改动（构造变体），每条的 docstring
里写了改的是哪一处、为什么 2026-10 抽查的 30 张美国页上没有现成的例子。

每条用例**两条引擎各跑一遍**（模块开关 ``_USE_SELECTOLAX``），走 ``parse_product``
整个出口（含 ``_clean_deep`` 与品牌出口归一）。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import worker.parser as P                        # noqa: E402
from worker.parser import AmazonParser          # noqa: E402

_FX = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "fixtures", "parser_text")
ENGINES = ("selectolax", "lxml")


def _html(name: str) -> str:
    with open(os.path.join(_FX, name + ".html"), encoding="utf-8") as f:
        return f.read()


def _parse(html: str, engine: str, asin: str = "B0CG19X7RN") -> dict:
    with mock.patch.object(P, "_USE_SELECTOLAX", engine == "selectolax"):
        return AmazonParser().parse_product(html, asin, "10001")


def _one_change(html: str, old: str, new: str) -> str:
    """构造变体：``old`` 在片段里必须**恰好出现一次**，否则这处改动的含义就不确定了。"""
    assert html.count(old) == 1, (html.count(old), old)
    return html.replace(old, new)


#: us_B0CG19X7RN 片段里的几行原文（th 原文带缩进空格）。
_ROW_ITEM_WEIGHT_TH = "    Item Weight   </th>"
_ROW_ITEM_DIMS_TH = "    Item Dimensions   </th>"
_ROW_ITEM_DIMS_TD = "    11.8 x 23.6 x 72 inches   </td>"
_ROW_MPN_TH = "    Manufacturer Part Number   </th>"
_ROW_MANUFACTURER_TD = "    aboxoo   </td>"


# ==========================================================================
# BSR
# ==========================================================================
class BestSellersRank(unittest.TestCase):

    def test_th_td_bsr_keeps_both_ranks_and_spaces(self):
        """美国页 30 张里带 BSR 的全部是 th/td 详情表这一行：
        ``<td><span><ul><li>#108,747 in Home & Kitchen (<a>See Top 100 …</a>)</li>
        <li>#244 in <a>Bookcases</a></li></ul></span></td>``。
        基线 selectolax 出 ``'#108,747 in Home & Kitchen (See Top 100 in Home & Kitchen)#244 inBookcases'``，
        lxml 出带 "(See Top 100 …)" 的那一串。"""
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.assertEqual(_parse(_html("us_B0CG19X7RN"), engine)["best_sellers_rank"],
                                 "#108,747 in Home & Kitchen #244 in Bookcases")

    def _detail_bullets_bsr(self) -> str:
        """构造：detailBullets 版详情表里的 BSR 行。

        30 张美国对照页都是 th/td 详情表，没有一张带 #detailBullets_feature_div，所以这里
        按 Amazon detailBullets 模板的原样结构手写一行（键名后挂 &rlm; : &lrm;、大类是
        a-text-bold 之后的裸文本、子类在嵌套 ul 里），排名数字取自美国页 B0D12731YT 的 BSR。
        """
        return (
            '<html><body><span id="productTitle">T</span>'
            '<div id="detailBullets_feature_div"><ul class="a-unordered-list a-nostyle a-vertical '
            'a-spacing-none detail-bullet-list">'
            '<li><span class="a-list-item"> <span class="a-text-bold"> Best Sellers Rank: </span> '
            '#1,182 in Office Products (<a href="/gp/bestsellers/office-products/ref=pd_zg_ts_office-products">'
            'See Top 100 in Office Products</a>) <ul class="a-unordered-list a-nostyle a-vertical zg_hrsr">  '
            '<li><span class="a-list-item"> #1 in <a href="/gp/bestsellers/office-products/1069514/ref=pd_zg_hrsr_office-products">'
            'Book Display &amp; Storage</a></span></li>  </ul>    </span></li>'
            '</ul></div></body></html>')

    def test_detail_bullets_bsr_keeps_the_top_level_rank(self):
        """基线 selectolax 取 spans[i+1] —— 只拿到嵌套 ul 里的子类那一条，还粘成
        ``'#1 inBook Display & Storage'``，大类 #1,182 整个丢了。

        只测 selectolax：lxml 路径的 li 分支读 ``following-sibling::span``，这种结构里
        a-text-bold 后面不是 span，lxml 根本不从 li 取 BSR（既有的引擎差异，与本修复无关）。"""
        self.assertEqual(_parse(self._detail_bullets_bsr(), "selectolax")["best_sellers_rank"],
                         "#1,182 in Office Products #1 in Book Display & Storage")


# ==========================================================================
# 详情值空白：首尾去掉，中间连续空白并成一个普通空格
# ==========================================================================
class DetailValueWhitespace(unittest.TestCase):

    def test_squash_rule(self):
        """用户确认的口径。"""
        sq = AmazonParser._squash_ws
        for raw in (" Beach Camping ", "Beach  Camping", "Beach\xa0Camping",
                    "\tBeach Camping", "\n    Beach \t\xa0 Camping   \n"):
            with self.subTest(raw=raw):
                self.assertEqual(sq(raw), "Beach Camping")

    def test_nbsp_in_th_td_detail_value_both_engines(self):
        """构造变体：真实 Manufacturer 行的值换成带 &nbsp; 的写法（30 张页上没撞上，
        但 selectolax 的 strip 与 lxml 的 normalize-space 都不认 \\xa0，是确定会漏的）。
        同一处取值喂所有详情字段，制造商就是品牌之外受影响的那一类。
        基线两条引擎都出 ``'aboxoo\\xa0\\xa0Co.\\xa0Ltd'``。"""
        html = _one_change(_html("us_B0CG19X7RN"), _ROW_MANUFACTURER_TD,
                           "    aboxoo&nbsp;&nbsp;Co.&nbsp;Ltd   </td>")
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.assertEqual(_parse(html, engine)["manufacturer"], "aboxoo Co. Ltd")

    def test_overview_only_manufacturer_has_no_edge_spaces(self):
        """守卫：美国 B0F3JB4D4Z 的 Product Overview 原文（片段里有意不带后面的 th/td 详情表，
        只有 Product Overview 时制造商只能从双 td 行取）。值要用 separator=" " 取
        （BSR 那条修复），而 selectolax 的 separator 会让 span 两侧的缩进各贡献一个空格 ->
        ``' CHANGZHOU JUTAI ELECTRONIC CO.,LTD '`` —— 所以取完必须过 _squash_ws。
        只测 selectolax：lxml 路径不读双 td 行（这里出 N/A，是既有的引擎差异）。"""
        r = _parse(_html("us_B0F3JB4D4Z"), "selectolax", "B0F3JB4D4Z")
        self.assertEqual(r["manufacturer"], "CHANGZHOU JUTAI ELECTRONIC CO.,LTD")
        self.assertEqual(r["brand"], "SHareconn")

    def _po_brand(self, inner: str) -> str:
        """构造变体：只替换真实 po-brand 行里那个 span 的内容。"""
        return _one_change(_html("us_B0F3JB4D4Z"),
                           '<span class="a-size-base po-break-word">SHareconn</span>', inner)

    def test_words_split_across_tags_get_one_space(self):
        """两个词分在两个标签里：基线无分隔符取值直接粘成一个词（``'BeachCamping'``）。
        只测 selectolax：lxml 路径不读双 td 行（品牌走 byline），这条在它上面不存在。"""
        html = self._po_brand('<span>Beach</span> <span>Camping</span>')
        self.assertEqual(_parse(html, "selectolax", "B0F3JB4D4Z")["brand"], "Beach Camping")

    def test_invisible_mark_next_to_space_is_squashed_at_the_exit(self):
        """U+200E 由出口的 _clean_deep 删掉，删完才露出首部空格（基线 ``' SHareconn'``）——
        所以品牌的出口归一必须排在 _clean_deep 之后。"""
        html = self._po_brand('<span class="a-size-base po-break-word">‎ SHareconn</span>')
        self.assertEqual(_parse(html, "selectolax", "B0F3JB4D4Z")["brand"], "SHareconn")

    def test_nbsp_in_detail_bullets_li_value_both_engines(self):
        """detailBullets 的 li 分支（``li > span > span.a-text-bold`` + 值 span），两条引擎
        各有一处取值，各自都要归一。构造：30 张美国页没有 detailBullets 版详情表，按 Amazon
        模板原样结构手写一行 Manufacturer（键名后挂 &rlm; : &lrm;）。
        基线 selectolax 出 ``'NOW\\xa0\\xa0STO'``，lxml 出 ``'NOW\\xa0\\xa0STO\\xa0'``。"""
        def page(value):
            return ('<html><body><span id="productTitle">T</span>'
                    '<div id="detailBullets_feature_div"><ul class="a-unordered-list a-nostyle '
                    'a-vertical a-spacing-none detail-bullet-list">'
                    '<li><span class="a-list-item"> <span class="a-text-bold">Manufacturer\n'
                    '      &rlm;\n      :\n      &lrm;\n    </span>    <span>%s</span>   </span></li>'
                    '</ul></div></body></html>' % value)
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.assertEqual(_parse(page("NOWSTO"), engine)["manufacturer"], "NOWSTO")
                self.assertEqual(_parse(page("NOW&nbsp;&nbsp;STO&nbsp;"), engine)["manufacturer"],
                                 "NOW STO")


# ==========================================================================
# 卖点
# ==========================================================================
def _bullets(html: str, engine: str) -> list:
    return [x for x in (_parse(html, engine)["bullet_points"] or "").split("\n") if x]


class BulletPoints(unittest.TestCase):

    def _ul(self) -> str:
        html = _html("us_B0CG19X7RN")
        start = html.index('<ul class="a-unordered-list a-vertical a-spacing-mini">')
        return html[start:html.index("</ul>", start) + len("</ul>")]

    def test_baseline_bullets_on_the_real_fragment(self):
        """守卫：真实片段上两条引擎都是那 5 条卖点（下面的变体都拿它对照）。"""
        for engine in ENGINES:
            with self.subTest(engine=engine):
                got = _bullets(_html("us_B0CG19X7RN"), engine)
                self.assertEqual(len(got), 5, got)
                self.assertTrue(got[0].startswith("✔DURABILITY & STABILITY:"), got[0])

    def test_see_more_copy_is_deduped_and_order_kept(self):
        """构造变体：在 #feature-bullets 里追加一份同一个 ul 的 "See more" 展开区副本
        （``div.a-expander-content``）。真机采集里六条卖点就是这样采成了十二条；30 张美国
        对照页的卖点都只有一个 ul，所以没有现成例子。基线两条引擎都出 10 条。"""
        base = _html("us_B0CG19X7RN")
        ul = self._ul()
        html = _one_change(base, ul, ul + '<div class="a-expander-content a-expander-partial-collapse-content">'
                           + ul + '</div>')
        want = _bullets(base, "selectolax")
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.assertEqual(_bullets(html, engine), want)

    def test_genuinely_different_lists_are_all_kept(self):
        """守卫：去重不能把"真的两组卖点"砍掉一组（有的页面真把卖点拆在两个 ul 里）。"""
        a = "".join(f'<li><span class="a-list-item">A{i}</span></li>' for i in (1, 2))
        b = "".join(f'<li><span class="a-list-item">B{i}</span></li>' for i in (1, 2))
        html = (f'<html><body><span id="productTitle">T</span>'
                f'<div id="feature-bullets"><ul>{a}</ul><ul>{b}</ul>'
                f'</div></body></html>')
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.assertEqual(_bullets(html, engine), ["A1", "A2", "B1", "B2"])

    def test_lxml_matches_list_items_with_extra_classes(self):
        """构造变体：卖点 span 的 class 改成 ``a-list-item a-size-base a-color-base``（真机
        采集的页面上就是这种多 class 写法；30 张美国对照页恰好都是单个 ``a-list-item``）。
        基线 lxml 的 ``@class="a-list-item"`` 要求完全相等，一条都取不到。"""
        base = _html("us_B0CG19X7RN")
        ul = self._ul()
        html = _one_change(base, ul, ul.replace('<span class="a-list-item">',
                                                '<span class="a-list-item a-size-base a-color-base">'))
        want = _bullets(base, "selectolax")
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.assertEqual(_bullets(html, engine), want)


# ==========================================================================
# 重量 / 尺寸 / 料号
# ==========================================================================
class WeightDimensionPartNumber(unittest.TestCase):
    """30 张美国对照页上只出现过 Item Weight / Item Dimensions / Manufacturer Part Number
    三种写法，下面几条都在真实行上改**标签或值的一处**，钉住另几种 Amazon 写法。"""

    def test_baseline_values_on_the_real_fragment(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                r = _parse(_html("us_B0CG19X7RN"), engine)
                self.assertEqual(r["item_weight"], "20.7 pounds")
                self.assertEqual(r["item_dimensions"], "11.8 x 23.6 x 72 inches")
                self.assertEqual(r["part_number"], "HM2618")
                self.assertEqual(r["package_weight"], "N/A")

    def test_package_and_shipping_weight_labels_without_semicolon(self):
        """标签换成 ``Package Weight`` / ``Shipping Weight``。基线两种都是 N/A：前者因为
        值里没有分号（取的是 "尺寸; 重量" 的第二半），后者整条 elif 链走空。"""
        for label in ("Package Weight", "Shipping Weight"):
            html = _one_change(_html("us_B0CG19X7RN"), _ROW_ITEM_WEIGHT_TH, f"    {label}   </th>")
            for engine in ENGINES:
                with self.subTest(label=label, engine=engine):
                    self.assertEqual(_parse(html, engine)["package_weight"], "20.7 pounds")

    def test_weight_embedded_in_a_dimensions_row_is_kept(self):
        """``Item Dimensions`` 的值后面接上 ``; 20.7 pounds``，并把单独的 Item Weight 行的
        标签改成一个不映射的 ``Color``（两处改动，缺一不可：有 Item Weight 行时它更权威）。
        基线把重量那一半直接扔了。"""
        html = _html("us_B0CG19X7RN")
        html = _one_change(html, _ROW_ITEM_WEIGHT_TH, "    Color   </th>")
        html = _one_change(html, _ROW_ITEM_DIMS_TD, "    11.8 x 23.6 x 72 inches; 20.7 pounds   </td>")
        for engine in ENGINES:
            with self.subTest(engine=engine):
                r = _parse(html, engine)
                self.assertEqual(r["item_dimensions"], "11.8 x 23.6 x 72 inches")
                self.assertEqual(r["item_weight"], "20.7 pounds")

    def test_a_dedicated_weight_row_wins_over_the_embedded_one(self):
        """守卫：页面上另有专门的 Item Weight 行时，尺寸行里的重量不许覆盖它。"""
        html = _one_change(_html("us_B0CG19X7RN"), _ROW_ITEM_DIMS_TD,
                           "    11.8 x 23.6 x 72 inches; 99 pounds   </td>")
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.assertEqual(_parse(html, engine)["item_weight"], "20.7 pounds")

    def test_package_dimensions_row_fills_package_weight(self):
        """``Item Dimensions`` 行改成 ``Package Dimensions: …; 1.5 kg``：尺寸进 package_*，
        重量回填 package_weight（基线是 N/A），且不许落进 item_dimensions。"""
        html = _html("us_B0CG19X7RN")
        html = _one_change(html, _ROW_ITEM_DIMS_TH, "    Package Dimensions   </th>")
        html = _one_change(html, _ROW_ITEM_DIMS_TD, "    30 x 20 x 10 cm; 1.5 kg   </td>")
        for engine in ENGINES:
            with self.subTest(engine=engine):
                r = _parse(html, engine)
                self.assertEqual(r["package_dimensions"], "30 x 20 x 10 cm")
                self.assertEqual(r["package_weight"], "1.5 kg")
                # 前面那行 Item Dimensions D x W x H 仍是商品尺寸
                self.assertEqual(r["item_dimensions"], '11.8"D x 23.6"W x 72"H')

    def test_part_number_label_without_spaces(self):
        """标签写成 ``ManufacturerPartNumber``（一个空格都没有，真机采集的页面上见过）。
        基线判据是 ``'part number' in k_lower``，part_number 恒空。"""
        html = _one_change(_html("us_B0CG19X7RN"), _ROW_MPN_TH, "ManufacturerPartNumber</th>")
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.assertEqual(_parse(html, engine)["part_number"], "HM2618")


# ==========================================================================
# 卖家：2026 版 buybox 卖家挂件 #merchantInfoFeature_feature_div
# ==========================================================================
class MerchantInfoFeatureSeller(unittest.TestCase):

    def test_third_party_seller_is_unchanged(self):
        """守卫：真实片段（第三方卖家 callalily）。"""
        for engine in ENGINES:
            with self.subTest(engine=engine):
                r = _parse(_html("us_B0CG19X7RN"), engine)
                self.assertEqual((r["seller_id"], r["seller_name"]), ("A5WPZBUT3Z26Y", "callalily"))

    def _first_party(self) -> str:
        """构造变体：把挂件里两处卖家档案链接 + 浮层里那处卖家名换成自营的写法 ——
        Amazon 自营时这个挂件里没有 sellerProfileTriggerId，只有一个指向客服页的链接，
        文字是站点域名（2026-09 真机采集的自营页上就是这个形状）。30 张美国对照页里没有
        Amazon 自营的商品。"""
        html = _html("us_B0CG19X7RN")
        link = ("<a href='/gp/help/seller/at-a-glance.html/ref=dp_merchant_link?ie=UTF8&amp;"
                "seller=A5WPZBUT3Z26Y&amp;asin=B0CG19X7RN&amp;ref_=dp_merchant_link&amp;"
                "isAmazonFulfilled=1' id='sellerProfileTriggerId' "
                "class='a-size-small a-link-normal offer-display-feature-text-message'>callalily</a>")
        assert html.count(link) == 2
        html = html.replace(link, '<a href="/hz/contact-us/foresight/hubgateway?_encoding=UTF8'
                                  '&amp;nodeId=508510">Amazon.com</a>')
        return _one_change(html, '<span class="a-size-small offer-display-feature-text-message">callalily</span>',
                           '<span class="a-size-small offer-display-feature-text-message">Amazon.com</span>')

    def test_first_party_seller_is_detected(self):
        """基线只在 #merchant-info / #tabular-buybox / #offerDisplay_feature_div 里找
        "Sold by Amazon.com"，这三个容器在 2026 版 buybox 上不一定存在（30 张美国页里
        15 张只有新挂件），于是自营页恒落 ("N/A","N/A")。"""
        for engine in ENGINES:
            with self.subTest(engine=engine):
                r = _parse(self._first_party(), engine)
                self.assertEqual((r["seller_id"], r["seller_name"]), ("AMAZON", "Amazon.com"))

    def test_third_party_name_without_link_is_not_claimed_as_amazon(self):
        """守卫：新挂件的域名判据只认域名本身。构造变体：把真实片段里的两处卖家档案链接换成
        纯文字（第一、二步都取不到 ID），挂件里只剩第三方店名 —— 不许被判成自营。"""
        html = _html("us_B0CG19X7RN")
        link = ("<a href='/gp/help/seller/at-a-glance.html/ref=dp_merchant_link?ie=UTF8&amp;"
                "seller=A5WPZBUT3Z26Y&amp;asin=B0CG19X7RN&amp;ref_=dp_merchant_link&amp;"
                "isAmazonFulfilled=1' id='sellerProfileTriggerId' "
                "class='a-size-small a-link-normal offer-display-feature-text-message'>callalily</a>")
        assert html.count(link) == 2
        html = html.replace(link, "<span>callalily</span>")
        for engine in ENGINES:
            with self.subTest(engine=engine):
                r = _parse(html, engine)
                self.assertEqual((r["seller_id"], r["seller_name"]), ("N/A", "N/A"))


if __name__ == "__main__":
    unittest.main()
