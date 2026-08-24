"""common/core/results_sort.py —— `/api/results` 的排序规则（唯一真源）。

两个存储后端各自拼自己的 SQL，但**排序键与 keyset 谓词的文本必须逐字相同**，
否则同一个游标在两个后端上翻出不同的页 —— 而这种分叉不会报错，只会让人
在某一个后端上偶尔"跳页"或"丢行"。所以这三样东西定义在这里，两边 import。

--------------------------------------------------------------------------
两种排序，以及为什么默认那个不能动
--------------------------------------------------------------------------
``id``（默认）
    ``ORDER BY d.id DESC``。``asin_data`` 是**一 ASIN 一行**、按 asin UPSERT，
    ``id`` 在**首次入库**时分配、之后永不改变 —— 所以这个顺序是"第一次见到
    这个 ASIN 的时间"倒序，**不是**"最近采集"。一个两年前入库、今天刚重采完
    的 ASIN，在这个排序下仍然沉在最底下。
    它是默认值，因为游标语义（cursor = 上一页最后一行的 id）是对外契约的一部分。

``recent``
    ``ORDER BY d.updated_at DESC, d.id DESC`` —— 真正的"最近采的排前面"。
    ``id DESC`` 是 tiebreaker：同一秒采完的多行必须有稳定的先后，否则翻页会
    跳行/重复。``updated_at`` 是 ``text``、格式 ``YYYY-MM-DD HH:MM:SS``，
    字节序 == 时间序（两个后端的该列都是 ``COLLATE "C"`` / BINARY）。

--------------------------------------------------------------------------
⚠ 排序键是 ``COALESCE(d.updated_at, '')``，不是裸列 —— 三个理由
--------------------------------------------------------------------------
1. **两个引擎的 NULL 默认位置正好相反。** 实测（同一份数据，含一行
   ``updated_at IS NULL``，id=4）::

       ORDER BY u DESC, id DESC
           PG      -> 4,2,3,1     （DESC 默认 NULLS FIRST）
           SQLite  -> 2,3,1,4     （DESC 默认 NULLS LAST）

2. **写 ``NULLS LAST`` 只能治 ORDER BY，治不了索引。** SQLite 的
   ``CREATE INDEX`` 不接受该修饰符（3.45.1 实测 ``unsupported use of
   NULLS LAST``），于是索引和 ORDER BY 的写法必然分叉。

3. **最要命的一条：行值比较遇到 NULL 会静默丢行。**
   ``(NULL, 7) < ('2026-05-01', 4)`` 的结果是 NULL 而不是 true/false，
   所以 ``updated_at IS NULL`` 的行在**第一页之后再也不会出现**。
   这不是理论风险 —— ``test_full_pagination_walk_is_complete_and_ordered``
   第一次跑就红了：一次大页返回 8 行，逐页翻只翻到 7 行，丢的正是那一行。

``COALESCE(updated_at, '')`` 一次解决三条：``''`` 在两个引擎里都小于任何
非空字符串，DESC 时自然排最后（== NULLS LAST），而且表达式里**没有 NULL**，
行值比较不会再退化。ORDER BY 与索引可以用同一个表达式，两边写法也统一了::

    PG      idx_asin_data_updated_id (COALESCE(updated_at, '') DESC, id DESC)
    SQLite  idx_asin_data_updated_id (COALESCE(updated_at, '') DESC, id DESC)

⚠ 表达式索引要被用上，**索引里的表达式必须和查询里的逐字一致**。所以这里
是唯一真源，两个后端和两处 CREATE INDEX 都从 ``SORT_KEY`` 取。
实测两边都走索引（PG: ``Index Cond: (ROW(COALESCE(updated_at, ''::text), id)
< ROW(...))``；SQLite: ``SCAN t USING INDEX ix``）。

--------------------------------------------------------------------------
游标：仍然是一个整数 id，**没有**变成复合游标
--------------------------------------------------------------------------
``recent`` 模式下的 keyset 谓词需要 ``(updated_at, id)`` 两个值，但对外的
``cursor`` 参数保持 int —— 调用方先按主键把该行的 ``updated_at`` 查出来即可
（一次索引查找，可忽略）。这样做的理由：

* ``/api/results`` 的 ``cursor`` 现在是 ``int`` 类型的查询参数，改成字符串
  会连带改 OpenAPI 类型与非法输入的报错形状；
* 调用方（含本仓库的前端）不需要理解任何新的游标编码；
* ``(updated_at, id)`` 与 ``id`` 一一对应（id 是主键），信息量没有损失。

代价是游标行被删掉时查不到 ``updated_at``。那时抛 ``CursorExpired``，由
HTTP 层转成 422 —— **不能**退回按 id 比较：那会在 ``ORDER BY updated_at`` 下
给出一页语义错误的数据，而调用方看不出来。

绑定游标那一侧的值时记得也要过 ``COALESCE`` 的等价物（Python 侧写
``row["updated_at"] or ""``），否则谓词左边是 ``''`` 而右边是 ``None``，
比较结果又变成 NULL。
"""
from __future__ import annotations

#: ``sort`` 参数的合法取值。HTTP 层按它校验并 422。
SORT_MODES = ("id", "recent")

#: ``recent`` 的排序键表达式。**唯一真源** —— 两个后端的 ORDER BY / keyset
#: 谓词、以及两处 CREATE INDEX 全都从这里取。表达式索引要求索引与查询里的
#: 表达式逐字一致，写岔一个字索引就静默失效（查询照样对，只是慢几百倍）。
SORT_KEY = "COALESCE({alias}updated_at, '')"


def sort_key(alias: str = "d.") -> str:
    """排序键表达式。``alias=''`` 用于 CREATE INDEX（那里没有表别名）。"""
    return SORT_KEY.format(alias=alias)

DEFAULT_SORT = "id"


class CursorExpired(LookupError):
    """``sort=recent`` 且游标那一行已经不在库里（多半是被删了）。

    调用方应当从第一页重新开始。**不要**在库层悄悄降级成按 id 比较 ——
    那样翻出来的页在 ``ORDER BY updated_at`` 下是错的，而且看不出来。
    """

    def __init__(self, cursor_id):
        self.cursor_id = cursor_id
        super().__init__(f"游标行不存在（id={cursor_id}），可能已被删除")


def is_next(direction: str) -> bool:
    """``direction`` 只有 'prev' 一个特殊值，其余（含非法值）都当 next。

    这是既有行为，别"顺手收紧"成 422：``direction=xxx`` 今天等价于 next。
    """
    return direction != "prev"


def order_by(sort: str, direction: str) -> str:
    """ORDER BY 后面那一串（不含 "ORDER BY"）。两个后端逐字共用。"""
    nxt = is_next(direction)
    if sort == "recent":
        k = sort_key()
        return f"{k} DESC, d.id DESC" if nxt else f"{k} ASC, d.id ASC"
    return "d.id DESC" if nxt else "d.id ASC"


def keyset_predicate(sort: str, direction: str) -> str:
    """keyset 谓词。占位符个数：``id`` 模式 1 个，``recent`` 模式 2 个。

    ⚠ 谓词文本里**不要**出现会被误当成别的东西的标记 —— 历史上 count 查询
    靠 ``"d.id" not in p`` 猜哪个谓词是游标，把搜索谓词一起剔了（决策 D-8）。
    那个猜法已经删掉（count 的谓词与参数改成同一时刻快照），这里不再有隐含
    约束，但别把它加回来。
    """
    nxt = is_next(direction)
    if sort == "recent":
        # 行值比较：两个引擎都支持且语义一致。排序键用 COALESCE 包过，
        # 表达式里没有 NULL —— 否则 (NULL, id) < (...) 恒为 NULL，
        # updated_at 为空的行会在第一页之后**静默消失**（见模块头第 3 条）。
        k = sort_key()
        return f"({k}, d.id) < (?, ?)" if nxt else f"({k}, d.id) > (?, ?)"
    return "d.id < ?" if nxt else "d.id > ?"


def normalize_sort(sort) -> str:
    """把外部传进来的 ``sort`` 归一到合法值；非法值一律回退到默认。

    HTTP 层负责对非法值 422（不静默丢弃）；库层调用方可能是内部代码，
    这里只保证**永远不会**把非法字符串拼进 SQL。
    """
    return sort if sort in SORT_MODES else DEFAULT_SORT
