"""标题被 Amazon 拆成两段之后还要拼回去（2026-08 改版）。

2026-08 Amazon 把商品标题拆成两个元素：`span#productTitle` 只装前半段，
后半段挪进兄弟节点 `div.dp-title-differentiators`。采集到的 title 因此
**静默变短** —— 不是空、不是 N/A、不报错，只是少了后半段。

用户报的实例（River Dream 浴帘）：

    改版前  River Dream Waffle No Hook Shower Curtain with Liner,Graphite Grey,71x74
            | Snap-in Liner,Heavy Duty,Hotel Grade,Mesh Top Window,With Bottom
            Magnets,Washable,Dotted Waffle,Standard Size
    改版后  River Dream Waffle No Hook Shower Curtain with Liner,Graphite Grey,71x74

------------------------------------------------------------------------
夹具是从**真实页面逐字节抄下来的**
------------------------------------------------------------------------
`_REAL_TITLE_SECTION` 是 2026-08-19 实抓 `https://www.amazon.com/dp/B0F3JKMZLN`
的 `#titleSection` 原文，一个空格都没动（含 Amazon 自己那两行注释、那串缩进
空白、以及文本前后的空格）。

这一条是**上一次事故的直接教训**：`slow.variant.theme` 那个 bug 之所以能在
守卫全绿的情况下坏掉，就是因为夹具写的是 `"Color:Red"` —— 一个采集侧从未
产出过的形态。**夹具不真实，守卫就是装饰。**
"""
from __future__ import annotations

import unittest

from lxml import html as lhtml
from selectolax.parser import HTMLParser

from worker.parser import AmazonParser


# ---- 实抓：B0F3JKMZLN，2026-08-19，有 differentiators ----
_REAL_TITLE_SECTION = (
    '<div id="titleSection" class="a-section a-spacing-none"> '
    '<h1 id="title" class="a-size-medium a-spacing-none">  '
    '<span id="productTitle" class="a-size-medium product-title-word-break">'
    '        710PCS M3 Screw Assortment Kit, M3x6/8/10/12/16/20/25/30mm '
    'M3 Screw Kits       </span>      </h1> '
    '<!-- Title Differentiators for Desktop -->\n'
    '         <div class="a-section dp-title-differentiators">       '
    '<!-- Show as comma-separated string -->\n'
    '                        <span class="a-size-base a-color-secondary"> '
    '10.9 Grade Alloy Steel Metric Hex Button Head Cap Screws, Nuts and Flat '
    'Washers, Black Zinc Plated Screw Set for 3D Printing </span>   </div>  '
    '<div id="expandTitleToggle" class="a-section a-spacing-none expand '
    'aok-hidden"></div>  </div>'
)

#: 同一张页面里 Amazon **自己**把两段拼成的串（举报低价弹窗的隐藏域）。
#: 我们拼出来的必须与它逐字节相同 —— 分隔符不是我们编的。
_AMAZON_OWN_JOIN = (
    "710PCS M3 Screw Assortment Kit, M3x6/8/10/12/16/20/25/30mm M3 Screw Kits"
    " | "
    "10.9 Grade Alloy Steel Metric Hex Button Head Cap Screws, Nuts and Flat "
    "Washers, Black Zinc Plated Screw Set for 3D Printing"
)

# ---- 实抓：B07FZ8S74R（Echo Dot），2026-08-19，**整块 differentiators 都没有** ----
_REAL_NO_DIFF_SECTION = (
    '<div id="titleSection" class="a-section a-spacing-none"> '
    '<h1 id="title" class="a-size-large a-spacing-none">  '
    '<span id="productTitle" class="a-size-large product-title-word-break"> '
    'Echo Dot (3rd Gen, 2018 release) - Smart speaker with Alexa - Charcoal '
    '</span>      </h1>   </div>'
)
_NO_DIFF_TITLE = ("Echo Dot (3rd Gen, 2018 release) - Smart speaker with "
                  "Alexa - Charcoal")


def _page(body: str) -> str:
    return f"<html><head><title>t</title></head><body>{body}</body></html>"


def _both(body: str):
    """两个解析引擎各跑一遍，返回 (selectolax, lxml)。"""
    p = AmazonParser()
    return (p._slx_parse_title(HTMLParser(_page(body))),
            p._parse_title(lhtml.fromstring(_page(body))))


class TitleDifferentiatorTests(unittest.TestCase):
    """**这一组对应"标题静默变短"那个 bug。**"""

    def test_differentiator_is_appended(self):
        slx, lx = _both(_REAL_TITLE_SECTION)
        for name, got in (("selectolax", slx), ("lxml", lx)):
            self.assertEqual(
                got, _AMAZON_OWN_JOIN,
                f"{name} 只拿到了前半段 —— 2026-08 起后半段在 "
                "div.dp-title-differentiators 里，不在 #productTitle 里了")

    def test_separator_is_amazons_own_not_ours(self):
        """分隔符不是我们编的：与同页 Amazon 自己拼的串逐字节相同。

        这条用例的价值在于：哪天有人觉得 `" - "` / `", "` 更好看而改掉，
        输出就不再等于历史值，下游的 slow_hash 会**全库**报一次假变更。
        """
        slx, _ = _both(_REAL_TITLE_SECTION)
        self.assertEqual(slx, _AMAZON_OWN_JOIN)
        self.assertIn(" | ", slx)

    def test_missing_differentiator_leaves_no_dangling_separator(self):
        """没有后半段时原样返回主标题，**不许留下孤零零的分隔符**。

        `"标题 | "` 这种尾巴会进 slow_hash，把一次解析瑕疵变成一次
        "慢变字段变了"的假变更 —— 比少个后半段更难查。
        """
        slx, lx = _both(_REAL_NO_DIFF_SECTION)
        for name, got in (("selectolax", slx), ("lxml", lx)):
            self.assertEqual(got, _NO_DIFF_TITLE, name)
            self.assertFalse(got.rstrip().endswith("|"), f"{name} 留下了尾巴")
            self.assertNotIn("|", got, name)

    def test_both_engines_agree(self):
        """两个引擎必须给出**同一个** title。

        不然同一个商品会因为走了哪条引擎而产生不同的 slow_hash，
        看起来像"标题变了"。这也是 `_join_title_parts` 必须共用的理由。
        """
        for body in (_REAL_TITLE_SECTION, _REAL_NO_DIFF_SECTION):
            slx, lx = _both(body)
            self.assertEqual(slx, lx)

    def test_differentiator_outside_title_section_is_ignored(self):
        """页面别处的同名容器不该被当成本商品标题的后半段。

        `dp-title-differentiators` 这个 class 名足够通用，推荐位/变体卡片
        用上它并非不可想象。选择器限定在 `#titleSection` 之内。
        """
        decoy = ('<div class="a-section dp-title-differentiators">'
                 '<span>DECOY FROM SOME OTHER WIDGET</span></div>')
        slx, lx = _both(decoy + _REAL_NO_DIFF_SECTION)
        for name, got in (("selectolax", slx), ("lxml", lx)):
            self.assertEqual(got, _NO_DIFF_TITLE, f"{name} 吸进了页面别处的容器")

    def test_multiple_text_nodes_in_container_are_all_collected(self):
        """容器里不止一个文本节点时要全收。

        lxml 侧这里踩过坑：`.../text()` 只拿直接文本节点（文案在子 span 里
        -> 恒空），而 `string(...)` 会让 `_get_text` 的 `result[0]`
        取到**第一个字符**。两条都是"静默变短"，正是本次要修的那种。
        """
        body = ('<div id="titleSection"><h1 id="title">'
                '<span id="productTitle">Main</span></h1>'
                '<div class="a-section dp-title-differentiators">'
                '<span>First</span> <span>Second</span></div></div>')
        slx, lx = _both(body)
        for name, got in (("selectolax", slx), ("lxml", lx)):
            self.assertIn("First", got, name)
            self.assertIn("Second", got, name)


class JoinHelperTests(unittest.TestCase):
    """`_join_title_parts` 的单元语义。它是两个引擎的唯一真源。"""

    def setUp(self):
        self.join = AmazonParser._join_title_parts

    def test_plain_join(self):
        self.assertEqual(self.join("A", "B"), "A | B")

    def test_empty_differentiator_returns_main_unchanged(self):
        for empty in (None, "", "   "):
            self.assertEqual(self.join("A", empty), "A")

    def test_empty_main_falls_back_without_inventing_a_title(self):
        self.assertEqual(self.join(None, "B"), "B")
        self.assertEqual(self.join("", ""), "N/A")

    def test_whitespace_is_trimmed_on_both_parts(self):
        self.assertEqual(self.join("  A  ", "  B  "), "A | B")

    def test_contained_differentiator_is_not_appended_twice(self):
        """Amazon 偶尔两处都给全量。那时拼两遍会造出一个页面上不存在的标题。"""
        full = "A | B"
        self.assertEqual(self.join(full, "B"), full)


if __name__ == "__main__":
    unittest.main()
