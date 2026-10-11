"""文本类字段的三个解析缺陷（2026-10 加拿大站抽查暴露，美国站同样复现）。

    G3 bullet_points 黑名单按子串整条删，误删卖家真实卖点
    G4 image_urls    ``"hiRes":null`` 的图整张丢（包括主图）
    G5 brand / 详情值 双 td 布局取值带首尾空格；&nbsp; 与跨标签双空格

夹具是真实页面的原文片段，来历与截取规则见 ``tests/fixtures/parser_text/README.md``。
少数用例在真实片段上做了**一处**标明的改动（构造变体），用来钉住规则的边界 ——
那几条的 docstring 里写了改的是哪一处、为什么真实页面上没有现成的例子。

每个缺陷的用例都**两条引擎各跑一遍**：解析器有 selectolax / lxml 两条路径，
生产上 lxml 只在 selectolax 缺失时才跑，只测一条等于另一条没人看。
走的是 ``parse_product`` 整个出口（含 ``_clean_deep`` 与品牌出口归一），
引擎用模块开关 ``_USE_SELECTOLAX`` 切。
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
    """按夹具名（``ca_<ASIN>`` / ``us_<ASIN>``）决定站点与邮编，走完整出口。"""
    site, asin = name.split("_", 1)
    zip_code, mp = ("K1V 7P8", "amazon.ca") if site == "ca" else ("10001", None)
    with mock.patch.object(P, "_USE_SELECTOLAX", engine == "selectolax"):
        return AmazonParser().parse_product(html, asin, zip_code, mp)


def _fx(name: str, engine: str) -> dict:
    return _parse(_html(name), name, engine)


def _lines(s: str) -> list:
    return [x for x in (s or "").split("\n") if x]


# ==========================================================================
# G3  bullet_points —— 黑名单只挡 Amazon 模板句，不挡卖家小标题开头的卖点
# ==========================================================================
#: 三张页面的第 5 条卖点原文开头 —— 基线上都被 "customer service" 整条删掉。
_SELLER_BULLETS = {
    "ca_B0D4QGVN6F": "Smiley Customer Service: Welcome to Contact Us;",
    "ca_B09Y3DKJKQ": "Customer Support - Comes with 1 luggage rack as picture shown,",
    "ca_B0D45YH9FL": "Instruction and Customer Service: You could see the installation steps",
}


class BulletBlacklistNarrowed(unittest.TestCase):

    def test_seller_bullets_mentioning_customer_service_are_kept(self):
        for name, head in _SELLER_BULLETS.items():
            for engine in ENGINES:
                with self.subTest(page=name, engine=engine):
                    bullets = _lines(_fx(name, engine)["bullet_points"])
                    self.assertEqual(len(bullets), 5, bullets)
                    self.assertTrue(bullets[4].startswith(head), bullets[4])

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
        """判据本身，全部用真实页面上的原句。"""
        is_bp = AmazonParser._is_bullet_boilerplate
        # Amazon 模板句：美国退货弹层的三句 + 标题；加拿大 offer 区的退货说明
        for s in ("Go to your orders and start the return",
                  "Select your preferred free shipping option",
                  "Drop off and leave!",
                  "Return this item for free",
                  "Eligible for Return, Refund or Replacement within 30 days of receipt"):
            with self.subTest(template=s):
                self.assertTrue(is_bp(s))
        # 卖家卖点：以自己的小标题开头（冒号 / 短横线），含黑名单词也放过
        for s in ("Smiley Customer Service: Welcome to Contact Us; Your satisfaction is our first priority",
                  "Customer Support - Comes with 1 luggage rack as picture shown, instructions and docking "
                  "kit are not included. Back-up with 24/7 customer service, if any questions you have, "
                  "please message us to get a satisfying solution.",
                  "Instruction and High Quality Wood: You could see the installation steps on the manual and "
                  "our customer service team is always there to help you"):
            with self.subTest(seller=s[:40]):
                self.assertFalse(is_bp(s))
        # 不含黑名单词的照旧放过
        self.assertFalse(is_bp("5 Tier Open Multifunctional Bookshelf: This five shelf bookcase"))


# ==========================================================================
# G4  image_urls —— colorImages.initial 逐张取 hiRes or large
# ==========================================================================
def _initial_entries(html: str) -> list:
    """测试侧独立解析一遍 initial 数组，作为对照（不走被测代码）。"""
    m = re.search(r"'initial': A\.\$\.parseJSON\('(\[.*?\])'\)", html)
    return json.loads(m.group(1))


class ColorImagesPerEntry(unittest.TestCase):

    def test_main_image_with_null_hires_is_kept_first(self):
        """加拿大 B09Y3DKJKQ：MAIN 图只有 500x500，hiRes=null。基线把**主图**丢了，
        列表第 1 张变成 PT01。"""
        entries = _initial_entries(_html("ca_B09Y3DKJKQ"))
        self.assertIsNone(entries[0]["hiRes"])
        want = [e["hiRes"] or e["large"] for e in entries]
        self.assertEqual(want[0], "https://m.media-amazon.com/images/I/4104axN68uL._AC_.jpg")
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.assertEqual(_lines(_fx("ca_B09Y3DKJKQ", engine)["image_urls"]), want)

    def test_trailing_null_hires_image_is_kept_in_place(self):
        """加拿大 B0FFGFT343：第 9 张 PT08 hiRes=null（714x401）。"""
        entries = _initial_entries(_html("ca_B0FFGFT343"))
        self.assertIsNone(entries[-1]["hiRes"])
        want = [e["hiRes"] or e["large"] for e in entries]
        self.assertEqual(len(want), 9)
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.assertEqual(_lines(_fx("ca_B0FFGFT343", engine)["image_urls"]), want)

    def test_falls_back_to_largest_main_when_large_is_missing(self):
        """构造变体：把 MAIN 的 large 也置成 null（真实页面上没见到两者都缺的）。
        这时取 main 里面积最大的那张 —— 它在 main 里排**最后**，取第一个就错了。"""
        html = _html("ca_B09Y3DKJKQ").replace(
            '"large":"https://m.media-amazon.com/images/I/4104axN68uL._AC_.jpg"', '"large":null', 1)
        for engine in ENGINES:
            with self.subTest(engine=engine):
                urls = _lines(_parse(html, "ca_B09Y3DKJKQ", engine)["image_urls"])
                self.assertEqual(urls[0], "https://m.media-amazon.com/images/I/4104axN68uL._AC_.jpg")
                self.assertEqual(len(urls), 8)

    def test_duplicate_entry_is_dropped_and_order_kept(self):
        """构造变体：把 initial 数组的第 2 个条目原文复制一份接到数组末尾（一处改动；
        抽查页面里没撞上重复条目，但规格要求"保序去重"，不钉住的话去掉去重也全绿）。
        复制的是原始字节区间，不经 json.dumps 重新序列化。"""
        html = _html("ca_B09Y3DKJKQ")
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
        self.assertEqual(len(entries), 9)
        self.assertEqual(len(want), 8)
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.assertEqual(_lines(_parse(html, "ca_B09Y3DKJKQ", engine)["image_urls"]), want)

    def test_js_escapes_in_alt_text_do_not_break_parsing(self):
        """构造变体：真实页面的 altText 全是 null。给 MAIN 填一个带撇号和双引号的值，
        按 Amazon 写进单引号 JS 字符串时的转义（``\\'`` 与 ``\\\\"``）。
        解析失败会退回整段正则 —— 那样主图又丢了，所以"8 张、主图在首位"能区分两者。"""
        html = _html("ca_B09Y3DKJKQ").replace(
            '"altText":null', r'''"altText":"Rider\'s \\"Touring\\" rack"''', 1)
        for engine in ENGINES:
            with self.subTest(engine=engine):
                urls = _lines(_parse(html, "ca_B09Y3DKJKQ", engine)["image_urls"])
                self.assertEqual(len(urls), 8)
                self.assertIn("4104axN68uL", urls[0])

    def test_unrecognised_structure_keeps_the_old_behaviour(self):
        """守卫：没有 ``initial`` 的写法（twister 按颜色分组的 colorImages，截自同一页
        后面那段 script 的一个条目）认不出来，退回原来的整段正则，结果与改动前一致。"""
        html = ('<html><body><script>var data = {"landingAsinColor":"Black","colorImages":{"Chrome":'
                '[{"large":"https://m.media-amazon.com/images/I/41HCt4xu3BL._AC_.jpg",'
                '"thumb":"https://m.media-amazon.com/images/I/41HCt4xu3BL._AC_US40_.jpg",'
                '"hiRes":"https://m.media-amazon.com/images/I/71aGjufxaeL._AC_SL1500_.jpg",'
                '"variant":"MAIN"}]}};</script></body></html>')
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.assertEqual(_parse(html, "ca_B09Y3DKJKQ", engine)["image_urls"],
                                 "https://m.media-amazon.com/images/I/71aGjufxaeL._AC_SL1500_.jpg")


# ==========================================================================
# G5  品牌 / 详情值空白 —— 首尾去掉，中间连续空白并成一个普通空格
# ==========================================================================
class DetailValueWhitespace(unittest.TestCase):

    def test_squash_rule(self):
        """用户确认的口径。"""
        sq = AmazonParser._squash_ws
        for raw in (" Beach Camping ", "Beach  Camping", "Beach\xa0Camping",
                    "\tBeach Camping", "\n    Beach \t\xa0 Camping   \n"):
            with self.subTest(raw=raw):
                self.assertEqual(sq(raw), "Beach Camping")

    def test_product_overview_brand_has_no_edge_spaces(self):
        """加拿大 B09Y3DKJKQ：``<td class="a-span9">    <span>MoKitDora</span>   </td>``。
        基线 selectolax 出 ' MoKitDora '，lxml 出 'MoKitDora'（走 byline）—— 两条引擎还不一致。"""
        self.assertIn('<td class="a-span9" role="presentation">    <span class="a-size-base '
                      'po-break-word">MoKitDora</span>   </td>', _html("ca_B09Y3DKJKQ"))
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.assertEqual(_fx("ca_B09Y3DKJKQ", engine)["brand"], "MoKitDora")

    def test_double_td_branch_squashes_at_the_root(self):
        """根因处（_slx_parse_all_details 的双 td 分支）本身。品牌在 parse_product 出口
        还有一道归一，只看出口的 brand 测不出这一行有没有改 —— 所以直接调详情扫描。
        lxml 路径不读双 td 行，没有对应的分支。"""
        from selectolax.parser import HTMLParser
        html = _html("ca_B09Y3DKJKQ")
        d = AmazonParser()._slx_parse_all_details(HTMLParser(html), html)
        self.assertEqual(d["brand"], "MoKitDora")
        for k, v in d.items():
            with self.subTest(field=k):
                self.assertEqual(v, AmazonParser._squash_ws(v))

    def test_overview_only_manufacturer_has_no_edge_spaces(self):
        """美国 B0F3JB4D4Z 的 Product Overview 原文（片段里有意不带后面的 th/td 详情表）。
        整页上详情表那一行排在后面、会覆盖 po- 行（_map_detail 后写的赢），所以整页看不出；
        只有 Product Overview 的页面上制造商就只能从双 td 行取，而出口只给品牌补了一道归一，
        制造商没有。基线 selectolax 出 ' CHANGZHOU JUTAI ELECTRONIC CO.,LTD '。
        只测 selectolax：lxml 路径不读双 td 行（这里出 N/A，是既有的引擎差异，不归本组）。"""
        r = _fx("us_B0F3JB4D4Z", "selectolax")
        self.assertEqual(r["manufacturer"], "CHANGZHOU JUTAI ELECTRONIC CO.,LTD")
        self.assertEqual(r["brand"], "SHareconn")

    def test_nbsp_in_detail_bullets_li_value_both_engines(self):
        """detailBullets 的 li 分支（``li > span > span.a-text-bold`` + 值 span），两条引擎
        各有一处取值，各自都要归一。加拿大 B0B3RDLC1J 的真实 Manufacturer 行先原样跑一遍，
        再把值换成带 &nbsp; 的写法（构造变体，一处改动）。基线 selectolax 出
        'NOW\\xa0\\xa0STO'，lxml 出 'NOW\\xa0\\xa0STO\\xa0'。"""
        html = _html("ca_B0B3RDLC1J")
        self.assertEqual(html.count("<span>NOWSTO</span>"), 1)
        nbsp = html.replace("<span>NOWSTO</span>", "<span>NOW&nbsp;&nbsp;STO&nbsp;</span>")
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.assertEqual(_parse(html, "ca_B0B3RDLC1J", engine)["manufacturer"], "NOWSTO")
                self.assertEqual(_parse(nbsp, "ca_B0B3RDLC1J", engine)["manufacturer"], "NOW STO")

    def _po_brand(self, inner: str) -> str:
        """构造变体：只替换真实 po-brand 行里那个 span 的内容。"""
        return _html("ca_B09Y3DKJKQ").replace(
            '<span class="a-size-base po-break-word">MoKitDora</span>', inner, 1)

    def test_words_split_across_tags_get_one_space(self):
        """两个词分在两个标签里：分隔符 + 标签间的空白文本节点 -> 双空格。只测
        selectolax：lxml 路径不读双 td 行（品牌走 byline），这条在它上面不存在。"""
        html = self._po_brand('<span>Mo</span> <span>KitDora</span>')
        self.assertEqual(_parse(html, "ca_B09Y3DKJKQ", "selectolax")["brand"], "Mo KitDora")

    def test_invisible_mark_next_to_space_is_squashed_at_the_exit(self):
        """U+200E 由出口的 _clean_deep 删掉，删完才露出首部空格 —— 所以品牌的出口
        归一必须排在 _clean_deep 之后。"""
        html = self._po_brand('<span class="a-size-base po-break-word">\u200e MoKitDora</span>')
        self.assertEqual(_parse(html, "ca_B09Y3DKJKQ", "selectolax")["brand"], "MoKitDora")

    def test_nbsp_in_th_td_detail_value_both_engines(self):
        """构造变体：真实 Manufacturer 行的值换成带 &nbsp; 的写法（抽查页面里没撞上，
        但 lxml 的 normalize-space 不认 \\xa0，是确定会漏的）。同一处取值喂所有详情字段，
        制造商就是品牌之外受影响的那一类。"""
        html = _html("ca_B09Y3DKJKQ").replace(
            "<td class=\"a-size-base prodDetAttrValue\">    MoKitDora   </td>",
            "<td class=\"a-size-base prodDetAttrValue\">    MoKitDora&nbsp;&nbsp;Co.&nbsp;Ltd   </td>", 1)
        self.assertIn("&nbsp;Ltd", html)
        for engine in ENGINES:
            with self.subTest(engine=engine):
                self.assertEqual(_parse(html, "ca_B09Y3DKJKQ", engine)["manufacturer"],
                                 "MoKitDora Co. Ltd")


# ==========================================================================
# 两条引擎在全部夹具上对本组三个字段给出同样的答案
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
