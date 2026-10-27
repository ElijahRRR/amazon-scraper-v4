"""【本分支说明】这是 main 上 ``tests/test_parser_text_fields.py`` 的副本，只用美国站片段，在加拿大分支上守住
美国站的同一批行为（加拿大分支自己的 ``tests/test_parser_text_fields.py`` 大多换成了加拿大片段）。
main 改那个文件时，把改动同步到这里。

五点黑名单与图片的两个解析缺陷（2026-10 抽查暴露，美国站真实页面复现）。

    bullet_points 黑名单按子串整条删，误删卖家真实卖点
    image_urls    ``"hiRes":null`` 的图整张丢（是 MAIN 图时丢的就是主图）

（品牌 / 详情值空白、卖点去重等见 tests/test_parser_detail_fixes.py。）

夹具是美国站真实页面的原文片段，来历与截取规则见 ``tests/fixtures/parser_text/README.md``。
少数用例在真实片段上做了**一处**标明的改动（构造变体），用来钉住规则的边界 ——
那几条的 docstring 里写了改的是哪一处、为什么真实页面上没有现成的例子。

每个缺陷的用例都**两条引擎各跑一遍**：解析器有 selectolax / lxml 两条路径，
生产上 lxml 只在 selectolax 缺失时才跑，只测一条等于另一条没人看。
走的是 ``parse_product`` 整个出口，引擎用模块开关 ``_USE_SELECTOLAX`` 切。
"""
from __future__ import annotations

import json
import os
import re
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


def _parse(html: str, name: str, engine: str) -> dict:
    """夹具名是 ``us_<ASIN>``；走完整出口。"""
    asin = name.split("_", 1)[1]
    with mock.patch.object(P, "_USE_SELECTOLAX", engine == "selectolax"):
        return AmazonParser().parse_product(html, asin, "10001")


def _fx(name: str, engine: str) -> dict:
    return _parse(_html(name), name, engine)


def _lines(s: str) -> list:
    return [x for x in (s or "").split("\n") if x]


# ==========================================================================
# bullet_points —— 黑名单只挡 Amazon 模板句，不挡卖家小标题开头的卖点
# ==========================================================================
#: 两张美国页上被 "customer service" 整条删掉的那条卖点的原文开头。
_SELLER_BULLETS = {
    "us_B0D4QGVN6F": "Smiley Customer Service: Welcome to Contact Us;",
    "us_B0D45YH9FL": "Instruction and High Quality Wood: You could see the installation steps",
}


class BulletBlacklistNarrowed(unittest.TestCase):

    def test_seller_bullets_mentioning_customer_service_are_kept(self):
        """基线两条引擎都只出 4 条（那一条被删）。"""
        for name, head in _SELLER_BULLETS.items():
            for engine in ENGINES:
                with self.subTest(page=name, engine=engine):
                    bullets = _lines(_fx(name, engine)["bullet_points"])
                    self.assertEqual(len(bullets), 5, bullets)
                    self.assertEqual(sum(b.startswith(head) for b in bullets), 1, bullets)
                    self.assertIn("customer service", next(
                        b for b in bullets if b.startswith(head)).lower())

    def _return_popover_as_bullet_list(self) -> str:
        """黑名单当初要挡的东西：退货弹层 "How to return the item?" 的三句。

        真实页面上它是 ``div.a-expander-content > ol``；五点的兜底选择器认的是
        ``div.a-expander-content ul > li span.a-list-item``。这里只把 ``ol`` 换成 ``ul``
        （一处改动），模拟模板句真的流进卖点解析器 —— 页面上没有 #feature-bullets 时，
        兜底选择器会在整页的 a-expander-content 里捞。
        """
        html = _html("us_B0D86XBV23")
        html = html.replace('<ol class="a-ordered-list', '<ul class="a-unordered-list')
        return html.replace("</ol>", "</ul>")

    def test_amazon_return_template_sentences_are_still_dropped(self):
        """守卫（旧样例）：三句模板句仍然一条都进不来，两条引擎都是。"""
        html = self._return_popover_as_bullet_list()
        from selectolax.parser import HTMLParser
        picked = [n.text(strip=True) for n in
                  HTMLParser(html).css("div.a-expander-content ul > li span.a-list-item")]
        # 先确认模板句**确实**被兜底选择器选中了，否则下面的空结果是真空的
        self.assertEqual(picked, ["Go to your orders and start the return",
                                  "Select your preferred free shipping option",
                                  "Drop off and leave!"])
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.assertEqual(_parse(html, "us_B0D86XBV23", engine)["bullet_points"], "")

    def test_boilerplate_judgement_on_real_text(self):
        """判据本身，全部用美国真实页面上的原句。"""
        is_bp = AmazonParser._is_bullet_boilerplate
        # Amazon 模板句：退货弹层的三句 + 弹层标题
        for s in ("Go to your orders and start the return",
                  "Select your preferred free shipping option",
                  "Drop off and leave!",
                  "Return this item for free"):
            with self.subTest(template=s):
                self.assertTrue(is_bp(s))
        # 卖家卖点：以自己的小标题开头（冒号），含黑名单词也放过
        for name in _SELLER_BULLETS:
            for b in _lines(_fx(name, "selectolax")["bullet_points"]):
                with self.subTest(seller=b[:40]):
                    self.assertFalse(is_bp(b))
        # 不含黑名单词的照旧放过
        self.assertFalse(is_bp("5 Tier Open Multifunctional Bookshelf: This five shelf bookcase"))


# ==========================================================================
# image_urls —— colorImages.initial 逐张取 hiRes or large
# ==========================================================================
def _initial_entries(html: str) -> list:
    """测试侧独立解析一遍 initial 数组，作为对照（不走被测代码）。"""
    m = re.search(r"'initial': A\.\$\.parseJSON\('(\[.*?\])'\)", html)
    return json.loads(m.group(1))


_MAIN_HIRES = '"hiRes":"https://m.media-amazon.com/images/I/81ZTLHGWFeL._AC_SL1500_.jpg"'


class ColorImagesPerEntry(unittest.TestCase):

    def test_trailing_null_hires_image_is_kept_in_place(self):
        """美国 B0FFGFT343：第 9 张 PT08 hiRes=null（714x401）。基线整张丢掉，只出 8 张。"""
        entries = _initial_entries(_html("us_B0FFGFT343"))
        self.assertIsNone(entries[-1]["hiRes"])
        want = [e["hiRes"] or e["large"] for e in entries]
        self.assertEqual(len(want), 9)
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.assertEqual(_lines(_fx("us_B0FFGFT343", engine)["image_urls"]), want)

    def test_main_image_with_null_hires_is_kept_first(self):
        """构造变体：把 MAIN 图的 hiRes 置成 null（一处改动；30 张美国页上 hiRes=null 的只有
        上面那张 PT08，真机抽查的别的页面上见过 MAIN 图只有 500x500、Amazon 不给 hiRes）。
        基线把**主图**丢了，列表第 1 张变成 PT01。"""
        html = _html("us_B0FFGFT343")
        self.assertEqual(html.count(_MAIN_HIRES), 1)
        html = html.replace(_MAIN_HIRES, '"hiRes":null')
        entries = _initial_entries(html)
        self.assertEqual(entries[0]["variant"], "MAIN")
        want = [e["hiRes"] or e["large"] for e in entries]
        for engine in ENGINES:
            with self.subTest(engine=engine):
                urls = _lines(_parse(html, "us_B0FFGFT343", engine)["image_urls"])
                self.assertEqual(urls, want)
                self.assertEqual(urls[0], entries[0]["large"])

    def test_falls_back_to_largest_main_when_large_is_missing(self):
        """构造变体：把 PT08（hiRes 已是 null）的 large 也置成 null（真实页面上没见到两者都缺的）。
        这时取 main 里面积最大的那张 —— 它在 main 里排**最后**，取第一个就错了。"""
        large = '"large":"https://m.media-amazon.com/images/I/51E41zXXguL._AC_.jpg"'
        html = _html("us_B0FFGFT343")
        self.assertEqual(html.count(large), 1)
        html = html.replace(large, '"large":null')
        for engine in ENGINES:
            with self.subTest(engine=engine):
                urls = _lines(_parse(html, "us_B0FFGFT343", engine)["image_urls"])
                self.assertEqual(len(urls), 9)
                self.assertEqual(urls[-1], "https://m.media-amazon.com/images/I/51E41zXXguL._AC_.jpg")

    def test_duplicate_entry_is_dropped_and_order_kept(self):
        """构造变体：把 initial 数组的第 2 个条目原文复制一份接到数组末尾（一处改动；
        抽查页面里没撞上重复条目，但"保序去重"不钉住的话去掉去重也全绿）。
        复制的是原始字节区间，不经 json.dumps 重新序列化。"""
        html = _html("us_B0FFGFT343")
        m = re.search(r"'initial': A\.\$\.parseJSON\('(\[.*?\])'\)", html)
        payload = m.group(1)
        dec = json.JSONDecoder()
        _, end1 = dec.raw_decode(payload, 1)
        self.assertEqual(payload[end1], ",")
        _, end2 = dec.raw_decode(payload, end1 + 1)
        dup = payload[:-1] + "," + payload[end1 + 1:end2] + "]"
        html = html[:m.start(1)] + dup + html[m.end(1):]

        entries = _initial_entries(html)
        self.assertEqual(entries[-1], entries[1], "变体里第 2 个条目确实重复出现在末尾")
        want = list(dict.fromkeys(e["hiRes"] or e["large"] for e in entries))
        self.assertEqual(len(entries), 10)
        self.assertEqual(len(want), 9)
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.assertEqual(_lines(_parse(html, "us_B0FFGFT343", engine)["image_urls"]), want)

    def test_js_escapes_in_alt_text_do_not_break_parsing(self):
        """构造变体：真实页面的 altText 全是 null。给 MAIN 填一个带撇号和双引号的值，
        按 Amazon 写进单引号 JS 字符串时的转义（``\\'`` 与 ``\\\\"``）。
        解析失败会退回整段正则 —— 那样 PT08 又丢了，所以"9 张"能区分两者。"""
        html = _html("us_B0FFGFT343").replace(
            '"altText":null', r'''"altText":"Rider\'s \\"Touring\\" rack"''', 1)
        for engine in ENGINES:
            with self.subTest(engine=engine):
                urls = _lines(_parse(html, "us_B0FFGFT343", engine)["image_urls"])
                self.assertEqual(len(urls), 9)
                self.assertIn("81ZTLHGWFeL", urls[0])

    def test_unrecognised_structure_keeps_the_old_behaviour(self):
        """守卫：没有 ``initial`` 的写法（twister 按颜色分组的 colorImages）认不出来，
        退回原来的整段正则，结果与改动前一致。"""
        html = ('<html><body><script>var data = {"landingAsinColor":"Black","colorImages":{"Chrome":'
                '[{"large":"https://m.media-amazon.com/images/I/41HCt4xu3BL._AC_.jpg",'
                '"thumb":"https://m.media-amazon.com/images/I/41HCt4xu3BL._AC_US40_.jpg",'
                '"hiRes":"https://m.media-amazon.com/images/I/71aGjufxaeL._AC_SL1500_.jpg",'
                '"variant":"MAIN"}]}};</script></body></html>')
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.assertEqual(_parse(html, "us_B0FFGFT343", engine)["image_urls"],
                                 "https://m.media-amazon.com/images/I/71aGjufxaeL._AC_SL1500_.jpg")


# ==========================================================================
# 两条引擎在全部夹具上对文本类字段给出同样的答案
# ==========================================================================
class EnginesAgreeOnTextFields(unittest.TestCase):
    FIELDS = ("bullet_points", "image_urls", "brand")

    def test_parity(self):
        for fn in sorted(os.listdir(_FX)):
            if not fn.endswith(".html"):
                continue
            name = fn[:-5]
            slx, lxm = _fx(name, "selectolax"), _fx(name, "lxml")
            for field in self.FIELDS:
                with self.subTest(page=name, field=field):
                    self.assertEqual(slx[field], lxm[field])


if __name__ == "__main__":
    unittest.main()
