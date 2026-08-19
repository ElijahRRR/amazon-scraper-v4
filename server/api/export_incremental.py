"""catalog_sync 增量导出端点 —— 契约 v1 的服务端实现。

    GET /api/export/incremental?cursor=<int>&limit=<≤1000>
    -> {"records": [...], "next_cursor": <int>, "has_more": <bool>}

规格来源：沃尔玛侧 `docs/scraper_migration_brief.md` §5（本仓库存有一份副本：
`docs/incremental_export_contract.md`）。**契约双方各存一份，改动需两侧同步升版本号。**

本文件是一层**适配器**，不是新的存储。数据来自 Phase 2 建的事件流
（`scraper.scrape_events`），与 `server/api/sync.py` 读同一张表、同一套快照纪律。
两者的关系：本端点是**对外契约**；`/api/v1/sync/*` 是运维面（status/counts/ack），
沃尔玛侧不需要实现它们也能正常消费。

------------------------------------------------------------------------
承重约束
------------------------------------------------------------------------

1. **路由注册顺序是承重的。**
   `server/api/export.py` 有 `@router.get("/api/export/{batch_name}")`，对不认识的
   名字回 **404**。实测（未挂本端点时）：

       GET /api/export/incremental -> 404 {"detail":"批次不存在: incremental"}

   Starlette 按**注册顺序**匹配，所以本 router 必须排在那条 catch-all **之前**。

   **这条依赖已经从「隐式」变成「局部」**（Phase 3.7）：`server/api/export.py`
   在文件顶部、任何 `@router.get` 之前就 `router.include_router(_incr.router)`，
   `server/app.py` 只 include 一个 export router。于是顺序由**单文件自上而下阅读**
   保证，app.py 的 include 列表怎么重排都打不破它。
   —— 此前它靠 app.py 里两个 include 的相对位置维系，那是隐式的、跨文件的、
   任何一次重排都可能无声破坏。

   **为什么必须有回归守卫**：把 `include_router(_incr.router)` 挪到 export.py 末尾、
   或者把本端点改成定义在 catch-all 之后，都会让它静默退化成 404 ——
   而 404 正是消费方最容易读成「暂无数据」的码，游标于是永不推进、同步静默停摆，
   两侧都不会报错。

   守卫共四层，实测变异后**四层同时红**（Phase 3.7 审计）：
     * 结构：`tests/test_incremental_export.py::test_route_order_is_load_bearing`
       （必须**递归展开** `_IncludedRouter` —— 这版 FastAPI 的 include_router 不平铺，
       而且 `_IncludedRouter` 没有 `.routes`，要走 `original_router.routes`）
     * 行为：`::test_endpoint_is_reachable_not_swallowed`（真打一次，响应体不含「批次不存在」）
     * 源码：`::test_export_module_includes_incremental_before_first_route`
     * 上机：`tools/preflight.py` 的「路由顺序」一项

2. **鉴权：`X-Export-Token` 请求头，契约 v1 定为可选。**
   配了 `EXPORT_TOKEN` 就强制校验（`hmac.compare_digest`，不匹配即 401）；
   没配则放行并打 WARNING，要 fail closed 设 `EXPORT_REQUIRE_TOKEN=1`。
   完整理由见 `_check_token` 的 docstring —— 那里记着为什么最初的 fail-closed
   实现被改掉了（契约是权威）。
   **只加在本 router 上，不加全局中间件**：本服务今天一处鉴权都没有，
   全局中间件会当场打死所有 worker 与 erpAPI。

3. **永不用 404 表达「没有数据」。** 空就是 `200 + records: [] + has_more: false`。
   这与同前缀下 `/api/export/{batch_name}` 的既有行为**故意相反**，理由见第 1 条。

4. **一个快照。** MIN/MAX/页查询同一个 `REPEATABLE READ READ ONLY` 事务，
   否则保留期可以卡在中间跑完，让消费者拿到一个「跳过了被裁区间」的 200。
"""
from __future__ import annotations

import hmac
import logging
import os
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse

from common.core.completeness import completeness_ok
from common.slowhash import parse_variant_attributes, split_multivalue
from server.api import sync as _sync

logger = logging.getLogger(__name__)

#: 契约版本。改任何字段语义都必须升它，并同步沃尔玛侧那份。
CONTRACT_VERSION = 1

MAX_LIMIT = 1000
DEFAULT_LIMIT = 500          # 契约 v1 定的默认值

#: 沃尔玛侧的 marketplace 值域，当前恒 "US"（对方已在 db_schema / schema.sql /
#: 契约三处同步为默认 'US'，复合主键 (marketplace, asin)）。
#:
#: ⚠ 语义提示（已在交付说明里单列）：**这个 marketplace 是「上架目的地」，
#: 不是「采集来源站点」。** 本仓库内部的 `scrape_events.marketplace` 存的是
#: 采集来源（封闭集 {'amazon.com'}）。今天两者一一对应，开了 Walmart CA 之后
#: 就不再对应了——那时很可能仍然从 amazon.com 采、却要上架到 CA。
#: 所以这里**不做**「把内部值改名」的处理，而是显式映射，并把来源站点原样
#: 放进 scrape_params.source_marketplace，让两个概念从第一天就是两个字段。
DEST_MARKETPLACE = "US"

#: 我们不采集币种。amazon.com 恒为美元。
#: ⚠ 这是本适配器**凭空补出来**的字段，不是采集到的事实。
DEFAULT_CURRENCY = "USD"

#: 采集侧的库存文案 -> 契约枚举。
#: ⚠ 枚举取值需与 §5 核对，见交付说明的假设清单第 4 条。
_STOCK_IN = re.compile(r"in\s*stock|available|有货", re.I)
_STOCK_OUT = re.compile(r"out\s*of\s*stock|unavailable|currently\s+unavailable|无货|缺货", re.I)

_NA = {"", "n/a", "none", "null", "-"}

#: ``include_in_schema=False`` 是硬要求，不是风格。
#: ``/openapi.json`` 是**既有端点**，黄金基线第 5 步逐字节钉死它，而沃尔玛侧
#: 明确要求「既有端点全部不动」。少这一行，黄金门当场红——实测过：
#:   [openapi_schema].paths./api/export/incremental 新增字段（…）
#: 契约文档是给人看的那份（docs/incremental_export_contract.md），不靠 schema 分发。
router = APIRouter(tags=["export-incremental"], include_in_schema=False)


# ============================================================ 取值助手

def _clean(v: Any) -> Optional[str]:
    """占位符归一到 None。采集侧用 "N/A" 表示「本次没取到」。"""
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in _NA else s


def _split_list(field: str, v: Any) -> List[str]:
    """多值字段 -> 列表。分隔符**只能**来自 common/slowhash 的那张表。

    这里曾经自带一份分隔符（images 按 ','、bullet_points 按 '|'），而 parser 的
    四条路径（selectolax/lxml × images/bullets）全是 ``"\n".join(...)``。真机第一条
    真实记录就把它照出来了：``slow.images`` 是一个长度为 1 的列表，里面塞着 6 条
    换行拼起来的 URL；``slow.bullet_points`` 同理。两个都是契约字段。

    逗号那份还不只是"切不开"：老式 Amazon 图片 URL 的变换参数里**含逗号**
    （``._AC_,0,0_.jpg``），真按逗号切会把一条 URL 打成碎片 —— 比不切更坏。

    ``_clean`` 这道前置不能省：它比 slowhash 的 ``SENTINELS`` 宽 —— 大小写不敏感，
    而且认 ``none`` / ``null`` / ``-``。哈希那侧的哨兵是**全等匹配**（有意的，
    见 ``normalize_text`` 的 docstring），直接透传会让 ``"n/a"`` 变成一个真元素。
    本次只该改分隔符，不该顺手改哨兵语义。
    """
    s = _clean(v)
    return [] if s is None else split_multivalue(field, s)


def _price(v: Any) -> Optional[float]:
    """'$1,299.00' / '19.99' -> 1299.0 / 19.99；取不到 -> None。

    绝不返回 0.0 当「取不到」—— 0 是个合法价格，用它当哨兵会让消费侧
    把「没采到」误读成「免费」。
    """
    s = _clean(v)
    if s is None:
        return None
    m = re.search(r"\d[\d,]*\.?\d*", s.replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _int(v: Any) -> Optional[int]:
    """'37' / '1,299' -> 37 / 1299；取不到 -> None。

    与 ``_price`` 同一条原则：**绝不用 0 表示「没取到」**。
    ``stock_count=0`` 是个合法值（缺货），拿它当哨兵会让消费侧把「这次没采到
    库存」误读成「确认为 0 件」——那正好是最需要区分的两种情况。

    只接受**整数**形态：``"3.7"`` 之类回 None 而不是截成 3。库存和配送天数
    出现小数说明解析出问题了，静默取整会把一个信号变成一个看着正常的错值。
    """
    s = _clean(v)
    if s is None:
        return None
    s = s.replace(",", "").replace(" ", "")
    m = re.fullmatch(r"[+-]?\d+", s)
    if not m:
        return None
    try:
        return int(s)
    except ValueError:
        return None


#: 采集侧用来表示「免运费」的字面量。**只认这一个**（大小写不敏感）——
#: 两个解析引擎的 `_parse_buybox_shipping` / `_slx_parse_buybox_shipping`
#: 在四条分支上返回的都是这个词（Prime 免邮、满额免邮达标、以及兜底）。
#: 想再认 "free shipping" / "$0.00" 之类，请先去采集侧确认它真会产出，
#: 别在这里凭印象放宽 —— 放宽的代价是把一个没采到的值算成 0。
_SHIPPING_FREE = "free"


def _shipping(v: Any) -> Optional[float]:
    """运费数值。``FREE`` -> ``0.0``；``$5.99`` -> ``5.99``；没采到 -> ``None``。

    ``FREE`` 必须映射成 **0.0 而不是 None** —— 「确认免运费」是一条真信息，
    丢成 None 会让消费侧把它和「这次没采到运费」混在一起。反过来，采集侧的
    ``"N/A"`` 是**没采到**（``_clean`` 已经把它归一到 None），绝不能当 0：

        FREE  -> 0.0    确认免运费，落地价 = price + 0
        N/A   -> None   没采到，落地价**算不出来**
        $5.99 -> 5.99

    这正是 ``stock_count`` 那条不变量（3b）的同一个形状：``null`` ≠ ``0``，
    消费端不能写 ``or 0``。UI 导出的「总价」列现在就把 ``N/A`` 当 0 加进总价
    （见 ``server/api/export.py:_prepare_row``），于是「没采到运费」和「免运费」
    在那一列上是同一个结果 —— **本函数刻意不复制那个行为**。
    """
    s = _clean(v)
    if s is None:
        return None
    if s.strip().lower() == _SHIPPING_FREE:
        return 0.0
    return _price(s)


def _stock_state(payload: Dict[str, Any]) -> str:
    """-> in_stock | out_of_stock | unknown"""
    s = _clean(payload.get("stock_status"))
    if s is None:
        return "unknown"
    if _STOCK_OUT.search(s):
        return "out_of_stock"
    if _STOCK_IN.search(s):
        return "in_stock"
    return "unknown"


def _category_path(payload: Dict[str, Any]) -> List[str]:
    """'Home > Test > Sub' -> ['Home','Test','Sub']。

    采集侧只有面包屑一个数据源，软降级页会把它整块剥掉，那时这里是 []。
    **[] 表示「本次没采到」，不表示「该商品无类目」** —— 配合 record 里的
    `completeness_ok` 判断，别拿 [] 去覆盖你侧已有的类目。
    """
    raw = _clean(payload.get("category_tree"))
    if raw is None:
        return []
    return [p.strip() for p in re.split(r"\s*>\s*", raw) if p.strip()]


# ============================================================ record 映射

#: 不进 `raw` 的键：内部字段（下划线前缀由调用方约定）、以及已经在
#: slow/fast/scrape_params 里给过一遍的大文本。`raw` 是「备查」不是「全量镜像」，
#: 把 long_description/image_urls 再存一遍会让 jsonb 体积翻倍而没有新信息。
_RAW_DROP = frozenset({
    "long_description", "image_urls", "bullet_points",
    "content_hash", "title_bullets_hash", "screenshot_path",
})


def _raw(payload: Dict[str, Any]) -> Dict[str, Any]:
    """裁剪后的原始载荷。保留采集侧原样的键值，去掉内部字段与重复大文本。"""
    return {k: v for k, v in payload.items()
            if not k.startswith("_") and k not in _RAW_DROP}


def _short_hash(v: Any) -> Optional[str]:
    """契约 v1：slow_hash 是 **sha256 前 16 位**。

    采集侧内部存的是 ``"v1:<64 位十六进制>"``（带版本前缀，便于将来做
    「过渡期双输出」而不引发全语料复审风暴）。对外按契约截成 16 位。
    截断只损失碰撞空间（16 位十六进制 = 64 bit，对变更检测远远够用），
    不影响「同输入同输出」这条本质。
    """
    if not v:
        return None
    s = str(v)
    if ":" in s:
        s = s.split(":", 1)[1]
    return s[:16]


def _iso_seconds(dt: Any) -> Optional[str]:
    """契约 v1：``scraped_at`` **精确到秒**。库里的 timestamptz 带小数秒，截掉。"""
    s = _sync._iso(dt)
    if s is None:
        return None
    # '2026-08-05T09:11:02.114000Z' -> '2026-08-05T09:11:02Z'
    return re.sub(r"\.\d+(?=Z$)", "", s)


def _variant(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """slow.variant = {parent_asin, theme}。

    theme 取变体**维度名**（``"color_name/size_name"``），不是取值 —— 取值属于
    **这一个**变体，维度才是这个变体族共有的慢变属性。

    ------------------------------------------------------------------
    这里曾经自带一份**格式写错**的解析，导致 theme 恒为 null
    ------------------------------------------------------------------
    原实现是 ``seg.split(":", 1)[0] for seg in re.split(r"[|;]", raw) if ":" in seg``，
    docstring 还写着「采集侧存的是 ``"Color:Red|Size:M"`` 形态」。**采集侧从来
    没有产出过那个形态**：``worker/parser.py:_parse_twister`` 拼的是

        "; ".join("%s=%s" % (dim, val) ...)   ->  "color_name=Red; size_name=L"

    分隔符是 ``=`` 不是 ``:``，于是 ``if ":" in seg`` 对每一段都为假 ⇒ keys 恒空
    ⇒ **theme 对所有真实记录恒为 null**。而 parent_asin 照常有值，所以
    ``variant`` 对象本身不是 None，从响应上看不出任何异常 —— 下游只会读成
    「这个商品没有变体维度」。

    根因是仓库里记过的同一种错法（`.agent/MIGRATION_STATUS.md` §5.5 的 V2/V4）：
    **抄了一份该共用的东西，然后两份各自演化**。正确的那份一直都在 ——
    ``common/slowhash.parse_variant_attributes``，它连采集侧的两种形态都在
    注释里标了 file:line。所以这里不是去「修对分隔符」，而是**改成复用它**。

    两种形态的处理：
      * ``"color_name=Red; size_name=L"``  -> theme ``"color_name/size_name"``
      * ``"Red; L"``（采集侧拿不到维度名时的回退）-> 值落进 slowhash 的无键槽
        ``""``，**theme 仍为 None** —— 我们确实不知道维度叫什么，
        编一个出来比留空更糟。

    维度顺序**保持采集侧的顺序**（``dimensions`` 数组的顺序），不排序：
    ``color/size`` 与 ``size/color`` 描述的是同一族，但前者才是页面上的次序。
    ``parse_variant_attributes`` 返回的 dict 按输入序插入，直接迭代即可。
    """
    parent = _clean(payload.get("parent_asin"))
    attrs = parse_variant_attributes(payload.get("variant_attributes"))
    theme = None
    if attrs:
        # 排除无键槽 ""（回退形态的裸值），它不是维度名
        keys = [k for k in attrs if k]
        theme = "/".join(keys) or None
    if parent is None and theme is None:
        return None
    return {"parent_asin": parent, "theme": theme}


def _to_record(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = row.get("payload") or {}
    if isinstance(payload, str):          # asyncpg 在某些配置下回字符串
        import json
        try:
            payload = json.loads(payload)
        except Exception:                 # noqa: BLE001
            payload = {}

    # P4.5：此前这里是契约 §4.3 判据的**第二份手工实现**（重算同一个合取式）。
    # 判据改一次要改两处，漏一处就是 /api/export/incremental.completeness_ok 与
    # /api/v1/sync/records.completeness_ok **对同一行给出不同答案** ——
    # 两个消费者、两个结论，而且没有任何一侧会报错。现在两处走同一个函数对象
    # （server/api/sync.py:_completeness_ok is completeness_ok 为真）。
    complete = completeness_ok(row.get("completeness"))

    return {
        # ---- 契约必填 ----
        "source_id": row["source_id"],
        "cursor": row["seq"],
        "marketplace": DEST_MARKETPLACE,
        "asin": row["asin"],
        "scraped_at": _iso_seconds(row.get("collected_at")),
        "scrape_params": {
            # 契约 v1 的键名是 `zipcode`（不是 zip / zip_code）。
            "zipcode": row.get("zip_requested"),
            # 以下是采集侧附加的采集参数，契约说的是「影响结果的**全部**采集参数」，
            # 所以它们该在这里：邮编是否真的生效直接决定这条价格属不属于该邮编分组。
            "zip_observed": row.get("zip_observed"),
            "zip_verify": row.get("zip_verify"),
            # 采集来源站点。与顶层 marketplace（上架目的地）是两个概念，见文件头。
            "source_marketplace": row.get("marketplace"),
            "parse_engine": row.get("parse_engine"),
        },
        "slow": {
            # ---- 契约必填 ----
            "title": _clean(payload.get("title")),
            "brand": _clean(payload.get("brand")),
            "category_path": _category_path(payload),
            "images": _split_list("image_urls", payload.get("image_urls")),
            # ---- 契约可选，采集侧有就给 ----
            "bullet_points": _split_list("bullet_points", payload.get("bullet_points")),
            "description": _clean(payload.get("long_description")),
            "weight": {
                "package": _clean(payload.get("package_weight")),
                "item": _clean(payload.get("item_weight")),
            },
            "dimensions": {
                "package": _clean(payload.get("package_dimensions")),
                "item": _clean(payload.get("item_dimensions")),
            },
            "variant": _variant(payload),
        },
        "fast": {
            # ---- 契约必填 ----
            "price": _price(payload.get("current_price")),
            "currency": DEFAULT_CURRENCY,
            "stock_state": _stock_state(payload),
            # ---- 契约可选 ----
            # stock_count / delivery_days 是**追加**字段（契约 §3.2 允许单方面
            # 加字段，因此仍是 v1）。数据本来就在事件体里、只是没往外给过，
            # 所以**老事件也会拿到**，不需要回填。
            #
            # delivery_days 的源字段叫 delivery_time（采集侧存的是天数，
            # 与 delivery_date 那个人读日期是两个字段）。对外用 _days 命名是
            # 因为 "time" 读起来像时刻而不是时长。
            "stock_count": _int(payload.get("stock_count")),
            "delivery_days": _int(payload.get("delivery_time")),
            # shipping / shipping_raw 同为**追加**字段（契约 §3.2 允许单方面加
            # 字段，仍是 v1）。数据本来就在事件体里、只是以前只能从 `raw` 里翻，
            # 所以**老事件也会拿到**，不需要回填。
            #
            # 为什么给两个而不是一个：采集侧存的是字符串（"FREE" / "N/A" /
            # "$5.99"），把它压成一个数值会丢掉「凭什么是这个数」。
            #   shipping     -> 可直接参与落地价计算，FREE 是 0.0、没采到是 null
            #   shipping_raw -> 原始串原样，消费侧可复核，也留住将来出现的新形态
            #                   （比如满额免邮的门槛信息）而不必等采集侧改契约
            "shipping": _shipping(payload.get("buybox_shipping")),
            "shipping_raw": _clean(payload.get("buybox_shipping")),
            "buybox_price": _price(payload.get("buybox_price")),
            "buybox_seller": _clean(payload.get("seller_name")),
            "buybox_seller_id": _clean(payload.get("seller_id")),
            # 采集侧**不采**优惠券与秒杀，恒为 null。给出键是为了让「没这个字段」
            # 和「这次没采到」不被混淆——真要用需要先在采集侧加抓取。
            "coupon": None,
            "deal": None,
        },
        # ---- 契约「建议带」 ----
        # 契约描述的是「字段排序后 sha256 前 16 位」。这里给的是 16 位十六进制，
        # **但算法是采集侧那一套**（common/slowhash.py：NFKC + 空白折叠 + 哨兵值
        # 全等归一 + 列表排序 + 图片 URL 归约到 image ID + 排序键 JSON + SHA-256）。
        # ⚠ **当作不透明值用，不要自己按 `slow` 对象重算再比对**——两边算法不同，
        # 必然不等。它保证的是「同一个页面跨进程、跨引擎稳定；慢变字段真变了才变」。
        "slow_hash": _short_hash(row.get("slow_hash")),

        # ---- 契约「可选」：裁剪后的原始载荷，沃尔玛侧存 jsonb 备查 ----
        "raw": _raw(payload),

        # ---- 采集侧附加，契约未要求，收着无害 ----
        # outcome != 'ok' 的记录**只进 snapshots，不要 upsert products**。
        # 这类记录的 slow/fast 基本为空，那是「本次没采到」不是「值变了」。
        "outcome": row.get("outcome"),
        "completeness_ok": complete,
        "review_hash": _short_hash(row.get("review_hash")),
        "recorded_at": _iso_seconds(row.get("recorded_at")),
    }


# ============================================================ 鉴权

_warned_anonymous = False


def _check_token(token: Optional[str]) -> Optional[JSONResponse]:
    """契约 v1：鉴权是**可选**的（「建议加上，服务器是公网 IP」）。

    所以语义是：
      * 配了 ``EXPORT_TOKEN``  -> 强制校验，不匹配/缺失即 401
      * 没配                   -> 放行，但每次都打 WARNING

    我最初实现的是 fail closed（没配就 503）。**契约是权威，这里服从契约** ——
    否则对方按契约部署、不配 token，会得到一个它没预期的 503。
    但「整个商品库挂在公网上、零鉴权」不该是悄无声息的，所以放行这条路径
    一直响；要回到 fail closed 设 ``EXPORT_REQUIRE_TOKEN=1``。
    """
    global _warned_anonymous
    expected = os.environ.get("EXPORT_TOKEN", "").strip()

    if not expected:
        if os.environ.get("EXPORT_REQUIRE_TOKEN", "").strip() in ("1", "true", "yes"):
            return _sync._err(503, "export_token_not_configured",
                              "EXPORT_REQUIRE_TOKEN=1 但服务端未配置 EXPORT_TOKEN。")
        if not _warned_anonymous:
            logger.warning(
                "⚠ /api/export/incremental 正在【无鉴权】提供服务："
                "未配置 EXPORT_TOKEN。服务器是公网 IP，这等于把整个商品库"
                "敞在互联网上。设 EXPORT_TOKEN 开启校验，或设 "
                "EXPORT_REQUIRE_TOKEN=1 让缺失配置直接关闭该端点。")
            _warned_anonymous = True
        return None

    if not token or not hmac.compare_digest(token, expected):
        return _sync._err(401, "invalid_export_token",
                          "X-Export-Token 缺失或不匹配。")
    return None


# ============================================================ 端点

@router.get("/api/export/incremental")
async def export_incremental(
    cursor: int = Query(0, ge=0, description="独占下界，返回 cursor 大于它的记录；从头拉传 0"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    x_export_token: Optional[str] = Header(None, alias="X-Export-Token"),
):
    denied = _check_token(x_export_token)
    if denied is not None:
        return denied

    unavailable = _sync._unavailable()
    if unavailable is not None:
        return unavailable

    if cursor > _sync.MAX_BIGINT:
        return _sync._err(422, "invalid_parameter",
                          f"cursor 超出 bigint 上限：{cursor}")

    try:
        async with _sync._snapshot() as conn:
            meta = await _sync._read_meta(conn)
            min_raw, max_raw = await _sync._bounds(conn)
            rows = await conn.fetch(
                f"SELECT {_sync._RECORD_SELECT} FROM scraper.scrape_events "
                "WHERE seq > $1 ORDER BY seq LIMIT $2",
                cursor, limit + 1)
            # 快照内复核下界（保守方向）：宁可多报一次 409，也不能漏。
            min_after, max_after = await _sync._bounds(conn)
    except _sync._PoolUnavailable:
        return _sync._err(503, "event_stream_unavailable", "连接池尚未就绪。")
    except Exception as exc:                                   # noqa: BLE001
        if _sync._schema_missing(exc):
            return _sync._err(503, "event_stream_unavailable",
                              f"事件流表还没建好: {type(exc).__name__}")
        raise

    # ⚠ 两次 _window **各自算完再比**，绝不能先把两个 min 压成一个数再传进去。
    # 这里曾经写成 `max(_as_int(min_raw) or 0, _as_int(min_after) or 0)`：空表时
    # `min(seq)` 是 NULL -> None -> `or 0` -> **0**，而 _window 判空靠的正是
    # `min_seq is None`，于是它走进「非空」分支返回 min_available=0，
    # 而 `cursor + 1 < 0` 永假 —— **409 永远发不出来**。
    #
    # 真机实测（TRUNCATE 掉事件流之后）：/api/v1/sync/status 正确报
    # min_available_seq=676 / max_seq=675，同一时刻 /api/export/incremental?cursor=0
    # 却回 200 空页。一个丢了全部数据的消费者会就此永远等下去，两侧都不告警 ——
    # 正是 _window 的 docstring 点名要防的那一种。
    # `/api/v1/sync/records` 一直是对的（sync.py:512），照它的写法对齐。
    ever = _sync._as_int(meta.get("max_seq_ever"))
    min_available, max_seq = _sync._window(min_raw, max_raw, ever)
    min_available_after, max_seq_after = _sync._window(min_after, max_after, ever)
    if min_available_after > min_available:
        min_available = min_available_after
        max_seq = max(max_seq, max_seq_after)

    # 游标掉出保留窗口 —— 契约 v1 没写这一种，但静默跳过是不可接受的。
    # 见交付说明假设清单第 6 条：需要写进 v1.1。
    if cursor + 1 < min_available:
        return _sync._err(
            409, "cursor_below_retention",
            "你要的下一条已被保留期裁掉。请告警并做一次全量对账。",
            cursor=cursor, min_available_cursor=min_available, max_cursor=max_seq)

    has_more = len(rows) > limit
    rows = rows[:limit]
    records = [_to_record(dict(r)) for r in rows]
    # 游标只推进到**真正投递过的那一条**。空页不推进 —— 这是唯一不丢数据的方向。
    next_cursor = records[-1]["cursor"] if records else cursor

    return JSONResponse(content={
        "contract_version": CONTRACT_VERSION,
        "records": records,
        "next_cursor": next_cursor,
        "has_more": has_more,
    })


# ============================================================ 按批次取记录

#: 单次分页上限。与全量流用同一个 MAX_LIMIT —— 两个端点回同一种 record，
#: 单页体量该由 record 大小决定，而不是由"哪个 URL"决定。
BATCH_DEFAULT_LIMIT = DEFAULT_LIMIT


@router.get("/api/export/batch/{batch_name}/records")
async def export_batch_records(
    batch_name: str,
    cursor: int = Query(0, ge=0, description="独占下界，返回 cursor 大于它的记录；从头拉传 0"),
    limit: int = Query(BATCH_DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    x_export_token: Optional[str] = Header(None, alias="X-Export-Token"),
):
    """**这一批次真正采到了什么** —— 逐次采集事件，不是"最新态"。

    ------------------------------------------------------------------
    为什么需要这个端点（它补的是一个会静默给出陈旧数据的洞）
    ------------------------------------------------------------------
    此前"按批次拿数据"只有两条路，都读 ``asin_data``：

        SELECT d.* FROM asin_data d
        JOIN batch_asins ba ON ba.asin = d.asin AND ba.batch_id = ?

    ``asin_data`` 是**每个 ASIN 一行的最新态**，``batch_id`` 只回答"属不属于
    这批"，**不参与取哪一行**。于是这批采失败的 ASIN——只要它以前采过——
    照样命中 JOIN，返回**上一次的旧行**：

      * ``/api/results?batch_id=`` 只 ``SELECT d.*``，**不带任何本次任务状态**，
        消费侧拿到的是一行看不出年龄的商品数据。摄进自己的库、盖上一个新鲜的
        接收时间，陈旧数据就此看起来很新鲜 —— 而且两侧都不会报错。
      * CSV/xlsx 批次导出有防护（``server/api/export.py:174`` 的
        ``data_source`` 列会写"历史产品库数据，本次未更新"），但那是文件出口，
        脚本消费不了。

    本端点从**事件流**读，语义上就没有这个洞：``scraper.scrape_events`` 的
    每一行都是**一次真实发生过的采集**，没采成就没有行。不存在"旧行冒充新行"。

    ------------------------------------------------------------------
    与 ``/api/export/incremental`` 的关系
    ------------------------------------------------------------------
    同一张表、同一套快照纪律、**同一个 ``_to_record``**（刻意共用：两个端点对
    同一行必须逐字段给出同一个答案；再抄一份就是本仓库 V2/V4 那个老毛病）。
    差别只在取哪些行、以及游标的定义域：

      * ``/incremental``：``WHERE seq > $1``，游标是**全局**的，用于持续同步。
      * 本端点：``WHERE batch_id = $1 AND seq > $2``，游标只在**这个批次内部**
        有意义。**两个游标不可互换**，别拿本端点的 ``next_cursor`` 去喂
        ``/incremental``（数值同源于 ``seq``，喂进去不会报错，会**静默跳过**
        中间所有别的批次的事件）。

    所以本端点不是增量同步的替代品，它回答的是另一个问题：
    "我刚推的这一批，到底采到了什么。"

    ------------------------------------------------------------------
    覆盖率自查（``coverage``）
    ------------------------------------------------------------------
    响应里带 ``coverage``，让调用方**不必再发一次请求**就能判断这批齐没齐：

        asin_total       这批入过队的 ASIN 数（``batch_asins``，含失败的）
        asin_with_event  本次分页游标走到此处为止、出现过事件的去重 ASIN 数

    ``asin_with_event`` 是**整批**的口径（一次聚合查询算的，与分页无关），
    不是当前这一页的。两者相等 ⇒ 每个 ASIN 至少有过一次采集事件；
    不等 ⇒ 差额那些 ASIN 一次都没采成（或事件已被保留期裁掉，见下）。

    ⚠ ``asin_total == asin_with_event`` **不等于"这批都成功了"**：一个
    ``outcome='not_found'`` 的事件也算"有事件"。要判成功看每条记录的
    ``outcome``。

    ------------------------------------------------------------------
    保留期
    ------------------------------------------------------------------
    事件流有保留期，老批次的事件会被裁掉。本端点**不**回 409
    （那是全量流的语义：游标掉出窗口 ⇒ 数据丢了 ⇒ 全量对账），而是照实回
    200 + 一个可能不完整的集合，并把 ``retention_min_cursor`` 一并给出。
    判据交给调用方：``coverage`` 对不上且批次早于保留窗口 ⇒ 被裁过。

    永不用 404 表达"这批没有数据"——批次存在但一条事件都没有，回
    ``200 + records: []``。404 **只**表示批次名不存在（见文件头第 3 条：
    404 会被读成"暂无数据"而静默停摆，所以它必须只有一个含义）。
    """
    denied = _check_token(x_export_token)
    if denied is not None:
        return denied

    unavailable = _sync._unavailable()
    if unavailable is not None:
        return unavailable

    if cursor > _sync.MAX_BIGINT:
        return _sync._err(422, "invalid_parameter",
                          f"cursor 超出 bigint 上限：{cursor}")

    batch = await _sync._database().get_batch_by_name(batch_name)
    if not batch:
        return _sync._err(404, "batch_not_found", f"批次不存在: {batch_name}",
                          batch_name=batch_name)
    batch_id = int(batch["id"])

    try:
        async with _sync._snapshot() as conn:
            meta = await _sync._read_meta(conn)
            min_raw, max_raw = await _sync._bounds(conn)
            rows = await conn.fetch(
                f"SELECT {_sync._RECORD_SELECT} FROM scraper.scrape_events "
                "WHERE batch_id = $1 AND seq > $2 ORDER BY seq LIMIT $3",
                batch_id, cursor, limit + 1)
            # 覆盖率的两个数**必须在同一个快照里算**，否则它们来自两个时刻，
            # 而 asin_with_event > asin_total 这种不可能的组合会出现在一个
            # 被当作告警判据的字段上。
            #
            # 所以分母这里直接查 batch_asins，而不是走 db.get_batch_asin_set()
            # ——后者会另借一条连接、另开一个时刻。表名不带 schema 是对的：
            # 连接池把 search_path 钉死成 public（common/pgdb/pool.py:830），
            # 业务表都在那儿；事件流才需要 scraper. 前缀。
            #
            # batch_asins 记的是"这一批**入过队**的 ASIN"，任务失败与否它都在
            # （common/pgdb/results_read.py:290）。正因如此它才是覆盖率的分母。
            asin_total = await conn.fetchval(
                "SELECT count(DISTINCT asin) FROM batch_asins WHERE batch_id = $1",
                batch_id)
            asin_with_event = await conn.fetchval(
                "SELECT count(DISTINCT asin) FROM scraper.scrape_events "
                "WHERE batch_id = $1", batch_id)
    except _sync._PoolUnavailable:
        return _sync._err(503, "event_stream_unavailable", "连接池尚未就绪。")
    except Exception as exc:                                   # noqa: BLE001
        if _sync._schema_missing(exc):
            return _sync._err(503, "event_stream_unavailable",
                              f"事件流表还没建好: {type(exc).__name__}")
        raise

    min_available, max_seq = _sync._window(
        min_raw, max_raw, _sync._as_int(meta.get("max_seq_ever")))

    has_more = len(rows) > limit
    rows = rows[:limit]
    records = [_to_record(dict(r)) for r in rows]
    # 与全量流同一条纪律：游标只推进到**真正投递过的那一条**，空页不推进。
    next_cursor = records[-1]["cursor"] if records else cursor

    return JSONResponse(content={
        "contract_version": CONTRACT_VERSION,
        "batch": {
            "id": batch_id,
            "name": batch.get("name"),
            "status": batch.get("status"),
            "created_at": batch.get("created_at"),
        },
        "coverage": {
            "asin_total": int(asin_total or 0),
            "asin_with_event": int(asin_with_event or 0),
        },
        "records": records,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "retention_min_cursor": min_available,
        "max_cursor": max_seq,
    })
