"""解析出口必须剔除亚马逊漏出来的不可见控制字符（U+200E 等）。

------------------------------------------------------------------------
背景：这是真机采集数据里发现的
------------------------------------------------------------------------
2026-08-06 的真实采集，增量导出 10 条里 **2 条**带 U+200E
（LEFT-TO-RIGHT MARK）——亚马逊详情表那块 HTML 会在字段值前面插它：

    "brand": "\\u200eBBRGIRL"          肉眼、网页、Excel 全都看不出来
    "item_weight": "\\u200e4.6 ounces"

最能说明问题的一例：同一个商品 ``brand`` 是干净的 ``"AYLIFU"``、
``manufacturer`` 却是 ``"\\u200eAYLIFU"`` —— 同一个值、两个字段、一个干净
一个脏，因为取自页面不同位置。

两个后果：下游按品牌 join / 去重会把它当成另一个品牌；而这些字段全在
``SLOW_HASH_FIELDS`` 里，亚马逊哪天不再输出这个标记就会集体翻转一次哈希 =
一批假的「商品有变更」。

------------------------------------------------------------------------
⚠ 用例必须走 ``parse_product``，不能走 ``_parse_with_selectolax``
------------------------------------------------------------------------
清洗挂在 ``parse_product`` 的出口（两个引擎共用那一处）。而
``tests/test_parser_quality.py`` 的 ``slx()`` / ``lxm()`` 助手是**直接调用
两个引擎的私有方法**的——那条路径绕过出口，照那个写法写用例会永远是绿的，
测了个寂寞。这条陷阱本身也钉一条用例（``test_engine_level_helpers_bypass...``），
免得后来人"顺手统一一下风格"把覆盖悄悄改没了。
"""
from __future__ import annotations

import unittest

from worker.parser import AmazonParser

LRM = "‎"      # LEFT-TO-RIGHT MARK —— 元凶
RLM = "‏"      # RIGHT-TO-LEFT MARK
ZWSP = "​"     # ZERO WIDTH SPACE
BOM = "﻿"      # ZERO WIDTH NO-BREAK SPACE
ZWJ = "‍"      # ZERO WIDTH JOINER —— **必须保留**，emoji 组合序列靠它


def _html(*, title="Test Widget", brand="BBRGIRL",
          manufacturer="BBRGIRL", weight="4.6 ounces"):
    return f"""<html><body>
<span id="productTitle">{title}</span>
<a id="bylineInfo" href="/stores/X/page/Y">Visit the {brand} Store</a>
<div id="productDetails_feature_div">
<table id="productDetails_techSpec_section_1" class="prodDetTable">
<tr><th class="prodDetSectionEntry">Manufacturer</th>
    <td class="prodDetAttrValue">{manufacturer}</td></tr>
<tr><th class="prodDetSectionEntry">Item Weight</th>
    <td class="prodDetAttrValue">{weight}</td></tr>
</table></div>
</body></html>"""


class InvisibleCharsStrippedTests(unittest.TestCase):

    def setUp(self):
        self.p = AmazonParser()

    def _parse(self, html):
        return self.p.parse_product(html, "B0FQV1WY4P", "10001")

    def test_the_real_world_case_from_production_data(self):
        """真机数据里那两条的形状，逐字段复现。"""
        r = self._parse(_html(
            brand=f"{LRM}BBRGIRL",
            manufacturer=f"{LRM}BBRGIRL",
            weight=f"{LRM}4.6 ounces",
        ))

        self.assertEqual(r["brand"], "BBRGIRL")
        self.assertEqual(r["manufacturer"], "BBRGIRL")
        self.assertEqual(r["item_weight"], "4.6 ounces")

    def test_downstream_string_equality_actually_works(self):
        """这才是修它的**理由**：下游按品牌比对能不能对上。

        断言写成与干净字面量的 ``==``，而不是 ``LRM not in ...`` ——
        后者只证明"这一个字符没了"，前者证明"下游 join 能命中"。
        """
        dirty = self._parse(_html(brand=f"{LRM}BBRGIRL"))["brand"]
        clean = self._parse(_html(brand="BBRGIRL"))["brand"]

        self.assertEqual(dirty, clean)
        self.assertEqual(len(dirty), len("BBRGIRL"))

    def test_other_invisible_marks_too(self):
        """不止 U+200E —— 同一族的其它几个也一起洗。"""
        r = self._parse(_html(
            title=f"Wid{ZWSP}get",
            brand=f"{BOM}Acme",
            manufacturer=f"{RLM}Acme",
        ))

        self.assertEqual(r["title"], "Widget")
        self.assertEqual(r["brand"], "Acme")
        self.assertEqual(r["manufacturer"], "Acme")

    def test_emoji_zwj_sequences_survive(self):
        """**不能按 Unicode Cf 类别一扫了之。**

        U+200D（ZWJ）也是 Cf，但它是 emoji 组合序列的粘合剂：
        ``👨‍👩‍👧`` 去掉 ZWJ 会碎成三个独立 emoji。而商品标题和五点描述里
        确实有 emoji（真机数据里就有 🧩💨）。
        """
        family = f"👨{ZWJ}👩{ZWJ}👧"
        r = self._parse(_html(title=f"Gift Set {family} 🧩"))

        self.assertIn(family, r["title"], "ZWJ 被删了，emoji 组合序列被打碎")
        self.assertIn("🧩", r["title"])

    def test_ordinary_text_is_untouched(self):
        """清洗只删那几个不可见字符，不做 strip / 大小写 / NFKC。"""
        r = self._parse(_html(title="2 Pcs Clipboard — 办公用品 (12.8\"L)"))
        self.assertEqual(r["title"], "2 Pcs Clipboard — 办公用品 (12.8\"L)")

    def test_engine_level_helpers_bypass_the_cleaning(self):
        """守住本文件的**覆盖有效性**本身。

        清洗挂在 `parse_product` 出口。如果哪天有人把上面的用例"顺手改成"
        和 test_parser_quality.py 一样直接调 `_parse_with_selectolax`，
        用例会继续绿，但什么都没测到。

        这条用例把那个差异钉死：私有入口**确实**不洗。它红了只有两种可能
        —— 清洗被下移到了引擎内部（那是好事，把本用例删掉即可），
        或者出口那处清洗被人删了。两种都需要人来看一眼。
        """
        html = _html(brand=f"{LRM}BBRGIRL")
        raw = self.p._parse_with_selectolax(
            html, "B0FQV1WY4P", "10001",
            self.p._default_result("B0FQV1WY4P", "10001"),
            self.p._extract_jsonld(html))

        self.assertIn(LRM, raw["brand"],
                      "私有引擎入口现在也洗了 —— 请确认这是有意的，并删掉本用例")


if __name__ == "__main__":
    unittest.main()
