"""tests/test_offer_condition.py —— buybox offer 品相提取。

守的是两类**性质不同**的信号：

  1. DOM 品相行 —— 同一个 ASIN 上的二手 offer（卖家如 "Amazon Resale"，即原
     Amazon Warehouse 的退货/开箱件）。标题里没有任何二手标记，只能从 buybox
     区域读。这是漏网的大头，也是本功能存在的理由。
  2. 标题里的 (Renewed) / (Refurbished) —— 亚马逊给翻新品单独 ASIN，属于产品
     属性，不随 buybox 变。

⚠ 本文件测的是「给定这样的 HTML 应当得出什么」，**不是**「Amazon 今天真的长
  这样」。那几个容器 id 没有真实二手页面的语料可验证（沙箱无 Amazon 通道，库里
  只存 PNG 不存 HTML）。所以最要紧的几条用例是**反向**的：读不到就必须 "N/A"，
  绝不能编造出一个品相 —— 误判成 "New" 比留空危险得多。
"""
from __future__ import annotations

import unittest

from worker.parser import AmazonParser


def _cond(html: str, title: str = "") -> str:
    """两条引擎路径各跑一遍，顺带断言它们**结论一致**。"""
    p = AmazonParser()
    got = []
    for build in (_slx, _lxml):
        tree = build(html)
        if tree is None:
            continue
        got.append(p._parse_offer_condition(tree, title))
    assert got, "两条引擎都不可用，用例无意义"
    assert len(set(got)) == 1, f"两条引擎路径结论分叉: {got}（品相不该依赖引擎）"
    return got[0]


def _slx(html):
    try:
        from selectolax.parser import HTMLParser
        return HTMLParser(html)
    except Exception:
        return None


def _lxml(html):
    try:
        from lxml import html as lh
        return lh.fromstring(html)
    except Exception:
        return None


class OfferConditionTests(unittest.TestCase):

    # ------------------------------------------------ 该识别出来的
    def test_used_grades_from_condition_row(self):
        """Amazon 二手 listing 的专用品相行，四个等级逐一。"""
        for raw, want in (
            ("Used - Like New", "Used - Like New"),
            ("Used - Very Good", "Used - Very Good"),
            ("Used - Good", "Used - Good"),
            ("Used - Acceptable", "Used - Acceptable"),
        ):
            html = f'<div id="condition-and-price-row"><span>{raw}</span> <span>$20.70</span></div>'
            self.assertEqual(_cond(html), want, f"raw={raw!r}")

    def test_case_and_dash_variants(self):
        """大小写与三种破折号/冒号都要吃下 —— Amazon 各站点写法不统一。"""
        for raw in ("used - very good", "USED – VERY GOOD", "Used — Very Good",
                    "Used: Very Good"):
            html = f'<div id="buybox">{raw}</div>'
            self.assertEqual(_cond(html), "Used - Very Good", f"raw={raw!r}")

    def test_used_grades_are_mutually_exclusive(self):
        """四个等级的正则互斥 —— 每个探针只命中一条。

        这条记录的是一个**事实**，不是优先级：`used-good` 的正则要求破折号后
        紧跟 good，吃不下 "used - very good" 中间那个 very。所以等级之间无需
        排序。（原先这里有一条用例声称"顺序即优先级"，把词表整个反转它依然
        全绿 —— 空心的，已替换成下面那条真正能分辨顺序的。）
        """
        import re
        from worker.parser import AmazonParser
        for probe in ("used - like new", "used - very good",
                      "used - good", "used - acceptable"):
            hits = [n for n, p in AmazonParser._CONDITION_PATTERNS
                    if re.search(p, probe)]
            self.assertEqual(len(hits), 1, f"{probe!r} 命中多条: {hits}")

    def test_specific_used_grade_beats_generic_renewed(self):
        """同一段文本里出现多个品相时，**具体的 Used 等级**必须赢。

        这是词表顺序唯一真正起作用的场合。把 _CONDITION_PATTERNS 反转，这条
        会返回 "Renewed" 而红 —— 它是那个顺序的唯一守卫。
        """
        html = ('<div id="buybox">Used - Very Good'
                '<span>Renewed unit, tested</span></div>')
        self.assertEqual(_cond(html), "Used - Very Good")

    def test_renewed_from_title_when_dom_silent(self):
        """翻新品是独立 ASIN，标题就能判 —— 你库里有 135 个这种。"""
        self.assertEqual(
            _cond("<div id=\"buybox\">$99.00</div>",
                  "Apple iPhone 12 64GB (Renewed)"),
            "Renewed")
        self.assertEqual(
            _cond("<div></div>", "Dell Latitude 7420 Laptop, Refurbished"),
            "Refurbished")

    def test_dom_beats_title(self):
        """DOM 说的是**本次 offer**，比标题那个产品级标记更具体，必须赢。"""
        html = '<div id="condition-and-price-row">Used - Acceptable</div>'
        self.assertEqual(_cond(html, "Something (Renewed)"), "Used - Acceptable")

    # ------------------------------------------------ 绝不能误判的（最要紧）
    def test_new_product_is_na_not_new(self):
        """全新品返回 "N/A"，**不是** "New"。

        我们没有任何"页面明确说了这是全新"的证据 —— 全新 offer 的 buybox 根本
        不写品相。返回 "New" 等于把"没读到"伪装成"读到了全新"，而下游会拿它当
        真去比价。
        """
        html = ('<div id="buybox"><span class="a-price">$29.99</span>'
                '<span>FREE delivery</span></div>')
        self.assertEqual(_cond(html), "N/A")

    def test_other_sellers_blurb_must_not_leak(self):
        """#rightCol 里的 "used & new from" / "Buy used:" 是**其他卖家的计数**，
        不是本 offer 的品相。泛扫 #rightCol 就会栽在这里 —— 这条正是那个作用域
        白名单存在的理由。"""
        for blurb in ("12 used &amp; new from $18.00",
                      "Buy used: $20.70",
                      "See all 3 used offers",
                      "Save with Used - Very Good options from other sellers"):
            html = f'<div id="rightCol"><span>{blurb}</span></div>'
            self.assertEqual(_cond(html), "N/A", f"blurb={blurb!r} 泄漏了")

    def test_empty_and_malformed_are_na(self):
        for html in ("", "<html></html>", "<div id=\"buybox\"></div>",
                     "<div id=\"condition-and-price-row\"></div>"):
            self.assertEqual(_cond(html), "N/A", f"html={html!r}")

    def test_unknown_condition_words_are_na(self):
        """不在词表里的说法一律 "N/A" —— 宁可留空也不要自创品相。"""
        for raw in ("Pre-owned", "Second hand", "Grade B", "Damaged box"):
            html = f'<div id="condition-and-price-row">{raw}</div>'
            self.assertEqual(_cond(html), "N/A", f"raw={raw!r}")

    # ------------------------------------------------ 结构性
    def test_default_result_carries_the_key(self):
        """解析整体失败时也必须有这个键，否则写库时缺键。"""
        d = AmazonParser()._default_result("B0TEST00001", "10001")
        self.assertEqual(d["offer_condition"], "N/A")

    def test_column_is_last_and_exportable(self):
        """列序是承重的（`SELECT d.*` 无 response_model，列序直通 erpAPI）。"""
        from common.core.asindata import ASIN_DATA_FIELDS
        from common.models import EXPORTABLE_FIELDS
        from common.pgdb.schema import EXPECTED_COLUMNS
        self.assertEqual(ASIN_DATA_FIELDS[-1], "offer_condition")
        self.assertEqual(EXPECTED_COLUMNS["asin_data"][-1], "offer_condition")
        self.assertIn("offer_condition", EXPORTABLE_FIELDS)


if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------------------ accordion 布局
#
# 2026-09 起部分商品的 buybox 变成多 offer 的 accordion：一张卡片里纵向堆
# 会员价行 / 全新行 / 二手行，只有一行展开（a-accordion-active）。夹具按
# B0GVNC5WHS 真实页面的 DOM 路径精简而来（id 与 class 逐字照抄）：
#   #buybox > … > #buyBoxAccordion > #primeSavingsUpsellAccordionRow
#                                  > #usedAccordionRow > #newAccordionRow_1
#
# 旧实现把整张 #buybox 当一个文本块匹配，于是折叠着的二手行标题被当成了
# 主推 offer 的品相 —— B0GVNC5WHS 被错标成 "Used - Very Good"。

def _accordion(active: str, with_used: bool = True) -> str:
    def row(rid, caption, price):
        cls = "a-box a-accordion-row" + (" a-accordion-active" if rid == active else "")
        return (f'<div id="{rid}" class="{cls}"><div class="a-box-inner">'
                f'<span>{caption}</span><span class="a-offscreen">{price}</span>'
                f'</div></div>')
    rows = [row("primeSavingsUpsellAccordionRow", "Prime Member Price", "$36.99")]
    if with_used:
        rows.append(row("usedAccordionRow", "Used - Very Good", "$37.49"))
    rows.append(row("newAccordionRow_1", "Regular Price", "$49.99"))
    return ('<div id="desktop_buybox"><div id="buybox"><div id="buyBoxAccordion">'
            + "".join(rows) + '</div></div></div>')


class AccordionLayoutTests(unittest.TestCase):

    def test_collapsed_used_row_does_not_leak(self):
        """B0GVNC5WHS 的回归：主推是会员价全新件，折叠的二手行不许串进来。"""
        self.assertEqual(_cond(_accordion("primeSavingsUpsellAccordionRow")), "N/A")

    def test_regular_row_active_is_not_used(self):
        self.assertEqual(_cond(_accordion("newAccordionRow_1")), "N/A")

    def test_used_row_active_is_detected(self):
        """二手行真的是主推时必须识别出来 —— 否则修复就是空心的（恒返回 N/A）。"""
        self.assertEqual(_cond(_accordion("usedAccordionRow")), "Used - Very Good")

    def test_two_tier_without_used_row(self):
        """B0BG4WNK3V 形态：只有会员价 + 全新两行。"""
        self.assertEqual(
            _cond(_accordion("primeSavingsUpsellAccordionRow", with_used=False)), "N/A")

    def test_title_fallback_still_applies(self):
        """accordion 里读不到品相时，翻新品标题兜底照旧生效。"""
        self.assertEqual(
            _cond(_accordion("primeSavingsUpsellAccordionRow"), "Phone 64GB (Renewed)"),
            "Renewed")
