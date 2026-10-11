"""加拿大站详情表的两处**标签差异**：原产国、UPC（PR #23，P 组）。

------------------------------------------------------------------------
症状
------------------------------------------------------------------------
2026-10-10 抽查 36 张加拿大真实商品页：

* 13 张页面详情表里写着 ``Country of Publication | China``，导出的原产国全是 N/A；
* 6 张页面写着 ``ProductIDType | 604938601102``，导出的 UPC 列表全空。

两处根因相同，也和 F-012 的 ``Parcel Dimensions`` 是同一类：**加拿大站给同一个
属性用了另一个标签**，``_map_detail`` 的判据只认美国站那个写法，整行走空 ——
不是页面没有、也不是解析不到，是认不出来。

------------------------------------------------------------------------
为什么用原文切片
------------------------------------------------------------------------
夹具是真实页面上那一栏详情表的原文（来历与裁剪规则见
``tests/fixtures/ca/labels/README.md``）。标签前后的空白、``prodDetSectionEntry``
这些 class、一格两个码之间的分隔，都是页面原样 —— 构造的 HTML 只能测
"我以为页面长什么样"。

优先级类用例（Origin 与 Publication 同页、UPC 与 ProductIDType 同页）在抽查的
页面里没有真实样本，所以用**真实的行**拼表，只把值换成互不相同的，好分辨谁赢。

------------------------------------------------------------------------
两条引擎都要过
------------------------------------------------------------------------
每条用例都分别把 ``_USE_SELECTOLAX`` 钉成 True / False 走完整的
``parse_product``（含出口清洗）。生产上 lxml 只在 selectolax 缺失时才跑，
靠模块级开关永远测不到它。
"""
from __future__ import annotations

import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 与 test_parser_quality.py 同理：别的测试文件可能把 worker.parser 桩成假的
# 塞进 sys.modules，这里要的是真实解析器。
sys.modules.pop("worker.parser", None)
import worker.parser as P                        # noqa: E402

_FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fixtures", "ca", "labels")
#: (邮编, marketplace) —— 与 verify_ca 抽查时 parse_product 的调用口径一致。
_SITE = {"ca": ("K1V 7P8", "amazon.ca"), "us": ("10001", None)}

_ENGINES = [("lxml", False)]
if P.SlxParser is not None:
    _ENGINES.append(("selectolax", True))


def _fixture(name: str) -> str:
    with open(os.path.join(_FIXTURE_DIR, name), encoding="utf-8") as f:
        return f.read()


def _parse_all_engines(html: str, asin: str, site: str) -> dict:
    """{引擎名: parse_product 的结果}。"""
    zip_code, marketplace = _SITE[site]
    out = {}
    for name, use_slx in _ENGINES:
        with mock.patch.object(P, "_USE_SELECTOLAX", use_slx):
            out[name] = P.AmazonParser().parse_product(html, asin, zip_code,
                                                       marketplace)
    return out


def _row(fixture: str, label: str) -> str:
    """从夹具里原样取出 th 为 ``label`` 的那一整行 ``<tr>...</tr>``。"""
    for m in re.finditer(r"<tr>.*?</tr>", _fixture(fixture), re.S):
        th = re.search(r"<th[^>]*>(.*?)</th>", m.group(0), re.S)
        if th and th.group(1).strip() == label:
            return m.group(0)
    raise AssertionError(f"{fixture} 里没有 {label!r} 这一行 —— 夹具被改过？")


def _with_value(tr: str, value: str) -> str:
    """把一行的 td 内容换成 ``value``，标签、class、空白都不动。"""
    return re.sub(r"(<td[^>]*>)(.*?)(</td>)",
                  lambda m: f"{m.group(1)}    {value}   {m.group(3)}",
                  tr, count=1, flags=re.S)


def _page(*rows: str) -> str:
    """把若干真实的行拼成一张详情表，外层容器照抄真实页面。"""
    return ('<!doctype html><html><body>'
            '<div id="prodDetails" class="a-section a-spacing-medium">'
            '<div id="productDetails_expanderSectionTables" class="a-section">'
            '<table class="a-keyvalue prodDetTable"><tbody>'
            + "".join(rows) +
            '</tbody></table></div></div></body></html>')


class CountryOfPublicationIsTheCanadianOriginLabel(unittest.TestCase):
    """``Country of Publication`` -> ``country_of_origin``，但 Origin 优先。"""

    def test_publication_row_fills_country_of_origin(self):
        """实测症状：页面写着 ``Country of Publication | China``，导出原产国 N/A。

        压紧后的键 ``countryofpublication`` 不含 ``countryoforigin``，elif 链
        一条都不中，值被扔掉，出口落默认值 N/A。
        """
        for asin in ("B07JN24QCB", "B0CR5D95NN"):
            html = _fixture(f"{asin}.ca.html")
            for engine, r in _parse_all_engines(html, asin, "ca").items():
                with self.subTest(asin=asin, engine=engine):
                    self.assertEqual(r["country_of_origin"], "China")

    def test_origin_row_wins_whichever_comes_first(self):
        """同页两行都有时以 Origin 为准，**与行序无关**。

        Publication 在书类商品上字面意思是出版国，不一定是原产国，所以它只能
        填空。只写「先到先得」不够：两行谁在前取决于模板，字典迭代顺序 = 页面
        行序；只写「后到覆盖」也不够：Publication 在后就把 Origin 冲掉了。
        """
        origin = _row("B0CQWZR6L8.ca.html", "Country of Origin")       # China
        pub = _with_value(_row("B07JN24QCB.ca.html", "Country of Publication"),
                          "Viet Nam")
        cases = {
            "只有 Publication": ((pub,), "Viet Nam"),
            "Origin 在前": ((origin, pub), "China"),
            "Publication 在前": ((pub, origin), "China"),
            "只有 Origin（原有写法不受影响）": ((origin,), "China"),
        }
        for case, (rows, want) in cases.items():
            for engine, r in _parse_all_engines(_page(*rows), "B0COO00001",
                                                "ca").items():
                with self.subTest(case=case, engine=engine):
                    self.assertEqual(r["country_of_origin"], want)


class ProductIDTypeIsTheCanadianUpcLabel(unittest.TestCase):
    """``ProductIDType`` -> ``upc_list``，形状与美国站 ``UPC`` 那一格一致。"""

    def test_single_code(self):
        """实测症状：页面写着 ``ProductIDType | 604938601102``，导出 UPC 列表为空。

        ``ProductIDType`` 是 Amazon 没翻译的原始属性名，不含 "upc" 三个字母；
        兜底的两条正则（``"upc":"..."`` 与 ``UPC: 123...``）也都要字面 UPC。
        """
        html = _fixture("B0CR5D95NN.ca.html")
        for engine, r in _parse_all_engines(html, "B0CR5D95NN", "ca").items():
            with self.subTest(engine=engine):
                self.assertEqual(r["upc_list"], "604938601102")

    def test_two_codes_in_one_cell_keep_the_us_shape(self):
        """一格两个码都要留，而且同一件商品两站导出**同一个串**。

        美国站同一位置标签是 ``UPC``，那一格原样进 upc_list，导出就是
        ``"673986005705 196393154472"``（空格连着，没拆成逗号列表）。
        加拿大站照这个形状给 —— 拆不拆是两站一起改的事，这里拆了就是同一件
        商品两站两种形状。美国站那一半也钉住：它不该被这次改动碰到。
        """
        both = "673986005705 196393154472"
        ca = _parse_all_engines(_fixture("B07JN24QCB.ca.html"), "B07JN24QCB", "ca")
        us = _parse_all_engines(_fixture("B07JN24QCB.us.html"), "B07JN24QCB", "us")
        for engine in ca:
            with self.subTest(engine=engine):
                self.assertEqual(us[engine]["upc_list"], both, "美国站输出不该变")
                self.assertEqual(ca[engine]["upc_list"], both)

    def test_only_pure_12_or_13_digit_codes_are_taken(self):
        """这个属性名泛指「外部商品码」，只收 UPC-A（12 位）/ EAN-13（13 位）纯数字。

        一格里有怪值时丢怪值、留好码；整格都不合格就什么都不收，
        不把别的东西吞进 upc_list。
        """
        row = _row("B0CR5D95NN.ca.html", "ProductIDType")
        cases = [
            ("604938601102", "604938601102"),                  # UPC-A
            ("4006381333931", "4006381333931"),                # EAN-13
            ("B0CR5D95NN 604938601102", "604938601102"),       # 混进 ASIN
            ("604938601102 604938601102", "604938601102"),     # 同格重复
            ("60493860110", ""),                               # 11 位
            ("96385074", ""),                                  # 8 位 EAN-8
            ("10604938601109", ""),                            # 14 位 GTIN-14
            ("６０４９３８６０１１０２", ""),  # 全角数字
            ("N/A", ""),
        ]
        for value, want in cases:
            html = _page(_with_value(row, value))
            for engine, r in _parse_all_engines(html, "B0UPC00001", "ca").items():
                with self.subTest(value=value, engine=engine):
                    self.assertEqual(r["upc_list"], want)

    def test_upc_row_wins_whichever_comes_first(self):
        """同页真有 ``UPC`` 行时以它为准，``ProductIDType`` 只填空 —— 与行序无关。"""
        upc = _with_value(_row("B07JN24QCB.us.html", "UPC"), "673986005705")
        pid = _with_value(_row("B0CR5D95NN.ca.html", "ProductIDType"),
                          "604938601102")
        cases = {
            "只有 ProductIDType": ((pid,), "604938601102"),
            "UPC 在前": ((upc, pid), "673986005705"),
            "ProductIDType 在前": ((pid, upc), "673986005705"),
        }
        for case, (rows, want) in cases.items():
            for engine, r in _parse_all_engines(_page(*rows), "B0UPC00002",
                                                "ca").items():
                with self.subTest(case=case, engine=engine):
                    self.assertEqual(r["upc_list"], want)


if __name__ == "__main__":
    unittest.main()
