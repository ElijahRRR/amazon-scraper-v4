"""common/core/dbtables.py —— 两个后端共用的表清单 / 分块大小 / LIKE 模式（唯一真源）。

* ``CLEAR_TABLES`` —— ``clear_all_data`` 的删除清单。两侧各有一份实现
  （SQLite 删 ``sqlite_sequence``、PG 做 identity RESTART），但**删哪些表**
  必须是同一份。
* ``ASIN_DELETE_CHUNK`` / ``ASIN_DELETE_TABLES`` —— ``delete_asins`` 的分块大小
  与表清单。分块边界分叉 = 两个后端发出的语句序列不同，出事时对不上号。
* ``search_like_pattern`` —— ``%term%``，**不转义**。读路径（``get_results``）
  与删除路径（``find_asins_by_search``）必须用同一个，否则同一个 search
  在 GET 和 DELETE 下选中不同的行（D-16 记的就是这个事故）。

注意：SQLite 侧的 ``SEARCH_TERM_OR``（带 ``?`` 占位符的 SQL 片段）**不在这里**，
它留在 ``common/database.py``——那是方言相关的 SQL，不是共享的纯 Python 常量。
"""

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
]
