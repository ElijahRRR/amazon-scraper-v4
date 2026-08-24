"""common/pgdb/results_read.py —— 结果读取 / 搜索 / 分页 / 流式导出。

OWNS（7 个方法）:
    get_results        database.py:2120   ← 返回值**就是** HTTP 响应体（app.py:1758）
    get_result_by_asin database.py:2272
    get_asin_changes   database.py:2282
    iter_results       database.py:2344   ← **async generator**，不是 coroutine
    get_total_asins    database.py:2464
    get_all_asins      database.py:2468
    get_change_stats   database.py:2476

依赖别人:
    media.py -> _hydrate_screenshot_paths(items, batch_id)  （原地改 items）

--------------------------------------------------------------------------
一、搜索：FTS5 没了，换 ascii_lower + LIKE（**不是** ILIKE）
--------------------------------------------------------------------------
词条解析逐字照抄 database.py:2174-2179：
    search = str(search)[:500]
    terms  = [t.strip()[:100] for t in search.split(",") if t.strip()][:10]
截断顺序（先 strip 后截）、500/100/10 三个常数、逗号是唯一分隔符、多词是 OR、
terms 为空则**完全不加 where 子句**（静默返回全量）——全部保持。

谓词形状：
    (ascii_lower(d.asin) LIKE ascii_lower(?) ESCAPE '' OR
     ascii_lower(d.title) LIKE ascii_lower(?) ESCAPE '' OR
     ascii_lower(d.brand) LIKE ascii_lower(?) ESCAPE '')
* 用 ascii_lower 而不是 ILIKE：实测 39 个探针 × 5 种写法，ILIKE 有 9 处与
  SQLite 不一致（根因是非 ASCII：SQLite 只折叠 ASCII，'CAFÉ CREME' 不匹配
  '%café%'），ascii_lower 是 0 处。ILIKE 还依赖 collation。
* 反斜杠（决策 D-16）：SQLite 的 LIKE **没有**转义字符，PG 默认是反斜杠。
  统一用 ``ESCAPE ''`` 把 PG 的转义机制整个关掉——这正好就是 SQLite 的语义，
  模式因此可以**原样**传下去。
  以前这里靠"在 Python 侧把反斜杠加倍"抵消，但那只覆盖得到本模块自己拼的
  SQL；server/app.py:2277 的 DELETE 用 f-string 自己拼模式、走
  pool.translate_sql 的自动改写，加倍不了 —— 于是同一个 ``search`` 在 GET 和
  DELETE 上命中**不同的行**（GET 对、DELETE 错，而且两边都回 deleted:1）。
  ``ESCAPE ''`` 加在 SQL 侧，两条路径共用同一份语义，不可能再漂。
* ``%`` 和 ``_`` **不要**转义：用户输入里的通配符现在就是生效的（'Gol%rand'
  是模糊匹配），两个引擎的元字符一样，这个 bug 免费保留。
* 也可以直接写 ``d.title LIKE ?`` —— pool.translate_sql 会自动改写成上面的
  形状（含 ``ESCAPE ''``）。但显式写出来更利于对照表达式 GIN 索引（schema.py
  建的就是 ``public.ascii_lower(col) public.gin_trgm_ops``，表达式必须逐字
  一致）。实测 ``ESCAPE ''`` 不影响 GIN 走索引。
* 查询形状用**扁平 OR**，不要照搬 ``d.id IN (SELECT ... UNION ...)``。
  200k 行实测：非选择性词条上扁平 OR 是 2.2ms，id-IN-UNION 是 536ms
  （HashAggregate 整个 id 集）。

--------------------------------------------------------------------------
二、COUNT 查询的 bug（决策 D-8）—— **已修**，本节保留为病历
--------------------------------------------------------------------------
⚠ 下面整节描述的是**历史状态**。D-8 当年的决定是"照着复现这个崩溃"，并注明
"修它是 Phase 1.5 的事"。本轮修了，改动有三处，缺一不可：
  1. count 的谓词与参数改成**同一时刻快照**（keyset 谓词追加之前），
     不再用 ``[p for p in where_parts if "d.id" not in p]`` 猜哪个是游标谓词。
  2. 搜索谓词不再需要 ``d.id IS NOT NULL`` 那层"标记壳"，
     ``any(len(t) < 3 ...)`` 的分支判断随之删除（两条分支行集本就相同）。
  3. 测试 ``test_count_bug_is_reproduced`` 改名 ``test_search_with_cursor_no_longer_500s``
     并改成断言两边都是 200 且逐字相等；SQLite 裁判快照里那 3 条
     ``["raise"]`` 已重录成正常返回值。
根因值得记住：**靠文本匹配识别"这个谓词是什么"从来都不成立**。
任何一个新谓词只要碰巧提到 d.id 就会重蹈覆辙，而症状是 500，不是错结果。

（以下为原文，描述修复前的行为）
--------------------------------------------------------------------------
database.py:2249-2255 在有 cursor 时会把所有文本里含 ``"d.id"`` 的 where 片段
从 count 查询里剔掉——本意是剔掉 keyset 谓词 ``d.id < ?``，但 FTS 快路径的
谓词字面上就以 ``d.id IN (`` 开头，于是被一并剔掉，而 count_params
（第 2207 行就快照好了）里仍带着搜索参数 → 参数个数对不上 → 500。

即：今天 ``/api/results?search=GoldenBrand&cursor=3`` 是 500，
而 ``search=Go&cursor=3``（<3 字符走慢路径，谓词文本里没有 "d.id"）是 200。
黄金场景没覆盖这个组合（scenario.py:214-224 翻页不带 search、搜索不带 cursor）。

**决策：复现这个 bug。** 做法是给 >=3 字符那条分支的谓词套一层带标记的壳：
    "(d.id IS NOT NULL AND (" + flat_or + "))"
行集不变、GIN 计划不变、``"d.id" not in p`` 的过滤照样命中，asyncpg 会抛
"the server expects 0 arguments for this query, N was passed"，
FastAPI 渲染成同样的 500。
→ 因此 ``any(len(t) < 3 for t in terms)`` 这个分支判断**必须保留**，
  它现在唯一的作用就是决定谓词要不要带这个标记。
→ ``[p for p in where_parts if "d.id" not in p]` 这个过滤逐字照抄，别"修好"。
Phase 1.5 会连同 COUNT(*) 重构一起有意修掉。

--------------------------------------------------------------------------
三、分页 / 排序
--------------------------------------------------------------------------
* ``ORDER BY d.id DESC``（next）/ ``ASC``（prev），``LIMIT ?`` 传 limit+1，
  has_more = len > limit，prev 方向在 **Python 里 reverse**，
  next_cursor = items[-1]['id']，prev_cursor = items[0]['id']。全部照抄。
  d.id 是 PK 不会有 NULL，也不会有并列，不需要 tiebreaker。
* iter_results 的 batch 路径用 ``ba.asin > ?`` + ``ORDER BY ba.asin ASC`` 做
  keyset。导出的 CSV 是**逐行**比对的，所以行序是契约。
  batch_asins.asin 已声明 COLLATE "C"，字节序 = SQLite BINARY。
* 凡是排序键可空（screenshots/tasks 的 updated_at 等）：
  DESC 补 ``NULLS LAST``、ASC 补 ``NULLS FIRST``，PG 默认与 SQLite 正好相反。

--------------------------------------------------------------------------
四、其它
--------------------------------------------------------------------------
* iter_results 必须仍然是 ``async def`` + ``yield``（调用方 ``async for``）。
  两条分支（batch / 非 batch）的 SQL 文本要**保持分开**：游标种子在 batch
  路径是 ``""``（text），非 batch 路径是 ``0``（bigint），合并成一条会撞上
  asyncpg 的参数类型推断。
  batch 路径额外加 11 个 ``batch_*`` 别名键，并且会用 ba.asin **覆盖**
  ``d["asin"]``。照抄。
* get_results / get_result_by_asin 末尾调 ``self._hydrate_screenshot_paths``
  （media.py 提供，原地改 items）。
* ``screenshot_path: null`` 是基线里的值。**不要**加 ``COALESCE(col,'')``
  之类的"防御"，那会把 null 变成 ""。
* get_change_stats 返回稀疏 dict（只有出现过的 change_type 才有 key）。
* 这些多语句读路径本来就每条语句一个快照（SQLite 读池如此），
  **不要**加 REPEATABLE READ。
* 导出会长时间占着一条池连接。config.PG_POOL_MAX 要留够余量，
  别让导出饿死 pull_tasks。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from common.pgdb._shared import (  # noqa: F401
    ASIN_DATA_FIELDS,
    _ASIN_DATA_COLUMN_SET,
    _normalize_screenshot_path,
    search_like_pattern,
)
from common.core.results_sort import (
    DEFAULT_SORT,
    CursorExpired,
    is_next,
    keyset_predicate,
    normalize_sort,
    order_by as _order_by,
)
from common.pgdb.pool import LIKE_NO_ESCAPE, as_int, text_affinity


# 一个 term 的三列 OR 谓词。写成 ascii_lower(...) LIKE ascii_lower(?) 而不是
# ILIKE：见模块头注释与 OWNERSHIP.md D-5。表达式必须与 schema.py 建的
# 表达式 GIN 索引（public.ascii_lower(col) public.gin_trgm_ops）对得上。
_SEARCH_COLUMNS = ("asin", "title", "brand")


def _col_like(col: str) -> str:
    """单列单词的 LIKE 谓词。``_TERM_OR`` 与搜索 CTE 的分支都由它拼出来，
    保证两处与表达式 GIN 索引**逐字**同源（改一处漏一处 = 索引静默失效）。"""
    return f"ascii_lower(d.{col}) LIKE ascii_lower(?)" + LIKE_NO_ESCAPE


_TERM_OR = "(" + " OR ".join(_col_like(c) for c in _SEARCH_COLUMNS) + ")"


# ``%term%``，不转义。**唯一真源在 common/database.py**（经 _shared 再导出）——
# 读路径与删除路径共用它。这里以前有一份独立实现，注释里论证的正是 D-16：
# 曾经靠"把反斜杠加倍"去抵消 PG 的默认转义，结果读路径加倍了、删除路径
# （handler 里的 f-string）没加倍，同一个 search=back\\slash，GET 命中
# back\\slash 那行、DELETE 却删掉 backslash 那行。现在两条路径都走
# SQL 侧的 ``ESCAPE ''``（LIKE_NO_ESCAPE / _LIKE_QMARK_RE），模式原样传。
_like_pattern = search_like_pattern


class ResultsReadMixin:
    """只定义方法，绝不定义 __init__。"""

    # ==================== 查询操作（keyset 分页）====================

    async def get_results(self, batch_id: int = None, cursor_id: int = None,
                          limit: int = 50, search: str = None,
                          change_filter: str = "all",
                          direction: str = "next",
                          columns: List[str] = None,
                          with_total: bool = True,
                          sort: str = DEFAULT_SORT) -> Dict:
        """
        获取结果列表（keyset 分页）
        change_filter: all / price_stock / title_bullets / new
        direction: next (向后翻页) / prev (向前翻页)
        columns: 只取这些列（None = 全部 56 列，**默认行为不变**）
        with_total: 算不算 total（False -> total 为 None，**默认行为不变**）
        sort: "id"（默认，按首次入库倒序）/ "recent"（按 updated_at 倒序，
              即"最近采的排前面"）。规则的唯一真源在 common/core/results_sort.py，
              两个后端共用；``recent`` 且游标行已被删除时抛 CursorExpired。
        返回: {"items": [...], "has_more": bool, "next_cursor": int, "prev_cursor": int, "total": int|None}

        ------------------------------------------------------------------
        columns / with_total 是干什么的
        ------------------------------------------------------------------
        `server/api/results.py` 头部那段实测记着「**82% 的账在 Python 序列化
        上**」。这两个参数就是冲那 82% 去的 —— 它们**不优化 SQL**，
        优化的是"要吐多大一坨"。

        实测（100 万行、单页 50 行、long_description 等宽列有真实内容）：

            默认 SELECT d.*（56 列）+ count      60.9 ms    274.2 KB
            首屏：窄投影 + 算 total              52.1 ms     20.0 KB
            翻页：窄投影 + 不算 total             2.7 ms     20.0 KB
            其中 COUNT(*) 那一条                 48.4 ms

        采集结果页只渲染 15 个字段，而 `bullet_points`(24KB) / `image_urls`(22KB)
        / `long_description` 这三个它一个都不显示 —— 274 KB 里约 250 KB 是白发
        的，还要过公网再让浏览器 parse。

        `total` 是**全表 COUNT**，随行数线性增长且**每次翻页都重算一遍**，
        而它的值在整个翻页过程中恒定不变。前端只在首屏需要它。

        ------------------------------------------------------------------
        ⚠ 两个都必须**默认关闭**
        ------------------------------------------------------------------
        `items[]` 的列集是对外契约（docs/erpapi_contract.md §3.2：可以单方面
        **加**字段，不可以删）。所以默认必须是今天这 56 列、必须照算 total；
        窄投影与省 count 只能是**调用方显式要求**的行为。

        ⚠ `columns` 非空时会**强制补上四列**，即使调用方没要：
            id             next_cursor / prev_cursor 取的就是它，少了翻页直接断
            asin           `_hydrate_screenshot_paths` 的查找键
            screenshot_path 同上（它要就地归一化这一列）
            updated_at     `_hydrate_batch_task_status` 的 batch_asin_data_updated_at
        少任何一个都是"看起来能用、翻两页或点开截图才炸"的那种坏法。
        """
        proj = None
        if columns:
            # 白名单过滤：列名会拼进 SQL。端点层已经拒绝过非法列名（422），
            # 这里是第二道 —— db 层不假设调用方一定是那个端点。
            wanted = [c for c in dict.fromkeys(columns) if c in _ASIN_DATA_COLUMN_SET]
            if wanted:
                for forced in ("id", "asin", "screenshot_path", "updated_at"):
                    if forced not in wanted:
                        wanted.append(forced)
                proj = ", ".join(f"d.{c}" for c in wanted)

        sort = normalize_sort(sort)

        # recent 模式：游标是 int id，但谓词要 (updated_at, id)。
        # 先按主键把那一行的 updated_at 查出来。查不到 = 该行已被删除 ->
        # 抛 CursorExpired 让调用方从第一页重来。**不要**退回按 id 比较：
        # 那会在 ORDER BY updated_at 下给出一页语义错误的数据，而且看不出来。
        cursor_ts = None
        if cursor_id is not None and sort == "recent":
            async with self.read() as rc, rc.execute(
                    "SELECT updated_at FROM asin_data WHERE id = ?",
                    (as_int(cursor_id),)) as c:
                row = await c.fetchone()
            if row is None:
                raise CursorExpired(cursor_id)
            # ⚠ 也要过 COALESCE 的等价物：谓词左边是 COALESCE(...,'')，
            #    右边若绑 None，行值比较又退化成 NULL -> 那一页恒空。
            cursor_ts = row["updated_at"] or ""

        join_parts = []
        count_join_parts = []
        join_params: list = []
        where_parts = []
        where_params: list = []

        # 批次筛选 - 通过 batch_asins JOIN
        if batch_id:
            join_parts.append("JOIN batch_asins ba ON ba.asin = d.asin AND ba.batch_id = ?")
            count_join_parts.append("JOIN batch_asins ba ON ba.asin = d.asin AND ba.batch_id = ?")
            join_params.append(as_int(batch_id))

        # 变动筛选 —— 由 `JOIN (SELECT DISTINCT asin FROM asin_changes ...)`
        # 改写成 EXISTS 半连接。
        #
        # 等价性：子查询里的 DISTINCT 保证每个 asin 至多匹配一行，所以那个 JOIN
        # 本来就不放大行数，与 EXISTS 的半连接语义逐行相同（ac 的列一个都没投影出去）。
        #
        # ⚠ 参数绑定顺序变了：谓词从 join_parts 挪进 where_parts，绑定参数就必须
        #   同步从 join_params 挪进 where_params —— 因为下面是
        #   `params = join_params + where_params`。这里在 search 之前追加，
        #   与 SQL 文本里 WHERE 子句的先后顺序一致。
        # ⚠ D-8：谓词文本里**绝不能出现 "d.id"**。下面 count 查询的
        #   `[p for p in where_parts if "d.id" not in p]` 是刻意保留的缺陷复现
        #   （只该剔掉 keyset 谓词和 FTS 快路径谓词）；这里写的是 `ac.asin = d.asin`，
        #   不含 "d.id"，所以不会被误剔、count 的参数个数照旧对得上。
        if change_filter in ("price_stock", "title_bullets"):
            # change_filter 的取值被上面这个成员判断限死在两个字面量上，不是外部拼接
            pred = ("EXISTS (SELECT 1 FROM asin_changes ac "
                    f"WHERE ac.asin = d.asin AND ac.change_type = '{change_filter}'")
            if batch_id:
                pred += " AND ac.batch_id = ?"
                where_params.append(as_int(batch_id))
            where_parts.append(pred + ")")
        elif change_filter == "new":
            if batch_id:
                sub = "JOIN batch_asins ba2 ON ba2.asin = d.asin AND ba2.batch_id = ? AND ba2.is_new = 1"
                join_params.append(as_int(batch_id))
                join_parts.append(sub)
                count_join_parts.append(sub)
            else:
                where_parts.append(
                    "EXISTS (SELECT 1 FROM asin_changes ac "
                    "WHERE ac.asin = d.asin AND ac.change_type = 'new')")

        # 搜索（支持逗号分隔的批量搜索）—— 限长防 DoS
        # PG 侧没有 FTS5：两条分支产生**同一个**扁平 OR 谓词（实测 SQLite 的
        # 快/慢路径在 37 个非空探针上行集完全一致），由 pg_trgm 的表达式 GIN
        # 索引加速；扁平 OR 而不是 d.id IN (... UNION ...)，200k 行实测非选择性
        # 词条 2.2ms vs 536ms（后者要 HashAggregate 整个 id 集）。
        #
        # ⚠ ``any(len(t) < 3 ...)`` 的分支判断已经**去掉**：它此前唯一的作用是
        # 决定谓词文本里要不要带 "d.id" 标记，用来刻意复现 count 查询那个崩溃
        # （决策 D-8）。D-8 已按它自己写的"留给 Phase 1.5 修"修掉（见下方
        # count_where 处），标记失去意义，两条分支产生的行集本来就完全相同
        # （实测 37 个非空探针一致）。
        search_pred = ""
        search_params: list = []
        search_patterns: List[str] = []
        if search:
            # 单个请求最多 500 字符、最多 10 个关键词，每个关键词截断到 100 字符
            search = str(search)[:500]
            terms = [t.strip()[:100] for t in search.split(",") if t.strip()][:10]
            if terms:
                or_clauses = []
                for t in terms:
                    or_clauses.append(_TERM_OR)
                    like_pattern = _like_pattern(t)
                    search_patterns.append(like_pattern)
                    search_params.extend([like_pattern] * len(_SEARCH_COLUMNS))
                search_pred = f"({' OR '.join(or_clauses)})"

        # 构建 count 查询的参数**与谓词**——两者必须在同一时刻快照，
        # 也就是 keyset 谓词追加**之前**。见下面 count_where 处的注释。
        # 搜索谓词在数据查询里被搬进了 CTE（见下），但 count 仍然要它，
        # 所以在这里**追加到末尾**：AND 可交换，行集不变，而放末尾能让
        # count_params 的拼接顺序（join -> where -> search）与 SQL 文本一致。
        count_params = join_params + where_params + search_params
        count_where_parts = list(where_parts) + ([search_pred] if search_pred else [])

        # keyset 分页。谓词文本与排序键都来自 common/core/results_sort.py，
        # 两个后端逐字共用 —— 分叉的症状是"同一个游标在两个后端翻出不同的页"，
        # 而它不会报错。
        if cursor_id is not None:
            where_parts.append(keyset_predicate(sort, direction))
            if sort == "recent":
                # recent 模式要 (updated_at, id) 两个值，而对外的游标只有 id。
                # 按主键把那一行的 updated_at 查出来（一次索引查找）。
                where_params.extend([cursor_ts, as_int(cursor_id)])
            else:
                where_params.append(as_int(cursor_id))

        where_clause = " AND ".join(where_parts) if where_parts else "1=1"
        join_clause = " ".join(join_parts) if join_parts else ""
        count_join_clause = " ".join(count_join_parts) if count_join_parts else ""

        order_by_clause = _order_by(sort, direction)

        # 查询数据（d.id 是 PK，非空且无并列，不需要 NULLS 位置和 tiebreaker）
        #
        # ⚠ 搜索谓词必须待在 ``WITH ... AS MATERIALIZED`` 里，**不能**放进
        # 外层 WHERE。原因是外层有 ``ORDER BY d.id DESC LIMIT 51``：
        #
        #   词数越多，OR 分支越多，规划器估计的命中行数就越大（实测估算值
        #   随词数线性上涨）。一旦越过某个点，它就认为"反正命中很多，沿主键
        #   倒序扫、凑够 51 行就停，还省掉排序"比"GIN 位图 + 排序"便宜 ——
        #   于是**丢掉三个 trgm 索引**改成全表逐行过滤。
        #   命中多时这个计划确实快（几百行就凑满）；命中少时它要扫穿整张表，
        #   直接撞 PG_COMMAND_TIMEOUT=60s -> 500。
        #   线上表现：4 个高频词 245ms，4 个 ASIN（低命中）60s 超时。
        #   而搜索框明写着"支持批量、逗号分隔"，粘一串 ASIN 进去就是必崩路径。
        #
        # 关键在于**每个分支只有一个 LIKE 谓词**，估算不再被 OR 的数量推高，
        # 规划器对单谓词的选择性判断是准的：命中多 -> 主键倒序早退，
        # 命中少 -> trgm 位图。两个方向都对，不需要我们替它选。
        # MATERIALIZED 关键字不可省 —— PG 12+ 会把只被引用一次的 CTE **内联**
        # 回外层，内联之后外层的 LIMIT 又能影响分支的扫描方式，等于没改。
        #
        # 200k 行实测（psql 启动开销约 40ms 已含在内）：
        #                        现状扁平 OR   本形状
        #   高命中 1 词              63ms       60ms
        #   高命中 4 词              45ms       60ms
        #   零命中 4 词              42ms       42ms
        #   零命中 10 词           5273ms       45ms   ← 就是线上那条 60s 超时
        #   chair + 零命中 ×9       475ms       63ms
        # 六组用例行集逐个 id 相同。
        #
        # 试过但**不行**的两种写法，别再走回头路：
        #   * 单个 MATERIALIZED CTE 包住整个扁平 OR：零命中 10 词好了（42ms），
        #     但高命中 4 词从 45ms 劣化到 3179ms —— 它要把全部命中行物化一遍，
        #     拿灾难换了平庸。
        #   * enable_seqscan=off：完全无效（5295ms）。因为坏计划**不是** seq scan，
        #     是主键倒序 Index Scan + Filter，那个开关管不着它。
        #
        # 只有数据查询需要这层保护：count 查询既没有 ORDER BY 也没有 LIMIT，
        # 触发不了这个计划，所以它照旧把扁平 OR 放在 WHERE 里。
        if search_patterns:
            # 每个 (词 × 列) 一个分支，各自 ORDER BY + LIMIT，再 UNION。
            #
            # 为什么正确（全局前 N ⊆ 各分支前 N 的并集）：设 x 属于全局前 N，
            # 则 x 至少满足一个分支的谓词；在那个分支里排在 x 前面的行，是
            # 全局里排在 x 前面的行的子集，不足 N 个 —— 所以 x 必在该分支的前 N。
            #
            # ⚠ **其余筛选（batch join / change_filter / keyset 游标）必须原样
            # 推进每一个分支**，不能只留在外层。否则分支取的是"全局前 N"，
            # 外层再拿 batch 一滤可能一条不剩，而更靠后其实还有合格行 ——
            # 静默丢行。实测：不推进去时 batch 筛选 + 高命中词丢了整整 51 行。
            branches = []
            branch_params: list = []
            for pat in search_patterns:
                for col in _SEARCH_COLUMNS:
                    branches.append(
                        f"(SELECT d.id FROM asin_data d {join_clause}"
                        f" WHERE {where_clause} AND {_col_like(col)}"
                        f" ORDER BY {order_by_clause} LIMIT ?)")
                    branch_params.extend(join_params)
                    branch_params.extend(where_params)
                    branch_params.append(pat)
                    branch_params.append(limit + 1)
            sql = f"""
                WITH search_hit AS MATERIALIZED (
                    {' UNION '.join(branches)}
                )
                SELECT {proj or 'd.*'} FROM asin_data d
                {join_clause}
                JOIN search_hit sh ON sh.id = d.id
                WHERE {where_clause}
                ORDER BY {order_by_clause}
                LIMIT ?
            """
            # 参数顺序 = SQL 文本顺序：各分支 -> 外层 join -> 外层 where -> limit
            params = branch_params + join_params + where_params
        else:
            sql = f"""
                SELECT {proj or 'd.*'} FROM asin_data d
                {join_clause}
                WHERE {where_clause}
                ORDER BY {order_by_clause}
                LIMIT ?
            """
            params = join_params + where_params
        params.append(limit + 1)  # 多取一条判断 has_more

        async with self.read() as rc, rc.execute(sql, params) as c:
            rows = await c.fetchall()

        items = [dict(r) for r in rows]
        has_more = len(items) > limit
        if has_more:
            items = items[:limit]

        if not is_next(direction):
            items.reverse()

        # 注意：读连接已经归还，_hydrate 内部还要再借一条（避免池内嵌套借用）
        await self._hydrate_screenshot_paths(items, batch_id)
        if batch_id:
            await self._hydrate_batch_task_status(items, batch_id)

        # 查询总数。
        #
        # count_where_parts 与 count_params 在**同一时刻**快照（keyset 谓词追加
        # 之前），所以它天然不含 cursor 条件，谓词与参数不可能对不齐。
        #
        # ⚠ 这里以前是靠 `[p for p in where_parts if "d.id" not in p]` 猜的 ——
        #   本意只想剔掉 keyset 谓词 `d.id < ?`，但搜索快路径的谓词文本里也带
        #   "d.id"，于是被一并剔掉，而 count_params 里仍留着它那 3N 个参数
        #   -> 参数个数对不上 -> asyncpg/sqlite3 抛错 -> 500。
        #   也就是说 `?search=GoldenBrand&cursor=3` 一直是必崩的（决策 D-8 把它
        #   当既有行为**刻意复现**过；本轮按 D-8 自己写的"留给 Phase 1.5 修"修掉）。
        #   靠文本匹配识别"哪个谓词是 cursor"本身就是错的方案：任何一个新谓词
        #   只要碰巧提到 d.id 就会重蹈覆辙。快照法从结构上消灭这一整类。
        count_where = " AND ".join(count_where_parts) if count_where_parts else "1=1"

        # with_total=False -> 整条 count 都不发。它随行数线性增长、且翻页途中
        # 值恒定不变，前端只在首屏要它。
        total = None
        if with_total:
            count_sql = f"SELECT COUNT(*) FROM asin_data d {count_join_clause} WHERE {count_where}"
            async with self.read() as rc, rc.execute(count_sql, count_params) as c:
                total = (await c.fetchone())[0]

        next_cursor = items[-1]["id"] if items else None
        prev_cursor = items[0]["id"] if items else None

        return {
            "items": items,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "prev_cursor": prev_cursor,
            "total": total,
        }

    #: `/api/results?batch_id=` 追加的三列。**名字必须与 `iter_results`
    #: （CSV/xlsx 批次导出走的那条）逐字相同** —— 它们是同一条信息的两个出口，
    #: 名字分叉就等于逼消费侧写两套代码。
    #:
    #: 为什么需要它们：`get_results` 的 SQL 是
    #:     SELECT d.* FROM asin_data d JOIN batch_asins ba ON ... AND ba.batch_id = ?
    #: `asin_data` 是**每个 ASIN 一行的最新态**，`batch_id` 只回答"属不属于这批"，
    #: **不参与取哪一行**。这批采失败的 ASIN 只要以前采过就照样命中 JOIN，
    #: 返回**上一次的旧行**，而 `SELECT d.*` 之外一个字段都没有能看出它的年龄。
    #: 消费侧摄进自己的库、盖上一个新鲜的接收时间，陈旧数据就此看起来很新鲜，
    #: 两侧都不报错。CSV/xlsx 出口早就有防护（`data_source` 列），JSON 没有。
    #:
    #: ⚠ `batch_has_asin_data` 在**本端点恒为 1**，这不是 bug 也不是占位：
    #: 驱动表是 `asin_data`、走的是 INNER JOIN，能返回的行必然有 asin_data。
    #: 给出它是为了让消费侧能用**同一套代码**从 JSON 复算出 CSV 的 `data_source`
    #: （`server/api/export.py:_batch_status_export_values`）。
    #: 真正的差别在另一头：`iter_results` 以 `batch_asins` 为驱动表 LEFT JOIN，
    #: 所以 CSV 里**会出现**从没采过的 ASIN（那时该列是 0），而本端点
    #: **整行都不会返回**。要查"这批有哪些 ASIN 一次都没采过"，用
    #: `GET /api/export/batch/{name}/records` 的 `coverage`，或直接比对
    #: `/api/batches/{name}/status` 的任务数。这条差异有用例钉着。

    async def _hydrate_batch_task_status(self, items: List[Dict], batch_id):
        """**原地修改** items，无返回值。只在带 `batch_id` 时调用。

        一次查询取完整页，不是逐行查 —— 单页最多 1000 行，逐行就是 1000 次往返。

        用 `= ANY(?::text[])` 而不是变长 `IN (?,?,...)`：一个参数、空数组也合法，
        顺带绕开 asyncpg 的参数个数上限。与同文件的
        `_get_done_screenshot_paths` 同一种写法（那里有更详细的说明）。
        SQLite 侧没有 `= ANY`，走变长 IN —— 两边行集相同。
        """
        if not items:
            return
        asins = [it["asin"] for it in items if it.get("asin")]
        rows = {}
        if asins:
            async with self.read() as rc, rc.execute(
                "SELECT asin, status, updated_at FROM tasks "
                "WHERE batch_id = ? AND asin = ANY(?::text[])",
                (self.as_int(batch_id),
                 [self.text_affinity(a) for a in asins])
            ) as c:
                for r in await c.fetchall():
                    # 同一批次同一 ASIN 只可能有一行（tasks 上 UNIQUE(batch_id, asin)），
                    # 不必去重
                    rows[r["asin"]] = dict(r)

        for item in items:
            t = rows.get(item.get("asin"))
            item["batch_task_status"] = t["status"] if t else None
            item["batch_task_updated_at"] = t["updated_at"] if t else None
            # 见上：本端点是 INNER JOIN asin_data，能返回的行必然有 asin_data
            item["batch_has_asin_data"] = 1
            item["batch_asin_data_updated_at"] = item.get("updated_at")

    async def get_batch_asin_set(self, batch_id) -> set:
        """一个批次里的 ASIN 集合（``DELETE /api/results`` 的 batch_id 分支）。

        ``batch_asins`` 记的是"这一批采过哪些 ASIN"，与 ``tasks`` 不同：
        任务可以失败、可以重试，这张表只管入过队的 ASIN。删除端点用它来
        「删掉这一批的全部结果」以及与 asins/search 取交集。

        与 ``get_all_asins`` 的区别是后者读 ``asin_data``（全库已采 ASIN），
        两者不可互换。

        ⚠ **一处有意的行为对齐，Phase 3.8 显式声明**（原来只写成 asyncpg 传参细节，
        读起来像纯移植，不对）：``batch_id`` 走 ``int()`` 强制转换。
        这让 ``DELETE /api/results {"batch_id": "1"}``（JSON body 里字符串形状的 id）
        在 **PG 上从 500（asyncpg DataError）变成 200**，与 SQLite 拉齐 ——
        SQLite 靠类型亲和本来就吃字符串。
        方向上是 C4 要的（同类残余差异 e21e2c6 的提交信息里以「已知残余」记过
        ``/api/tasks/release`` 那条），但它是**行为变更**不是移植细节，
        所以钉在 ``tests/test_results_delete_api.py::test_batch_id_accepts_string_form``。
        """
        async with self.read() as rc, rc.execute(
            "SELECT asin FROM batch_asins WHERE batch_id = ?", (int(batch_id),)
        ) as c:
            return {row["asin"] for row in await c.fetchall()}

    async def find_asins_by_search(self, terms) -> set:
        """按模糊搜索词选中 ASIN 集合（``DELETE /api/results`` 的 search 分支）。

        terms: 已经切好、去空、限过长的关键词列表（切词与限长是 handler 的
            请求校验，不在这里做）。词之间是 **OR**。

        谓词与 ``get_results`` 的搜索分支使用同一份文本
        （_TERM_OR），这正是 Phase 3.8 批 (3) 的收益：
        在这之前删除路径是 handler 里自己拼的 f-string，PG 侧靠
        ``pool._LIKE_QMARK_RE`` **按字面文本**把它改写成带 ``ESCAPE ''`` 的形式
        才跟读路径对齐 —— 把 ``LIKE ?`` 换个写法、三段 OR 拆开、甚至多一个空格，
        正则就不再命中，PG 当场换语义而两边都不报错（D-16）。现在两条路径引用
        的是同一个常量，改不歪。
        
        ``_TERM_OR`` 带 ``ascii_lower(...)``：SQLite 的 LIKE 折 ASCII 大小写，
        PG 的不折（D-5）。

        走只读连接：这里只是选行，真正的删除在 ``delete_asins`` 里另开事务。
        """
        terms = [t for t in terms if t]
        if not terms:
            return set()
        or_clauses = []
        params = []
        for term in terms:
            or_clauses.append(_TERM_OR)
            pat = _like_pattern(term)
            params.extend([pat, pat, pat])
        where = " OR ".join(or_clauses)
        sql = f"SELECT d.asin FROM asin_data d WHERE {where}"
        async with self.read() as rc, rc.execute(sql, params) as c:
            return {row["asin"] for row in await c.fetchall()}

    async def get_result_by_asin(self, asin: str) -> Optional[Dict]:
        # 注意：先释放读连接再 _hydrate（其内部还要借读连接），避免池内嵌套借用导致死锁
        async with self.read() as rc, rc.execute(
            "SELECT * FROM asin_data WHERE asin = ?", (text_affinity(asin),)
        ) as c:
            row = await c.fetchone()
        if not row:
            return None
        item = dict(row)
        await self._hydrate_screenshot_paths([item])
        return item

    async def get_asin_changes(self, asin: str) -> List[Dict]:
        """获取 ASIN 的变动历史"""
        async with self.read() as rc, rc.execute(
            "SELECT * FROM asin_changes WHERE asin = ? ORDER BY id DESC",
            (text_affinity(asin),)
        ) as c:
            return [dict(r) for r in await c.fetchall()]

    # ==================== 导出操作 ====================

    async def iter_results(self, batch_id: int = None, change_filter: str = "all",
                           batch_size: int = 500, columns: Optional[List[str]] = None):
        """流式迭代结果（keyset 分页，支持 batch_id + change_filter）。
        整个导出（可能数千次 LIMIT 查询、持续数分钟）借用同一条池连接，
        全程不触碰写连接，导出再大也不会拖慢 worker 拉取/上传。
        （反过来说：导出期间那条池连接一直被占着，PG_POOL_MAX 要留够余量。）

        columns: 仅投影这些 asin_data 列（导出实际需要的字段）；None 时回退 d.*。

        批次路径游标用 ba.asin，走 PK 索引 (batch_id, asin)；batch_asins.asin 声明了
        COLLATE "C"，字节序与 SQLite 的 BINARY 一致，所以导出行序逐行可比。
        """
        # 收窄投影：把导出需要的 asin_data 列拼成 "d.col" 列表；
        # 防注入起见与已知列集取交集（列名来自 EXPORTABLE_FIELDS 白名单，这里再兜一层）。
        proj_cols = None
        if columns:
            proj_cols = [c for c in dict.fromkeys(columns) if c in _ASIN_DATA_COLUMN_SET] or None

        # 游标：批次路径按 asin（TEXT），非批次路径按 asin_data.id（bigint）。
        # 两条分支的 SQL 文本必须保持分开——同一条文本上游标类型不一致会撞
        # asyncpg 的参数类型推断。
        cursor: Any = "" if batch_id else 0
        async with self.read() as rc:
            while True:
                if batch_id:
                    d_select = ", ".join(f"d.{c}" for c in proj_cols) if proj_cols else "d.*"
                    joins = [
                        "LEFT JOIN asin_data d ON d.asin = ba.asin",
                        "LEFT JOIN tasks t ON t.batch_id = ba.batch_id AND t.asin = ba.asin",
                    ]
                    join_params: list = []
                    where = ["ba.batch_id = ?", "ba.asin > ?"]
                    where_params: list = [as_int(batch_id), cursor]

                    # 同 get_results：JOIN (SELECT DISTINCT ...) 改 EXISTS 半连接。
                    # ⚠ 这一处的驱动表是 **batch_asins (ba)**，不是 asin_data —— 原
                    #   join 条件写的就是 `ac.asin = ba.asin`，EXISTS 里必须照抄 ba.asin。
                    # ⚠ 参数从 join_params 挪到 where_params：`params = join_params +
                    #   where_params + [batch_size]`，而 where 里这一条排在
                    #   ba.batch_id / ba.asin 之后，追加顺序因此天然对齐。
                    if change_filter in ("price_stock", "title_bullets"):
                        where.append(
                            "EXISTS (SELECT 1 FROM asin_changes ac WHERE ac.asin = ba.asin "
                            f"AND ac.change_type='{change_filter}' AND ac.batch_id=?)")
                        where_params.append(as_int(batch_id))
                    elif change_filter == "new":
                        where.append("ba.is_new = 1")

                    params = join_params + where_params + [batch_size]
                    sql = f"""
                        SELECT {d_select},
                               ba.asin AS batch_requested_asin,
                               ba.is_new AS batch_is_new,
                               t.status AS batch_task_status,
                               t.error_type AS batch_error_type,
                               t.error_detail AS batch_error_detail,
                               t.retry_count AS batch_retry_count,
                               t.auto_retry_count AS batch_auto_retry_count,
                               t.worker_id AS batch_worker_id,
                               t.updated_at AS batch_task_updated_at,
                               d.updated_at AS batch_asin_data_updated_at,
                               CASE WHEN d.asin IS NULL THEN 0 ELSE 1 END AS batch_has_asin_data
                        FROM batch_asins ba
                        {' '.join(joins)}
                        WHERE {' AND '.join(where)}
                        ORDER BY ba.asin ASC
                        LIMIT ?
                    """

                    async with rc.execute(sql, params) as c:
                        rows = await c.fetchall()
                    if not rows:
                        break
                    for row in rows:
                        d = dict(row)
                        cursor = d["batch_requested_asin"]
                        d["asin"] = d.get("batch_requested_asin") or d.get("asin")
                        yield d
                    continue

                # 非批次路径（导出全部）：主键 id 游标，本就最优。
                # 收窄投影时须显式带上 d.id 作游标。
                if proj_cols:
                    d_select = "d.id, " + ", ".join(f"d.{c}" for c in proj_cols)
                else:
                    d_select = "d.*"
                joins = []
                join_params = []
                where = ["d.id > ?"]
                where_params = [cursor]

                # 同上，改 EXISTS 半连接。这三条都不带参数，绑定顺序不受影响。
                if change_filter in ("price_stock", "title_bullets", "new"):
                    where.append(
                        "EXISTS (SELECT 1 FROM asin_changes ac WHERE ac.asin = d.asin "
                        f"AND ac.change_type='{change_filter}')")

                join_clause = " ".join(joins)
                where_clause = " AND ".join(where)
                params = join_params + where_params + [batch_size]

                sql = f"SELECT {d_select} FROM asin_data d {join_clause} WHERE {where_clause} ORDER BY d.id ASC LIMIT ?"

                async with rc.execute(sql, params) as c:
                    rows = await c.fetchall()
                if not rows:
                    break
                for row in rows:
                    d = dict(row)
                    cursor = d["id"]
                    yield d

    # ==================== 统计 ====================

    async def get_total_asins(self) -> int:
        async with self.read() as rc, rc.execute("SELECT COUNT(*) FROM asin_data") as c:
            return (await c.fetchone())[0]

    async def get_all_asins(self) -> List[str]:
        """获取所有已知 ASIN（用于自动采集）"""
        result = []
        async with self.read() as rc, rc.execute("SELECT asin FROM asin_data ORDER BY id") as c:
            async for row in c:
                result.append(row["asin"])
        return result

    async def get_change_stats(self, batch_id: int = None) -> Dict:
        """获取变动统计"""
        batch_filter = "WHERE batch_id = ?" if batch_id else ""
        params = (as_int(batch_id),) if batch_id else ()

        stats = {}
        sql = f"SELECT change_type, COUNT(DISTINCT asin) as cnt FROM asin_changes {batch_filter} GROUP BY change_type"
        async with self.read() as rc, rc.execute(sql, params) as c:
            async for row in c:
                stats[row["change_type"]] = row["cnt"]
        return stats
