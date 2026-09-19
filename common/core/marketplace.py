"""common/core/marketplace.py —— 「站点」这件事的唯一真源（F-012 多站点采集）。

------------------------------------------------------------------------
为什么需要它
------------------------------------------------------------------------
在本模块之前，「站点」在仓库里是**一个进程级常量加一堆散落的字面量**：

* ``worker/session.py:AMAZON_BASE`` —— 类常量 ``https://www.amazon.com``，
  详情页 / 卖家页 / AOD ajax / captcha 全部从它拼；
* ``server/app.py:_US_ZIP_RE`` —— ``^\\d{5}\\Z``，美国邮编形状写死在校验里；
* ``worker/ziputil.py:_NON_US_CURRENCY`` —— 「非美国区」的货币标识清单；
* ``worker/parser.py`` —— 价格串必须含 ``$``，JSON-LD 只收 ``priceCurrency == "USD"``；
* ``common/core/searchurl.py:SUPPORTED_DOMAINS`` —— 18 个站点的白名单。

只有最后一条是**真·多站点**的：关键词搜索能翻 ``www.amazon.ca`` 的结果页。
其余全部钉死在美国站。这两者拼在一起会产生一条**静默错误**：用
``domain=www.amazon.ca`` 建的关键词批次，发现阶段走加拿大站，而
``discover_mode=with_detail`` 派生出来的详情任务没有任何站点字段
（``common/pgdb/media.py`` 的 INSERT 只有 batch_id/asin/zip_code/
needs_screenshot/task_type），于是详情全部去 ``amazon.com`` 抓 ——
批次名正常、进度正常、发现数正常，**只有数据是另一个国家的**，
两侧都不会响。

本模块把「站点」从常量变成**一等数据**：一张注册表，一个 ``MarketplaceSpec``
描述一个站点的全部差异（base URL、货币、邮编规则、locale 头）。
server 与 worker 都从这里取，谁也不许再自己拼。

------------------------------------------------------------------------
键用 ``amazon.ca`` 而不是 ``www.amazon.ca``
------------------------------------------------------------------------
规范键（``MarketplaceSpec.id``）取**不带 www 的域名**，因为库里已经有三处
用的是这个形状，它们是既成事实、改不动：

* ``scrape_events.marketplace`` 的封闭集 ``{'amazon.com'}``
  （``common/pgdb/schema.py:EVENT_MARKETPLACES``）；
* ``asin_data.site`` 的列默认值 ``'amazon.com'``（两套 DDL 都是）；
* ``common/models.py:AsinData.site`` 的 dataclass 默认值。

``searchurl`` 那边用的是**完整主机名**（``www.amazon.ca``），因为它要直接拼
URL。两种形状都留着，用 ``host`` 字段连起来。

两张表**不合并**，因为它们描述的是两种不同的能力：

* ``searchurl.SUPPORTED_DOMAINS``（18 个）—— 「能翻这个站点的搜索结果页」。
  翻页只需要一个主机名，所以门槛低。
* 本模块的注册表（当前 2 个）—— 「能完整采集这个站点的商品详情」。
  它要求邮编规则、货币口径、locale 头全都有可靠答案，所以门槛高。

合并会逼着二选一：要么给 16 个没有详情规则的站点编出假的邮编规则，
要么把已经能用的搜索能力砍掉 16 个站点。两个都是坏交易。

代价是这两张表可能分叉（注册表里写了一个 ``searchurl`` 不认的主机名，
于是关键词批次建不出来、而详情任务能建）。这条由
``assert_registry_within_search_domains()`` 挡住，测试里会调它。

------------------------------------------------------------------------
⚠ 货币不是从符号认出来的，是站点决定的
------------------------------------------------------------------------
这是本模块**最要紧**的一条，写在最显眼的地方免得被绕过：

    美国站和加拿大站的价格**都渲染成** ``$24.99``。

``worker/parser.py`` 现在的判定是 ``if p and "$" in p: return p`` ——
拿一个加拿大站页面喂进去，它会当作美元原样收下：**数字对、币种错、
没有任何标记**。这比解析失败坏得多，因为解析失败会被 completeness 捕获，
而"币种记错"在下游看起来是一条完好的记录。

所以本模块的口径是：

* ``currency`` —— 这个站点的价格**应该**是什么币种。它由站点决定，
  与页面上出现什么符号无关。
* ``price_symbols`` —— 只用来**定位**价格串（"这串文字是不是一个价格"），
  **不用来判定币种**。
* ``foreign_currency_markers`` —— 出现即说明「页面不是这个站点的本地语境」
  （串区了 / 代理落在别的国家 / 被重定向）。这是 ``ziputil`` 那条
  ``_NON_US_CURRENCY`` 检查的泛化。

------------------------------------------------------------------------
⚠ amazon.com 的每一个值都与改造前逐字节相同
------------------------------------------------------------------------
本模块是**加维度**，不是**改行为**。``amazon.com`` 那条记录里的每个字段都
照抄改造前散落在各处的字面量：``_NON_US_CURRENCY`` 原样搬成它的
``foreign_currency_markers``（**没有**顺手补 ``CDN$`` —— 那会改变既有判定，
而既有美国站数据的正确性不是这次要动的东西），``^\\d{5}\\Z`` 原样搬成它的
``postal_pattern``，``10001`` 原样搬成 ``default_postal``。

推论：任何一条「美国站行为变了」的测试失败都不是可接受的代价，是 bug。

本模块只依赖标准库（``re`` / ``typing`` / ``dataclasses``），worker 侧
import 它是安全的 —— 与同包的 ``searchurl`` / ``zipcode`` 同规格。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Optional, Pattern, Tuple

from common.core.zipcode import _zfill_short_numeric

#: 规范键的兜底值。**不要**把它写成 ``"US"``：那是 ``parser._default_result``
#: 里 ``site`` 的值，与本模块的键不是一个值域（见模块 docstring 第二节，
#: 以及 ``worker/parser.py`` 里 P4-8 那段「三处不一致」的说明）。
DEFAULT_MARKETPLACE = "amazon.com"


@dataclass(frozen=True)
class MarketplaceSpec:
    """一个 Amazon 站点的全部站点相关差异。

    ``frozen=True`` 是有意的：注册表是**只读**的全局数据，
    worker 的并发任务会同时持有同一个 spec 实例。可变的话，
    一处任务改了字段，另一处任务的 base URL 就跟着变了。
    """

    #: 规范键，不带 www 的域名（``amazon.ca``）。与 ``scrape_events.marketplace`` 同值域。
    id: str
    #: 完整主机名（``www.amazon.ca``）。``searchurl`` 拼 URL 用的就是它。
    host: str
    #: 人类可读的站点名，只用于日志与控制台下拉框。
    label: str
    #: ISO 4217 币种码。**站点决定币种**，不看页面符号 —— 见模块 docstring 第四节。
    currency: str
    #: 价格串的符号前缀，只用于「这串文字是不是价格」的定位，不用于判定币种。
    #: 顺序无关紧要（它只被 ``any(sym in s ...)`` 用），**不要**从这里推断
    #: "拼价格串时该用哪个符号" —— 那是下面 ``render_symbol`` 的事。
    price_symbols: Tuple[str, ...]
    #: 我们**自己拼**一个价格串时用的符号（页面只给了裸数字时）。
    #:
    #: 单列一个字段而不是从 price_symbols 里挑，是因为挑不出来：
    #: 美国站是 ("$", "US$")、加拿大站是 ("CDN$", "C$", "$")，
    #: "第一个"对美国站对、对加拿大站错，"最后一个"反过来。
    #: F-012 的实现里真的先写成了「取最后一个」，于是美国站拼出了 "US$19.99"
    #: —— 一个既有行为的静默改动，而且只有拼接路径（页面给裸数字时）才走到。
    #: 显式字段没有这种隐式约定可以踩错。
    render_symbol: str
    #: 出现即说明页面不在本站点语境（串区 / 代理落错国家 / 被重定向）。
    foreign_currency_markers: Tuple[str, ...]
    #: 邮编/邮政编码的形状。``None`` 表示这个站点还没定规则（见 ``validate_postal``）。
    postal_pattern: Optional[Pattern[str]]
    #: 该站点的默认投递地。
    default_postal: str
    #: 该站点期望的 ``Accept-Language``。
    accept_language: str
    #: 补零规则是否适用。**只有美国站适用** —— 见 ``normalize_postal``。
    postal_zero_fill: bool = False
    #: 该站点是否已经过实测验证（见模块末尾 ``VERIFIED`` 一节）。
    verified: bool = False

    @property
    def base_url(self) -> str:
        """``https://www.amazon.ca`` —— 详情页 / 卖家页 / 搜索页的拼接基点。"""
        return f"https://{self.host}"

    @property
    def zip_change_url(self) -> str:
        """glow 地址切换的 ajax 端点。

        ⚠ 路径部分（``/gp/delivery/ajax/address-change.html``）在所有站点上
        **假定同构**，只换主机名。美国站那条是实测过的（改造前就在跑）；
        其余站点**没有实测**，见模块末尾 ``VERIFIED``。
        """
        return f"{self.base_url}/gp/delivery/ajax/address-change.html"


#: ``re.VERBOSE`` 下的加拿大邮政编码。形如 ``A1A 1A1``。
#:
#: 字母集不是随便写的：加拿大邮政编码**首字母**不用 D/F/I/O/Q/U/W/Z，
#: **其余字母位**不用 D/F/I/O/Q/U。写成 ``[A-Z]`` 会放进一批根本不存在的
#: 编码，而这个值会被原样 POST 给 Amazon —— 收一个不存在的邮编，
#: 拿回来的是「设置失败」还是「静默落到默认地区」不可知，两种都比当场
#: 400 难查。
_CA_POSTAL_RE = re.compile(
    r"""^
    [ABCEGHJ-NPRSTVXY]   # 首字母：排除 D F I O Q U W Z
    \d
    [ABCEGHJ-NPRSTV-Z]   # 其余字母位：排除 D F I O Q U
    [ ]?                 # 规范形有一个空格；不带空格的输入也收
    \d
    [ABCEGHJ-NPRSTV-Z]
    \d
    $""",
    re.VERBOSE,
)

#: 美国邮编。**逐字节**等于改造前 ``server/app.py:_US_ZIP_RE`` 的
#: ``^\d{5}\Z``（``\Z`` 与 ``$`` 在这里等价：模式里没有换行，而
#: ``re.match`` + ``$`` 只在串尾多允许一个 ``\n``；改造前后都不会收到
#: 带尾随换行的邮编，因为 ``_normalize_zip`` 先 ``.strip()`` 过）。
_US_POSTAL_RE = re.compile(r"^\d{5}$")


#: 站点注册表。
#:
#: 当前只有两条**完整**记录（美国站 + 加拿大站）。其余 16 个站点在
#: ``searchurl.SUPPORTED_DOMAINS`` 里仍然是「能翻搜索页」的，但没有
#: 详情采集所需的邮编/货币规则 —— 那是有意的，与 ``searchurl`` 对
#: ``delivery`` 的处置同一条口径：**没有可靠对照就不猜**，让调用方
#: 当场拿到一个说得清的拒绝，而不是一批看着正常、实则错了国家的数据。
_REGISTRY: Dict[str, MarketplaceSpec] = {
    "amazon.com": MarketplaceSpec(
        id="amazon.com",
        host="www.amazon.com",
        label="美国站",
        currency="USD",
        price_symbols=("$", "US$"),
        # 逐字节等于改造前那些 f"${...}" 字面量。
        render_symbol="$",
        # ⚠ 逐字节照抄改造前的 worker/ziputil.py:_NON_US_CURRENCY。
        #   **没有**补 "CDN$"/"C$"：补了就是改既有的美国站判定，
        #   而这次改造的前提是美国站行为一个字节都不变。
        foreign_currency_markers=("CNY", "¥", "€", "£", "JP¥"),
        postal_pattern=_US_POSTAL_RE,
        default_postal="10001",
        accept_language="en-US,en;q=0.9",
        postal_zero_fill=True,
        verified=True,
    ),
    "amazon.ca": MarketplaceSpec(
        id="amazon.ca",
        host="www.amazon.ca",
        label="加拿大站",
        currency="CAD",
        # "$" 必须在列：加拿大站大量页面就渲染成 $24.99，不含 CDN 前缀。
        # 正因如此，**币种不能从符号推**，见模块 docstring 第四节。
        price_symbols=("CDN$", "C$", "$"),
        # 加拿大站页面上 "$24.99" 与 "CDN$ 24.99" 两种都有，拼接时用裸 $：
        # 它是该站点更常见的渲染形态，而 marketplace 列已经说明了币种，
        # 不需要靠符号去承载"这是加元"这个信息。
        render_symbol="$",
        # 美元符号不在这里：它在加拿大站是**本地**币种符号。
        # 能说明「串区了」的是欧元/英镑/人民币/日元这些。
        foreign_currency_markers=("CNY", "¥", "€", "£", "JP¥"),
        postal_pattern=_CA_POSTAL_RE,
        # 渥太华（K1V 7P8）。用户指定值。
        #
        # 这个默认值**不是**「随便挑一个地方」：它与美国站的 10001 承担同一个
        # 职责 —— 把「页面按哪个地区渲染」这件事**钉死**，否则 Amazon 会按
        # 出口 IP 自己挑一个地区，于是同一批任务采回来的价格、配送时长、
        # 库存来自不同地区，而数据看起来完全正常。
        # 改它就是改整批加拿大数据的口径，改之前先想清楚。
        default_postal="K1V 7P8",
        accept_language="en-CA,en;q=0.9",
        # 加拿大邮编不是纯数字，补零规则**不适用**（补了会把 "M5V 3L9"
        # 变成别的东西，或者更糟：让一个本来非法的输入看起来合法）。
        postal_zero_fill=False,
        # ⚠ 未实测 —— 见模块末尾 VERIFIED。
        verified=False,
    ),
}


def all_ids() -> Tuple[str, ...]:
    """注册表里全部站点的规范键（排序后，供报错信息与 UI 用）。"""
    return tuple(sorted(_REGISTRY))


def is_supported(value: Optional[str]) -> bool:
    """``value`` 是否是注册表里的站点（已归一化或未归一化都行）。"""
    return normalize_id(value) in _REGISTRY


def normalize_id(value: Optional[str]) -> str:
    """把各种写法收敛成规范键。不认识的原样返回（由调用方决定怎么拒）。

    收的形状：``amazon.ca`` / ``www.amazon.ca`` / ``https://www.amazon.ca/``
    / ``AMAZON.CA``。**不**收 ``CA`` / ``加拿大`` 这类别名 —— 两个字母的
    国家码和站点键不是一一对应的（``amazon.co.uk`` 的国家码是 GB，
    ``amazon.ae`` 是 AE 但站点键里没有国家码），留着这条路只会让
    「站点」这个概念重新变糊。
    """
    if value is None:
        return ""
    s = str(value).strip().lower()
    if not s:
        return ""
    if s.startswith("http://") or s.startswith("https://"):
        s = s.split("://", 1)[1]
    s = s.split("/", 1)[0]
    if s.startswith("www."):
        s = s[4:]
    return s


def get(value: Optional[str] = None) -> MarketplaceSpec:
    """取站点 spec。``None`` / 空串 -> 美国站。

    Raises:
        ValueError: 不在注册表里。**故意抛而不是静默回退到美国站** ——
            静默回退正是本模块要消灭的那类故障：调用方以为在采加拿大，
            实际采的是美国，而没有任何一侧会响。
    """
    key = normalize_id(value) or DEFAULT_MARKETPLACE
    try:
        return _REGISTRY[key]
    except KeyError:
        raise ValueError(
            f"不支持的站点: {value!r}（归一化后 {key!r}）；"
            f"可选: {', '.join(all_ids())}"
        ) from None


def assert_registry_within_search_domains() -> None:
    """注册表里的每个 ``host`` 都必须在 ``searchurl.SUPPORTED_DOMAINS`` 里。

    这两张表描述不同的能力（见模块 docstring 第二节），但**详情能力是搜索
    能力的子集**：能采详情的站点，必然也得能翻它的搜索结果页，否则
    ``discover_mode=with_detail`` 的关键词批次在建批次那一步就 400 了，
    而详情任务却能单独建出来 —— 一个站点「一半能用一半不能用」。

    ``import`` 放在函数体内：本模块是 ``searchurl`` 的**下游**
    （``searchurl`` 之后会 import 本模块来做 domain 归一化），
    模块级互相 import 会成环。

    Raises:
        AssertionError: 有 host 不在白名单里。
    """
    from common.core.searchurl import SUPPORTED_DOMAINS

    missing = sorted(
        spec.host for spec in _REGISTRY.values()
        if spec.host not in SUPPORTED_DOMAINS
    )
    assert not missing, (
        f"marketplace 注册表里的这些主机名不在 searchurl.SUPPORTED_DOMAINS 里: "
        f"{missing}。两张表分叉会让这些站点「能采详情、建不出关键词批次」。"
    )


def host_to_id(host: str) -> str:
    """``www.amazon.ca`` -> ``amazon.ca``。``searchurl`` 的 domain 转规范键。"""
    return normalize_id(host)


def id_to_host(value: Optional[str] = None) -> str:
    """规范键 -> 完整主机名。不在注册表里的站点按 ``www.<id>`` 兜底。

    兜底分支是给 ``searchurl.SUPPORTED_DOMAINS`` 里那 16 个「只能翻搜索页、
    没有详情规则」的站点用的：它们拼 URL 需要主机名，但没有 spec。
    """
    key = normalize_id(value) or DEFAULT_MARKETPLACE
    spec = _REGISTRY.get(key)
    return spec.host if spec else f"www.{key}"


# ==========================================================================
# 邮编 / 邮政编码
# ==========================================================================

def normalize_postal(value: object, marketplace: Optional[str] = None) -> Optional[str]:
    """按站点归一化投递地编码。不合法返回 ``None``。

    这是**站点感知**版本的 ``server/app.py:_normalize_zip``。两条规则：

    1. **补零只对美国站做**（``postal_zero_fill``）。补零是「整数往返丢掉
       前导零」的唯一还原方式（Excel 把 ``01234`` 存成 ``1234``），
       而加拿大邮编不是纯数字，这条规则对它不适用。
    2. **归一化必须幂等**。``zip_requested`` 是消费侧分组键
       ``(asin, marketplace, zip_requested)`` 的一部分
       （``common/pgdb/relay.py:normalize_zip`` 的 docstring），
       同一个地点归出两种形状就会把一个商品的价格序列劈成两组。

    加拿大邮编统一归到**带空格的大写规范形**（``M5V 3L9``）：那是
    Canada Post 的官方格式，也是 Amazon glow 挂件显示的形状。
    ``m5v3l9`` / ``M5V3L9`` / ``m5v 3l9`` 三种输入归出同一个值。

    ⚠ relay 侧**不需要**改：``normalize_zip`` 对「非纯数字」是原样透传
    （``_zfill_short_numeric`` 返回 ``None`` 就走透传分支），
    所以 ``M5V 3L9`` 经过 relay 不会被改形状。前提是本函数在
    **入口处**就把它归成规范形 —— 这正是本函数存在的理由。
    """
    spec = get(marketplace)
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None

    # Excel 数字 90001.0 -> "90001"。只对补零站点做：加拿大邮编不会长这样，
    # 而对一个字母数字串去尾 ".0" 只会造出更奇怪的东西。
    if spec.postal_zero_fill and s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]

    if spec.postal_zero_fill:
        # ZIP+4 取前段（"10001-1234" -> "10001"）。
        s = s.split("-", 1)[0].strip()
        # 补零规则的唯一真源仍是 common/core/zipcode.py。
        s = _zfill_short_numeric(s) or s
    else:
        # 加拿大规范形：大写 + 去掉全部内部空白后按 3+3 重新插一个空格。
        # 先去后插而不是「压缩连续空白」，是为了让 "M5V3L9" 与 "M5V  3L9"
        # 归出同一个值 —— 幂等性要求的是「同一个地点只有一种形状」。
        s = s.upper()
        compact = re.sub(r"[\s-]+", "", s)
        if len(compact) == 6:
            s = f"{compact[:3]} {compact[3:]}"
        else:
            s = re.sub(r"\s+", " ", s)

    return s if validate_postal(s, spec.id) else None


def validate_postal(value: object, marketplace: Optional[str] = None) -> bool:
    """``value`` 是否是该站点形状合法的投递地编码。

    站点没有 ``postal_pattern`` 时返回 ``False``（而不是 ``True``）：
    与 ``searchurl`` 对 ``delivery`` 的处置同一条口径 —— 没有可靠规则就
    **拒绝**，不要放一个校验不了的值进去。
    """
    spec = get(marketplace)
    if spec.postal_pattern is None:
        return False
    if value is None:
        return False
    return bool(spec.postal_pattern.match(str(value).strip()))


def postal_equal(a: object, b: object, marketplace: Optional[str] = None) -> bool:
    """两个投递地编码是否指同一个地点（用于 glow 观测值与请求值的比对）。

    存在的理由：页面上的 glow 文案与我们 POST 出去的值**形状可能不同**
    （``M5V3L9`` vs ``M5V 3L9``）。直接字符串比会凭空造出一个
    ``mismatch`` —— 与 ``relay.normalize_zip_observed`` docstring 里
    记的 ``'01001' vs '1001'`` 是同一类事故，只是换了个国家。
    """
    if a is None or b is None:
        return False
    na = normalize_postal(a, marketplace)
    nb = normalize_postal(b, marketplace)
    if na is None or nb is None:
        # 有一边归一化不了就退回「去空白 + 大写」的宽松比较：
        # 这里的任务是「判定是否相同」，不是「判定是否合法」。
        return (re.sub(r"[\s-]+", "", str(a)).upper()
                == re.sub(r"[\s-]+", "", str(b)).upper())
    return na == nb


# ==========================================================================
# 货币
# ==========================================================================

def expected_currency(marketplace: Optional[str] = None) -> str:
    """该站点的价格**应该**是什么币种（ISO 4217）。"""
    return get(marketplace).currency


def has_local_price_symbol(text: object, marketplace: Optional[str] = None) -> bool:
    """``text`` 里是否出现了该站点的本地货币符号。

    ⚠ 这**不是**「币种是不是对的」的判定，只是「这串文字看着像不像价格」。
    币种由站点决定 —— 模块 docstring 第四节。
    """
    if text is None:
        return False
    s = str(text)
    return any(sym in s for sym in get(marketplace).price_symbols)


def looks_foreign(text: object, marketplace: Optional[str] = None,
                  scan_limit: int = 50000) -> bool:
    """页面是否出现了「不属于这个站点」的货币标识（串区 / 代理落错国家）。

    ``scan_limit`` 默认 50000 是照抄 ``worker/ziputil.zip_effective_in_html``
    改造前的 ``text[:50000]``：头部扫描避免把整页 1MB+ HTML 过一遍，
    而货币标识出现在页头（glow 挂件、导航栏）就足够判定了。
    """
    if text is None:
        return False
    head = str(text)[:scan_limit]
    return any(m in head for m in get(marketplace).foreign_currency_markers)


# ==========================================================================
# VERIFIED —— 哪些站点是实测过的
# ==========================================================================
# ``MarketplaceSpec.verified`` 记的是「这条记录的值有没有拿真实页面验证过」。
#
#   amazon.com  verified=True   —— 改造前就在生产跑，每个值都是从既有代码
#                                  逐字节搬过来的。
#   amazon.ca   verified=False  —— **没有实测**。下面三项是假设，不是事实：
#
#     1. ``zip_change_url`` 的路径与美国站同构，且 ``zipCode`` 参数原样接受
#        带空格的加拿大邮编（``M5V 3L9``）。也可能要求无空格形，或者要求
#        额外的 ``countryCode`` 字段。
#     2. glow 挂件的 ``id="glow-ingress-line2"`` 在加拿大站同名，
#        且文案里含邮编（美国站是 "New York 10001"，加拿大站**可能**只显示
#        城市名或前三位 FSA "Toronto M5V"）。``postal_equal`` 的宽松比较
#        能吸收形状差异，但吸收不了「文案里根本没有邮编」。
#     3. 价格渲染形态：``CDN$ 24.99`` 与 ``$24.99`` 两种都可能出现，
#        ``price_symbols`` 两种都收了，但哪种是主流未知。
#
#   这三条在本仓库的开发环境里**验证不了** —— Amazon 对机房出口 IP 直接返回
#   ``api-services-support@amazon.com`` 拦截页（``worker/parser.py:2057``
#   与 ``worker/engine.py:1495`` 认的就是它），住宅代理不在开发环境里。
#
#   验证脚本：``tools/probe_marketplace.py``。在**有住宅代理**的机器上跑，
#   它会把上面三项逐条打出实测结果。跑完请把 ``verified`` 改成 True，
#   并把实测到的差异回填进这张表。
