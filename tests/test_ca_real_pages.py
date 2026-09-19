"""拿**真实**的加拿大站商品页守住 F-012 实测暴露的那 5 个字段缺陷。

------------------------------------------------------------------------
为什么这份文件必须用真实页面
------------------------------------------------------------------------
``tests/test_marketplace.py`` 里那组 ``FieldBugsFoundByRealCollection`` 用的是
**构造的** HTML —— 它守的是"修好之后别退回去"，够用。

但那组用例**发现不了**这些缺陷，而且差点让我以为已经修好了：

    第一轮我构造了一张带 ``#merchant-info`` 的"加拿大站页面"，解析器
    正确识别出了 ("AMAZON", "Amazon.ca")，于是我以为卖家那条修好了。
    真实页面拿来一跑才发现：**那个容器根本不存在**。2026 版 buybox 用的是
    ``#merchantInfoFeature_feature_div``，而且自营文案是
    "Shipper / Seller Amazon.ca" —— 既不是 "Sold by" 也不是 "Ships from"。

构造的 HTML 只能测"我以为页面长什么样"。这份文件测的是"页面真的长什么样"。

------------------------------------------------------------------------
夹具会过期，这是**设计如此**
------------------------------------------------------------------------
Amazon 随时改版。这份文件红了**不一定**是代码坏了 —— 先抓一张新页面用
``tools/diag_parse.py`` 跑一遍，确认是页面结构变了还是解析器退化了。

夹具的来历、裁剪规则、以及**已知不覆盖 image_urls** 这一条，
见 ``tests/fixtures/ca/README.md``。
"""
from __future__ import annotations

import gzip
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from worker.parser import AmazonParser          # noqa: E402

_FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fixtures", "ca")
#: 采集这两张页面时用的邮编。写死是有意的：``_zip_verify`` 要靠它才判得出
#: ``confirmed``，随便换一个就变成 ``mismatch``，那条断言就失去意义了。
_ZIP = "K1V 7P8"


def _parse(asin: str) -> dict:
    path = os.path.join(_FIXTURE_DIR, f"{asin}.html.gz")
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return AmazonParser().parse_product(f.read(), asin, _ZIP, "amazon.ca")


class GildanFirstPartyPage(unittest.TestCase):
    """B09S6W6H5B —— Amazon.ca **自营**，detailBullets 版详情表。"""

    @classmethod
    def setUpClass(cls):
        cls.r = _parse("B09S6W6H5B")

    def test_first_party_seller_is_detected(self):
        """实测症状：自营卖家 Amazon.ca 漏采（seller_id/name 都是 N/A）。

        根因有两层，**都得修才行**：
          1. 解析器找的三个容器（#merchant-info / #tabular-buybox /
             #offerDisplay_feature_div）在这张页面上**一个都不存在**；
          2. 就算找对了容器，自营文案是 "Shipper / Seller Amazon.ca" ——
             而判据枚举的是 "Sold by" / "Ships from"。

        所以新版容器 #merchantInfoFeature_feature_div 用的是**域名判据**
        而不是短语判据：那个容器只装卖家，域名出现即自营，枚举不完文案。
        """
        self.assertEqual((self.r["seller_id"], self.r["seller_name"]),
                         ("AMAZON", "Amazon.ca"))

    def test_zip_observed_and_verified(self):
        """实测症状：邮编观测值为空，而页面上明明有。

        glow 的内容是 ``K1V 7P8&zwnj;`` —— 尾巴挂着一个零宽非连接符实体。
        加上原来用的是 5 位数字正则，两个原因叠在一起。
        """
        self.assertEqual(self.r["_zip_observed"], _ZIP)
        self.assertEqual(self.r["_zip_verify"], "confirmed")

    def test_bullets_are_six_not_twelve(self):
        """实测症状：六条卖点采成十二条（#feature-bullets 里有展开区副本）。"""
        bullets = self.r["bullet_points"].split("\n")
        self.assertEqual(len(bullets), 6)
        self.assertEqual(len(set(bullets)), 6, "不许有重复")

    def test_parcel_dimensions_land_in_package_not_item(self):
        """实测症状：包装尺寸误存为商品尺寸、包装重量漏采。

        加拿大站的标签是 **"Parcel Dimensions"**，不是 "Package Dimensions"。
        原判据只认 ``package``，于是整行落进了 item_*。
        值形如 ``2.54 x 2.54 x 2.54 cm; 453.59 g`` —— 尺寸和重量在同一行，
        分号后面那半原来被直接扔掉。
        """
        self.assertEqual(self.r["package_dimensions"], "2.54 x 2.54 x 2.54 cm")
        self.assertEqual(self.r["package_weight"], "453.59 g")
        self.assertEqual(self.r["item_dimensions"], "N/A",
                         "这一行说的是包装，不该出现在商品尺寸里")

    def test_best_sellers_rank_keeps_the_top_level_rank(self):
        """实测症状：大类排名漏采。

        BSR 那一行是嵌套结构，取 spans[i+1] 只拿到最里层的子类排名（#3），
        大类那条（#105 in Clothing）整个丢了。另外无分隔符拼接会产出
        "#3 inMen's T-Shirts"（in 和类目名粘在一起）。
        """
        bsr = self.r["best_sellers_rank"]
        self.assertIn("#105 in Clothing, Shoes & Accessories", bsr, "大类排名")
        self.assertIn("#3 in Men's T-Shirts", bsr, "子类排名 + 空格")
        self.assertNotIn("See Top 100", bsr, "那是导航链接，不是排名")

    def test_currency_and_url_follow_the_site(self):
        self.assertEqual(self.r["current_price"], "$32.67")
        self.assertEqual(self.r["product_url"],
                         "https://www.amazon.ca/dp/B09S6W6H5B")


class YatineyThirdPartyPage(unittest.TestCase):
    """B0F673BSBL —— **第三方卖家** + Amazon 发货，tech-spec 表格版详情。"""

    @classmethod
    def setUpClass(cls):
        cls.r = _parse("B0F673BSBL")

    def test_third_party_seller_is_detected(self):
        """反向哨兵：自营判定不能把三方卖家吞掉。

        这张页面的 #fulfillerInfoFeature_feature_div 写着 "Ships from Amazon"
        —— 如果自营判据扫的是整个 offer-display-features，这里就会被误判成
        自营。所以域名判据**只对"只装卖家"的那个容器**生效。
        """
        self.assertEqual((self.r["seller_id"], self.r["seller_name"]),
                         ("A33H6DRE4ECNRF", "YATINEY"))

    def test_part_number_survives_a_label_without_spaces(self):
        """实测症状：料号 DN01UDBBY2 漏采。

        页面上的标签是 ``ManufacturerPartNumber`` —— **一个空格都没有**。
        而判据是 ``'part number' in k_lower``，恒为假。
        同一张页面上 ``Model Number`` 是带空格的，所以 model_number 取到了
        —— 一半对一半错，最难发现的那种。
        """
        self.assertEqual(self.r["part_number"], "DN01UDBBY2")

    def test_model_number_is_copied_verbatim_even_when_amazon_pollutes_it(self):
        """``model_number`` 是 ``DN01UDBBY2 Computer Desk`` —— 页面上就这么写的。

        看着像脏数据，但它**不是我们的 bug**：Amazon 那一行的原文就是
        "Model Number: DN01UDBBY2 Computer Desk"。如实照搬是对的，
        想要干净的料号就读 part_number（上面那条）。
        写成用例是为了挡住"顺手清洗一下"的冲动 —— 那会开始编造数据。
        """
        self.assertEqual(self.r["model_number"], "DN01UDBBY2 Computer Desk")

    def test_product_dimensions_stay_in_item_fields(self):
        """反向哨兵：加了 parcel 同义词之后，**商品**尺寸不能被带偏。"""
        self.assertEqual(self.r["item_dimensions"], "41.9 x 80 x 83.1 centimetres")
        self.assertEqual(self.r["item_weight"], "7 kg")
        self.assertEqual(self.r["package_dimensions"], "N/A")

    def test_best_sellers_rank_is_clean(self):
        bsr = self.r["best_sellers_rank"]
        self.assertEqual(bsr, "#21,305 in Home #48 in Desks & Workstations")

    def test_zip_observed_and_verified(self):
        self.assertEqual(self.r["_zip_observed"], _ZIP)
        self.assertEqual(self.r["_zip_verify"], "confirmed")


class BothEnginesAgree(unittest.TestCase):
    """两条解析引擎（selectolax / lxml）对真实页面必须给出同样的答案。

    仓库里已有 ``EngineParity`` 在构造样本上比对，这里补的是**真实页面**：
    引擎差异往往只在真实 DOM 的复杂嵌套上才暴露出来。
    """

    #: 只比对本次改动碰过的字段。全字段比对会被两条引擎既有的、与本次无关的
    #: 差异淹没（长描述的空白处理等），那种噪音会让这条用例被忽略。
    FIELDS = ("seller_id", "seller_name", "best_sellers_rank",
              "package_dimensions", "package_weight",
              "item_dimensions", "item_weight",
              "model_number", "part_number", "bullet_points")

    def test_parity_on_the_real_pages(self):
        import worker.parser as P
        for asin in ("B09S6W6H5B", "B0F673BSBL"):
            path = os.path.join(_FIXTURE_DIR, f"{asin}.html.gz")
            with gzip.open(path, "rt", encoding="utf-8") as f:
                html = f.read()
            p = AmazonParser()
            spec = P._spec_or_default(None)
            from common.core import marketplace as M
            spec = M.get("amazon.ca")

            slx = p._parse_with_selectolax(
                html, asin, _ZIP, p._default_result(asin, _ZIP, spec), {}, spec)
            lxm = p._parse_with_lxml(
                html, asin, _ZIP, p._default_result(asin, _ZIP, spec), {}, spec)
            for field in self.FIELDS:
                with self.subTest(asin=asin, field=field):
                    self.assertEqual(slx.get(field), lxm.get(field),
                                     f"{asin} 的 {field} 两条引擎不一致")


if __name__ == "__main__":
    unittest.main()
