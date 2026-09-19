"""F-012 多站点采集：站点注册表与它撑住的那几条承重约束。

------------------------------------------------------------------------
这份文件守的是什么
------------------------------------------------------------------------
F-012 之前，「站点」在仓库里是一个进程级常量加一堆散落的字面量，
「这个采集器只能采美国站」这件事编码在 ``worker/session.py`` 的类定义里。
改造把它变成了一张注册表 + 一路传下去的参数。

改造的**前提**是：美国站行为一个字节都不变。这份文件里最重的一组用例
（``UsBehaviourIsUnchanged``）就是钉这一条 —— 它不是"新功能能用"的测试，
是"旧功能没坏"的测试，而后者才是这次改造真正的风险。

另外几组守的是改造中**真的踩过**的坑，每一条都写明了踩的是什么：
  * ``RenderSymbolIsExplicit``  —— 曾用"取 price_symbols 最后一个"推拼接符号，
    美国站于是拼出 "US$19.99"。
  * ``PostalShapesAreDisjoint`` —— pull 的排序省掉了 marketplace 一层，
    前提是两站邮编形状互斥。往注册表加站点时这条必须复验。
  * ``CurrencyIsNotInferredFromSymbol`` —— 美加价格都渲染成 $24.99。
"""
from __future__ import annotations

import os
import random
import string
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.core import marketplace as M  # noqa: E402
from common.core.asindata import ASIN_DATA_FIELDS  # noqa: E402


# ==========================================================================
# 1) 注册表自身的一致性
# ==========================================================================
class RegistryConsistency(unittest.TestCase):

    def test_registry_hosts_are_all_searchable_domains(self):
        """能采详情的站点必须也能翻搜索页（详情能力 ⊆ 搜索能力）。

        分叉的后果是一个站点"一半能用一半不能用"：详情任务建得出来、
        关键词批次在建批次那一步就 400。
        """
        M.assert_registry_within_search_domains()

    def test_every_spec_is_self_consistent(self):
        for mid in M.all_ids():
            with self.subTest(marketplace=mid):
                spec = M.get(mid)
                self.assertEqual(spec.id, mid)
                self.assertEqual(spec.host, f"www.{mid}")
                self.assertEqual(spec.base_url, f"https://www.{mid}")
                self.assertTrue(spec.zip_change_url.startswith(spec.base_url))
                self.assertEqual(len(spec.currency), 3, "ISO 4217 是三位")
                self.assertTrue(spec.currency.isupper())
                self.assertIn(spec.render_symbol, spec.price_symbols,
                              "拼接用的符号必须也是该站点认得出的符号之一")
                self.assertIsNotNone(spec.postal_pattern,
                                     "注册表里的站点必须有邮编规则 —— 没有就不该进来")
                self.assertTrue(M.validate_postal(spec.default_postal, mid),
                                "默认投递地必须通过自己站点的校验")

    def test_normalize_id_accepts_the_shapes_callers_actually_pass(self):
        for raw in ("amazon.ca", "www.amazon.ca", "https://www.amazon.ca/",
                    "AMAZON.CA", "  www.Amazon.CA  "):
            with self.subTest(raw=raw):
                self.assertEqual(M.normalize_id(raw), "amazon.ca")

    def test_unknown_marketplace_raises_instead_of_falling_back(self):
        """不认识的站点**抛异常**，不静默回退到美国站。

        静默回退正是 F-012 要消灭的那类故障：调用方以为在采加拿大、
        实际采的是美国，而没有任何一侧会响。
        """
        for bogus in ("amazon.de", "walmart.com", "amazon", "ca"):
            with self.subTest(bogus=bogus):
                with self.assertRaises(ValueError):
                    M.get(bogus)

    def test_none_and_empty_mean_us(self):
        """缺省 = 美国站。这是老 worker / 老调用方的兼容路径。"""
        for empty in (None, "", "   "):
            with self.subTest(empty=empty):
                self.assertEqual(M.get(empty).id, "amazon.com")


# ==========================================================================
# 2) 美国站行为逐字节不变 —— 本文件最重的一组
# ==========================================================================
class UsBehaviourIsUnchanged(unittest.TestCase):
    """改造的前提：``amazon.com`` 的每个值都照抄自改造前散落的字面量。"""

    def test_us_values_are_the_pre_f012_literals(self):
        us = M.get("amazon.com")
        self.assertEqual(us.default_postal, "10001",
                         "改造前 config.DEFAULT_ZIP_CODE 的默认值")
        self.assertEqual(us.accept_language, "en-US,en;q=0.9",
                         "改造前 session._build_headers 里写死的那一行")
        self.assertEqual(us.render_symbol, "$",
                         '改造前那些 f"${...}" 字面量')
        self.assertEqual(us.foreign_currency_markers,
                         ("CNY", "¥", "€", "£", "JP¥"),
                         "改造前 worker/ziputil.py:_NON_US_CURRENCY，逐元素照抄。"
                         "**没有**顺手补 CDN$ —— 那会改变既有的美国站判定")
        self.assertTrue(us.postal_zero_fill, "补零规则只对美国站适用")

    def test_us_zip_normalization_is_equivalent_to_the_old_implementation(self):
        """新旧邮编归一化在**随机模糊输入**下零差异。

        旧实现（F-012 之前的 ``server/app.py:_normalize_zip``）在这里原样重写
        一份当裁判 —— 不 import 老代码，因为老代码已经不在了。这份重写与
        当时那个函数逐行对应，改动点只有"去掉 re 依赖"。

        4000+ 组随机输入 + 手挑的边界值。这条用例是整次改造里唯一能
        证明"美国站没被改坏"的东西，别删。
        """
        def old_normalize_zip(val):
            import re as _re
            us_zip_re = _re.compile(r"^\d{5}\Z")
            if val is None:
                return None
            s = str(val).strip()
            if not s:
                return None
            if s.endswith(".0") and s[:-2].isdigit():
                s = s[:-2]
            head = s.split("-", 1)[0].strip()
            if head.isdigit() and 0 < len(head) < 5:
                head = head.zfill(5)
            return head if us_zip_re.match(head) else None

        cases = [
            "10001", "1001", "101", "1", "0", "00000", "99999", "90001.0",
            "1234.0", "10001-1234", "10001-", "  10001  ", "", "   ", None,
            "abcde", "1234a", "M5V 3L9", "123456", "12345678", "10001.5",
            "-10001", "+10001", "1.0", "0.0", "007", "10001\n", "\t90210",
            "1e5", "٠١٢٣٤",
        ]
        rnd = random.Random(20260919)
        pools = (string.digits, string.digits + "-. ", string.printable[:70])
        for _ in range(4000):
            k = rnd.randint(1, 7)
            pool = rnd.choice(pools)
            cases.append("".join(rnd.choice(pool) for _ in range(k)))
        cases += [rnd.randint(0, 999999) for _ in range(300)]
        cases += [float(rnd.randint(0, 99999)) for _ in range(200)]

        for c in cases:
            with self.subTest(raw=c):
                self.assertEqual(M.normalize_postal(c, "amazon.com"),
                                 old_normalize_zip(c))


# ==========================================================================
# 3) 加拿大邮编
# ==========================================================================
class CanadianPostalCodes(unittest.TestCase):

    def test_normalization_is_idempotent_and_canonical(self):
        """同一个地点只能归出**一种**形状。

        ``zip_requested`` 是消费侧分组键 (asin, marketplace, zip_requested)
        的一部分：归出两种形状就会把一个商品的价格序列劈成两组。
        """
        for raw in ("M5V 3L9", "m5v3l9", "M5V3L9", "m5v 3l9", "M5V  3L9",
                    "m5v-3l9", "  M5V3L9  "):
            with self.subTest(raw=raw):
                once = M.normalize_postal(raw, "amazon.ca")
                self.assertEqual(once, "M5V 3L9")
                self.assertEqual(M.normalize_postal(once, "amazon.ca"), once,
                                 "归一化必须幂等")

    def test_letters_excluded_by_canada_post_are_rejected(self):
        """D/F/I/O/Q/U 不出现在加拿大邮编里；W/Z 不做首字母。

        写成 [A-Z] 会放进一批根本不存在的编码，而这个值会被原样 POST 给
        Amazon —— 收到一个不存在的邮编，拿回来的是"设置失败"还是"静默落到
        默认地区"不可知，两种都比当场 400 难查。
        """
        for bad in ("D5V 3L9", "F5V 3L9", "I5V 3L9", "O5V 3L9", "Q5V 3L9",
                    "U5V 3L9", "W5V 3L9", "Z5V 3L9",
                    "M5D 3L9", "M5V 3D9", "M5I 3L9"):
            with self.subTest(bad=bad):
                self.assertIsNone(M.normalize_postal(bad, "amazon.ca"), bad)

    def test_malformed_shapes_are_rejected(self):
        for bad in ("M5V 3L", "M5V3L99", "12345", "M5V", "", None, "M5V-3L9X"):
            with self.subTest(bad=bad):
                self.assertIsNone(M.normalize_postal(bad, "amazon.ca"))

    def test_zero_fill_does_not_apply(self):
        """补零对加拿大邮编不适用 —— 补了会造出别的东西。"""
        self.assertFalse(M.get("amazon.ca").postal_zero_fill)
        self.assertIsNone(M.normalize_postal("1001", "amazon.ca"))

    def test_postal_equal_absorbs_shape_differences(self):
        """glow 回显的形状与我们 POST 出去的可能不同，比较必须吸收这个差异。

        直接字符串比会凭空造出一次 mismatch —— 与 relay 那条
        '01001' vs '1001' 是同一类事故，只是换了个国家。
        """
        self.assertTrue(M.postal_equal("M5V3L9", "M5V 3L9", "amazon.ca"))
        self.assertTrue(M.postal_equal("m5v 3l9", "M5V3L9", "amazon.ca"))
        self.assertFalse(M.postal_equal("M5V 3L9", "M4B 1B3", "amazon.ca"))

    def test_us_zip_is_not_valid_on_ca_and_vice_versa(self):
        self.assertFalse(M.validate_postal("10001", "amazon.ca"))
        self.assertFalse(M.validate_postal("M5V 3L9", "amazon.com"))


# ==========================================================================
# 4) 踩过的坑：拼接符号不能从 price_symbols 的顺序推
# ==========================================================================
class RenderSymbolIsExplicit(unittest.TestCase):
    """F-012 实现中途真踩过：用"取最后一个"推拼接符号，美国站拼出 "US$19.99"。

    两个站点的 price_symbols 顺序是反的（美国 ("$","US$")、
    加拿大 ("CDN$","C$","$")），"取第一个"和"取最后一个"各自只对一半。
    """

    def test_no_positional_rule_works_for_both_sites(self):
        """反向哨兵：证明"按位置取"这条路确实走不通，所以显式字段是必须的。"""
        firsts = {M.get(m).price_symbols[0] for m in M.all_ids()}
        lasts = {M.get(m).price_symbols[-1] for m in M.all_ids()}
        wanted = {M.get(m).render_symbol for m in M.all_ids()}
        self.assertEqual(wanted, {"$"})
        self.assertNotEqual(firsts, wanted, "若两者相等，说明注册表变了，本用例要重写")
        self.assertNotEqual(lasts, wanted, "若两者相等，说明注册表变了，本用例要重写")

    def test_render_symbol_is_a_bare_dollar_on_both_sites(self):
        for mid in ("amazon.com", "amazon.ca"):
            with self.subTest(marketplace=mid):
                self.assertEqual(M.get(mid).render_symbol, "$")


# ==========================================================================
# 5) 踩过的坑：pull 的排序省掉了 marketplace 一层
# ==========================================================================
class PostalShapesAreDisjoint(unittest.TestCase):
    """``pull_tasks`` 的 ORDER BY 里**没有** marketplace，这是有意的。

    理由是"按 zip 分组已经蕴含按站点分组" —— 而那条推理依赖
    「任意两个站点的邮编形状互斥」。往注册表加新站点时必须复验这一条，
    所以把它写成一条会红的用例，而不是一句注释。
    """

    #: 每个站点几个真实形状的样本。加站点时这里要补。
    SAMPLES = {
        "amazon.com": ("10001", "90210", "16602", "01001", "99999"),
        "amazon.ca": ("M5V 3L9", "K1A 0B1", "V6B 1A1", "H3Z 2Y7"),
    }

    def test_samples_cover_every_registered_marketplace(self):
        self.assertEqual(set(self.SAMPLES), set(M.all_ids()),
                         "注册表加了站点，这里的样本要补 —— 否则下面那条"
                         "互斥断言会在没覆盖的站点上静默失效")

    def test_no_postal_code_is_valid_on_two_marketplaces(self):
        for owner, samples in self.SAMPLES.items():
            for s in samples:
                for other in M.all_ids():
                    if other == owner:
                        continue
                    with self.subTest(postal=s, owner=owner, other=other):
                        self.assertFalse(
                            M.validate_postal(s, other),
                            f"{s!r} 同时是 {owner} 和 {other} 的合法邮编 —— "
                            f"pull_tasks 的分组前提破了，那里要显式加一层 "
                            f"marketplace 排序键")


# ==========================================================================
# 6) 币种不从符号推
# ==========================================================================
class CurrencyIsNotInferredFromSymbol(unittest.TestCase):

    def test_both_sites_share_the_dollar_sign(self):
        """这条是整个币种设计的前提，写出来免得有人"优化"成按符号推。"""
        self.assertIn("$", M.get("amazon.com").price_symbols)
        self.assertIn("$", M.get("amazon.ca").price_symbols)
        self.assertNotEqual(M.get("amazon.com").currency,
                            M.get("amazon.ca").currency)

    def test_a_bare_dollar_string_is_a_local_price_on_both(self):
        for mid in ("amazon.com", "amazon.ca"):
            with self.subTest(marketplace=mid):
                self.assertTrue(M.has_local_price_symbol("$24.99", mid))

    def test_expected_currency_comes_from_the_site(self):
        self.assertEqual(M.expected_currency("amazon.com"), "USD")
        self.assertEqual(M.expected_currency("amazon.ca"), "CAD")
        self.assertEqual(M.expected_currency(None), "USD")

    def test_foreign_markers_flag_a_wrong_locale_page(self):
        for mid in ("amazon.com", "amazon.ca"):
            with self.subTest(marketplace=mid):
                self.assertTrue(M.looks_foreign("价格 €24,99", mid))
                self.assertTrue(M.looks_foreign("£19.99", mid))
                self.assertFalse(M.looks_foreign("$24.99", mid),
                                 "$ 在两个站点都是本地符号，不能当成串区信号")

    def test_dollar_is_never_a_foreign_marker(self):
        """反向哨兵：把 $ 加进 foreign_currency_markers 会让每条价格都被标记。"""
        for mid in M.all_ids():
            with self.subTest(marketplace=mid):
                self.assertNotIn("$", M.get(mid).foreign_currency_markers)


# ==========================================================================
# 7) 与库结构的接线
# ==========================================================================
class WiredIntoStorage(unittest.TestCase):

    def test_marketplace_is_an_asin_data_column(self):
        """它是 asin_data 的真列，不是 `_` 元数据 —— 两者的界线是承重的。"""
        self.assertIn("marketplace", ASIN_DATA_FIELDS)

    def test_event_marketplaces_derive_from_the_registry(self):
        """事件流的值域与注册表同源。

        手写两份的后果：往注册表加站点之后事件流悄悄把它纠正成 amazon.com，
        于是 asin_data 里是 amazon.ca、事件流里是 amazon.com，而这两张表
        正是要靠 marketplace join 起来的。
        """
        from common.pgdb.schema import (EVENT_MARKETPLACES,
                                        EVENT_DEFAULT_MARKETPLACE)
        self.assertEqual(tuple(EVENT_MARKETPLACES), M.all_ids())
        self.assertEqual(EVENT_DEFAULT_MARKETPLACE, M.DEFAULT_MARKETPLACE)

    def test_event_check_sql_quotes_every_value(self):
        from common.pgdb.schema import _EVENT_MARKETPLACES_SQL
        for mid in M.all_ids():
            with self.subTest(marketplace=mid):
                self.assertIn(f"'{mid}'", _EVENT_MARKETPLACES_SQL)

    def test_marketplace_is_not_in_the_content_hash(self):
        """它是**采集参数**，不是商品属性（与 site / zip_code 同一条口径）。

        算进 content_hash 会让同一个商品在两个站点产出不同的内容指纹，
        而内容指纹的用途是"目录层内容变没变"，与从哪个站点采的无关。
        """
        from common.core.asindata import _HASH_FIELDS
        self.assertNotIn("marketplace", _HASH_FIELDS)

    def test_slow_hash_excludes_it_too(self):
        from common.slowhash import SLOW_HASH_FIELDS
        self.assertNotIn("marketplace", SLOW_HASH_FIELDS)


# ==========================================================================
# 8) 审计补漏：第一轮改造漏掉、第二轮才补上的那几处
# ==========================================================================
class ThingsTheFirstPassMissed(unittest.TestCase):
    """第一轮把「站点」贯通了主干，但漏了几条支路。

    这一组每条对应一个**实际会产生错数据**的遗漏，写成用例是为了它们不会
    在下次重构时悄悄退回去。共同特征：漏掉时**不报错**，数据看着正常。
    """

    def setUp(self):
        from worker.parser import AmazonParser
        self.p = AmazonParser()

    def _page(self, site_word, currency, shown):
        return (
            '<html><head><script type="application/ld+json">'
            '{"@type":"Product","name":"W","offers":{"@type":"Offer",'
            f'"price":"24.99","priceCurrency":"{currency}",'
            '"availability":"http://schema.org/InStock"}}'
            '</script></head><body>'
            '<span id="productTitle">Widget</span>'
            f'<div id="corePrice_feature_div"><span class="a-offscreen">{shown}</span></div>'
            f'<div id="merchant-info">Ships from {site_word} Sold by {site_word}</div>'
            '</body></html>')

    def test_product_url_points_at_the_right_site(self):
        """写死 amazon.com 的话，加拿大商品的链接指向**另一个国家**的商品页。

        而同一个 ASIN 在美国站多半也存在，所以点进去能打开、看不出是错的。
        """
        ca = self.p.parse_product(self._page("Amazon.ca", "CAD", "$24.99"),
                                  "B0URLTEST1", "K1V 7P8", "amazon.ca")
        self.assertEqual(ca["product_url"], "https://www.amazon.ca/dp/B0URLTEST1")
        us = self.p.parse_product(self._page("Amazon.com", "USD", "$19.99"),
                                  "B0URLTEST1", "10001")
        self.assertEqual(us["product_url"], "https://www.amazon.com/dp/B0URLTEST1",
                         "美国站必须逐字节不变")

    def test_first_party_seller_is_detected_on_each_site(self):
        """自营文案里的域名跟着站点变：加拿大站是 "Sold by Amazon.ca"。

        写死 amazon.com 不会让解析失败，而是**静默走错分支** —— 掉进
        merchantID 兜底，seller_id/seller_name（两个导出列）变成别的值。
        """
        ca = self.p.parse_product(self._page("Amazon.ca", "CAD", "$24.99"),
                                  "B0SELLCA01", "K1V 7P8", "amazon.ca")
        self.assertEqual((ca["seller_id"], ca["seller_name"]), ("AMAZON", "Amazon.ca"))
        us = self.p.parse_product(self._page("Amazon.com", "USD", "$19.99"),
                                  "B0SELLUS01", "10001")
        self.assertEqual((us["seller_id"], us["seller_name"]), ("AMAZON", "Amazon.com"))

    def test_a_ca_page_parsed_as_us_does_not_claim_amazon_first_party(self):
        """反向哨兵：站点传错时宁可认不出，也不要认错。"""
        r = self.p.parse_product(self._page("Amazon.ca", "CAD", "$24.99"),
                                 "B0MIXED0001", "10001", "amazon.com")
        self.assertNotEqual(r["seller_id"], "AMAZON")

    def test_jsonld_rejects_an_offer_in_another_currency(self):
        """JSON-LD 的 priceCurrency 是唯一能看见真实币种代码的地方。

        ⚠ 这条用例**只能**测 JSON-LD 这一条路，所以页面里故意不放 CSS 价格。
          第一版写成"整页都有价格"然后断言美国站不收 CAD —— 那是错的，
          而且错得有意义：CSS 路径拿到的是一个 ``$24.99`` 字符串，
          美加两站长得一模一样，**它永远分辨不出币种**。
          真正的保障不是"解析器能认出来"，而是"站点决定去哪抓、决定币种"
          —— 也就是本文件其余那些用例守的东西。这条只守最后一道明关。
        """
        jsonld_only = (
            '<html><head><script type="application/ld+json">'
            '{"@type":"Product","name":"W","offers":{"@type":"Offer",'
            '"price":"24.99","priceCurrency":"CAD",'
            '"availability":"http://schema.org/InStock"}}'
            '</script></head><body><span id="productTitle">Widget</span>'
            '</body></html>')
        us = self.p.parse_product(jsonld_only, "B0CURTEST1", "10001", "amazon.com")
        self.assertEqual(us["current_price"], "N/A",
                         "标着 CAD 的 offer 不该被美国站收下")
        ca = self.p.parse_product(jsonld_only, "B0CURTEST1", "K1V 7P8", "amazon.ca")
        self.assertEqual(ca["current_price"], "$24.99",
                         "同一条 CAD offer 在加拿大站必须被收下")

    def test_screenshot_base_href_follows_the_site(self):
        """截图子进程拿不到站点，所以 base 必须由 engine 在写盘前注入。"""
        from worker.engine import Worker
        html = "<html><head><meta charset='utf-8'></head><body>x</body></html>"
        self.assertIn('<base href="https://www.amazon.ca/">',
                      Worker._inject_base_href(html, "amazon.ca"))
        self.assertIn('<base href="https://www.amazon.com/">',
                      Worker._inject_base_href(html, None))

    def test_screenshot_base_href_is_not_injected_twice(self):
        """判据必须与 worker/screenshot.py 那条一致，否则会注入两个 <base>。"""
        from worker.engine import Worker
        once = "<html><head><base href=\"https://www.amazon.ca/\"></head></html>"
        self.assertEqual(Worker._inject_base_href(once, "amazon.com"), once)

    def test_no_hardcoded_amazon_com_url_left_in_worker_code(self):
        """代码级哨兵：worker/ 的**代码**里不许再出现写死的 amazon.com URL。

        用 AST 而不是文本扫描：第一版写成 ``line.split("#")[0]``，
        把 docstring 里的说明文字也算成了代码，于是 4 条纯注释被报成违规。
        哨兵误报比没有哨兵更糟 —— 它会训练人去忽略它。

        这里只看**真正会被执行**的字符串字面量（排除模块/类/函数的 docstring），
        且只查 ``https://www.amazon.com`` 这种 URL 形态。
        唯一的白名单是 ``worker/screenshot.py`` 的 ``<base>`` 兜底：
        那个子进程只从磁盘读 HTML、拿不到站点，正常路径由
        ``engine._inject_base_href`` 按站点注入，它只接住改造前遗留的老文件
        （那些必然是美国站的）。
        """
        import ast
        import os

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        allowed = {("screenshot.py",)}          # 见 docstring
        offenders = []

        for fn in sorted(os.listdir(os.path.join(root, "worker"))):
            if not fn.endswith(".py"):
                continue
            path = os.path.join(root, "worker", fn)
            tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)

            # 收集全部 docstring 节点，后面跳过它们
            docstrings = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef,
                                     ast.FunctionDef, ast.AsyncFunctionDef)):
                    body = getattr(node, "body", None)
                    if (body and isinstance(body[0], ast.Expr)
                            and isinstance(body[0].value, ast.Constant)
                            and isinstance(body[0].value.value, str)):
                        docstrings.add(id(body[0].value))

            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant):
                    continue
                if not isinstance(node.value, str) or id(node) in docstrings:
                    continue
                if "https://www.amazon.com" not in node.value:
                    continue
                if (fn,) in allowed:
                    continue
                offenders.append(
                    f"worker/{fn}:{node.lineno}: {node.value[:70]!r}")

        self.assertEqual(offenders, [],
                         "worker 代码里还有写死的 amazon.com URL：\n  "
                         + "\n  ".join(offenders))


if __name__ == "__main__":
    unittest.main()
