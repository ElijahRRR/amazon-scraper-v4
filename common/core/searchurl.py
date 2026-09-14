"""关键词搜索的参数规范化 + URL 构造（F-010）—— server 与 worker 的**唯一真源**。

------------------------------------------------------------------------
为什么单独一个模块
------------------------------------------------------------------------
关键词采集有两个地方要理解同一组参数：

* **server** 建批次时要校验（价格区间是否合法、配送方式是否受支持），
  非法就当场 400，而不是让 worker 跑到一半才发现；
* **worker** 翻页时要把它们拼成真正的 Amazon URL。

这两件事必须由**同一份**规则驱动。分叉的后果不是报错，而是一次静默的
"筛选条件没生效"：server 收了 ``delivery="prime"``、worker 拼 URL 时把它
丢了，于是批次名、进度、发现数全部正常，只有数据是错的 —— 采回来的是
全量结果而不是 Prime 结果，而没有任何一侧会响。

放在 ``common/core/`` 是照本包既定的分工：这里只放**后端无关的纯逻辑**，
不 import ``common.database`` / ``common.pgdb`` / ``worker.*``（本模块只依赖
标准库）。用法与 ``error_types`` / ``zipcode`` 一致：

    from common.core import searchurl

------------------------------------------------------------------------
⚠ 精细化（refinement）节点 ID 是 **Amazon 侧**的，会变
------------------------------------------------------------------------
``rh=`` 里那些 ``p_85:2470955011`` 之类的 ID 不是本项目能控制的东西：
它们按**站点**不同，而且 Amazon 改版时会变。所以这里的态度是：

1. 只硬编码有把握的 ``www.amazon.com`` 一套，并**逐条注明含义**；
2. 站点没有对应表时 **拒绝** ``delivery=``（400），而不是"随便挑一个"
   或"悄悄忽略"—— 后两种都会得到一批看着正常、实则没筛过的数据；
3. 留一条逃生口 ``rh_extra=``：调用方自己贴 refinement 串，原样拼进 ``rh``。
   任何本模块没预置的筛选（品牌、类目、折扣、评分…）都走它；
4. 允许用环境变量 ``SEARCH_DELIVERY_FILTERS`` 覆盖/扩充这张表（JSON），
   ID 变了不必改代码、更不必等发版。

价格区间同理但要稳得多：``p_36`` 与 ``low-price`` / ``high-price`` 这套
是全站通用的老接口，各站点一致。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

#: 支持的站点域名。限定白名单是防注入：``domain`` 会直接拼进 URL，
#: 不校验的话调用方能把 worker 的请求指到任意主机去。
SUPPORTED_DOMAINS: Tuple[str, ...] = (
    "www.amazon.com",
    "www.amazon.co.uk",
    "www.amazon.de",
    "www.amazon.fr",
    "www.amazon.it",
    "www.amazon.es",
    "www.amazon.ca",
    "www.amazon.co.jp",
    "www.amazon.com.au",
    "www.amazon.com.mx",
    "www.amazon.in",
    "www.amazon.nl",
    "www.amazon.se",
    "www.amazon.pl",
    "www.amazon.com.br",
    "www.amazon.ae",
    "www.amazon.sa",
    "www.amazon.sg",
)

DEFAULT_DOMAIN = "www.amazon.com"

#: Amazon 搜索结果最多 7 页（每页 ~48/16 条，取决于布局）之后基本是重复与
#: 低相关结果；再往后翻 Amazon 自己也会截断。上限设 20 是留出余量，
#: **不是**说翻到 20 页一定有东西。
MAX_PAGES_CAP = 20
DEFAULT_MAX_PAGES = 7

#: 配送/履约方式 -> refinement 串。key 是对外的稳定名字，值按站点分。
#:
#: 逐条含义（www.amazon.com）：
#:   prime          p_85:2470955011   Prime 资格（"Prime Eligible" 侧栏勾选项）
#:   free_shipping  p_76:1            Free Shipping by Amazon
#:   sold_by_amazon p_6:ATVPDKIKX0DER 卖家 = Amazon.com 自营（p_6 是 merchant 面）
#:   get_it_today   p_90:8308921011   Get It Today（当日达）
#:
#: ⚠ ``p_6`` 后面那串是**卖家 ID**，不是节点 ID —— 它就是 Amazon.com 自营号，
#:   全站通用。要筛任意三方卖家的话用 ``rh_extra="p_6:<sellerid>"``，
#:   不过那种需求走卖家店铺采集（F-009）更直接。
_BUILTIN_DELIVERY_FILTERS: Dict[str, Dict[str, str]] = {
    "www.amazon.com": {
        "prime": "p_85:2470955011",
        "free_shipping": "p_76:1",
        "sold_by_amazon": "p_6:ATVPDKIKX0DER",
        "get_it_today": "p_90:8308921011",
    },
    # 其余站点故意留空：没有可靠对照就不猜。配 SEARCH_DELIVERY_FILTERS 补，
    # 或直接用 rh_extra。
}


def _load_overrides() -> Dict[str, Dict[str, str]]:
    """读环境变量 ``SEARCH_DELIVERY_FILTERS``（JSON）覆盖/扩充内置表。

    形如 ``{"www.amazon.de": {"prime": "p_85:xxxxxx"}}``。**按站点浅合并**：
    同一站点内 key 逐个覆盖，没提到的站点原样保留。

    坏 JSON 不抛异常 —— 这个模块会在 import 期被 server 与 worker 同时拉起来，
    一个手滑的环境变量不该让整个进程起不来。坏值当作"没配"。
    """
    raw = os.environ.get("SEARCH_DELIVERY_FILTERS", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for domain, table in parsed.items():
        if isinstance(table, dict):
            out[str(domain)] = {str(k): str(v) for k, v in table.items() if v}
    return out


def delivery_filters(domain: str) -> Dict[str, str]:
    """某站点当前可用的 ``delivery`` 取值 -> refinement 串。"""
    merged = dict(_BUILTIN_DELIVERY_FILTERS.get(domain, {}))
    merged.update(_load_overrides().get(domain, {}))
    return merged


def delivery_choices(domain: str = DEFAULT_DOMAIN) -> List[str]:
    """某站点当前可用的 ``delivery`` 取值（排序后，供报错信息与 UI 用）。"""
    return sorted(delivery_filters(domain).keys())


#: 排序方式 -> Amazon 的 ``s=`` 取值。这套 key 跨站点通用。
SORT_CHOICES: Dict[str, str] = {
    "relevance": "relevanceblender",
    "price_asc": "price-asc-rank",
    "price_desc": "price-desc-rank",
    "newest": "date-desc-rank",
    "review_rank": "review-rank",
    "featured": "featured-rank",
}

#: ``rh_extra`` 的白名单字符集。refinement 串长这样：``p_36:1000-5000,p_85:2470955011``。
#: 收紧到 ``[A-Za-z0-9_:,\-|]`` 是因为它会**原样**拼进 URL —— 放开 ``&`` / ``?`` /
#: 空格就等于允许调用方往 URL 里塞任意查询参数。
_RH_EXTRA_RE = re.compile(r"^[A-Za-z0-9_:,\-|]{1,300}$")

#: 关键词长度上限。它同时是 ``search_discoveries`` 主键的一部分，
#: 无上限的话一条超长关键词能顶爆 PG 的 B-tree 行宽（~2704 字节）。
MAX_KEYWORD_LEN = 200


def normalize_keyword(value: Any) -> Optional[str]:
    """关键词归一化：去首尾空白、压缩内部连续空白、截断到上限。

    返回 ``None`` 表示这条不可用（空 / 全是空白）。**不做大小写转换** ——
    Amazon 搜索本身不区分大小写，但把用户输入的关键词改掉会让
    ``search_discoveries`` 里的行与用户提交的内容对不上号。
    """
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value)).strip()
    if not s:
        return None
    return s[:MAX_KEYWORD_LEN]


def _normalize_price(value: Any, field: str) -> Optional[float]:
    """价格归一化。``None`` / 空串 = 不限；负数或非数字 -> ValueError。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} 不是合法数字: {value!r}")
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf
        raise ValueError(f"{field} 不是有限数字: {value!r}")
    if f < 0:
        raise ValueError(f"{field} 不能为负: {f}")
    if f > 1_000_000:
        raise ValueError(f"{field} 过大（上限 1000000）: {f}")
    return f


def normalize_search_params(raw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """校验 + 归一化一组搜索筛选参数，返回**可直接落进 task_meta 的 dict**。

    非法输入一律 ``ValueError``（server 侧转 400）。返回的 dict 键是固定的
    ——落库之后 worker 直接按键取，多一个少一个都会在 worker 侧变成
    ``KeyError`` 或"筛选静默丢失"，所以这里不做 "有才放" 的稀疏结构。

    Raises:
        ValueError: 任何一项不合法。
    """
    raw = raw or {}

    domain = (raw.get("domain") or DEFAULT_DOMAIN).strip().lower()
    if domain.startswith("http://") or domain.startswith("https://"):
        domain = domain.split("://", 1)[1]
    domain = domain.split("/", 1)[0]
    if domain not in SUPPORTED_DOMAINS:
        raise ValueError(
            f"不支持的站点: {domain}；可选: {', '.join(SUPPORTED_DOMAINS)}"
        )

    min_price = _normalize_price(raw.get("min_price"), "min_price")
    max_price = _normalize_price(raw.get("max_price"), "max_price")
    if min_price is not None and max_price is not None and min_price > max_price:
        raise ValueError(f"min_price({min_price}) 不能大于 max_price({max_price})")

    delivery = raw.get("delivery")
    delivery = (str(delivery).strip().lower() or None) if delivery else None
    if delivery:
        table = delivery_filters(domain)
        if delivery not in table:
            choices = ", ".join(sorted(table)) or "（该站点未配置，请用 rh_extra）"
            raise ValueError(
                f"{domain} 不支持 delivery={delivery!r}；可选: {choices}。"
                "自定义筛选请用 rh_extra（原样拼进 rh=）"
            )

    sort = raw.get("sort")
    sort = (str(sort).strip().lower() or None) if sort else None
    if sort and sort not in SORT_CHOICES:
        raise ValueError(
            f"不支持的 sort={sort!r}；可选: {', '.join(sorted(SORT_CHOICES))}"
        )

    rh_extra = raw.get("rh_extra")
    rh_extra = (str(rh_extra).strip() or None) if rh_extra else None
    if rh_extra and not _RH_EXTRA_RE.match(rh_extra):
        raise ValueError(
            "rh_extra 只允许 refinement 串（字母/数字/下划线/冒号/逗号/连字符/竖线），"
            f"最长 300 字符，收到: {rh_extra[:80]!r}"
        )

    max_pages = raw.get("max_pages")
    if max_pages is None or (isinstance(max_pages, str) and not max_pages.strip()):
        max_pages = DEFAULT_MAX_PAGES
    try:
        max_pages = int(max_pages)
    except (TypeError, ValueError):
        raise ValueError(f"max_pages 不是整数: {raw.get('max_pages')!r}")
    if max_pages < 1:
        raise ValueError(f"max_pages 至少为 1，收到 {max_pages}")
    if max_pages > MAX_PAGES_CAP:
        raise ValueError(f"max_pages 上限 {MAX_PAGES_CAP}，收到 {max_pages}")

    return {
        "domain": domain,
        "min_price": min_price,
        "max_price": max_price,
        "delivery": delivery,
        "sort": sort,
        "rh_extra": rh_extra,
        "max_pages": max_pages,
        "include_sponsored": bool(raw.get("include_sponsored", False)),
    }


def _price_refinement(min_price: Optional[float],
                      max_price: Optional[float]) -> Optional[str]:
    """价格区间 -> ``p_36:<分>-<分>``。两端都为 None 时返回 None。

    Amazon 的 ``p_36`` 单位是**分**（美分/便士/…），开区间写法是把一端留空：
    ``p_36:1000-``（≥$10）/ ``p_36:-5000``（≤$50）。
    """
    if min_price is None and max_price is None:
        return None
    lo = "" if min_price is None else str(int(round(min_price * 100)))
    hi = "" if max_price is None else str(int(round(max_price * 100)))
    return f"p_36:{lo}-{hi}"


def build_search_url(keyword: str, page: int = 1,
                     params: Optional[Dict[str, Any]] = None) -> str:
    """把关键词 + 一组**已归一化**的参数拼成 Amazon 搜索页 URL。

    ``params`` 必须是 :func:`normalize_search_params` 的输出（缺键按缺省处理，
    这样 worker 拿到旧版本写下的 task_meta 也不会 KeyError）。

    价格同时发 ``rh=p_36:...`` 与 ``low-price`` / ``high-price``：
    前者是侧栏筛选真正认的那个，后者是价格输入框提交的那对。两个都带上是
    因为 Amazon 在不同布局下认的不是同一个 —— 只发一个的话会出现"某些
    请求筛了、某些没筛"，而结果页看起来毫无区别。
    """
    p = params or {}
    domain = p.get("domain") or DEFAULT_DOMAIN

    query: List[str] = [f"k={quote_plus(keyword)}"]

    rh_parts: List[str] = []
    price_rh = _price_refinement(p.get("min_price"), p.get("max_price"))
    if price_rh:
        rh_parts.append(price_rh)
    delivery = p.get("delivery")
    if delivery:
        ref = delivery_filters(domain).get(delivery)
        if ref:
            rh_parts.append(ref)
    if p.get("rh_extra"):
        rh_parts.append(str(p["rh_extra"]))
    if rh_parts:
        query.append("rh=" + quote_plus(",".join(rh_parts)))

    if p.get("min_price") is not None:
        query.append(f"low-price={p['min_price']:g}")
    if p.get("max_price") is not None:
        query.append(f"high-price={p['max_price']:g}")

    sort = p.get("sort")
    if sort and sort in SORT_CHOICES:
        query.append(f"s={SORT_CHOICES[sort]}")

    if page and int(page) > 1:
        query.append(f"page={int(page)}")
        # Amazon 在第 2 页起要求 ref_ 才稳定返回下一页布局；缺它偶发回到第 1 页。
        query.append(f"ref=sr_pg_{int(page)}")

    return f"https://{domain}/s?" + "&".join(query)


__all__ = [
    "SUPPORTED_DOMAINS",
    "DEFAULT_DOMAIN",
    "MAX_PAGES_CAP",
    "DEFAULT_MAX_PAGES",
    "MAX_KEYWORD_LEN",
    "SORT_CHOICES",
    "delivery_filters",
    "delivery_choices",
    "normalize_keyword",
    "normalize_search_params",
    "build_search_url",
]
