"""common/core/dbtables.py —— 两个后端共用的表清单 / 分块大小 / LIKE 模式（唯一真源）。

* ``CLEAR_TABLES`` —— ``clear_all_data`` 的删除清单。两侧各有一份实现
  （SQLite 删 ``sqlite_sequence``、PG 做 identity RESTART），但**删哪些表**
  必须是同一份。
* ``ASIN_DELETE_CHUNK`` / ``ASIN_DELETE_TABLES`` —— ``delete_asins`` 的分块大小
  与表清单。分块边界分叉 = 两个后端发出的语句序列不同，出事时对不上号。
* ``search_like_pattern`` —— ``%term%``，**不转义**。读路径（``get_results``）
  与删除路径（``find_asins_by_search``）必须用同一个，否则同一个 search
  在 GET 和 DELETE 下选中不同的行（D-16 记的就是这个事故）。

* ``task_marketplace_sql`` / ``BA_ROW_MARKETPLACE_SQL`` / ``DEFAULT_MARKETPLACE_SQL``
  —— F-012：「批次里这一行 ASIN 是哪个站点的」的 SQL 片段。批次读路径
  （结果列表 / 导出 / 变体展开 / 截图回填）靠它把 ``asin_data`` 钉到任务行的
  站点上。两个后端逐字共用，分叉 = 同一个批次在两个后端读出不同的行。

注意：SQLite 侧的 ``SEARCH_TERM_OR``（带 ``?`` 占位符的 SQL 片段）**不在这里**，
它留在 ``common/database.py``——那是方言相关的 SQL，不是共享的纯 Python 常量。
本文件的站点片段能放进来，是因为它**一个占位符都没有**（默认站点是内联的
注册表常量），两个方言写法完全相同。
"""

from common.core.marketplace import DEFAULT_MARKETPLACE

# `DELETE /api/database` 清库的删除顺序（Database.clear_all_data 用）。
# 子表在前、父表在后 —— PG 侧有真外键，顺序在那边是正确性问题；SQLite 侧
# 顺序无所谓。两个后端**共用这一份**（pgdb 经 common/pgdb/_shared.py 再导出），
# 分叉 = 两个后端「清空」之后剩下的东西悄悄不同。
CLEAR_TABLES = ("asin_changes", "asin_data", "batch_asins", "tasks",
                "screenshots", "batches")

# 按 ASIN 删除时的分块大小。SQLite 的 SQLITE_MAX_VARIABLE_NUMBER 默认 999，
# 一条 `IN (?,?,...)` 不能超过它；500 是安全值。PG 没有这个限制，但**必须
# 用同一个值**：分块边界不同 = 两个后端发出的语句序列不同，一旦某一块中途
# 失败（两侧都在一个事务里，所以只会整体回滚），排障时对不上号。
ASIN_DELETE_CHUNK = 500

# 删批次时每条 DELETE 覆盖多少个 batch_id。两个后端**必须用同一个值**，
# 理由同上（分块边界不同 = 两边发出的语句序列不同，排障时对不上号）。
#
# 为什么不是 1（"每个批次一条"）也不是"一条 IN(全部)"：
#   * 一条 IN(全部) 的工作量**无上界** —— 选 500 个批次就是一次删几百万行，
#     某天会跨过 asyncpg 的 command_timeout（PG_COMMAND_TIMEOUT，默认 60s），
#     然后整个事务回滚、前端只看到一个 500。这正是本轮要修的那条路径。
#   * 每个批次一条能把单条语句钉死，但语句数变成 4N —— 而 pool.py 是
#     statement_cache_size=0（决策 D-7），每条 DELETE 都要重新 Parse/Bind/Execute。
#     这些往返全部发生在**写锁内**，worker 的 pull/result 会被一起挡住。
#
# 实测（PG 17，asyncpg 直连，量的是"锁内那串 DELETE"的总耗时）：
#   500 批 × 每表 20 行（往返成本主导）：一条 28.0ms / 每批次 264.3ms / 50 个一组 29.2ms
#   500 批 × 每表 500 行（100 万行）  ：一条 575.3ms / 每批次 666.8ms / 50 个一组 457.0ms
#   50 批 × 每表 2000 行（40 万行）   ：一条 151.1ms / 每批次 191.0ms / 50 个一组 147.0ms
# 50 个一组在三种形状上都追平或好于"一条 IN(全部)"，同时把单条语句的工作量
# 限死在 50 个批次的行数内（约为原来最坏情况的 1/10）。
# 50 个占位符也远低于 SQLite 的 SQLITE_MAX_VARIABLE_NUMBER（默认 999）。
BATCH_DELETE_CHUNK = 50

# 「共 N 条结果」允许走估算（PG 的 pg_class.reltuples）的最小行数门槛。
#
# 为什么要门槛，而不是"永远估算"：
#   * reltuples 是 ANALYZE / autovacuum 留下的**统计快照**，新建库或从没被
#     analyze 过的表上是 -1（PG 14+ 的"未知"哨兵），空表刚 ANALYZE 完是 0。
#     小库上直接把这个数吐出去 = 前端显示"共 0 条"而表格里有 12 行。
#   * 小表的精确 COUNT(*) 本来就不要钱（10 万行以内一次索引扫，个位数毫秒）。
#     花钱的是百万行 + 可见性图被持续写入打脏那个组合。
#   * golden 基线跑在几十行的夹具上，两个后端**必须给出同一个 total**。
#     门槛把小库整个排除在估算之外，基线因此逐字不变。
#
# 10 万这个值：低于它精确 COUNT 稳定在几十毫秒（实测 12.4 万行 / 冷可见性图
# 68 ms），高于它才开始出现"写入把可见性图打脏 -> 索引扫退化成堆读"的悬崖
# （124 万行实测：VACUUM 后 78.5 ms，改动 3% 的行之后 683.6 ms，I/O 49 倍）。
#
# ⚠ 两个后端共用这个常量，但**只有 PG 会用它**：SQLite 没有等价的行数统计
#   （sqlite_stat1 只有索引的平均重复度，没有总行数），那边永远精确。
#   这不是分叉：生产跑 PG，SQLite 只做小库对照，小库在 PG 上也精确。
TOTAL_ESTIMATE_MIN_ROWS = 100_000

# 按 ASIN 删除要清的四张表（顺序：子表在前）。asin_data 最后 —— PG 侧
# asin_changes / screenshots / batch_asins 都可能引用它。
ASIN_DELETE_TABLES = ("asin_changes", "screenshots", "batch_asins", "asin_data")


# ==========================================================================
# F-012：批次行 -> 站点
# ==========================================================================
# 「这个批次里的这个 ASIN 是哪个站点的」**唯一真源是 tasks 行**：
#   * tasks 上有 UNIQUE(batch_id, asin)，所以按 (batch_id, asin) 至多查到一行；
#   * batch_asins / batches 都没有站点列，而且**不要加**：加列要改两个后端的
#     EXPECTED_COLUMNS / verify_schema，还要在生产 PG 上 ALTER + 回填；
#   * 也不要按「批次」取一个站点（tasks LIMIT 1）：插件按批次名追加、变体展开
#     往同一批追加，逐行查才在任何情况下都对。
#
# 为什么要钉：asin_data 的唯一键是 (asin, marketplace)，同一个 ASIN 在美国站
# 和加拿大站各一行。批次读路径原来只按 asin 关联，于是加拿大批次会把美国行也
# 带出来（反之亦然）—— 而且一个加拿大行就能改掉美国批次的结果与导出，
# 美国站不变式当场破掉。
#
# 查不到任务行（F-012 之前的老 batch_asins、或者手工补的行）-> 美国站，
# 那正是 F-012 之前的答案（那时库里只有美国行）。
#
# ⚠ 默认站点**内联成字面量**、不走 ``?``：这样拼进任何一条 SQL 都不改变占位符
#   个数与顺序 —— get_results 里 join_params / where_params 的顺序注释和
#   tests/test_search_no_planner_cliff.py 的 ``?`` 计数断言因此全部照旧成立。
#   值来自注册表常量（不是外部输入），不存在注入面。

#: 默认站点的 SQL 字面量（带单引号）。
DEFAULT_MARKETPLACE_SQL = "'" + DEFAULT_MARKETPLACE.replace("'", "''") + "'"


def task_marketplace_sql(batch_id_expr: str = "ba.batch_id",
                         asin_expr: str = "ba.asin") -> str:
    """一个标量子查询：给定 (batch_id, asin) 两个**列表达式**，返回该行任务的站点。

    参数是外层查询里的列引用（不是值），默认就是 ``batch_asins ba`` 那两列；
    ``bset`` CTE 里用不带别名的 ``batch_asins.batch_id`` / ``batch_asins.asin``，
    截图路径回填用 ``s.batch_id`` / ``s.asin``。
    写路径（截图落地的 UPDATE）手里只有一个 batch_id 值，可以显式传 ``"?"``——
    那是**调用方自己**加的占位符，要自己把参数排进对应位置。
    """
    return (f"COALESCE((SELECT t.marketplace FROM tasks t "
            f"WHERE t.batch_id = {batch_id_expr} AND t.asin = {asin_expr}), "
            f"{DEFAULT_MARKETPLACE_SQL})")


#: 最常用的那一份：外层是 ``batch_asins ba``。
BA_ROW_MARKETPLACE_SQL = task_marketplace_sql()


def search_like_pattern(t: str) -> str:
    """``%term%``，**不做任何转义**。

    SQLite 的 LIKE 没有转义字符，所以模式原样传下去；PG 侧靠 SQL 里的
    ``ESCAPE ''`` 关掉默认的反斜杠转义来对齐（决策 D-16）。
    ``%`` 与 ``_`` 故意不转义：用户输入里的通配符今天就是生效的
    （``Gol%rand`` 是模糊匹配），两个引擎的元字符一样，这个行为免费保留。
    """
    return "%" + str(t) + "%"


__all__ = [
    "CLEAR_TABLES",
    "ASIN_DELETE_CHUNK",
    "TOTAL_ESTIMATE_MIN_ROWS",
    "ASIN_DELETE_TABLES",
    "search_like_pattern",
    "DEFAULT_MARKETPLACE_SQL",
    "task_marketplace_sql",
    "BA_ROW_MARKETPLACE_SQL",
]
