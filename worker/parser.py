"""
Amazon ASIN 采集系统 v4 - 页面解析模块
v3 增强：
- 多层 fallback 解析路径（JSON-LD → CSS → JS脚本数据 → meta/hidden）
- 非标准页面兼容性提升
- 解析失败时保留调试信息
"""
import re
import json
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple

# P4-1：邮编观测值只有**一份**真源 —— worker/ziputil.py 的 glow-ingress-line2 正则。
# 这里刻意复用它的模块常量而不是抄一份：抄一份就有两个会各自漂移的正则，而
# 「观测到的邮编」与「邮编是否生效」必须永远看同一个挂件，否则会出现
# zip_verify=confirmed 但 zip_effective_in_html 判 False 这种自相矛盾的记录。
from worker.ziputil import _GLOW_LINE2_RE as _GLOW_INGRESS_LINE2_RE

# P4.2：**worker -> common 的新依赖**（本文件此前只 import worker.ziputil）。
# 打包上没问题 —— worker/engine.py、session.py、proxy.py、metrics.py、adaptive.py
# 今天已经 `from common import config`，worker 侧运行环境本来就带着 common/。
# common/core/idents.py 只依赖标准库 `re`，不碰 aiosqlite / asyncpg，
# 所以这条新边**不会**把数据库驱动拖进 worker 进程。
# （import 它会连带执行 common/core/__init__.py，把 retry / lockmeter / asindata /
#  dbtables / coerce 五个兄弟模块也加载进来；实测那五个只依赖标准库与
#  已经是 worker 依赖的 common.config，sys.modules 里不出现任何数据库驱动。）
#
# 收的是 `_ASIN_RE`：本文件与 server/app.py 原先各有一份逐字节相同的
# `re.compile(r'^B[0-9A-Z]{9}$')`。**只换来源，不换值、不换逻辑**。
# 局部的 `ASIN_RE = r'B[0-9A-Z]{9}'`（_extract_page_asin 里那个无锚点裸片段）
# 是另一回事，**不搬**，理由写在 common/core/idents.py 的 docstring 里。
from common.core.idents import ASIN_RE as _ASIN_RE
from common.core.textclean import clean_deep as _clean_deep

# ==========================================================================
# P4-7：crawl_time 的线格式
# ==========================================================================
# 历史值：``datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')``
# —— 东八区的墙上时间，且 ``%z`` 被**刻意丢掉**。消费侧拿到的是无标记的 CST，
# 与它自己的 UTC 混排就是系统性 8 小时错位（审计需求 4）。
#
# 现值：RFC3339 的 UTC ``Z`` 形式。用户已确认 erpAPI 可接受（计划 §Phase 4 表）。
#
# 为什么是 ``T`` + ``Z`` 而不是 ``2026-08-05 10:00:00+00:00``：
# 后者会被「按老格式切片再 strptime」的消费者**静默**解析成功
# （``s[:19]`` 恰好是合法的老格式），然后被重新贴上 +08:00 —— 错 8 小时且无声。
# 前者的 ``s[:19]`` 是 ``2026-08-05T10:00:00``，``%Y-%m-%d %H:%M:%S`` 必然抛
# ValueError，消费者走它自己的兜底分支并留下计数。**同样是没改造的消费者，
# 这个格式让它响，那个格式让它错。** 见下方 P4-7 消费者清单。
_CRAWL_TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _utc_now_str() -> str:
    """当前时刻的 RFC3339 UTC 字符串（秒精度）。"""
    return datetime.now(timezone.utc).strftime(_CRAWL_TIME_FMT)


# ==========================================================================
# P4-2：完整性位图（契约 docs/sync_contract.md §6.4，位定义已对外公布，勿改）
# ==========================================================================
# 判据是 **HTML 区块是否存在**，不是解析出来的值是否非空。二者不等价：
# 「区块在但里面是空的」和「区块被整块删掉」解析值完全一样（都是 ""/"N/A"），
# 而后者正是软降级页的特征。category 只有面包屑一个数据源，好页→降级页→好页
# 于是就是两次哈希翻转、两次误复审 —— §6.5 那道合取门要拦的就是这个。
#
# P4.5：**四个位的数值不再定义在这里**，唯一真源是 common/core/completeness.py
# （schema.py 的 EVENT_COMPLETENESS_* 与 sync.py 的 COMPLETENESS_* 也都从那里来）。
# 本文件对外可见的名字**一个不变**：tests/test_parser_quality.py 与
# tests/pgdb/test_phase4_fields.py 都按下面这四个别名 import。
# ⚠ 只换来源，**不换任何数值、不换任何逻辑**：解析产出逐字节不变（C3 / D-27），
#   已实测（固定输入过解析器，改前改后 sha256 相同）。
# common/core/completeness.py 零依赖（连标准库都不 import），
# 不会把 aiosqlite / asyncpg 拖进 worker 进程 —— 与上面 _ASIN_RE 那条边同理。
from common.core.completeness import (  # noqa: E402  —— 位常量的唯一真源
    BREADCRUMB as COMPLETENESS_BREADCRUMB,   # bit0 面包屑区块存在
    DETAIL as COMPLETENESS_DETAIL_TABLE,     # bit1 详情表存在
    IMAGE as COMPLETENESS_MAIN_IMAGE,        # bit2 主图集存在
    MEASURED as COMPLETENESS_MEASURED,       # bit3 这次采集**真的测量过**上面三项
)

#: 面包屑区块。**只认 `_parse_categories` 真正会去读的那一个 id**：位的意义是
#: 「category 这个信号的唯一数据源在不在」，多认一个别名就会出现「位说在、
#: 解析器却什么都读不到」的假阳性，比不测更糟。
_BLOCK_IDS_BREADCRUMB = ("wayfinding-breadcrumbs_feature_div",)

#: 详情表：`_slx_parse_all_details` / `_parse_all_details` 能取到值的所有容器。
_BLOCK_IDS_DETAIL = (
    "productDetails_techSpec_section_1",
    "productDetails_detailBullets_sections1",
    "productDetails_feature_div",
    "detailBullets_feature_div",
    "technicalSpecifications_section_1",
    "productOverview_feature_div",
    "prodDetails",
)

#: 主图集：`_parse_images` 的取值容器 + 图集挂件本身。
_BLOCK_IDS_IMAGE = (
    "imgTagWrapperId",
    "imageBlock_feature_div",
    "altImages",
    "landingImage",
    "main-image-container",
    "booksImageBlock_feature_div",
)

# ==========================================================================
# P4-1：zip_verify 的封闭集（契约 §表 `zip_verify`）
# ==========================================================================
ZIP_VERIFY_CONFIRMED = "confirmed"      # 页面 glow-line2 的邮编 == 请求邮编
ZIP_VERIFY_MISMATCH = "mismatch"        # 页面给了邮编，但**不是**请求的那个
ZIP_VERIFY_ASSUMED = "assumed"          # 商品页解析成功，但页面没挂 glow-line2
ZIP_VERIFY_UNVERIFIED = "unverified"    # 根本没测（空页 / 拦截 / 解析失败）

#: 5 位邮编。两侧的零宽断言防止把 ZIP+4 的后 4 位或长数字串的一段当成邮编。
_ZIP5_RE = re.compile(r"(?<!\d)(\d{5})(?!\d)")

# ==========================================================================
# P4-9：parse_engine 取值
# ==========================================================================
ENGINE_SELECTOLAX = "selectolax"
ENGINE_LXML = "lxml"

# ==========================================================================
# P4-8：`site` 的值域 —— **本次有意不动**，理由记在这里
# ==========================================================================
# 现状是三处不一致：parser 写 ``"US"``（下方 _default_result）、SQLite 列默认
# ``'amazon.com'``（common/database.py:513）、PG 列默认 ``'amazon.com'``
# （common/pgdb/schema.py:155）、dataclass 默认 ``'amazon.com'``
# （common/models.py:69）。实际每一行都是 ``"US"`` —— 因为 parser 总会写。
#
# 不动的理由：
#   1. `site` 是**导出列**（common/config.py:229 HEADER_MAP "站点"），改值就是改
#      已交付给 erpAPI 的导出内容。而 crawl_time 的改动有用户明确确认，`site` 没有。
#   2. 它承载的那个信号已经**另有权威来源**：事件流的 `marketplace` 在 D-26/§2.3
#      被钉死成封闭集 ``{'amazon.com'}``，并且 relay 明确写着「绝不透传 parser 的
#      site」（common/pgdb/relay.py:281、schema.py:428）。沃尔玛侧读的是
#      `marketplace`，不是 `site`。
#   3. 改它不会修好任何一条误复审：`site` 在 SLOW_HASH_FIELDS 之外
#      （common/slowhash.py:133 "采集参数，不是商品属性"），值稳定与否与哈希无关。
#   4. 一次性回填会改写全部历史行，而 `asin_data` 每 ASIN 只有一行、没有版本，
#      回填之后**无法回滚**到"这行当年记的是什么"。
# 也就是说：收益 = 消掉一个没有消费者的三处不一致；代价 = 动一列已交付的导出数据
# 且不可逆。建议留到 Phase 5 并行验证期，与 `asin_data` 的其它列语义收紧一起做，
# 届时新旧两套系统同时在跑，可以对照。详细论证见交付说明。

# 优先 selectolax，fallback 到 lxml
_USE_SELECTOLAX = False
try:
    from selectolax.parser import HTMLParser as SlxParser
    _USE_SELECTOLAX = True
except ImportError:
    SlxParser = None

try:
    from lxml import html as lxml_html
    from lxml import etree
except ImportError:
    lxml_html = None
    etree = None

logger = logging.getLogger(__name__)

if _USE_SELECTOLAX:
    logger.info("解析引擎: selectolax (Lexbor)")
else:
    logger.info("解析引擎: lxml (fallback)")


# 配送日期匹配（Tomorrow/Today 或 "Month Day"）。抽成模块级编译常量，
# selectolax / lxml 两条解析路径共用同一份逻辑，避免重复维护。
_DELIVERY_DATE_RE = re.compile(
    r'(Tomorrow|Today|'
    r'Jan(?:uary)?\.?\s+\d+|Feb(?:ruary)?\.?\s+\d+|Mar(?:ch)?\.?\s+\d+|'
    r'Apr(?:il)?\.?\s+\d+|May\.?\s+\d+|Jun(?:e)?\.?\s+\d+|'
    r'Jul(?:y)?\.?\s+\d+|Aug(?:ust)?\.?\s+\d+|Sep(?:tember)?\.?\s+\d+|'
    r'Oct(?:ober)?\.?\s+\d+|Nov(?:ember)?\.?\s+\d+|Dec(?:ember)?\.?\s+\d+)',
    re.IGNORECASE,
)


def _slx_iter_descendants(node):
    """前序遍历 ``node`` 的**全部后代元素**（不含 node 自身），严格限制在子树内。

    为什么必须自己写：selectolax 的两个遍历 API 都不是 lxml ``Element.iter()`` 的语义。

    ``Node.traverse()`` —— 文档原话是 "Iterate over all child and next nodes starting
    from the current level"，**next 节点**指容器之后的兄弟节点，即它会越过子树边界继续
    往下扫整篇文档。实测（selectolax 0.4.11）：

        <div id="productDescription"><p>REAL DESCRIPTION TEXT HERE ok</p></div>
        <div id="price"><span class="a-offscreen">$549.99</span></div>
        <div id="availability"><span>In Stock</span></div>

        container.traverse() 依次产出：
            div  'REAL DESCRIPTION TEXT HERE ok'
            p    'REAL DESCRIPTION TEXT HERE ok'
            div  '$549.99'        <- 已经在容器外
            span '$549.99'        <- 已经在容器外
            div  'In Stock'       <- 已经在容器外
            span 'In Stock'       <- 已经在容器外

    ``Node.iter()`` —— "Iterate over nodes on the current level"，只产出**直接子节点**，
    不递归。而 lxml 的 ``.iter()`` 是「自身 + 全部后代」。两者名字一样、语义不同，
    这正是 selectolax 路径与 lxml 路径解析结果分叉的第二个来源。

    所以用 ``Node.iter()``（只要直接子节点，天然不越界）自己做前序 DFS。
    ``Node.css()`` / ``Node.css_first()`` 实测是子树受限的，不在此列。
    """
    stack = list(node.iter())
    stack.reverse()
    while stack:
        cur = stack.pop()
        yield cur
        kids = list(cur.iter())
        if kids:
            kids.reverse()
            stack.extend(kids)


def _slx_iter_subtree(node):
    """selectolax 版的 lxml ``Element.iter()``：先产出 node 自身，再按文档顺序产出后代。"""
    yield node
    yield from _slx_iter_descendants(node)


# ── 叶子文本：``<br>`` 必须产生换行 ──────────────────────────────────────────
# 两个引擎原本各自用「把子树里的文本首尾相接」取叶子文本
# （selectolax ``node.text(deep=True)`` / lxml ``"".join(node.xpath('.//text()'))``），
# 而 ``<br>`` 自身不含文本，于是它两侧的文字被直接粘在一起。真机记录实测（B09TT9X92Q
# 的 A+ 描述）四处：
#     fight corrosion.<br>The durable   -> "fight corrosion.The durable"
#     lightweight alloy<br>that does    -> "lightweight alloythat does"
#     a push<br>and pull mechanism      -> "a pushand pull mechanism"
#     Vintage Pull<br>Drawer Dresser    -> "Vintage PullDrawer Dresser"
#
# 影响不止可读性：``long_description ∈ SLOW_HASH_FIELDS``，而 slowhash 会折叠空白
# —— 粘住的词折不开，等于把两个词永久变成一个新词喂进哈希与导出。
#
# 两个引擎各一份实现、语义必须一致：解析器被重写成引擎无关之后，两条路径分叉过
# 一次（四个字段在 lxml 路径上结转旧值），所以这里配了对等性用例
# （tests/test_long_description.py::BrSeparatorParity）。
def _slx_leaf_text(node) -> str:
    """selectolax：叶子节点的全部文本，``<br>`` 记作 ``\n``。"""
    out = []

    def walk(n):
        for c in n.iter(include_text=True):
            tag = c.tag
            if tag == "br":
                out.append("\n")
            elif tag == "-text":
                out.append(c.text(deep=False) or "")
            else:
                walk(c)

    walk(node)
    return "".join(out)


def _lxml_leaf_text(node) -> str:
    """lxml：同上。``<br>`` 的文字挂在它的 ``tail`` 上，所以 tail 必须收。"""
    out = []

    def walk(el, is_root):
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag == "br":
            out.append("\n")
        elif el.text:
            out.append(el.text)
        for child in el:
            walk(child, False)
        if not is_root and el.tail:
            out.append(el.tail)

    walk(node, True)
    return "".join(out)


class AmazonParser:
    """Amazon 商品页面解析器"""

    # 垃圾文字黑名单（五点描述过滤用）
    BULLET_BLACKLIST = [
        "go to your orders", "start the return", "free shipping option", "drop off",
        "leave!", "return this item", "money back", "customer service",
        "full refund", "eligible for return",
    ]

    def parse_product(self, html_text: str, asin: str, zip_code: str = "10001") -> Dict[str, Any]:
        """
        解析 Amazon 商品页面
        返回包含所有字段的字典
        即使某些字段提取失败也不会崩溃
        """
        result = self._default_result(asin, zip_code)

        if not html_text:
            result["title"] = "[页面为空]"
            return result

        # JSON-LD 优先提取：从结构化数据获取核心字段
        jsonld = self._extract_jsonld(html_text)

        if _USE_SELECTOLAX:
            parsed = self._parse_with_selectolax(html_text, asin, zip_code, result, jsonld)
        else:
            parsed = self._parse_with_lxml(html_text, asin, zip_code, result, jsonld)

        # 统一在**出口**剔除不可见控制字符（U+200E 等），两个引擎共用这一处。
        #
        # 为什么在这里而不是各个提取点：文本提取的 `.strip()` 散落在几十处，
        # 逐个改必漏，而且新加一个字段就又漏一个。出口只有这两条 return，
        # 覆盖面是完备的。
        #
        # 为什么不在 slowhash.normalize_text 里洗：那里只作用于**哈希输入**，
        # 不改存下来的值 —— 洗了哈希稳了，下游拿到的仍然是脏数据，而下游按
        # 品牌 join 正是最需要它干净的地方。详见 common/core/textclean.py。
        return _clean_deep(parsed)

    # ==================== selectolax 解析路径 ====================

    def _parse_with_selectolax(self, html_text: str, asin: str, zip_code: str, result: Dict, jsonld: Dict) -> Dict:
        """使用 selectolax 解析"""
        # P4-9：先记引擎再解析 —— 连「解析器自己炸了」的记录也要能归因到引擎。
        result["_parse_engine"] = ENGINE_SELECTOLAX
        try:
            tree = SlxParser(html_text)
        except Exception as e:
            logger.error(f"HTML 解析失败: {e}")
            result["title"] = "[HTML解析失败]"
            return result

        # 检测反爬拦截
        block_status = self._check_block(html_text, None)
        if block_status:
            result["title"] = block_status
            return result

        # P4-1 / P4-2：质量信号。放在拦截判定**之后** —— 验证码页上的
        # completeness 必须是 0（未测量），而不是「三个区块都缺」。
        self._apply_quality_signals(result, tree, html_text, zip_code)

        # variant 偏移检测：提取页面当前 active ASIN 供上层校验
        # 多属性产品偶发 default variant 偏移，写到 result["_page_asin"]
        # 不在此处直接拒绝，由 worker._process_task 比对后处理
        result["_page_asin"] = self._extract_page_asin(tree, html_text)

        # v3: 提取 JS 脚本数据（额外 fallback 层）
        sp_data = self._extract_sp_data(html_text)

        # 全页预扫描 — 提取 Product Information 表格
        page_details = self._slx_parse_all_details(tree, html_text)

        # 逐字段提取：多层 fallback
        result["title"] = jsonld.get("title") or self._slx_parse_title(tree)
        # P4-1：`zip_code` 恒为**请求**邮编（_default_result 已经写好）。
        # 观测值在 result["_zip_observed"]，判定在 result["_zip_verify"]。

        # 商品可售状态检测
        is_unavailable = self._slx_check_unavailable(tree)
        is_see_price_in_cart = self._slx_check_see_price_in_cart(tree)
        is_no_featured_offer = self._slx_check_no_featured_offer(tree, html_text)

        # 品牌（v3 增强: JSON-LD → 表格 → CSS → meta）
        result["brand"] = self._slx_parse_brand_enhanced(tree, jsonld, page_details, sp_data)

        # 价格 & 库存 & 配送
        if is_no_featured_offer:
            result["current_price"] = "No Featured Offer"
            result["buybox_price"] = "N/A"
            result["original_price"] = "N/A"
            result["buybox_shipping"] = "N/A"
            result["is_fba"] = "N/A"
            result["stock_status"] = "No Featured Offer"
            result["stock_count"] = "0"
            result["delivery_date"] = "N/A"
            result["delivery_time"] = "N/A"
        elif is_unavailable:
            result["current_price"] = "不可售"
            result["buybox_price"] = "N/A"
            result["original_price"] = "N/A"
            result["buybox_shipping"] = "N/A"
            result["is_fba"] = "N/A"
            result["stock_status"] = "Currently unavailable"
            result["stock_count"] = "0"
            result["delivery_date"] = "N/A"
            result["delivery_time"] = "N/A"
        elif is_see_price_in_cart:
            result["current_price"] = "See price in cart"
            result["buybox_price"] = "N/A"
            result["original_price"] = self._slx_parse_original_price(tree)
            result["buybox_shipping"] = self._slx_parse_buybox_shipping(tree, None)
            result["is_fba"] = self._slx_parse_fulfillment(tree, html_text)
            avail_node = tree.css_first('div#availability span')
            stock_text = avail_node.text(strip=True) if avail_node else ""
            result["stock_status"] = stock_text if stock_text else "In Stock"
            result["stock_count"] = str(self._slx_parse_stock_count(result["stock_status"], tree))
            d_date, d_time = self._slx_parse_delivery(tree)
            result["delivery_date"] = d_date
            result["delivery_time"] = d_time
        else:
            # v3 增强价格解析
            result["current_price"] = self._slx_parse_price_enhanced(tree, jsonld, sp_data)
            bb = self._slx_parse_buybox_price(tree)
            result["buybox_price"] = bb if bb else result["current_price"]
            result["original_price"] = self._slx_parse_original_price(tree)
            result["buybox_shipping"] = self._slx_parse_buybox_shipping(tree, result["current_price"])
            result["is_fba"] = self._slx_parse_fulfillment(tree, html_text)
            # v3 增强库存解析
            result["stock_status"] = self._slx_parse_stock_enhanced(tree, jsonld, sp_data, html_text)
            if result["current_price"] == "N/A" and result["buybox_price"] == "N/A" and result["stock_status"] == "N/A":
                result["stock_status"] = "N/A"
            result["stock_count"] = str(self._slx_parse_stock_count(result["stock_status"], tree))
            d_date, d_time = self._slx_parse_delivery(tree)
            result["delivery_date"] = d_date
            result["delivery_time"] = d_time

        # 是否定制
        result["is_customized"] = self._slx_parse_customization(tree)

        # 详情参数
        result["model_number"] = page_details.get("model_number", "N/A")
        result["part_number"] = page_details.get("part_number", "N/A")
        result["country_of_origin"] = page_details.get("country_of_origin", "N/A")
        result["best_sellers_rank"] = page_details.get("best_sellers_rank", "N/A")
        result["manufacturer"] = page_details.get("manufacturer", "N/A")
        result["product_type"] = page_details.get("product_type", "N/A")
        result["first_available_date"] = page_details.get("date_first_available", "N/A")
        result["item_dimensions"] = page_details.get("item_dimensions", "N/A")
        result["item_weight"] = page_details.get("item_weight", "N/A")
        result["package_dimensions"] = page_details.get("package_dimensions", "N/A")
        result["package_weight"] = page_details.get("package_weight", "N/A")

        # 五点描述
        result["bullet_points"] = self._slx_parse_bullet_points(tree)

        # 长描述（v3 增强: CSS → HTML → JSON-LD → JS）
        result["long_description"] = self._slx_parse_description_enhanced(tree, html_text, jsonld, sp_data)

        # 图片（CSS 优先，JSON-LD 兜底）
        css_imgs = self._slx_parse_images(tree, html_text)
        result["image_urls"] = css_imgs if css_imgs else jsonld.get("image_urls", "")

        # UPC（EAN 已下线：amazon.com 基本不暴露，实测 100% 空，保留空值且不导出）
        result["upc_list"] = self._slx_parse_upc(tree, html_text, page_details)
        result["ean_list"] = ""

        # 父体 ASIN / 变体（twister：本 ASIN 自己的属性 + 精确同族列表）
        result["parent_asin"] = self._parse_parent_asin(html_text, asin)
        attrs, family = self._parse_twister(html_text, asin)
        result["variant_attributes"] = attrs
        result["variation_asins"] = family if family is not None \
            else self._parse_variation_asins(html_text, asin, result["parent_asin"])

        # 类目
        result["root_category_id"], result["category_ids"], result["category_tree"] = \
            self._slx_parse_categories(tree)

        # 评分 + 评论数（JSON-LD 优先，DOM 兜底）
        result["rating"] = self._parse_rating(tree, jsonld)
        result["review_count"] = self._parse_review_count(tree, jsonld)

        # 卖家店铺 ID + 名（buybox 卖家档案链接）
        seller_id, seller_name = self._parse_seller(tree, html_text)
        result["seller_id"] = seller_id
        result["seller_name"] = seller_name

        return result

    # ---- 标题：主标题 + Title Differentiators ----
    #
    # 2026-08 Amazon 改版：商品标题被**拆成两个元素**。以前 `#productTitle`
    # 一个元素装完整标题，现在它只装前半段，后半段挪进了一个兄弟节点：
    #
    #     <div id="titleSection">
    #       <h1 id="title">
    #         <span id="productTitle">River Dream Waffle No Hook Shower Curtain
    #                                 with Liner,Graphite Grey,71x74</span>
    #       </h1>
    #       <!-- Title Differentiators for Desktop -->
    #       <div class="a-section dp-title-differentiators">
    #         <span class="a-size-base a-color-secondary">Snap-in Liner,Heavy Duty,
    #               Hotel Grade,Mesh Top Window,...</span>
    #       </div>
    #     </div>
    #
    # 于是 title 静默变短，**而且看起来完全正常**——不是空、不是 N/A、不报错，
    # 只是少了后半段。这类改版最难发现，所以下面每一条判断都写清楚依据。
    #
    # 分隔符是 `" | "`，不是我们自己编的：同一张页面里 Amazon 自己也把两段拼
    # 成一个串，用的就是它（实测 B0F3JKMZLN，2026-08-19）——
    #
    #   <title>Amazon.com: 710PCS M3 Screw Assortment Kit, ... Screw Kits
    #          | 10.9 Grade Alloy Steel ... for 3D Printing : Industrial & Scientific</title>
    #   <input id="productTitle" value="710PCS ... Screw Kits | 10.9 Grade ... Printing"/>
    #
    # 与用户报的历史值也逐字对上：改版前采到的就是 `...,71x74 | Snap-in Liner,...`。
    #
    # ⚠ **不能改读那两个"已经拼好"的源**，虽然看着更省事：
    #   * `<title>` / `meta[name="title"]` 带 `Amazon.com: ` 前缀和 ` : 类目` 后缀，
    #     得再剥一层，而那层文案随语言/类目变化；
    #   * 那个 hidden input 属于"举报低价"弹窗表单，**不是每页都有**
    #     （实测 B07FZ8S74R Echo Dot 就没有）。
    # 所以真源是可见 DOM 的两段，我们自己按 Amazon 的分隔符拼。
    #
    # ⚠ differentiators **是可选的**（Echo Dot 那页整块都不存在）。没有时
    # 必须原样返回主标题，**不许留下孤零零的分隔符**——`"标题 | "` 这种尾巴
    # 会进 slow_hash，把一次解析瑕疵变成一次"慢变字段变了"的假变更。

    #: Amazon 自己拼这两段用的分隔符。
    TITLE_PART_SEPARATOR = " | "

    #: differentiators 容器。**限定在 `#titleSection` 之内**：这个 class 名
    #: 足够通用，页面别处（推荐位、变体卡片）出现同名容器时不该被当成本商品的
    #: 标题后半段。实测本商品页只出现一次，限定是防御性的。
    _TITLE_DIFF_CSS = "#titleSection .dp-title-differentiators"
    _TITLE_DIFF_XPATH = ('//*[@id="titleSection"]'
                         '//*[contains(@class, "dp-title-differentiators")]')

    @classmethod
    def _join_title_parts(cls, main: Optional[str],
                          differentiator: Optional[str]) -> str:
        """(主标题, 后半段) -> 完整标题。**两个解析引擎共用这一个实现。**

        共用不是风格问题：selectolax 与 lxml 两条路径必须对同一张页面给出
        **同一个** title，否则同一个商品会因为走了哪条引擎而产生不同的
        slow_hash，看起来像"标题变了"。本仓库 .agent/MIGRATION_STATUS.md §5.5
        记的 V2/V4 两次事故都是"抄了一份该共用的东西然后各自演化"。
        """
        m = (main or "").strip()
        d = (differentiator or "").strip()
        if not m:
            return d or "N/A"
        if not d:
            return m
        # 后半段已经被前半段包住（Amazon 偶尔两处都给全量）时不要拼两遍
        if d in m:
            return m
        return m + cls.TITLE_PART_SEPARATOR + d

    # ---- selectolax 辅助 ----

    def _slx_text(self, tree, selectors: List[str]) -> Optional[str]:
        """从多个 CSS 选择器中尝试获取文本"""
        for sel in selectors:
            try:
                node = tree.css_first(sel)
                if node:
                    text = node.text(strip=True)
                    if text:
                        return text
            except Exception:
                continue
        return None

    def _slx_all_text(self, tree, selector: str) -> List[str]:
        """获取 CSS 选择器匹配的所有节点文本"""
        try:
            nodes = tree.css(selector)
            return [n.text(strip=True) for n in nodes if n.text(strip=True)]
        except Exception:
            return []

    def _slx_parse_title(self, tree) -> str:
        try:
            meta = tree.css_first('meta[name="title"]')
            visible = self._slx_text(tree, [
                # `span#productTitle` 必须带上标签名：同一张页面里还有一个
                # `<input id="productTitle">`（举报低价弹窗），裸 `#productTitle`
                # 可能选中它。
                'span#productTitle',
                'h1 > span',
            ])
            if visible:
                # 2026-08 改版：后半段在兄弟节点里，见 `_join_title_parts` 上方注释
                diff = self._slx_text(tree, [self._TITLE_DIFF_CSS])
                return self._join_title_parts(visible, diff)
            if meta:
                content = meta.attributes.get('content', '')
                return content.strip() if content else "N/A"
            return "N/A"
        except Exception:
            return "N/A"

    # `_slx_parse_zip_code` 已删除（P4-1）。它读的是 `span#glow-ingress-line1`，
    # 而邮编在 **line2**（worker/ziputil.py:11-12），所以它近乎恒返回 None；它唯一
    # 的作用是让 `zip_code` 列在极少数情况下变成观测值、混进请求值里。
    # 观测邮编现在走 `_parse_zip_observed`（复用 ziputil 的 line2 正则）。

    def _slx_parse_brand(self, tree) -> str:
        try:
            # 1) 文本形式的 byline / brand 链接（最常见）
            for sel in ['a#bylineInfo', 'a#brand', 'span#bylineInfo',
                        '#bylineInfo_feature_div a', '#brand-snapshot-link']:
                node = tree.css_first(sel)
                if node:
                    brand_text = node.text(strip=True)
                    brand = re.sub(
                        r'Visit the |Brand:\s*| Store$', '', brand_text,
                        flags=re.IGNORECASE
                    ).strip()
                    if brand and len(brand) <= 80:
                        return brand

            # 2) Premium 品牌以 logo 图片形式呈现时（byline 文本为空）
            #    <img id="brandLogoHiResByline" alt="Yaheetech" title="Visit the Yaheetech Store">
            for sel in ['img#brandLogoHiResByline', 'img.premium-logoByLine-brand-logo',
                        'img#brand-logo']:
                node = tree.css_first(sel)
                if node:
                    alt = (node.attributes.get('alt') or '').strip()
                    title_attr = (node.attributes.get('title') or '').strip()
                    for candidate in (alt, title_attr):
                        if not candidate:
                            continue
                        brand = re.sub(
                            r'Visit the |Brand:\s*| Store$', '', candidate,
                            flags=re.IGNORECASE
                        ).strip()
                        if brand and len(brand) <= 80:
                            return brand

            # 3) "Visit the X Store" 链接（覆盖 brand 仅以图片呈现的兜底）
            for sel in ['a#visitStoreDesktopUrl', '#bylineInfo_feature_div a[href*="/stores/"]']:
                node = tree.css_first(sel)
                if node:
                    link_text = node.text(strip=True)
                    brand = re.sub(
                        r'Visit the |Brand:\s*| Store$', '', link_text,
                        flags=re.IGNORECASE
                    ).strip()
                    if brand and len(brand) <= 80:
                        return brand
                    # 退而求其次从 href 解析 /stores/{Brand}/page/...
                    href = (node.attributes.get('href') or '').strip()
                    m = re.search(r'/stores/([^/]+)/', href)
                    if m:
                        brand = m.group(1).replace('+', ' ').strip()
                        # URL 里可能是百分号转义的品牌名
                        try:
                            from urllib.parse import unquote
                            brand = unquote(brand)
                        except Exception:
                            pass
                        if brand and len(brand) <= 80 and brand.lower() not in ("page", "search"):
                            return brand
        except Exception:
            pass
        return "N/A"

    def _slx_parse_current_price(self, tree) -> str:
        try:
            # 方法1: 价格容器内的 a-offscreen（限定到价格区域，避免全页匹配）
            price_selectors = [
                '#corePrice_feature_div span.a-offscreen',
                '#corePriceDisplay_desktop_feature_div span.a-offscreen',
                '#corePrice_desktop span.a-offscreen',
                '#price span.a-offscreen',
                '#priceblock_ourprice',
                '#priceblock_dealprice',
                '#priceToPay span.a-offscreen',
                '#apex_offerDisplay_desktop span.a-offscreen',
            ]
            for sel in price_selectors:
                node = tree.css_first(sel)
                if node:
                    p = node.text(strip=True)
                    if p and "$" in p:
                        return p
                    # v3: 检测非 USD 价格（CNY, EUR, GBP 等）— 标记但仍提取
                    if p and re.search(r'(?:CNY|EUR|GBP|JPY|¥|€|£)\s*[\d,]+\.?\d*', p):
                        return f"[非USD]{p}"
            # 方法2: 拆分整数+小数（限定到价格容器）
            for container in ['#corePrice_feature_div', '#corePriceDisplay_desktop_feature_div',
                              '#corePrice_desktop', '#price', '#apex_offerDisplay_desktop']:
                prefix = f'{container} ' if container else ''
                whole_node = tree.css_first(f'{prefix}span.a-price-whole')
                frac_node = tree.css_first(f'{prefix}span.a-price-fraction')
                if whole_node and frac_node:
                    whole = whole_node.text(strip=True)
                    frac = frac_node.text(strip=True)
                    if whole and frac:
                        return f"${whole.replace('.', '')}.{frac}"
        except Exception:
            pass
        return "N/A"

    def _slx_parse_buybox_price(self, tree) -> Optional[str]:
        selectors = [
            '#tabular-buybox span.a-offscreen',
            '#buybox span.a-offscreen',
            '#newBuyBoxPrice',
            '#priceToPay span.a-offscreen',
            '#price_inside_buybox',
            '#corePrice_feature_div span.a-offscreen',
            '#corePriceDisplay_desktop_feature_div span.a-offscreen',
            '#apex_offerDisplay_desktop span.a-offscreen',
        ]
        for sel in selectors:
            try:
                nodes = tree.css(sel)
                for node in nodes:
                    text = node.text(strip=True)
                    if text and re.search(r'\d', text):
                        return text
            except Exception:
                continue
        return None

    def _slx_parse_original_price(self, tree) -> str:
        try:
            for container in ['#corePrice_feature_div', '#corePriceDisplay_desktop_feature_div', '#price']:
                node = tree.css_first(f'{container} span[data-a-strike="true"] span.a-offscreen')
                if node:
                    return node.text(strip=True)
        except Exception:
            pass
        return "N/A"

    def _slx_parse_buybox_shipping(self, tree, current_price: Optional[str]) -> str:
        try:
            delivery_nodes = tree.css('div#deliveryBlockMessage *')
            delivery_block = " ".join(n.text(strip=True) for n in delivery_nodes if n.text(strip=True))

            # Prime 免费配送
            if "prime members get free delivery" in delivery_block.lower():
                return "FREE"

            # 满额免邮
            free_over = re.search(r'free delivery.*?on orders over \$(\d+(?:\.\d+)?)', delivery_block, re.IGNORECASE)
            if free_over:
                threshold = float(free_over.group(1))
                if current_price and current_price not in ['N/A', 'See price in cart', '不可售']:
                    price_match = re.search(r'\$?([\d,]+\.?\d*)', current_price)
                    if price_match:
                        price_val = float(price_match.group(1).replace(',', ''))
                        if price_val >= threshold:
                            return "FREE"
                return "N/A"

            # data-csa-c-delivery-price 属性
            dp_node = tree.css_first('span[data-csa-c-delivery-price]')
            if dp_node:
                dp = dp_node.attributes.get('data-csa-c-delivery-price', '')
                if dp:
                    return dp.strip()

            # 兜底
            shipping = self._slx_text(tree, [
                'div#mir-layout-loaded-comparison-row-2 span',
            ])
            return shipping if shipping else "FREE"
        except Exception:
            return "N/A"

    def _slx_check_unavailable(self, tree) -> bool:
        try:
            nodes = tree.css('div#availability *')
            full = " ".join(n.text(strip=True) for n in nodes if n.text(strip=True)).lower()
            return "currently unavailable" in full
        except Exception:
            return False

    def _slx_check_no_featured_offer(self, tree, html_text: str) -> bool:
        """v3: 检测 'No featured offers available' 页面"""
        try:
            # 方法1: 页面文本检测
            if "no featured offer" in html_text.lower()[:50000]:
                # 确认没有 add-to-cart 按钮
                atc = tree.css_first('#add-to-cart-button')
                if not atc:
                    return True
            # 方法2: 检查 "See All Buying Options" 按钮存在但无价格区域
            see_all = any("see all buying" in (n.text(strip=True) or "").lower()
                         for n in tree.css('a, span')
                         if n.text(strip=True))
            if see_all:
                has_price = tree.css_first('#corePrice_feature_div') or tree.css_first('#corePriceDisplay_desktop_feature_div')
                if not has_price:
                    return True
        except Exception:
            pass
        return False

    def _slx_check_see_price_in_cart(self, tree) -> bool:
        try:
            # 检查链接文本
            for node in tree.css('a'):
                text = node.text(strip=True)
                if text and "see price in cart" in text.lower():
                    return True
            # 检查 popover
            if tree.css_first('#a-popover-map_help_pop_'):
                return True
            # 检查表格
            for node in tree.css('table.a-lineitem *'):
                text = node.text(strip=True)
                if text and "see price in cart" in text.lower():
                    return True
        except Exception:
            pass
        return False

    def _slx_parse_fulfillment(self, tree, html_text: str) -> str:
        try:
            # 1. 结构化标签
            rows = tree.css('div#tabular-buybox tr')
            for row in rows:
                # 检查是否包含 "Ships from"
                row_text = row.text(strip=True).lower()
                if "ships from" in row_text or "shipper" in row_text:
                    # 获取值节点
                    value_nodes = row.css('span.a-color-base')
                    for vn in value_nodes:
                        value = vn.text(strip=True)
                        if value:
                            if "amazon" not in value.lower():
                                return "FBM"
                            else:
                                return "FBA"

            # 2. 文本兜底
            blob_parts = []
            for sel in ['div#rightCol *', 'div#tabular-buybox *']:
                for n in tree.css(sel):
                    t = n.text(strip=True)
                    if t:
                        blob_parts.append(t)
            blob = " ".join(blob_parts).lower()

            match = re.search(r'(ships from|shipper / seller)\s*[:\s]*([a-z0-9\s]+)', blob)
            if match and "amazon" not in match.group(2).strip():
                return "FBM"
            if "fulfilled by amazon" in blob or "prime" in blob or "amazon.com" in blob:
                return "FBA"
        except Exception:
            pass
        return "FBA"

    def _slx_parse_stock_count(self, stock_status: str, tree) -> int:
        try:
            stock_lower = stock_status.lower() if stock_status else ""
            if "only" in stock_lower:
                count = re.search(r'(\d+)', stock_status)
                return int(count.group(1)) if count else 999
            if "out of stock" in stock_lower or "unavailable" in stock_lower:
                return 0
            # 下拉菜单
            options = tree.css('select[name="quantity"] option')
            if not options:
                options = tree.css('select#quantity option')
            if options:
                values = []
                for opt in options:
                    val = opt.attributes.get('value', '')
                    if val and val.strip().isdigit():
                        values.append(int(val.strip()))
                if values:
                    return max(values)
        except Exception:
            pass
        return 999

    def _delivery_raw_to_days(self, raw: str, today) -> Optional[int]:
        """把 "Tomorrow"/"Today"/"July 20" 之类的日期文本换算成距今天数；无法解析返回 None。"""
        low = raw.lower()
        if "today" in low:
            return 0
        if "tomorrow" in low:
            return 1
        try:
            import dateparser
            dt = dateparser.parse(raw, settings={'PREFER_DATES_FROM': 'future'})
            if not dt:
                return None
            if dt.month < today.month and today.month == 12:
                dt = dt.replace(year=today.year + 1)
            elif dt.year < today.year:
                dt = dt.replace(year=today.year)
            return (dt.date() - today.date()).days
        except Exception:
            return None

    def _pick_delivery(self, candidates: List[str]) -> Tuple[str, str]:
        """从配送候选文本里选**页面上能看到的最快**配送日期/时长（含 Prime 会员专享）。

        candidates: 配送文本列表——data-csa-c-delivery-time 属性值（权威），或回退
        用的整段配送文本。从中用正则抽出所有日期，取距今天数最小者。
        """
        today = datetime.now()
        best_raw, best_days = None, 999
        for text in candidates:
            for m in _DELIVERY_DATE_RE.finditer(text):
                raw = m.group(1)
                days = self._delivery_raw_to_days(raw, today)
                if days is None or days < 0:
                    continue
                if days < best_days:
                    best_raw, best_days = raw, days
        if best_raw is not None:
            return best_raw, str(best_days)
        return "N/A", "N/A"

    def _slx_parse_delivery(self, tree) -> Tuple[str, str]:
        try:
            # 优先直接读 data-csa-c-delivery-time 属性（权威、格式稳定 "Weekday, Month Day"），
            # 比抓子元素文本鲁棒：有的模板日期是属性所在 span 的直接文本节点，
            # 旧的 '[attr] *' 选择器抓不到任何子元素 → 返回 N/A。
            candidates: List[str] = []
            for n in tree.css('[data-csa-c-delivery-time]'):
                raw = (n.attributes.get('data-csa-c-delivery-time') or "").strip()
                if raw:
                    candidates.append(raw)

            # 回退：页面不带该属性时，用整段配送文本 + 正则（覆盖老/异形模板）。
            if not candidates:
                texts = []
                for n in tree.css('[data-csa-c-delivery-time] *, div.delivery-message *, '
                                  'div#deliveryBlockMessage *'):
                    t = n.text(strip=True)
                    if t:
                        texts.append(t)
                if texts:
                    candidates.append(" ".join(texts))

            if candidates:
                return self._pick_delivery(candidates)
        except Exception:
            pass
        return "N/A", "N/A"

    def _slx_parse_customization(self, tree) -> str:
        """检测商品是否需要定制（Yes/No）。
        老实现遍历整页 ~5 万 DOM 节点，每个节点都调 .text() 取子树文本，O(N²) 复杂度，
        99% 非定制商品每次都走完整遍历，单条解析烧 1-3s CPU。
        新实现：只在 buybox 等定制按钮可能出现的容器里取一次文本做子串查找。
        """
        try:
            # 只扫定制按钮可能出现的容器（按命中频率排序）
            for sel in [
                'div#customization-feature',          # 定制功能区
                'div#customizationFeature_div',
                'div#customization_feature_div',
                'div#rightCol',                       # buybox 右栏
                'div#tabular-buybox',                 # 现代 buybox 布局
                'div#partialStateBuyBox',
                'div#desktop_buybox',
                'div#buybox',
            ]:
                container = tree.css_first(sel)
                if not container:
                    continue
                # 取一次容器内全部文本，做 O(L) 子串查找（L ≈ 几 KB）
                blob = container.text(strip=True).lower()
                if not blob:
                    continue
                if ("customize now" in blob
                        or "needs to be customized" in blob
                        or "customization required" in blob):
                    return "Yes"
        except Exception:
            pass
        return "No"

    def _slx_parse_bullet_points(self, tree) -> str:
        try:
            bullets = []
            nodes = tree.css('div#feature-bullets ul > li span.a-list-item')
            if not nodes:
                nodes = tree.css('div.a-expander-content ul > li span.a-list-item')
            for n in nodes:
                txt = n.text(strip=True)
                if txt:
                    bullets.append(txt)

            clean = []
            for b in bullets:
                if len(b) < 2:
                    continue
                if any(spam in b.lower() for spam in self.BULLET_BLACKLIST):
                    continue
                clean.append(b)
            return "\n".join(clean)
        except Exception:
            return ""

    def _slx_parse_long_description(self, tree, html_text: str) -> str:
        try:
            parts = []
            # A+ 内容 — 尝试多种容器选择器
            _TEXT_TAGS = {'p', 'span', 'div', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'td', 'th'}
            container = None
            for sel in ['div.aplus', '#aplus', '#aplusProductDescription', '#productDescription']:
                container = tree.css_first(sel)
                if container:
                    break

            if container:
                # 必须用 _slx_iter_subtree 而不是 container.traverse()：traverse() 不受
                # 子树约束，会把容器**之后**的价格/库存/评分/BSR/CDN 图片 URL 一并吸进
                # long_description（详见 _slx_iter_descendants 的实测输出）。
                # 后果是 long_description ∈ SLOW_HASH_FIELDS，slow_hash 每次采集都变，
                # 且 long_description 是导出字段，脏值一路流到 erpAPI。
                for node in _slx_iter_subtree(container):
                    tag = node.tag
                    if tag == 'img':
                        src = node.attributes.get('src') or node.attributes.get('data-src', '')
                        if src and "pixel" not in src and "transparent" not in src:
                            parts.append(f"\n[Image: {src.strip()}]\n")
                    elif tag in _TEXT_TAGS:
                        # 只取叶子级文本节点，避免父子重复。
                        # 这里也必须是**整棵子树**的后代：lxml 版用的是 lxml 语义的
                        # node.iter()（自身+后代），而 selectolax 的 node.iter() 只有
                        # 直接子节点。用后者会把 <div><a><span>text</span></a></div>
                        # 判成叶子，div 与 span 各取一次文本 → 同一段文字重复两遍。
                        # 用 any() 而非 lxml 版的列表推导：语义等价（结果只用于真值判断），
                        # 但命中第一个后代就短路，A+ 大页上省掉整棵子树的重复扫描。
                        has_text_descendant = any(c.tag in _TEXT_TAGS
                                                  for c in _slx_iter_descendants(node))
                        if not has_text_descendant:
                            # 与 lxml 版 _lxml_leaf_text 对齐：整体 strip，而不是
                            # 逐个文本节点 strip（后者会把 "Hello <b>world</b> again"
                            # 拼成 "Helloworldagain"）。<br> 记作换行，见 _slx_leaf_text。
                            text = _slx_leaf_text(node).strip()
                            if text and len(text) > 5:
                                parts.append(text)

            if not parts:
                seo_keywords = ['ai-optimized', 'search submission', 'noindex', 'sponsored']
                for m in re.finditer(r'"description"\s*:\s*"([^"]{20,})"', html_text):
                    text = m.group(1)
                    if any(kw in text.lower() for kw in seo_keywords):
                        continue
                    try:
                        decoded = text.encode('utf-8').decode('unicode_escape')
                        clean = re.sub(r'<[^>]+>', '\n', decoded)
                        return clean.strip()[:4000]
                    except Exception:
                        continue

            return "\n".join(parts)[:10000]
        except Exception:
            return ""

    def _slx_parse_images(self, tree, html_text: str) -> str:
        try:
            img_urls = []
            scripts = tree.css('script')
            for script in scripts:
                text = script.text(strip=True)
                if text and "colorImages" in text:
                    urls = re.findall(r'"hiRes":"(https://[^"]+)"', text) or \
                           re.findall(r'"large":"(https://[^"]+)"', text)
                    # 保序去重：dict.fromkeys 在 Python 3.7+ 保留插入顺序，
                    # 这样首图（亚马逊页面第 1 张）始终排在结果第 1 位
                    img_urls = list(dict.fromkeys(urls))
                    break
            if not img_urls:
                img_node = tree.css_first('div#imgTagWrapperId img')
                if img_node:
                    src = img_node.attributes.get('src', '')
                    if src:
                        img_urls = [src]
            return "\n".join(img_urls)
        except Exception:
            return ""

    def _slx_parse_upc(self, tree, html_text: str, page_details: Dict) -> str:
        try:
            upc_set = set(re.findall(r'"upc":"(\d+)"', html_text))
            if 'upc' in page_details:
                upc_set.add(page_details['upc'])
            upc_set.update(re.findall(r'UPC\s*[:#]?\s*(\d{12})', html_text))
            # P4-5：`",".join(list(set(...)))` 的顺序由 str 的哈希决定，而 CPython
            # 的 str 哈希每进程随机加盐（PYTHONHASHSEED）⇒ 同一份 HTML 在不同
            # worker 进程里得到**不同的字符串**。upc_list 既进哈希（slowhash
            # _LIST_FIELDS 会排序，但那只救哈希）又是沃尔玛侧直接复用的挂牌资产，
            # 所以顺序不稳不只是哈希问题。排序 = 消除这个自由度。
            return ",".join(sorted(upc_set))
        except Exception:
            return ""

    def _slx_parse_categories(self, tree) -> Tuple[str, str, str]:
        try:
            breadcrumb = tree.css('div#wayfinding-breadcrumbs_feature_div a')
            cids = []
            names = []
            for a in breadcrumb:
                href = a.attributes.get('href', '')
                node_match = re.search(r'node=(\d+)', href)
                if node_match:
                    cids.append(node_match.group(1))
                name = a.text(strip=True)
                if name:
                    names.append(name)

            category_ids = ",".join(cids)
            root_id = cids[0] if cids else "N/A"
            category_tree = " > ".join(names)

            return root_id, category_ids, category_tree
        except Exception:
            return "N/A", "", ""

    def _slx_parse_all_details(self, tree, html_text: str) -> Dict[str, str]:
        """selectolax 版全页扫描：提取 Product Information 表格内容"""
        d = {}
        try:
            # 扫描表格行 — 包含两种布局：
            #   A) 经典 Product Information 表格：<th>Key</th><td>Value</td>
            #   B) 现代 Product Overview 表格：<td><span>Key</span></td><td><span>Value</span></td>
            for row in tree.css('tr'):
                try:
                    th = row.css_first('th')
                    tds = row.css('td')
                    if th and tds:
                        k = th.text(strip=True)
                        v = tds[0].text(strip=True)
                        if k and v:
                            self._map_detail(d, k, v)
                    elif len(tds) >= 2:
                        # 两列 td 布局：第一列通常是加粗的 key（a-text-bold）
                        key_span = tds[0].css_first('span.a-text-bold') or tds[0]
                        k = key_span.text(strip=True)
                        v = tds[1].text(strip=True)
                        if k and v and len(k) <= 50:
                            self._map_detail(d, k, v)
                except Exception:
                    continue

            # 扫描列表项
            for s in tree.css('li > span > span.a-text-bold'):
                try:
                    k = s.text(strip=True)
                    # 获取下一个 sibling span
                    parent = s.parent
                    if parent:
                        spans = parent.css('span')
                        for i, sp in enumerate(spans):
                            if sp.text(strip=True) == k and i + 1 < len(spans):
                                v = spans[i + 1].text(strip=True)
                                if k and v:
                                    self._map_detail(d, k.replace(':', ''), v)
                                break
                except Exception:
                    continue

            # product_type 从 JSON 中提取
            gl = re.search(r'"gl_product_group_type":"([^"]+)"', html_text)
            if gl:
                d['product_type'] = gl.group(1)
        except Exception as e:
            logger.warning(f"详情解析异常: {e}")

        return d

    # ==================== lxml 解析路径 (fallback) ====================

    def _parse_with_lxml(self, html_text: str, asin: str, zip_code: str, result: Dict, jsonld: Dict) -> Dict:
        """使用 lxml 解析（fallback）"""
        # P4-9：同 selectolax 路径，先记引擎。
        result["_parse_engine"] = ENGINE_LXML
        try:
            tree = lxml_html.fromstring(html_text)
        except Exception as e:
            logger.error(f"HTML 解析失败: {e}")
            result["title"] = "[HTML解析失败]"
            return result

        # 检测反爬拦截
        block_status = self._check_block(html_text, tree)
        if block_status:
            result["title"] = block_status
            return result

        # P4-1 / P4-2：与 selectolax 路径同一份逻辑、同一个位置。
        self._apply_quality_signals(result, tree, html_text, zip_code)

        # variant 偏移检测（同 selectolax 路径）：从 HTML 提取页面当前 active ASIN
        result["_page_asin"] = self._extract_page_asin(None, html_text)

        # 全页预扫描 — 提取 Product Information 表格
        page_details = self._parse_all_details(tree, html_text)

        # 逐字段提取（JSON-LD 优先，CSS/XPath 补充）
        result["title"] = jsonld.get("title") or self._parse_title(tree)
        # P4-1：同 selectolax 路径，`zip_code` 恒为请求邮编。

        # 商品可售状态检测
        is_unavailable = self._check_unavailable(tree)
        is_see_price_in_cart = self._check_see_price_in_cart(tree)

        # 品牌（JSON-LD > 表格 > XPath）
        result["brand"] = jsonld.get("brand") or page_details.get("brand") or self._parse_brand(tree)

        # 价格 & 库存 & 配送
        if is_unavailable:
            result["current_price"] = "不可售"
            result["buybox_price"] = "N/A"
            result["original_price"] = "N/A"
            result["buybox_shipping"] = "N/A"
            result["is_fba"] = "N/A"
            result["stock_status"] = "Currently unavailable"
            result["stock_count"] = "0"
            result["delivery_date"] = "N/A"
            result["delivery_time"] = "N/A"
        elif is_see_price_in_cart:
            result["current_price"] = "See price in cart"
            result["buybox_price"] = "N/A"
            result["original_price"] = self._parse_original_price(tree)
            result["buybox_shipping"] = self._parse_buybox_shipping(tree, None)
            result["is_fba"] = self._parse_fulfillment(tree, html_text)
            stock_text = self._get_text(tree, ['//div[@id="availability"]/span/text()'])
            result["stock_status"] = stock_text.strip() if stock_text else "In Stock"
            result["stock_count"] = str(self._parse_stock_count(result["stock_status"], tree))
            d_date, d_time = self._parse_delivery(tree)
            result["delivery_date"] = d_date
            result["delivery_time"] = d_time
        else:
            css_price = self._parse_current_price(tree)
            result["current_price"] = css_price if css_price != "N/A" else jsonld.get("current_price", "N/A")
            bb = self._parse_buybox_price(tree)
            result["buybox_price"] = bb if bb else result["current_price"]
            result["original_price"] = self._parse_original_price(tree)
            result["buybox_shipping"] = self._parse_buybox_shipping(tree, result["current_price"])
            result["is_fba"] = self._parse_fulfillment(tree, html_text)
            stock_text = self._get_text(tree, ['//div[@id="availability"]/span/text()'])
            result["stock_status"] = stock_text.strip() if stock_text else jsonld.get("stock_status", "In Stock")
            result["stock_count"] = str(self._parse_stock_count(result["stock_status"], tree))
            d_date, d_time = self._parse_delivery(tree)
            result["delivery_date"] = d_date
            result["delivery_time"] = d_time

        # 是否定制
        result["is_customized"] = self._parse_customization(tree)

        # 详情参数
        result["model_number"] = page_details.get("model_number", "N/A")
        result["part_number"] = page_details.get("part_number", "N/A")
        result["country_of_origin"] = page_details.get("country_of_origin", "N/A")
        result["best_sellers_rank"] = page_details.get("best_sellers_rank", "N/A")
        result["manufacturer"] = page_details.get("manufacturer", "N/A")
        result["product_type"] = page_details.get("product_type", "N/A")
        result["first_available_date"] = page_details.get("date_first_available", "N/A")
        result["item_dimensions"] = page_details.get("item_dimensions", "N/A")
        result["item_weight"] = page_details.get("item_weight", "N/A")
        result["package_dimensions"] = page_details.get("package_dimensions", "N/A")
        result["package_weight"] = page_details.get("package_weight", "N/A")

        # 五点描述
        result["bullet_points"] = self._parse_bullet_points(tree)

        # 长描述（XPath 优先，JSON-LD 兜底）
        css_desc = self._parse_long_description(tree, html_text)
        result["long_description"] = css_desc if css_desc else jsonld.get("_jsonld_description", "")

        # 图片（XPath 优先，JSON-LD 兜底）
        css_imgs = self._parse_images(tree, html_text)
        result["image_urls"] = css_imgs if css_imgs else jsonld.get("image_urls", "")

        # UPC（EAN 已下线：amazon.com 基本不暴露，实测 100% 空，保留空值且不导出）
        result["upc_list"] = self._parse_upc(tree, html_text, page_details)
        result["ean_list"] = ""

        # 父体 ASIN / 变体（twister：本 ASIN 自己的属性 + 精确同族列表）
        result["parent_asin"] = self._parse_parent_asin(html_text, asin)
        attrs, family = self._parse_twister(html_text, asin)
        result["variant_attributes"] = attrs
        result["variation_asins"] = family if family is not None \
            else self._parse_variation_asins(html_text, asin, result["parent_asin"])

        # 类目
        result["root_category_id"], result["category_ids"], result["category_tree"] = \
            self._parse_categories(tree)

        # P4-6：评分 / 评论数 / 卖家 —— 以前这三行**只**存在于 selectolax 路径，
        # lxml 路径连键都不产出，于是 server 跳过写入、行里留着上一次采集的旧值。
        # 现在两条路径调用同一组引擎无关的解析器。
        result["rating"] = self._parse_rating(tree, jsonld)
        result["review_count"] = self._parse_review_count(tree, jsonld)
        seller_id, seller_name = self._parse_seller(tree, html_text)
        result["seller_id"] = seller_id
        result["seller_name"] = seller_name

        return result

    # ==================== 通用辅助方法 ====================

    # ==================== v3 增强: 多层 fallback 方法 ====================

    def _extract_sp_data(self, html_text: str) -> Dict[str, Any]:
        """从 JavaScript SP/twister/AAPI 数据中提取字段（v3 新增 fallback 层）
        Amazon 页面经常在 <script> 中嵌入结构化数据，比 CSS 更稳定
        """
        result = {}
        try:
            # 1. SP API 数据（含价格、库存、配送信息）
            sp_match = re.search(r'"sp_detail_page_ssp_meta"\s*:\s*(\{[^}]+\})', html_text)
            if sp_match:
                try:
                    sp_data = json.loads(sp_match.group(1))
                    if "price" in sp_data:
                        result["_sp_price"] = sp_data["price"]
                except (json.JSONDecodeError, ValueError):
                    pass

            # 2. 从 twisterState 提取价格
            twister_match = re.search(r'"priceAmount":([\d.]+)', html_text)
            if twister_match:
                try:
                    price_val = float(twister_match.group(1))
                    result["_twister_price"] = f"${price_val:.2f}"
                except (ValueError, TypeError):
                    pass

            # 3. 从 storeJSON 提取库存信息
            avail_match = re.search(r'"stockStatus"\s*:\s*"([^"]+)"', html_text)
            if avail_match:
                result["_js_stock_status"] = avail_match.group(1)

            # 4. 从 buyboxJSON 提取价格
            bb_match = re.search(r'"displayPrice"\s*:\s*"([^"]+)"', html_text)
            if bb_match:
                result["_js_display_price"] = bb_match.group(1)

            # 5. 品牌 fallback：meta 标签
            brand_match = re.search(r'<meta\s+name="keywords"\s+content="([^"]+)"', html_text, re.IGNORECASE)
            if brand_match:
                keywords = brand_match.group(1)
                # Amazon 通常把品牌放在 keywords 的第一个位置
                parts = [p.strip() for p in keywords.split(",") if p.strip()]
                if parts and len(parts[0]) <= 60:
                    result["_meta_brand"] = parts[0]

            # 6. 描述 fallback: productDescription_feature_div
            desc_match = re.search(
                r'<div\s+id="productDescription_feature_div"[^>]*>(.*?)</div>',
                html_text, re.DOTALL
            )
            if desc_match:
                desc_html = desc_match.group(1)
                desc_clean = re.sub(r'<[^>]+>', ' ', desc_html).strip()
                desc_clean = re.sub(r'\s+', ' ', desc_clean)
                if len(desc_clean) > 20:
                    result["_html_description"] = desc_clean[:4000]

        except Exception as e:
            logger.debug(f"SP 数据提取异常: {e}")
        return result

    def _slx_parse_brand_enhanced(self, tree, jsonld: dict, page_details: dict, sp_data: dict) -> str:
        """增强品牌解析：JSON-LD → 表格 → CSS → meta keywords"""
        brand = jsonld.get("brand")
        if brand and brand != "N/A":
            return brand

        brand = page_details.get("brand")
        if brand and brand != "N/A":
            return brand

        brand = self._slx_parse_brand(tree)
        if brand and brand != "N/A":
            return brand

        # v3 fallback: meta keywords
        brand = sp_data.get("_meta_brand")
        if brand:
            return brand

        return "N/A"

    # ==================== 评分 + 评论数 ====================

    # ================= P4-6：评分 / 评论数 / 卖家（两条引擎路径共用）=================
    # 这三个解析器原来叫 `_slx_parse_*` 且只被 selectolax 路径调用，lxml 路径上
    # 对应的键**根本不存在** ⇒ server 的 `if val is not None` 跳过 ⇒ 行里留着上次
    # 的旧值配新的 crawl_time（审计 §1.5「四个结转字段」）。改成引擎无关之后，
    # 两条路径调用的是同一份逻辑，不可能再出现「只有一条路径产出」。

    def _parse_rating(self, tree, jsonld: dict) -> str:
        """评分（如 "4.4"）。JSON-LD aggregateRating.ratingValue 优先；DOM 兜底。"""
        # 1) JSON-LD（最稳，浏览器/服务端响应一致）
        try:
            jr = jsonld.get("_rating") if jsonld else None
            if jr:
                jr = str(jr).strip()
                # 标准化："4.4 out of 5 stars" → "4.4"
                m = re.match(r"^([0-9](?:\.[0-9]+)?)", jr)
                if m:
                    return m.group(1)
                return jr
        except Exception:
            pass
        # 2) #acrPopover[title="4.4 out of 5 stars"]
        title_attr = self._uni_first_attr(
            tree, 'span#acrPopover', '//span[@id="acrPopover"]/@title', 'title')
        if title_attr:
            m = re.match(r"^([0-9](?:\.[0-9]+)?)", title_attr)
            if m:
                return m.group(1)
        # 3) #averageCustomerReviews .a-icon-alt
        txt = self._uni_first_text(
            tree, '#averageCustomerReviews .a-icon-alt',
            '//*[@id="averageCustomerReviews"]//*[contains(concat(" ", normalize-space(@class), " "),'
            ' " a-icon-alt ")]')
        if txt:
            m = re.match(r"^([0-9](?:\.[0-9]+)?)", txt)
            if m:
                return m.group(1)
        return "N/A"

    def _parse_review_count(self, tree, jsonld: dict) -> str:
        """评论数（纯数字字符串，如 "323"）。JSON-LD reviewCount 优先；DOM 兜底。"""
        # 1) JSON-LD
        try:
            rc = jsonld.get("_review_count") if jsonld else None
            if rc:
                rc_s = str(rc).strip().replace(",", "")
                if rc_s.isdigit():
                    return rc_s
        except Exception:
            pass
        # 2) #acrCustomerReviewText "1,234 ratings" 或 "(323)"
        txt = self._uni_first_text(
            tree, '#acrCustomerReviewText', '//*[@id="acrCustomerReviewText"]')
        if txt:
            m = re.search(r"[\d,]+", txt)
            if m:
                digits = m.group(0).replace(",", "")
                if digits.isdigit():
                    return digits
        return "N/A"

    # ==================== 卖家店铺 ID + 名 ====================

    def _parse_seller(self, tree, html_text: str = "") -> Tuple[str, str]:
        """返回 (seller_id, seller_name)。
        - 第三方卖家：从 <a id="sellerProfileTriggerId" href="...seller=XXX">Name</a> 提取
        - Amazon 自营：sellerProfileTriggerId 不存在，但页面有 "Sold by Amazon.com" → 返回 ("AMAZON", "Amazon.com")
        - 其他无法识别：返回 ("N/A", "N/A")
        """
        # 1) 标准路径：buybox 卖家档案链接
        name = self._uni_first_text(
            tree, 'a#sellerProfileTriggerId', '//a[@id="sellerProfileTriggerId"]')
        href = self._uni_first_attr(
            tree, 'a#sellerProfileTriggerId',
            '//a[@id="sellerProfileTriggerId"]/@href', 'href')
        if name or href:
            m = re.search(r"seller=([A-Z0-9]+)", href or "")
            seller_id = m.group(1) if m else ""
            if seller_id and name:
                return seller_id, name
            if name:  # 拿到名字但没拿到 ID（极罕见）
                return "N/A", name

        # 2) 备选：任意带 seller= 的链接（href 路径变化时兜底）
        for css, xp in (
            ('#merchant-info a[href*="seller="]',
             '//*[@id="merchant-info"]//a[contains(@href, "seller=")]'),
            ('#tabular-buybox a[href*="seller="]',
             '//*[@id="tabular-buybox"]//a[contains(@href, "seller=")]'),
            ('a[href*="seller="]', '//a[contains(@href, "seller=")]'),
        ):
            href = self._uni_first_attr(tree, css, xp + '/@href', 'href')
            if not href:
                continue
            m = re.search(r"seller=([A-Z0-9]+)", href)
            if not m:
                continue
            name = self._uni_first_text(tree, css, xp) or ""
            if m.group(1):
                return m.group(1), (name or "N/A")

        # 3) Amazon 自营：检测 #merchant-info / #tabular-buybox 文本。
        #    注意 `_uni_first_text` 用的是**带空格分隔**的取文本：老实现用
        #    selectolax 默认的 `text(strip=True)`（无分隔符拼接），真实页面上的
        #    `Sold by <a>Amazon.com</a>` 会被拼成 `Sold byAmazon.com`，这个子串
        #    判断因此**从来没命中过**，自营页恒返回 ("N/A","N/A")。
        for css, xp in (
            ('#merchant-info', '//*[@id="merchant-info"]'),
            ('#tabular-buybox', '//*[@id="tabular-buybox"]'),
            ('#offerDisplay_feature_div', '//*[@id="offerDisplay_feature_div"]'),
        ):
            blob = (self._uni_first_text(tree, css, xp) or "").lower()
            if not blob:
                continue
            if "sold by amazon.com" in blob or "ships from amazon.com" in blob:
                return "AMAZON", "Amazon.com"

        # 4) HTML 文本最后兜底（脚本数据里偶尔有 merchantID）
        try:
            m = re.search(r'"merchantID"\s*:\s*"([A-Z0-9]+)"', html_text or "")
            if m and m.group(1):
                return m.group(1), "N/A"
        except Exception:
            pass

        return "N/A", "N/A"

    def _slx_parse_price_enhanced(self, tree, jsonld: dict, sp_data: dict) -> str:
        """增强价格解析：CSS → JSON-LD → JS脚本数据"""
        css_price = self._slx_parse_current_price(tree)
        if css_price and css_price != "N/A":
            return css_price

        jl_price = jsonld.get("current_price")
        if jl_price and jl_price != "N/A":
            return jl_price

        # v3 fallback: JS 脚本数据
        for key in ["_js_display_price", "_twister_price", "_sp_price"]:
            val = sp_data.get(key)
            if val:
                if "$" not in str(val):
                    try:
                        return f"${float(val):.2f}"
                    except (ValueError, TypeError):
                        continue
                return str(val)

        return "N/A"

    def _slx_parse_stock_enhanced(self, tree, jsonld: dict, sp_data: dict, html_text: str) -> str:
        """增强库存状态解析：CSS → JSON-LD → JS数据 → add-to-cart 按钮状态"""
        avail_node = tree.css_first('div#availability span')
        stock_text = avail_node.text(strip=True) if avail_node else ""
        if stock_text:
            return stock_text

        jl_status = jsonld.get("stock_status")
        if jl_status:
            return jl_status

        js_status = sp_data.get("_js_stock_status")
        if js_status:
            return js_status

        # v3 fallback: add-to-cart 按钮存在则认为有库存
        add_to_cart = tree.css_first('#add-to-cart-button')
        if add_to_cart:
            return "In Stock"

        # 检查 "Add to List" 存在但没有购买按钮
        add_to_list = tree.css_first('#add-to-wishlist-button-submit')
        if add_to_list and not add_to_cart:
            return "Currently unavailable"

        return "N/A"

    def _slx_parse_description_enhanced(self, tree, html_text: str, jsonld: dict, sp_data: dict) -> str:
        """增强长描述解析：CSS → HTML productDescription → JSON-LD → JS数据"""
        css_desc = self._slx_parse_long_description(tree, html_text)
        if css_desc:
            return css_desc

        # v3 fallback: productDescription_feature_div
        html_desc = sp_data.get("_html_description")
        if html_desc:
            return html_desc

        jl_desc = jsonld.get("_jsonld_description", "")
        if jl_desc:
            return jl_desc

        return ""

    def parse_offer_listing(self, html_text: str) -> Optional[Dict[str, Any]]:
        """v3: 解析 /gp/offer-listing/{ASIN} 页面，提取第一个 offer 完整信息
        限定在 #aod-offer / #aod-offer-list 区域内，避免提取到推荐产品的价格
        返回: {'price','shipping','delivery','seller','ships_from','is_fba'} 或 None
        """
        if not html_text or "validateCaptcha" in html_text:
            return None

        try:
            tree = SlxParser(html_text) if _USE_SELECTOLAX else None
            if not tree:
                return None

            # 在 #aod-offer / #aod-offer-list / #aod-pinned-offer 区域内搜索
            # 这些是 Amazon AOD (All Offers Display) 的标准容器
            offer_containers = ['#aod-pinned-offer', '#aod-offer', '#aod-offer-list']

            best_offer = {}

            for container_sel in offer_containers:
                container = tree.css_first(container_sel)
                if not container:
                    continue

                # 价格：先试 a-offscreen，再试 whole+fraction
                price_found = False
                for price_node in container.css('span.a-price'):
                    offscreen = price_node.css_first('span.a-offscreen')
                    if offscreen:
                        p = offscreen.text(strip=True)
                        if p and "$" in p:
                            best_offer['price'] = p
                            price_found = True
                            break

                    whole = price_node.css_first('span.a-price-whole')
                    frac = price_node.css_first('span.a-price-fraction')
                    if whole and frac:
                        w = whole.text(strip=True).replace('.', '')
                        f = frac.text(strip=True)
                        if w and f:
                            best_offer['price'] = f"${w}.{f}"
                            price_found = True
                            break

                if not price_found:
                    continue

                # 运费
                shipping_node = container.css_first('[data-csa-c-delivery-price]')
                if shipping_node:
                    sp = shipping_node.attributes.get('data-csa-c-delivery-price', '')
                    if sp:
                        best_offer['shipping'] = sp.strip()
                    else:
                        sp_text = shipping_node.text(strip=True)
                        # 只取运费部分，不要配送时间
                        sp_match = re.match(r'(FREE|\$[\d.]+)', sp_text)
                        if sp_match:
                            best_offer['shipping'] = sp_match.group(1)
                        elif sp_text:
                            best_offer['shipping'] = sp_text[:20]
                if 'shipping' not in best_offer:
                    delivery_block = container.text(strip=True).lower()
                    if 'free delivery' in delivery_block or 'free shipping' in delivery_block:
                        best_offer['shipping'] = 'FREE'

                # 配送时间
                delivery_node = container.css_first('[data-csa-c-delivery-time]')
                if delivery_node:
                    d_text = delivery_node.text(strip=True)
                    # 提取日期部分（支持单日和范围）
                    date_match = re.search(
                        r'(Tomorrow|Today|'
                        r'(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*,?\s+)?'
                        r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\.?\s+\d+'
                        r'(?:\s*-\s*\d+)?)',
                        d_text, re.IGNORECASE
                    )
                    if date_match:
                        best_offer['delivery'] = date_match.group(0).strip()
                    else:
                        # 清理多余文本
                        clean = re.sub(r'(Details|Or fastest|FREE delivery|\.)', '', d_text).strip()
                        best_offer['delivery'] = clean[:60] if clean else d_text[:60]

                # 卖家
                seller_node = container.css_first('#aod-offer-soldBy a.a-link-normal')
                if not seller_node:
                    seller_node = container.css_first('#aod-offer-soldBy a')
                if seller_node:
                    best_offer['seller'] = seller_node.text(strip=True)

                # Ships from → FBA/FBM 判断
                ships_node = container.css_first('#aod-offer-shipsFrom .a-color-base')
                if ships_node:
                    ships_text = ships_node.text(strip=True)
                    best_offer['ships_from'] = ships_text
                    best_offer['is_fba'] = 'FBA' if 'amazon' in ships_text.lower() else 'FBM'
                else:
                    best_offer['is_fba'] = 'FBM'

                # 找到第一个有效 offer 就返回
                if best_offer.get('price'):
                    return best_offer

            return best_offer if best_offer.get('price') else None

        except Exception as e:
            logger.debug(f"offer-listing 解析异常: {e}")
            return None

    def _default_result(self, asin: str, zip_code: str) -> Dict[str, Any]:
        """创建默认结果字典"""
        return {
            "asin": asin,
            # P4-7：RFC3339 UTC。历史上是裸 UTC+8，见模块头 _CRAWL_TIME_FMT 的说明。
            "crawl_time": _utc_now_str(),
            # P4-8：**有意保持 "US" 不动**，论证见模块头的 “P4-8” 注释块。
            "site": "US",
            # P4-1：这一列的语义收紧为「**请求**邮编」，恒等于 task 传进来的值。
            # 以前是 `_slx_parse_zip_code(tree) or zip_code`，即「能解析出来就用
            # 页面上的」—— 观测值混进请求值，而消费侧的分组键是
            # (asin, marketplace, zip_requested)：一条声称 zip A、价格其实是 zip B
            # 的记录会悄悄把一个商品的价格序列劈成两组。观测值现在单独放在
            # `_zip_observed`，判定放在 `_zip_verify`。
            "zip_code": zip_code,
            "product_url": f"https://www.amazon.com/dp/{asin}",
            # 内部字段（下划线前缀，server 端 _save_result_inner_unlocked
            # 只写 ASIN_DATA_FIELDS 中的字段，不会落库）
            "_page_asin": None,  # variant 偏移检测用
            # P4-1 / P4-2 / P4-9：采集质量信号。默认值 = 「没测」，不是「测了是空」。
            "_zip_observed": None,
            "_zip_verify": ZIP_VERIFY_UNVERIFIED,
            "_completeness": 0,          # 0 == 未测量（契约 §6.4）
            "_parse_engine": None,       # 没有任何引擎跑起来时保持 None
            "title": "N/A",
            "brand": "N/A",
            "product_type": "N/A",
            "manufacturer": "N/A",
            "model_number": "N/A",
            "part_number": "N/A",
            "country_of_origin": "N/A",
            "is_customized": "No",
            "best_sellers_rank": "N/A",
            "original_price": "N/A",
            "current_price": "N/A",
            "buybox_price": "N/A",
            "buybox_shipping": "N/A",
            "is_fba": "N/A",
            "stock_count": "0",
            "stock_status": "N/A",
            "delivery_date": "N/A",
            "delivery_time": "N/A",
            "image_urls": "",
            "bullet_points": "",
            "long_description": "",
            "upc_list": "",
            "ean_list": "",
            "parent_asin": asin,
            "variation_asins": "",
            "variant_attributes": "",
            "root_category_id": "N/A",
            "category_ids": "",
            "category_tree": "",
            "first_available_date": "N/A",
            "package_dimensions": "N/A",
            "package_weight": "N/A",
            "item_dimensions": "N/A",
            "item_weight": "N/A",
            # P4-6：这四个字段以前**只**在 selectolax 路径上被赋值，lxml 路径上
            # 整个键都不存在。server 的写库循环是 `val = data.get(f); if val is not
            # None:`（common/database.py:1906-1912），键不存在 ⇒ 跳过 ⇒ 该行**保留
            # 上一次采集的值**，却配着一个全新的 crawl_time。也就是说 lxml 路径下
            # 「4.4 分 / 1234 条评论 / 卖家 X」可能是三周前的，而记录声称是刚采的。
            # 放进 _default_result 之后，任何路径都必然产出这四个键。
            "rating": "N/A",
            "review_count": "N/A",
            "seller_id": "N/A",
            "seller_name": "N/A",
        }

    # ============ 引擎无关原语（P4-2 / P4-6 两条路径共用同一份逻辑）============

    @staticmethod
    def _is_slx(tree) -> bool:
        """tree 是 selectolax 树还是 lxml 树。

        用**结构探测**而不是模块级 `_USE_SELECTOLAX`：单测必须能在装着
        selectolax 的进程里直接驱动 lxml 路径（生产上 lxml 分支只在 selectolax
        缺失时才跑，靠全局开关的话这条路径永远测不到 —— 它正是 P4-6 那四个
        字段丢失的地方）。
        """
        return tree is not None and hasattr(tree, "css_first")

    def _uni_first_text(self, tree, css: str, xpath: str) -> Optional[str]:
        """两条引擎路径共用的「取第一个匹配节点的文本」。取不到返回 None。

        selectolax 侧显式传 ``separator=" "``：默认的 ``text(strip=True)`` 是
        **逐文本节点** strip 后**无分隔符**拼接，`Sold by <a>Amazon.com</a>` 会变成
        `Sold byAmazon.com`，子串判断全部落空（tests/test_long_description.py
        记的同一个坑，那里的结论也是「以 lxml 语义为准」）。
        """
        if tree is None:
            return None
        try:
            if self._is_slx(tree):
                node = tree.css_first(css)
                if node is None:
                    return None
                return (node.text(separator=" ", strip=True) or "").strip() or None
            nodes = tree.xpath(xpath)
            if not nodes:
                return None
            first = nodes[0]
            if isinstance(first, str):
                return first.strip() or None
            return " ".join(t.strip() for t in first.itertext() if t.strip()) or None
        except Exception:
            return None

    def _uni_first_attr(self, tree, css: str, xpath: str, attr: str) -> Optional[str]:
        """同上，取属性值。``xpath`` 必须**直接选到属性**（形如 ``...//@href``）。"""
        if tree is None:
            return None
        try:
            if self._is_slx(tree):
                node = tree.css_first(css)
                if node is None:
                    return None
                return (node.attributes.get(attr) or "").strip() or None
            for v in tree.xpath(xpath):
                s = str(v).strip()
                if s:
                    return s
            return None
        except Exception:
            return None

    # ==================== P4-2 完整性位图 ====================

    def _has_any_block(self, tree, ids: Tuple[str, ...]) -> bool:
        """这些 id 的**区块**在不在。只看存在性，**不看**里面有没有内容。"""
        if tree is None:
            return False
        for block_id in ids:
            try:
                if self._is_slx(tree):
                    if tree.css_first("#" + block_id) is not None:
                        return True
                elif bool(tree.xpath('boolean(//*[@id="%s"])' % block_id)):
                    return True
            except Exception:
                continue
        return False

    def _measure_completeness(self, tree) -> int:
        """按 HTML 区块存在性算完整性位图；tree 为空（没测）返回 0。

        契约 §6.4：``0`` 的含义是「未测量」，**不是**「三项都缺」。所以只有真的
        走到这里、手上有一棵树，才置 MEASURED 位。
        """
        if tree is None:
            return 0
        bits = COMPLETENESS_MEASURED
        if self._has_any_block(tree, _BLOCK_IDS_BREADCRUMB):
            bits |= COMPLETENESS_BREADCRUMB
        if self._has_any_block(tree, _BLOCK_IDS_DETAIL):
            bits |= COMPLETENESS_DETAIL_TABLE
        if self._has_any_block(tree, _BLOCK_IDS_IMAGE):
            bits |= COMPLETENESS_MAIN_IMAGE
        return bits

    # ==================== P4-1 邮编观测 / 判定 ====================

    @staticmethod
    def _parse_zip_observed(html_text: str) -> Optional[str]:
        """从 glow 配送挂件的**第二行**抽出页面上实际生效的邮编；取不到返回 None。

        `_slx_parse_zip_code` / `_parse_zip_code` 读的是 ``glow-ingress-line1``，
        而那一行是 "Deliver to" / "Hello, select your address" 这类提示语，几乎不
        含邮编 —— 本仓自己的 worker/ziputil.py:11-12 写明邮编在 **line2**
        （"New York 10001" / "Altoona 16602"）。于是那两个函数近乎恒返回 None，
        `or zip_code` 兜底恒生效，`zip_code` 列 100% 是请求值（审计 §需求3c）。

        这里**不做**「取不到就回退成请求值」的兜底：取不到就是 None。理由与
        ziputil.py:26-28 完全一致 —— 把请求值伪装成观测值，会让 zip_verify
        永远是 confirmed，等于把这个信号变成常量。
        """
        if not html_text:
            return None
        try:
            m = _GLOW_INGRESS_LINE2_RE.search(html_text)
            if not m:
                return None
            z = _ZIP5_RE.search(m.group(1) or "")
            return z.group(1) if z else None
        except Exception:
            return None

    @staticmethod
    def _norm_zip(z: Any) -> Optional[str]:
        """邮编归一化到 5 位字符串；无法归一化返回 None。

        ⚠ P4.6：这是仓库里四份邮编归一化之一，**刻意不与另外三份合并**
        （``server/app.py:_normalize_zip``、``common/pgdb/relay.py`` 的
        ``normalize_zip`` / ``normalize_zip_observed``）。本函数是**串内抽取**：
        补零规则不适用时它还会 ``_ZIP5_RE.search(s)`` 从任意串里捞一个 5 位数，
        另外三份都不这么干。完整的取舍写在 ``common/core/zipcode.py`` 的
        docstring 里，那里也是「补零」这一小步的唯一真源。

        本函数的数字分支写的是 ``0 < len(head) <= 5``，比真源多允许 ``== 5``，
        而 ``"12345".zfill(5) == "12345"`` ⇒ **结果完全一致**。没有改写成调用
        真源，是因为那要动 worker 侧的解析逻辑（C3 / D-27），4.6 不做。
        """
        s = str(z).strip() if z is not None else ""
        if not s:
            return None
        head = s.split("-")[0].strip()
        if head.isdigit() and 0 < len(head) <= 5:
            return head.zfill(5)
        m = _ZIP5_RE.search(s)
        return m.group(1) if m else None

    @classmethod
    def _judge_zip(cls, requested: Any, observed: Any) -> str:
        """(请求值, 观测值) -> zip_verify 封闭集之一。"""
        req = cls._norm_zip(requested)
        obs = cls._norm_zip(observed)
        if obs is None:
            # 页面没挂 glow 挂件：只能**假设**切邮编生效了。
            # 绝不在这里返回 confirmed —— 一个错的 confirmed 比一个诚实的
            # assumed 更糟（relay.py:222-225 记的是同一条理由）。
            return ZIP_VERIFY_ASSUMED if req else ZIP_VERIFY_UNVERIFIED
        if req is None:
            return ZIP_VERIFY_UNVERIFIED
        return ZIP_VERIFY_CONFIRMED if obs == req else ZIP_VERIFY_MISMATCH

    def _apply_quality_signals(self, result: Dict, tree, html_text: str,
                               zip_code: str) -> None:
        """把 P4-1 / P4-2 的质量信号写进 result（两条引擎路径共用）。"""
        observed = self._parse_zip_observed(html_text)
        result["_zip_observed"] = observed
        result["_zip_verify"] = self._judge_zip(zip_code, observed)
        result["_completeness"] = self._measure_completeness(tree)

    def _check_block(self, html_text: str, tree) -> Optional[str]:
        """检测反爬拦截，返回拦截类型或 None"""
        if "validateCaptcha" in html_text or "Robot Check" in html_text:
            return "[验证码拦截]"
        # 仅短页面（< 20KB）中出现才算封锁；正常商品页（50KB+）可能包含此邮箱
        if "api-services-support@amazon.com" in html_text and len(html_text) < 20000:
            return "[API封锁]"
        return None

    def _extract_page_asin(self, tree, html_text: str) -> Optional[str]:
        """
        提取页面当前展示的 active variant ASIN。
        多属性产品（如 B0G6KPHQ4G）偶发出现"variant 偏移"：
        - worker 请求 /dp/B0G6KPHQ4G?th=1&psc=1
        - Amazon 因 A/B test / 库存 / cookie 状态等原因返回兄弟 variant 的页面
        - parser 直接读 #productTitle 会拿到错误 variant 的 title
        - 调用方比对返回值 vs task.asin，不一致即"variant_offset"，拒绝写入主表
        信号源（按可靠度排序）：
            1. <input id="ASIN" value="..."> 隐藏字段（最可靠，几乎所有 PDP 都有）
            2. <link rel="canonical" href=".../dp/ASIN">
            3. JS 中 "currentAsin":"ASIN"
        """
        ASIN_RE = r'B[0-9A-Z]{9}'

        # 1. selectolax 直接读 #ASIN 隐藏字段
        if _USE_SELECTOLAX and tree is not None:
            try:
                node = tree.css_first('input#ASIN')
                if node is not None:
                    val = (node.attributes.get('value') or '').strip().upper()
                    if re.fullmatch(ASIN_RE, val):
                        return val
            except Exception:
                pass

        # 2. regex 兜底（兼容 lxml 路径 + selectolax 拿不到时）
        # input id="ASIN" value="B..."（属性顺序可能反过来）
        m = re.search(
            r'<input[^>]*\bid="ASIN"[^>]*\bvalue="(' + ASIN_RE + r')"',
            html_text
        )
        if m:
            return m.group(1).upper()
        m = re.search(
            r'<input[^>]*\bvalue="(' + ASIN_RE + r')"[^>]*\bid="ASIN"',
            html_text
        )
        if m:
            return m.group(1).upper()

        # 3. canonical link
        m = re.search(
            r'<link[^>]+rel="canonical"[^>]+href="[^"]*?/dp/(' + ASIN_RE + r')',
            html_text
        )
        if m:
            return m.group(1).upper()

        # 4. JS currentAsin（部分 PDP 页有）
        m = re.search(r'"currentAsin"\s*:\s*"(' + ASIN_RE + r')"', html_text)
        if m:
            return m.group(1).upper()

        return None

    def _extract_jsonld(self, html_text: str) -> Dict[str, Any]:
        """
        从 <script type="application/ld+json"> 中提取 Product 结构化数据。
        Amazon 页面通常包含 Schema.org Product 对象，字段稳定性远高于 CSS class。
        返回扁平化的字段字典，未找到则返回空字典。
        """
        result = {}
        try:
            # 提取所有 JSON-LD 块
            blocks = re.findall(
                r'<script\s+type="application/ld\+json"[^>]*>(.*?)</script>',
                html_text, re.DOTALL
            )
            product = None
            for block in blocks:
                try:
                    data = json.loads(block.strip())
                except (json.JSONDecodeError, ValueError):
                    continue

                # 可能是单个对象或数组
                items = data if isinstance(data, list) else [data]
                for item in items:
                    t = item.get("@type", "")
                    if t == "Product" or (isinstance(t, list) and "Product" in t):
                        product = item
                        break
                if product:
                    break

            if not product:
                return result

            # 标题
            name = product.get("name")
            if name and isinstance(name, str) and len(name) > 1:
                result["title"] = name.strip()

            # 品牌（过滤长描述文案：真正品牌名一般不超过 80 字符）
            brand = product.get("brand")
            if isinstance(brand, dict):
                brand = brand.get("name", "")
            if brand and isinstance(brand, str) and brand.strip() and len(brand.strip()) <= 80:
                result["brand"] = brand.strip()

            # 图片
            image = product.get("image")
            if image:
                if isinstance(image, str):
                    result["image_urls"] = image
                elif isinstance(image, list):
                    result["image_urls"] = "\n".join(
                        u for u in image if isinstance(u, str)
                    )

            # 价格 (offers)
            offers = product.get("offers")
            if isinstance(offers, dict):
                offers_list = [offers]
            elif isinstance(offers, list):
                offers_list = offers
            else:
                offers_list = []

            for offer in offers_list:
                if not isinstance(offer, dict):
                    continue
                currency = offer.get("priceCurrency", "USD")
                if currency != "USD":
                    continue
                price = offer.get("price") or offer.get("lowPrice")
                if price is not None:
                    try:
                        price_val = float(price)
                        result["current_price"] = f"${price_val:,.2f}"
                    except (ValueError, TypeError):
                        pass

                # 库存状态
                avail = offer.get("availability", "")
                if "InStock" in avail:
                    result["stock_status"] = "In Stock"
                elif "OutOfStock" in avail:
                    result["stock_status"] = "Out of Stock"
                break  # 只取第一个 USD offer

            # EAN / GTIN
            gtin13 = product.get("gtin13")
            if gtin13 and isinstance(gtin13, str) and gtin13.isdigit():
                result["ean_list"] = gtin13

            # 描述 (仅作兜底，CSS 提取的更完整)
            desc = product.get("description")
            if desc and isinstance(desc, str) and len(desc) > 10:
                result["_jsonld_description"] = desc.strip()

            # 评分信息 (当前 Result 模型不含此字段，但提取出来备用)
            agg = product.get("aggregateRating")
            if isinstance(agg, dict):
                result["_rating"] = agg.get("ratingValue")
                result["_review_count"] = agg.get("reviewCount")

        except Exception as e:
            logger.debug(f"JSON-LD 提取异常: {e}")

        return result

    # ==================== 纯文本/正则方法（两种引擎共用）====================

    def _parse_ean(self, html_text: str) -> str:
        try:
            eans = set(re.findall(r'"gtin13":"(\d+)"', html_text))
            return ",".join(sorted(eans))       # P4-5，同 _slx_parse_upc
        except Exception:
            return ""

    def _parse_parent_asin(self, html_text: str, asin: str) -> str:
        try:
            parent = re.search(r'"parentAsin":"(\w+)"', html_text)
            return parent.group(1) if parent else asin
        except Exception:
            return asin

    def _parse_variation_asins(self, html_text: str, asin: str, parent_asin: str) -> str:
        try:
            all_asins = set(re.findall(r'"asin":"(\w+)"', html_text))
            variations = all_asins - {asin, parent_asin}
            # P4-5：跨进程顺序不定，理由同 _slx_parse_upc。这条兜底路径本身还会
            # 捞进赞助位 ASIN（slowhash 因此把 variation_asins 整个排除在哈希外），
            # 但那是另一个问题 —— 顺序不稳是可以现在就消掉的那一半。
            return ",".join(sorted(variations))
        except Exception:
            return ""

    @staticmethod
    def _slice_balanced(text: str, key: str, opener: str, max_gap: int = 8) -> Optional[str]:
        """从 `"key":` 之后提取一段配平的 JSON 字面量（对象或数组）。
        opener 为 '{' 或 '['。返回原始子串；找不到返回 None。带 40 万字符上限防失控。
        max_gap：开括号必须紧跟在 `"key":` 之后（中间只允许 `:`/空白），否则视为该键
        不是期望的 `"key":[...]` / `"key":{...}` 形态（防止像 "dimensions" 这种常见词
        命中别处后跳到很远的无关括号）。"""
        i = text.find('"%s"' % key)
        if i < 0:
            return None
        after = i + len(key) + 2  # 跳过匹配到的 "key"
        j = text.find(opener, after)
        if j < 0 or (j - after) > max_gap:
            return None
        closer = ']' if opener == '[' else '}'
        depth = 0
        in_str = False
        esc = False
        end = min(len(text), j + 400000)
        for k in range(j, end):
            c = text[k]
            if in_str:
                if esc:
                    esc = False
                elif c == '\\':
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == opener:
                    depth += 1
                elif c == closer:
                    depth -= 1
                    if depth == 0:
                        return text[j:k + 1]
        return None

    def _parse_twister(self, html_text: str, asin: str) -> Tuple[str, Optional[str]]:
        """解析 Amazon twister 变体矩阵（单次请求即含全家族）。

        返回 (variant_attributes, variation_asins)：
          - variant_attributes: 本 ASIN 自己的属性，形如 "color_name=Red; size_name=L"；
            非变体 / 解析失败 → ""
          - variation_asins: 精确同族 ASIN（矩阵的键去掉自身），逗号分隔；
            twister 不可用时返回 None，交调用方回退到旧的全页正则。
        数据库其它多值字段一律用分隔字符串（无 JSON），此处沿用同一风格。
        """
        try:
            dvdd_raw = self._slice_balanced(html_text, "dimensionValuesDisplayData", "{")
            if not dvdd_raw:
                return "", None
            dvdd = json.loads(dvdd_raw)
            if not isinstance(dvdd, dict) or not dvdd:
                return "", None

            # 维度名（有序），如 ["color_name","size_name"]。
            # 实测真实页面用的是 "dimensions" 键；"dimensionsDisplay" 作兜底。
            # 两者顺序都与 dimensionValuesDisplayData 里每个 ASIN 的值顺序一致。
            dims = None
            for dim_key in ("dimensions", "dimensionsDisplay"):
                dims_raw = self._slice_balanced(html_text, dim_key, "[")
                if not dims_raw:
                    continue
                try:
                    d = json.loads(dims_raw)
                    if isinstance(d, list) and d and all(isinstance(x, str) for x in d):
                        dims = d
                        break
                except Exception:
                    pass

            # 本 ASIN 自己的属性值（大小写不敏感匹配键）
            my_vals = None
            au = asin.upper()
            for k, v in dvdd.items():
                if isinstance(k, str) and k.upper() == au:
                    my_vals = v
                    break

            attrs = ""
            if isinstance(my_vals, list) and my_vals:
                vals = [str(x).strip() for x in my_vals]
                if dims and len(dims) == len(vals):
                    attrs = "; ".join("%s=%s" % (d, val) for d, val in zip(dims, vals))
                else:
                    # 没拿到维度名或长度不匹配：只列值，避免错位贴标签
                    attrs = "; ".join(vals)

            # 精确同族：矩阵的键去掉自身
            family = [k for k in dvdd.keys()
                      if isinstance(k, str) and k.upper() != au]
            return attrs, ",".join(family)
        except Exception:
            return "", None

    #: P4-4：`manufacturer` 只认这些**精确**键名（归一化后比较）。
    #: 原来是子串匹配 `'manufacturer' in k_lower`，它命中的第一个常见键是
    #: **"Manufacturer recommended age"** —— manufacturer 于是变成 "3 years and up"。
    #: 由于 `_map_detail` 无条件覆盖、而 `_parse_all_details` 扫的是**全文档**所有
    #: <tr>（含 "Compare with similar items" 与 A+ 对比表），谁最后写谁赢完全取决于
    #: 文档顺序：模板/AB 一变就在 "Acme" ↔ "3 years and up" 之间来回翻，每翻一次
    #: 两次误复审（manufacturer ∈ 慢变字段）。
    _MANUFACTURER_KEYS = frozenset({
        "manufacturer",
        "manufacturer name",
        "manufactured by",
    })

    #: 详情表键名归一化时要丢掉的字符。Amazon 的 detailBullets 键长这样：
    #: ``"Manufacturer ‏ : ‎"`` —— 冒号 + 两个双向控制字符。
    _KEY_JUNK_RE = re.compile(r"[^a-z0-9&/ ]+")

    @classmethod
    def _norm_detail_key(cls, k: str) -> str:
        """详情表键名归一化：小写 → 非 [a-z0-9&/ ] 字符换空格 → 折叠空白。"""
        return re.sub(r"\s+", " ", cls._KEY_JUNK_RE.sub(" ", (k or "").lower())).strip()

    def _map_detail(self, d: Dict, k: str, v: str):
        """字段名映射"""
        k_lower = k.lower()
        if 'model number' in k_lower:
            d['model_number'] = v
        elif 'part number' in k_lower:
            # 'part number' 排在 'manufacturer' 之前，所以 "Manufacturer Part Number"
            # 一直落位正确 —— 当年活着的只有年龄段那一条。
            d['part_number'] = v
        elif 'country of origin' in k_lower:
            d['country_of_origin'] = v
        elif 'best sellers rank' in k_lower:
            d['best_sellers_rank'] = v
        elif self._norm_detail_key(k) in self._MANUFACTURER_KEYS:
            d['manufacturer'] = v
        elif k_lower.strip() == 'brand':
            d['brand'] = v
        elif 'date first available' in k_lower:
            d['date_first_available'] = v
        elif 'upc' in k_lower:
            d['upc'] = v
        elif 'weight' in k_lower and 'item' in k_lower:
            d['item_weight'] = v
        elif 'weight' in k_lower and 'package' in k_lower:
            _, w = self._split_dim_weight(v)
            d['package_weight'] = w if w != "N/A" else "N/A"
        elif 'dimensions' in k_lower:
            dim, _ = self._split_dim_weight(v)
            if 'package' in k_lower:
                d['package_dimensions'] = dim
            else:
                d['item_dimensions'] = dim

    def _split_dim_weight(self, s: str) -> Tuple[str, str]:
        """拆分尺寸和重量（用分号分隔）"""
        if not s:
            return "N/A", "N/A"
        parts = s.split(';')
        return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "N/A")

    # ==================== lxml 字段解析方法 (fallback) ====================

    def _get_text(self, tree, xpaths: List[str]) -> Optional[str]:
        """从多个 XPath 中尝试获取文本"""
        for xp in xpaths:
            try:
                result = tree.xpath(xp)
                if result:
                    text = result[0].strip() if isinstance(result[0], str) else ""
                    if text:
                        return text
            except Exception:
                continue
        return None

    def _get_all_text(self, tree, xpath: str) -> List[str]:
        """获取 XPath 匹配的所有文本"""
        try:
            return tree.xpath(xpath)
        except Exception:
            return []

    def _parse_title(self, tree) -> str:
        try:
            meta = tree.xpath('//meta[@name="title"]/@content')
            visible = self._get_text(tree, [
                '//span[@id="productTitle"]/text()',
                '//h1/span/text()',
            ])
            if not visible:
                return meta[0].strip() if meta else "N/A"
            # 2026-08 改版：后半段在兄弟节点里，见 `_join_title_parts` 上方注释。
            #
            # ⚠ 这里**不能**走 `_get_text`，两条路都不行，实测过：
            #   * `.../text()` 只拿容器的直接文本节点，而文案在子 `<span>` 里
            #     -> 恒空；
            #   * `string(...)` 让 `tree.xpath()` 返回的是**一个字符串**而不是
            #     列表，`_get_text` 的 `result[0]` 于是取到**第一个字符**
            #     （那是个空格，strip 后为空）-> 同样恒空。
            # 取元素再 `text_content()`：子孙文本全收，与 selectolax 的
            # `node.text(strip=True)` 同口径，两个引擎才会给出同一个 title。
            diff = None
            nodes = tree.xpath(self._TITLE_DIFF_XPATH)
            if nodes:
                diff = " ".join(nodes[0].text_content().split())
            return self._join_title_parts(visible, diff)
        except Exception:
            return "N/A"

    # `_parse_zip_code` 已删除（P4-1），理由同 `_slx_parse_zip_code`。

    def _parse_brand(self, tree) -> str:
        try:
            xpaths = [
                '//a[@id="bylineInfo"]/text()',
                '//a[@id="brand"]/text()',
                '//span[@id="bylineInfo"]/text()',
                '//*[@id="bylineInfo_feature_div"]//a/text()',
            ]
            brand_text = self._get_text(tree, xpaths)
            if brand_text:
                brand = re.sub(
                    r'Visit the |Brand:\s*| Store$', '', brand_text,
                    flags=re.IGNORECASE
                ).strip()
                if brand and len(brand) <= 80:
                    return brand
        except Exception:
            pass
        return "N/A"

    def _parse_current_price(self, tree) -> str:
        try:
            # 限定到价格容器内（避免全页匹配 a-offscreen）
            price_xpaths = [
                '//*[@id="corePrice_feature_div"]//span[@class="a-offscreen"]/text()',
                '//*[@id="corePriceDisplay_desktop_feature_div"]//span[@class="a-offscreen"]/text()',
                '//*[@id="price"]//span[@class="a-offscreen"]/text()',
                '//*[@id="priceblock_ourprice"]/text()',
                '//*[@id="priceblock_dealprice"]/text()',
                '//*[@id="priceToPay"]//span[@class="a-offscreen"]/text()',
                '//*[@id="apex_offerDisplay_desktop"]//span[@class="a-offscreen"]/text()',
            ]
            for xp in price_xpaths:
                vals = tree.xpath(xp)
                for v in vals:
                    if isinstance(v, str) and "$" in v:
                        return v.strip()
            # fallback: 拆分整数+小数（限定到价格容器）
            for container_id in ['corePrice_feature_div', 'corePriceDisplay_desktop_feature_div',
                                 'price', 'apex_offerDisplay_desktop']:
                whole = tree.xpath(f'//*[@id="{container_id}"]//span[@class="a-price-whole"]/text()')
                frac = tree.xpath(f'//*[@id="{container_id}"]//span[@class="a-price-fraction"]/text()')
                if whole and frac:
                    w = whole[0].strip().replace('.', '')
                    f = frac[0].strip()
                    if w and f:
                        return f"${w}.{f}"
            # 全局 fallback 已移除，避免匹配推荐区域价格
        except Exception:
            pass
        return "N/A"

    def _parse_buybox_price(self, tree) -> Optional[str]:
        xpaths = [
            '//*[@id="tabular-buybox"]//span[@class="a-offscreen"]/text()',
            '//*[@id="buybox"]//span[@class="a-offscreen"]/text()',
            '//*[@id="newBuyBoxPrice"]//text()',
            '//*[@id="priceToPay"]//span[@class="a-offscreen"]/text()',
            '//*[@id="price_inside_buybox"]/text()',
            '//*[@id="corePrice_feature_div"]//span[@class="a-offscreen"]/text()',
            '//*[@id="corePriceDisplay_desktop_feature_div"]//span[@class="a-offscreen"]/text()',
            '//*[@id="apex_offerDisplay_desktop"]//span[@class="a-offscreen"]/text()',
        ]
        for xp in xpaths:
            try:
                vals = tree.xpath(xp)
                for v in vals:
                    if isinstance(v, str) and re.search(r'\d', v):
                        return v.strip()
            except Exception:
                continue
        return None

    def _parse_original_price(self, tree) -> str:
        try:
            for container_id in ['corePrice_feature_div', 'corePriceDisplay_desktop_feature_div', 'price']:
                orig = tree.xpath(f'//*[@id="{container_id}"]//span[@data-a-strike="true"]//span[@class="a-offscreen"]/text()')
                if orig:
                    return orig[0].strip()
        except Exception:
            pass
        return "N/A"

    def _parse_buybox_shipping(self, tree, current_price: Optional[str]) -> str:
        try:
            delivery_texts = self._get_all_text(tree, '//div[@id="deliveryBlockMessage"]//text()')
            delivery_block = " ".join(delivery_texts)

            if "prime members get free delivery" in delivery_block.lower():
                return "FREE"

            free_over = re.search(r'free delivery.*?on orders over \$(\d+(?:\.\d+)?)', delivery_block, re.IGNORECASE)
            if free_over:
                threshold = float(free_over.group(1))
                if current_price and current_price not in ['N/A', 'See price in cart', '不可售']:
                    price_match = re.search(r'\$?([\d,]+\.?\d*)', current_price)
                    if price_match:
                        price_val = float(price_match.group(1).replace(',', ''))
                        if price_val >= threshold:
                            return "FREE"
                return "N/A"

            dp = tree.xpath('//span[@data-csa-c-delivery-price]/@data-csa-c-delivery-price')
            if dp:
                return dp[0].strip()

            shipping = self._get_text(tree, [
                '//div[@id="mir-layout-loaded-comparison-row-2"]//span/text()'
            ])
            return shipping if shipping else "FREE"
        except Exception:
            return "N/A"

    def _check_unavailable(self, tree) -> bool:
        try:
            texts = self._get_all_text(tree, '//div[@id="availability"]//text()')
            full = " ".join(texts).lower()
            return "currently unavailable" in full
        except Exception:
            return False

    def _check_see_price_in_cart(self, tree) -> bool:
        try:
            if tree.xpath('//a[contains(text(), "See price in cart")]'):
                return True
            if tree.xpath('//*[@id="a-popover-map_help_pop_"]'):
                return True
            texts = self._get_all_text(tree, '//table[@class="a-lineitem"]//text()')
            return any("see price in cart" in t.lower() for t in texts)
        except Exception:
            return False

    def _parse_fulfillment(self, tree, html_text: str) -> str:
        try:
            rows = tree.xpath('//div[@id="tabular-buybox"]//tr')
            for row in rows:
                label = row.xpath('.//span[contains(text(), "Ships from")]/text() | .//span[contains(text(), "Shipper")]/text()')
                if label:
                    value_nodes = row.xpath('.//span[contains(@class, "a-color-base")]/text()')
                    value = value_nodes[0] if value_nodes else ""
                    if value and "amazon" not in value.lower():
                        return "FBM"
                    if value and "amazon" in value.lower():
                        return "FBA"

            blob = " ".join(self._get_all_text(tree, '//div[@id="rightCol"]//text() | //div[@id="tabular-buybox"]//text()')).lower()
            match = re.search(r'(ships from|shipper / seller)\s*[:\s]*([a-z0-9\s]+)', blob)
            if match and "amazon" not in match.group(2).strip():
                return "FBM"
            if "fulfilled by amazon" in blob or "prime" in blob or "amazon.com" in blob:
                return "FBA"
        except Exception:
            pass
        return "FBA"

    def _parse_stock_count(self, stock_status: str, tree) -> int:
        try:
            stock_lower = stock_status.lower() if stock_status else ""
            if "only" in stock_lower:
                count = re.search(r'(\d+)', stock_status)
                return int(count.group(1)) if count else 999
            if "out of stock" in stock_lower or "unavailable" in stock_lower:
                return 0
            options = tree.xpath('//select[@name="quantity"]//option/@value')
            if not options:
                options = tree.xpath('//select[@id="quantity"]//option/@value')
            if options:
                values = [int(v) for v in options if v.strip().isdigit()]
                if values:
                    return max(values)
        except Exception:
            pass
        return 999

    def _parse_delivery(self, tree) -> Tuple[str, str]:
        try:
            # 与 selectolax 路径一致：优先直接读 data-csa-c-delivery-time 属性值；
            # 无属性时回退整段配送文本。
            candidates: List[str] = []
            for el in tree.xpath('//*[@data-csa-c-delivery-time]'):
                raw = (el.get('data-csa-c-delivery-time') or "").strip()
                if raw:
                    candidates.append(raw)

            if not candidates:
                texts = self._get_all_text(tree,
                    '//*[@data-csa-c-delivery-time]//text() | '
                    '//div[contains(@class,"delivery-message")]//text() | '
                    '//div[@id="deliveryBlockMessage"]//text()')
                if texts:
                    candidates.append(" ".join(texts))

            if candidates:
                return self._pick_delivery(candidates)
        except Exception:
            pass
        return "N/A", "N/A"

    def _parse_customization(self, tree) -> str:
        try:
            if tree.xpath('//*[contains(translate(text(), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "customize now")]'):
                return "Yes"
            texts = " ".join(self._get_all_text(tree, '//div[@id="rightCol"]//text() | //div[@id="tabular-buybox"]//text()')).lower()
            if "needs to be customized" in texts or "customization required" in texts:
                return "Yes"
        except Exception:
            pass
        return "No"

    def _parse_bullet_points(self, tree) -> str:
        try:
            bullets = self._get_all_text(tree, '//div[@id="feature-bullets"]//ul/li//span[@class="a-list-item"]//text()')
            if not bullets:
                bullets = self._get_all_text(tree, '//div[contains(@class,"a-expander-content")]//ul/li//span[@class="a-list-item"]//text()')

            clean = []
            for b in bullets:
                txt = b.strip()
                if not txt or len(txt) < 2:
                    continue
                if any(spam in txt.lower() for spam in self.BULLET_BLACKLIST):
                    continue
                clean.append(txt)
            return "\n".join(clean)
        except Exception:
            return ""

    def _parse_long_description(self, tree, html_text: str) -> str:
        try:
            _TEXT_TAGS = {'p', 'span', 'div', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'td', 'th'}
            container = None
            for xpath in [
                '//div[contains(@class,"aplus")]',
                '//*[@id="aplus"]',
                '//*[@id="aplusProductDescription"]',
                '//*[@id="productDescription"]',
            ]:
                results = tree.xpath(xpath)
                if results:
                    container = results[0]
                    break

            parts = []
            if container is not None:
                for node in container.iter():
                    tag = node.tag if isinstance(node.tag, str) else ''
                    if tag == 'img':
                        src = node.get('src') or node.get('data-src', '')
                        if src and "pixel" not in src and "transparent" not in src:
                            parts.append(f"\n[Image: {src.strip()}]\n")
                    elif tag in _TEXT_TAGS:
                        children_with_text = [c for c in node.iter() if (isinstance(c.tag, str) and c.tag in _TEXT_TAGS and c is not node)]
                        if not children_with_text:
                            text = _lxml_leaf_text(node).strip()
                            if text and len(text) > 5:
                                parts.append(text)

            if not parts:
                seo_keywords = ['ai-optimized', 'search submission', 'noindex', 'sponsored']
                for m in re.finditer(r'"description"\s*:\s*"([^"]{20,})"', html_text):
                    text = m.group(1)
                    if any(kw in text.lower() for kw in seo_keywords):
                        continue
                    try:
                        decoded = text.encode('utf-8').decode('unicode_escape')
                        clean = re.sub(r'<[^>]+>', '\n', decoded)
                        return clean.strip()[:4000]
                    except Exception:
                        continue

            return "\n".join(parts)[:10000]
        except Exception:
            return ""

    def _parse_images(self, tree, html_text: str) -> str:
        try:
            img_urls = []
            scripts = self._get_all_text(tree, '//script[contains(text(), "colorImages")]/text()')
            if scripts:
                script = scripts[0]
                urls = re.findall(r'"hiRes":"(https://[^"]+)"', script) or \
                       re.findall(r'"large":"(https://[^"]+)"', script)
                # 保序去重：保持亚马逊页面图片原始顺序，主图排在第 1 位
                img_urls = list(dict.fromkeys(urls))
            else:
                img_urls = tree.xpath('//div[@id="imgTagWrapperId"]/img/@src')
            return "\n".join(img_urls)
        except Exception:
            return ""

    def _parse_upc(self, tree, html_text: str, page_details: Dict) -> str:
        try:
            upc_set = set(re.findall(r'"upc":"(\d+)"', html_text))
            if 'upc' in page_details:
                upc_set.add(page_details['upc'])
            upc_set.update(re.findall(r'UPC\s*[:#]?\s*(\d{12})', html_text))
            return ",".join(sorted(upc_set))    # P4-5，同 _slx_parse_upc
        except Exception:
            return ""

    def _parse_categories(self, tree) -> Tuple[str, str, str]:
        try:
            hrefs = tree.xpath('//div[@id="wayfinding-breadcrumbs_feature_div"]//a/@href')
            cids = re.findall(r'node=(\d+)', " ".join(hrefs))
            category_ids = ",".join(cids)
            root_id = cids[0] if cids else "N/A"

            names = tree.xpath('//div[@id="wayfinding-breadcrumbs_feature_div"]//li//a/text()')
            category_tree = " > ".join([n.strip() for n in names if n.strip()])

            return root_id, category_ids, category_tree
        except Exception:
            return "N/A", "", ""

    def _parse_all_details(self, tree, html_text: str) -> Dict[str, str]:
        """全页扫描：提取 Product Information 表格内容"""
        d = {}
        try:
            for row in tree.xpath('//th/../..//tr'):
                try:
                    k_nodes = row.xpath('normalize-space(./th)')
                    v_nodes = row.xpath('normalize-space(./td)')
                    if k_nodes and v_nodes:
                        self._map_detail(d, str(k_nodes), str(v_nodes))
                except Exception:
                    continue

            for s in tree.xpath('//li/span/span[contains(@class, "a-text-bold")]'):
                try:
                    k = s.xpath('normalize-space(./text())')
                    v = s.xpath('normalize-space(./following-sibling::span)')
                    if k and v:
                        self._map_detail(d, str(k).replace(':', ''), str(v))
                except Exception:
                    continue

            gl = re.search(r'"gl_product_group_type":"([^"]+)"', html_text)
            if gl:
                d['product_type'] = gl.group(1)
        except Exception as e:
            logger.warning(f"详情解析异常: {e}")

        return d



# 全局解析器实例
parser = AmazonParser()


# ==================== 卖家店铺列表页解析（F-009）====================

# `_ASIN_RE` 现在从 common/core/idents.py import（见文件头那段说明）。
# 与 server/app.py 共用同一个编译对象；正则本身与旧的本地定义逐字节相同。


def parse_seller_listing(html_text: str) -> Dict[str, Any]:
    """解析三方卖家列表页 /s?me={seller_id}&page=N。

    Returns:
        {
          "items":   [{"asin","title","price","image"}, ...],
          "asins":   [...],
          "has_next": bool,
          "page_info": str,  # 'No results' / 'CAPTCHA' / 'OK' 等，便于诊断
        }
    """
    out = {"items": [], "asins": [], "has_next": False, "page_info": ""}
    if not html_text:
        out["page_info"] = "empty_html"
        return out

    # 验证码 / 拦截快速侦测（与 _parse_with_selectolax 对齐）
    low = html_text[:4096].lower()
    if "validateCaptcha" in html_text or "/errors/validateCaptcha" in html_text:
        out["page_info"] = "captcha"
        return out
    if "robot check" in low or "to discuss automated access" in low:
        out["page_info"] = "blocked"
        return out

    items = []
    seen = set()

    if _USE_SELECTOLAX:
        try:
            tree = SlxParser(html_text)
        except Exception as e:
            out["page_info"] = f"parse_error:{type(e).__name__}"
            return out

        # 主结果容器：data-asin 是首选锚点
        for node in tree.css('div.s-result-item[data-asin]'):
            asin = (node.attributes.get('data-asin') or '').strip().upper()
            if not asin or not _ASIN_RE.match(asin) or asin in seen:
                continue
            seen.add(asin)

            title = ""
            t_node = node.css_first('h2 span') or node.css_first('h2 a span')
            if t_node:
                title = (t_node.text() or "").strip()

            price = ""
            p_off = node.css_first('span.a-price[data-a-color="base"] span.a-offscreen') \
                or node.css_first('span.a-price span.a-offscreen')
            if p_off:
                price = (p_off.text() or "").strip()

            image = ""
            img = node.css_first('img.s-image')
            if img:
                image = (img.attributes.get('src') or '').strip()

            items.append({"asin": asin, "title": title, "price": price, "image": image})

        # 翻页：a.s-pagination-next 存在且不带 disabled 类
        next_node = tree.css_first('a.s-pagination-next')
        has_next = bool(next_node)
        if next_node:
            cls = (next_node.attributes.get('class') or '')
            if 's-pagination-disabled' in cls:
                has_next = False
        out["has_next"] = has_next

    elif lxml_html is not None:
        try:
            tree = lxml_html.fromstring(html_text)
        except Exception as e:
            out["page_info"] = f"parse_error:{type(e).__name__}"
            return out
        for node in tree.xpath('//div[@data-asin and contains(@class, "s-result-item")]'):
            asin = (node.get('data-asin') or '').strip().upper()
            if not asin or not _ASIN_RE.match(asin) or asin in seen:
                continue
            seen.add(asin)
            title_nodes = node.xpath('.//h2//span/text()')
            title = (title_nodes[0].strip() if title_nodes else "")
            price_nodes = node.xpath('.//span[contains(@class,"a-price")]//span[contains(@class,"a-offscreen")]/text()')
            price = (price_nodes[0].strip() if price_nodes else "")
            img_nodes = node.xpath('.//img[contains(@class,"s-image")]/@src')
            image = (img_nodes[0].strip() if img_nodes else "")
            items.append({"asin": asin, "title": title, "price": price, "image": image})

        next_nodes = tree.xpath('//a[contains(@class,"s-pagination-next")]')
        has_next = False
        if next_nodes:
            cls = next_nodes[0].get('class') or ''
            has_next = 's-pagination-disabled' not in cls
        out["has_next"] = has_next
    else:
        out["page_info"] = "no_parser"
        return out

    out["items"] = items
    out["asins"] = [it["asin"] for it in items]
    out["page_info"] = "ok" if items else "no_results"
    return out
