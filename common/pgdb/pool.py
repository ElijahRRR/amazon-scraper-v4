"""common/pgdb/pool.py —— asyncpg 连接层 + aiosqlite 形状兼容垫片。

OWNS（Database 公开面）:
    __init__(db_path=None)
    connect()
    close()
    read()                 @asynccontextmanager
    _open_read_pool()      —— PG 下为兼容保留的 no-op
    _db                    @property -> 写连接代理
    _write_lock            —— TimedLock（从 common.database 共享）

OWNS（非公开基础设施，其余 mixin 只准通过这些入口碰数据库）:
    _tx()                  @asynccontextmanager -> 写连接代理（已在事务里）
    _pool                  asyncpg.Pool（读侧）
    translate_sql()        ? -> $n（+ LIKE 的 ASCII 折叠改写）
    rowcount_from_tag()    'UPDATE 3' -> 3
    text_affinity()        复刻 SQLite TEXT affinity 的绑定强转
    as_int()               HTTP 边界的整数强转（定义在 common/core/coerce.py，此处再导出）

--------------------------------------------------------------------------
为什么要有"垫片"而不是把 SQL 搬进方法里
--------------------------------------------------------------------------
server/app.py 里有 7 个事务、11 条裸 SQL、6 处 ``db.read()``，全部直接用
aiosqlite 协议（``?`` 占位符、``async with conn.execute(...) as cur``、
``cursor.rowcount``）。把它们抽成 Database 方法要么改 common/database.py（禁止），
要么按后端分叉 app.py（破坏"SQLite 路径逐字节不变"）。所以这里提供一层
**形状兼容**的代理：app.py 一个字都不用改。

--------------------------------------------------------------------------
关键决策（详见 OWNERSHIP.md 的决策台账，实现者不要自行推翻）
--------------------------------------------------------------------------
D-2  _write_lock 保持**真锁**，并且 ``_db`` 是**一条专用写连接**。
     这样 17 个 BEGIN/COMMIT 块的串行语义与 SQLite 完全一致：不会出现
     pull_tasks 双发、mark_callback_attempt 丢更新、accept_results_batch
     与 reclaim 互相死锁这些 PG 独有的新故障模式。读侧走 asyncpg 池，
     "重读阻塞写"这个真正的痛点已经解决。真正的写并发是 Phase 1.5 的事
     （前提是先把 app.py 里的裸 SQL 抽干净）。
D-6  pgdb 内部的 SQL 方言是 ``?`` 占位符，由 translate_sql 统一改写成 $n。
     同一条语句里 **不准** 混用 ``?`` 和 ``$n``（会 raise）。
     好处：get_results 那个 "join_params + where_params 与文本顺序不一致"
     的编号陷阱直接消失——``?`` 天然按文本出现顺序绑定。
D-7  statement_cache_size=0。动态拼接的 UPDATE/INSERT 文本组合爆炸，且
     asyncpg 会把首次推断的参数类型 OID 冻结在缓存里；Phase 1 要的是确定性。
D-16 每一处 LIKE 都带 ``ESCAPE ''``。SQLite 的 LIKE 没有转义字符，PG 默认是
     反斜杠；只改写操作数不改写模式，会让 ``DELETE /api/results`` 静默删错行
     （两个后端都回 deleted:1）。读路径过去靠 Python 侧加倍反斜杠，删除路径
     （app.py 自己拼的 f-string）够不着——两条路径因此互相不一致。
     ``ESCAPE ''`` 把转义机制整个关掉 = SQLite 的语义，加在 SQL 侧，两条路径
     共用同一份定义。见 _LIKE_QMARK_RE / LIKE_NO_ESCAPE。
D-15 写连接上的事务是**有主的**（``ConnProxy._tx_owner``）。
     (a) 非持有者发的普通只读语句改道读池，不再挤进别人的事务里——一条读语句
         报错不该毁掉别人的写事务（F1），写方的错误也不该泄漏给后台协程（F2）。
         加锁读（FOR UPDATE / FOR SHARE）与事务持有者自己的读**不改道**。
     (b) 释放 ``_write_lock`` 时若事务还开着，说明它被遗弃了，直接回滚
         （见 WriteLock）；BEGIN 处再补一道兜底。把"一个失败请求永久焊死整条
         写路径"降级成"那一个请求 500"（F3/F4）。
     这条**推翻**了 D-13 里"两个后端都能读到别人的未提交改动"那句话，见
     ConnProxy.__init__ 的注释。
D-20 ``text_affinity`` 对 SQLite 会拒收的值一律抛异常（越界 int -> OverflowError，
     list/dict/bytes/… -> sqlite3.ProgrammingError），并复刻 SQLite 对
     -0.0 / NaN / ±Inf 的落库结果。等价性是双向的：以前 ``str(v)`` 兜底会把
     "SQLite 500 + 整批回滚"变成"PG 200 + 存下畸形字符串"，批次原子性被反转。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Any, List, Optional, Sequence

import asyncpg

from common import config
from common.core.coerce import as_int  # noqa: F401  —— 再导出，见下方 1) 节的说明
from common.core.lockmeter import record_pool_wait
from common.pgdb._shared import TimedLock

logger = logging.getLogger(__name__)


# ============================================================
# 1) 状态标签 / 类型强转 —— 纯函数，可单测
# ============================================================

def rowcount_from_tag(tag: Any) -> int:
    """把 asyncpg 的命令完成标签解析成 int。

    'UPDATE 3' -> 3 / 'DELETE 0' -> 0 / 'INSERT 0 1' -> 1 / 'SELECT 2' -> 2

    aiosqlite 的 ``cursor.rowcount`` 在 22 处被读取，其中 database.py:1673 与
    1745 是 **lease 门**（``if rowcount == 0: stale``）。若这里返回字符串，
    ``'UPDATE 0' == 0`` 为 False，lease 门会静默放行所有过期结果。
    所以：绝不把原始标签泄露到 pgdb 边界之外。
    """
    if tag is None:
        return -1
    if isinstance(tag, int):
        return tag
    try:
        return int(str(tag).rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return -1


# SQLite TEXT affinity 的复刻。见规格里的实测表：
#   True -> '1'（不是 'True'）；0.1+0.2 -> '0.3'（不是 '0.30000000000000004'）；
#   1e21 -> '1.0e+21'（不是 '1e+21'）
#
# ⚠ 曾经这里写着"-0.0 / inf / NaN 是 JSON 到不了的边角，记录备查"——**那是错的**。
# ``request.json()`` 用的是 Python 的 ``json.loads``，它接受 ``NaN`` /
# ``Infinity`` / ``-Infinity`` 字面量，而 ``-0.0`` 和 ``1e400`` 本来就是合法的
# JSON 数字（后者解析出来就是 ``inf``）。实测 SQLite 往 TEXT 列绑：
#   -0.0 -> '0.0'      nan -> NULL      inf -> 'Inf'      -inf -> '-Inf'
# 下面 text_affinity 逐条复刻这四个值（决策 D-20）。
_ASCII_UP = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_ASCII_LO = "abcdefghijklmnopqrstuvwxyz"
_ASCII_FOLD = str.maketrans(_ASCII_UP, _ASCII_LO)

# NUL 字节：SQLite 的 TEXT 收，PG 的 text 不收（CharacterNotInRepertoireError），
# 而且它会让**整个事务**回滚（一条脏标题毁掉一整批上传）。
# 默认 False = 严格等价（保留 NUL、让它报错），置 1 则在绑定层剔除。
# 这是一个需要人来拍板的取舍（剔除会改变落库数据，进而改变 content_hash /
# title_bullets_hash / asin_changes），所以做成开关而不是偷偷剔。
PG_STRIP_NUL = os.environ.get("PG_STRIP_NUL", "0") == "1"


def ascii_fold(s: str) -> str:
    """只折叠 ASCII 大小写——与 SQLite LIKE 的行为一致，不碰 Unicode。

    注意：**不要**用 Python 的 ``str.lower()``，它是 Unicode-aware 的
    （'É'.lower() -> 'é'），会把 SQLite 的大小写敏感行为改掉。
    """
    return s.translate(_ASCII_FOLD)


_INF = float("inf")

# sqlite3 的整数绑定上限就是 int64。越界时它抛
#   OverflowError: Python int too large to convert to SQLite INTEGER
_SQLITE_INT_MIN = -(2 ** 63)
_SQLITE_INT_MAX = 2 ** 63 - 1


def text_affinity(v: Any) -> Optional[str]:
    """把任意 JSON 标量转成 SQLite TEXT 列会存进去的那个字符串。

    asyncpg 对参数类型是严格的：往 text 列绑 int/float/bool 会 raise DataError，
    而 SQLite 会静默按 TEXT affinity 转换。``POST /api/tasks/result`` 收的是
    任意 JSON，所以任何一个客户端发 ``"review_count": 128`` 就会把 PG 版打成 500。
    仓库自带的 worker 全部 str 化、黄金夹具也全是字符串，**夹具抓不到这一类**。

    → results_write.py 必须让每一个绑到 TEXT 列的值都过这个函数。

    ⚠ 等价性是**双向**的（决策 D-20）。这个函数原来以 ``str(v)`` 兜底、且对
    int 不做范围检查，于是 SQLite **拒收**的载荷在 PG 下被静默收下：

        review_count = ["a","b"]        sqlite 500（整批回滚） / pg 200 存 "['a', 'b']"
        review_count = 9223372036854775808  sqlite 500        / pg 200 存那串数字

    ``POST /api/tasks/result/batch`` 一条毒项就能把"整批原子失败"变成"整批成功"
    ——批次原子性的语义被反转了。所以下面对 SQLite 会拒的值一律**抛异常**，
    异常类型也照抄 sqlite3 的（``OverflowError`` / ``ProgrammingError``），让调用
    方拿到与 SQLite 后端同样的 500 + 事务回滚。

    唯一有意保留的不等价：``bytes``。SQLite 把它当 BLOB 存进 TEXT 列，PG 的
    text 列没有对应物，这里归到"不支持"一并抛——JSON 到不了 bytes，无调用方。
    """
    if v is None:
        return None
    if isinstance(v, str):
        return _maybe_strip_nul(v)
    if v is True:
        return "1"
    if v is False:
        return "0"
    if isinstance(v, int):
        if not (_SQLITE_INT_MIN <= v <= _SQLITE_INT_MAX):
            # sqlite3 的原文，逐字复刻
            raise OverflowError(
                "Python int too large to convert to SQLite INTEGER")
        return str(v)
    if isinstance(v, float):
        # 实测 SQLite：nan -> NULL，inf -> 'Inf'，-inf -> '-Inf'，-0.0 -> '0.0'
        if v != v:
            return None
        if v == _INF:
            return "Inf"
        if v == -_INF:
            return "-Inf"
        if v == 0.0:
            return "0.0"          # -0.0 也走这条（"%.15g" 会给出 '-0'）
        s = "%.15g" % v
        if "e" in s:
            mant, _, exp = s.partition("e")
            if "." not in mant:
                mant += ".0"
            s = mant + "e" + exp
        elif "." not in s:
            s += ".0"
        return s
    # list / dict / tuple / set / bytes / 自定义对象 …… SQLite 全部拒收。
    # 用 sqlite3 自己的异常类型，两个后端的失败面完全一致。
    raise sqlite3.ProgrammingError(
        "Error binding parameter: type '%s' is not supported" % type(v).__name__)


def _maybe_strip_nul(s: str) -> str:
    if PG_STRIP_NUL and "\x00" in s:
        return s.replace("\x00", "")
    return s


# ``as_int`` 曾经定义在这里。自 Phase 4.1 起真源是 common/core/coerce.py ——
# 本模块是模块级 ``import asyncpg``，把纯函数留在这里等于只有 PG 后端能用它。
# 文件顶部已再导出，所以 ``from common.pgdb.pool import as_int``
# 与 ``Pool.as_int`` 两种既有写法都一字不用改。


# ============================================================
# 2) SQL 方言翻译
# ============================================================

# 事务控制语句：垫片自己拦下来，转成 asyncpg 的 transaction 对象。
# 'BEGIN IMMEDIATE' 在 PG 里是语法错误，而且它的语义（提前抢 SQLite 写锁）
# 在 PG 里没有对应物，直接当普通 BEGIN。
_TX_BEGIN = frozenset({
    "begin", "begin immediate", "begin deferred", "begin exclusive",
    "begin transaction", "begin immediate transaction", "start transaction",
})
_TX_COMMIT = frozenset({"commit", "commit transaction", "end", "end transaction"})
_TX_ROLLBACK = frozenset({"rollback", "rollback transaction"})

# 返回行的语句：走 fetch()；其余走 execute() 拿命令标签。
_ROW_RETURNING_HEAD = ("select", "values", "table", "show", "explain")
_RETURNING_RE = re.compile(r"\breturning\b", re.IGNORECASE)

#: CTE 外层语句可能的开头。用来判断 ``WITH ...`` 到底返不返回行。
_CTE_OUTER_HEADS = frozenset({"select", "values", "table",
                              "insert", "update", "delete", "merge"})


def cte_outer_head(norm: str) -> str:
    """``WITH ...`` 的**外层**语句关键字（取不到时返回空串）。

    ⚠ 为什么需要它：这里以前的判断是「``with`` 开头的语句，只有文本里出现
    ``returning`` 才算返回行」。那等于假设每个 CTE 都是数据修改型
    （``WITH x AS (UPDATE ... RETURNING ...)``）。一条**只读**的
    ``WITH x AS (SELECT ...) SELECT ...`` 会落到 execute() 分支，
    于是 ``fetchone()`` 返回 None、``fetchall()`` 返回 []
    —— **不报错、没有日志，就是查不到数据**。
    results_read 的搜索 CTE 第一次跑就踩中了：COUNT 说有 3 行，
    列表却是空的，两条查询谓词一模一样。

    做法：按括号深度扫描，取 CTE 定义列表**之外**（深度 0）的第一个语句关键字。
        with a as ( ... ), b as ( ... ) select ...   -> "select"
        with a as ( ... ) insert into t ... returning -> "insert"
    单引号 / 双引号里的内容整段跳过，免得字面量里的括号把深度算歪。
    """
    depth = 0
    i, n = 0, len(norm)
    word_start = -1
    while i < n:
        ch = norm[i]
        if ch == "'" or ch == '"':
            q = ch
            i += 1
            while i < n:
                if norm[i] == q:
                    # '' 是转义的单引号，不算收尾
                    if q == "'" and i + 1 < n and norm[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            word_start = -1
            continue
        if ch == "(":
            depth += 1
            word_start = -1
        elif ch == ")":
            depth -= 1
            word_start = -1
        elif depth == 0 and (ch.isalpha() or ch == "_"):
            if word_start < 0:
                word_start = i
            if i + 1 >= n or not (norm[i + 1].isalpha() or norm[i + 1] == "_"):
                word = norm[word_start:i + 1]
                if word in _CTE_OUTER_HEADS:
                    return word
                word_start = -1
        else:
            word_start = -1
        i += 1
    return ""

# `col LIKE ?` -> `ascii_lower(col) LIKE ascii_lower(?) ESCAPE ''`
#
# 为什么不用 ILIKE：实测（39 个探针 × 5 种候选写法）ILIKE 有 9 处与 SQLite 不一致，
# ascii_lower + LIKE 是 0 处。差异来自非 ASCII——SQLite 的 LIKE 只折叠 ASCII，
# 'CAFÉ CREME' 不匹配 '%café%'，而 ILIKE 会匹配。ILIKE 还依赖 collation，
# 换台机器行为就变。ascii_lower 是 IMMUTABLE 且与 collation 无关。
#
# 为什么带 ``ESCAPE ''``（决策 D-16）：**SQLite 的 LIKE 没有转义字符**，PG 默认
# 拿反斜杠当转义。只改写操作数、不管模式，反斜杠就会静默改变匹配的行集：
#
#   DELETE /api/results {"search": "back\\slash"}   sqlite 删掉 back\slash 那行
#                                                   pg     删掉 backslash  那行
#   DELETE /api/results {"search": "\\"}            sqlite 删 2 行 / pg 删 0 行
#
# 两边都回 ``{"deleted": 1}``，调用方**察觉不到删错了**。PG 支持
# ``LIKE ... ESCAPE ''`` 显式关掉转义机制，语义与 SQLite 逐字一致（实测：
# 见本文件末尾注释里的对照表 / tests/pgdb/test_like_escape.py）。顺带修掉
# "模式以孤立反斜杠结尾" 在默认转义下直接 InvalidEscapeSequenceError 的崩溃。
#
# 这条改写覆盖 server/app.py:2277（DELETE /api/results 的模糊选中）——那条 SQL
# 是 f-string 拼的、又选中行去**删除**，大小写敏感会静默少删。改写掉它就不必动 app.py。
# 已经显式写成 ``ascii_lower(x) LIKE ascii_lower(?)`` 的语句不会被二次匹配；
# 已经自带 ESCAPE 子句的也不会被再加一个（负向先行断言）。
_LIKE_QMARK_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)\s+LIKE\s+\?"
    r"(?!\s*ESCAPE\b)",
    re.IGNORECASE,
)

# 显式写 LIKE 的语句（results_read._TERM_OR）直接拼这个后缀，与上面的改写产物
# 逐字一致。**每一处 LIKE 都必须带上它**，否则读路径和删除路径又会不一致。
LIKE_NO_ESCAPE = " ESCAPE ''"

# 这里曾有一张 `_STATEMENT_OVERRIDES`：SQLite 独有、无法机械翻译的**整条语句**
# -> PG 等价语句序列，按"压平空白后转小写"的语句文本做键。
# 它只有过一个条目 —— `DELETE FROM sqlite_sequence`，服务于 `DELETE /api/database`
# 那段写在 handler 里的字面量 SQL。这套机制的代价是：handler 里的 SQL 改一个
# 字符就静默失配，PG 上 identity 不再重启，而响应仍然是 {"ok": true}，两侧都不报错。
#
# Phase 3.8 批 (1) 把那段裸事务收进了 `db.clear_all_data()`，两个后端各自实现
# 各自那半句（SQLite 删 sqlite_sequence 行 / PG 做 ALTER ... RESTART WITH 1），
# 于是这张表没有任何键了，连同 `_run_unlocked` 里的整句替换分支一起删掉。
# **不要因为"下一条 SQLite 专有语句"再把它加回来** —— 正确的做法是像 (1) 那样
# 在 `common/database.py` + `common/pgdb/` 各写一半，让差异待在有类型、有测试、
# 有 PUBLIC_API 守卫的地方，而不是待在一张按 SQL 文本匹配的字典里。

_WS_RE = re.compile(r"\s+")

# 加锁读（FOR UPDATE / FOR SHARE ...）必须留在写连接的事务里，改道读池等于
# 悄悄丢掉行锁。这些语句在本仓库里全部由事务持有者自己发。
_LOCKING_READ = ("for update", "for no key update", "for share", "for key share")


def _is_plain_read(norm: str) -> bool:
    """普通只读语句（可以安全地改道读池）。

    入参是 ``normalize_stmt()`` 的产物（压平空白 + 小写），所以 ``FOR\\n UPDATE``
    也能被认出来。字符串字面量里恰好含 "for update" 会误判成"不可改道"——
    往安全方向错。
    """
    head = norm.split(" ", 1)[0] if norm else ""
    if head not in _ROW_RETURNING_HEAD:
        return False
    return not any(k in norm for k in _LOCKING_READ)


def normalize_stmt(sql: str) -> str:
    return _WS_RE.sub(" ", sql.strip().rstrip(";").strip()).lower()


def qmark_to_numeric(sql: str) -> str:
    """把 ``?`` 换成 ``$1..$n``，跳过字符串字面量、标识符引号和注释。"""
    out: List[str] = []
    i, n, idx = 0, len(sql), 0
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(sql[i:j])
            i = j
            continue
        if ch == '"':
            j = sql.find('"', i + 1)
            j = n if j < 0 else j + 1
            out.append(sql[i:j])
            i = j
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            out.append(sql[i:j])
            i = j
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(sql[i:j])
            i = j
            continue
        if ch == "?":
            idx += 1
            out.append("$%d" % idx)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def translate_sql(sql: str) -> str:
    """pgdb 的唯一 SQL 入口翻译：LIKE 折叠改写 + ``?`` -> ``$n``。

    不含 ``?`` 的语句原样透传，所以想直接写 ``= ANY($1::bigint[])`` 也可以——
    但**同一条语句里不准混用两种占位符**。
    """
    if "?" not in sql:
        return sql
    if "$" in sql:
        raise ValueError(
            "同一条语句里混用了 ? 和 $n 占位符，翻译结果一定是错的：\n" + sql)
    sql = _LIKE_QMARK_RE.sub(r"ascii_lower(\1) LIKE ascii_lower(?) ESCAPE ''", sql)
    return qmark_to_numeric(sql)


# ============================================================
# 3) aiosqlite 形状的游标 / 连接代理
# ============================================================

class Cursor:
    """aiosqlite ``Cursor`` 的形状子集。

    支持：``await cur.fetchone()`` / ``fetchall()`` / ``async for row in cur``
    / ``cur.rowcount``，并且自身也是 async context manager（对应
    ``async with conn.execute(...) as cur:``）。

    行对象是 ``asyncpg.Record``：``row[0]`` / ``row["col"]`` / ``dict(row)``
    三种访问方式都支持，与 ``aiosqlite.Row`` 一致。
    """

    __slots__ = ("_rows", "_pos", "rowcount", "sql")

    def __init__(self, rows: Sequence[Any], rowcount: int, sql: str = ""):
        self._rows = rows
        self._pos = 0
        self.rowcount = rowcount
        self.sql = sql

    async def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    async def fetchall(self):
        rows = list(self._rows[self._pos:])
        self._pos = len(self._rows)
        return rows

    async def fetchmany(self, size: int = 1):
        rows = list(self._rows[self._pos:self._pos + size])
        self._pos += len(rows)
        return rows

    async def close(self):
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._pos >= len(self._rows):
            raise StopAsyncIteration
        row = self._rows[self._pos]
        self._pos += 1
        return row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _ExecOp:
    """``conn.execute(...)`` 的返回值：既可 ``await``，也可 ``async with``。

    aiosqlite 两种用法在本仓库里都有：
        cursor = await self._db.execute(sql, params)      # 读 rowcount
        async with self._db.execute(sql, params) as cur:  # 迭代
    """

    __slots__ = ("_proxy", "_sql", "_params", "_cursor")

    def __init__(self, proxy: "ConnProxy", sql: str, params: Sequence[Any]):
        self._proxy = proxy
        self._sql = sql
        self._params = params
        self._cursor: Optional[Cursor] = None

    def __await__(self):
        return self._proxy._run(self._sql, self._params).__await__()

    async def __aenter__(self) -> Cursor:
        self._cursor = await self._proxy._run(self._sql, self._params)
        return self._cursor

    async def __aexit__(self, *exc):
        return False


class ConnProxy:
    """一条 asyncpg 连接的 aiosqlite 形状包装。

    只暴露仓库真正用到的面：execute / executemany / executescript /
    fetch 系列 + BEGIN/COMMIT/ROLLBACK 拦截。
    """

    def __init__(self, conn: asyncpg.Connection, *, allow_tx: bool = True,
                 label: str = "", read_pool_getter=None):
        self._conn = conn
        self._allow_tx = allow_tx
        self._label = label
        self._tx: Optional[asyncpg.transaction.Transaction] = None
        # 开着的事务归哪个 asyncio.Task 所有（见 _run 里的"按事务归属路由"）。
        # 不变式：**任何给 _tx 赋值的地方都必须在同一条语句里给 _tx_owner 赋值。**
        self._tx_owner: Optional["asyncio.Task"] = None
        # 延迟取读池（而不是直接存池对象）：close() 把 _pool 置 None 之后
        # 路由自动失效，退回今天的行为。
        self._read_pool_getter = read_pool_getter
        # 单条连接上的**语句级**串行化，复刻 aiosqlite 的内部排队。
        #
        # 为什么必须有：D-2 让 ``_db`` 是**一条**专用写连接。aiosqlite 把每个
        # 操作丢到该连接自己的工作线程上排队，所以两个协程同时用同一条连接是
        # 合法的；asyncpg 不排队，直接
        #   InterfaceError: cannot perform operation: another operation is in progress
        #
        # 仓库里确实存在"不持 _write_lock 就碰 _db"的路径，它们在 SQLite 下
        # 完全合法，因此不能改（equivalence-first）：
        #   common/pgdb/batches.py  list_callback_due —— _callback_dispatcher 定时调用
        #   server/app.py:1298 / 2230 / 2281 / 2289 / 2294 / 2309 —— 裸 _db 读
        # 这些与任意持锁写路径并发就会 raise。黄金夹具看不见（它把 4 个后台
        # 协程 no-op 掉了，且 TestClient 是顺序的），但**真实服务**必然撞上。
        #
        # 锁只包住**一条**语句，不包住事务。
        #
        # ⚠ D-13 原来在这里写着"于是'另一个协程的 SELECT 插进某个开着的事务
        # 中间'这件事，两个后端行为一致（同一条连接 = 同一个事务，都能读到未提交
        # 的改动）"。**那个"一致"是拿数据安全换来的，现在已经作废**（决策 D-15）：
        # 在 PG 里，插进别人事务里的那条 SELECT 一旦报错（NUL 字节、客户端断开
        # 触发的 57014 …），整个事务立刻 abort——实测一个只读的删除预览请求
        # 就能把一个 worker 6 条结果的整批提交毁掉，任务卡在 processing。
        # 反向也成立：写方自己的错误会以 InFailedSQLTransactionError 泄漏给
        # 后台的 _callback_dispatcher。
        # 所以现在 _run 会把**非事务持有者发的普通只读语句**改道到读池
        # （见 _run / _is_foreign_tx）。代价是那条 SELECT 读到的是已提交数据而
        # 不是别人的未提交数据——而那本来就是个跟事件循环调度赛跑的脏读，
        # 没有调用方能依赖它（实测：同一个调用在事务前后都返回已提交结果）。
        #
        # 不会与 _write_lock 形成环：持有 _op_lock 期间绝不去拿 _write_lock，
        # 改道时也会**先放掉 _op_lock 再去借读池连接**。
        self._op_lock = asyncio.Lock()

    # ---- 原生连接（需要 asyncpg 专有能力时用，例如 copy / listen）----
    @property
    def raw(self) -> asyncpg.Connection:
        return self._conn

    @property
    def in_transaction(self) -> bool:
        return self._tx is not None

    # ---- aiosqlite 面 ----
    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> _ExecOp:
        return _ExecOp(self, sql, parameters or ())

    async def executemany(self, sql: str, seq_of_parameters) -> Cursor:
        stmt = translate_sql(sql)
        async with self._op_lock:
            await self._conn.executemany(stmt, [tuple(p) for p in seq_of_parameters])
        # asyncpg 的 executemany 不返回任何计数。谁要"实际插入了几行"，
        # 必须改用单条 set-based INSERT ... ON CONFLICT DO NOTHING 读命令标签
        # （见 OWNERSHIP.md 的 total_changes 条目），不要指望这里的 rowcount。
        return Cursor((), -1, stmt)

    async def executescript(self, script: str) -> Cursor:
        """多语句 DDL。asyncpg 的简单查询协议支持，但**不能带参数**。"""
        if "?" in script:
            raise ValueError("executescript 不接受占位符参数")
        async with self._op_lock:
            await self._conn.execute(script)
        return Cursor((), -1, script)

    async def begin(self):
        async with self._op_lock:
            await self._exec_tx_control("begin")

    async def commit(self):
        async with self._op_lock:
            await self._exec_tx_control("commit")

    async def rollback(self):
        async with self._op_lock:
            await self._exec_tx_control("rollback")

    async def close(self):
        return None

    # ---- 内部 ----
    def _is_foreign_tx(self) -> bool:
        """写连接上开着事务，且开它的不是当前这个 asyncio.Task。

        故意**不**判断 owner.done()：一个已经结束却没回滚的 owner，对别人来说
        照样是"外人的事务"，改道正是我们要的。
        """
        if self._tx is None:
            return False
        owner = self._tx_owner
        return owner is None or owner is not asyncio.current_task()

    async def _run(self, sql: str, params: Sequence[Any]) -> Cursor:
        # 语句级串行化（见 __init__ 里 _op_lock 的说明）。锁只包一条语句，
        # 行是即时取完的，所以 ``async with conn.execute(...) as cur`` 迭代
        # 期间并不持锁。
        #
        # 按事务归属路由（决策 D-15，F1/F2）：判定在 _op_lock 里做——BEGIN 也走
        # 同一把锁，所以"判定完再被人插进一个 BEGIN"不可能发生；一旦决定改道，
        # **先放锁**再去借读池连接，绝不举着 _op_lock 等池。
        async with self._op_lock:
            if not (self._is_foreign_tx() and _is_plain_read(normalize_stmt(sql))):
                return await self._run_unlocked(sql, params)
            pool = self._read_pool_getter() if self._read_pool_getter else None
            if pool is None:
                # 池还没建 / 已经关了：退回写连接，与今天的行为一致。
                return await self._run_unlocked(sql, params)
        conn = await pool.acquire()
        try:
            # 这个一次性代理走 _run_unlocked，不会再进路由逻辑；allow_tx=False
            # 保证它的 _tx 永远是 None。
            return await ConnProxy(conn, allow_tx=False,
                                   label="routed-read")._run_unlocked(sql, params)
        finally:
            await pool.release(conn)

    async def _run_unlocked(self, sql: str, params: Sequence[Any]) -> Cursor:
        norm = normalize_stmt(sql)

        # (a) 事务控制
        if norm in _TX_BEGIN or norm in _TX_COMMIT or norm in _TX_ROLLBACK:
            await self._exec_tx_control(norm)
            return Cursor((), -1, sql)

        # (b) 这里曾是"SQLite 专有语句整句替换"的分支，见本文件上方
        #     `_STATEMENT_OVERRIDES` 退休的说明（Phase 3.8 批 (1)）。

        stmt = translate_sql(sql)
        args = tuple(params or ())

        head = norm.split(" ", 1)[0] if norm else ""
        returns_rows = (
            head in _ROW_RETURNING_HEAD
            # WITH：看**外层**语句是什么。只读 CTE（外层是 SELECT/VALUES/TABLE）
            # 一定返回行；数据修改型 CTE 仍然要靠 RETURNING 判断。
            # 详见 cte_outer_head 的 docstring —— 判错的代价是静默返回空结果集。
            or (head == "with"
                and (cte_outer_head(norm) in ("select", "values", "table")
                     or bool(_RETURNING_RE.search(norm))))
            or (head in ("insert", "update", "delete")
                and bool(_RETURNING_RE.search(norm)))
        )
        if returns_rows:
            rows = await self._conn.fetch(stmt, *args)
            # aiosqlite 对 SELECT 也给 -1，保持一致，避免有人误把它当影响行数
            return Cursor(rows, -1, stmt)

        tag = await self._conn.execute(stmt, *args)
        return Cursor((), rowcount_from_tag(tag), stmt)

    async def _exec_tx_control(self, norm: str):
        if not self._allow_tx:
            raise RuntimeError(
                "只读连接上不允许事务控制语句（SQLite 侧读池同样不接受写事务）")
        if norm in _TX_BEGIN:
            if self._tx is not None:
                owner = self._tx_owner
                if (owner is not None and owner is not asyncio.current_task()
                        and not owner.done()):
                    # 另一个**仍然活着**的协程正持有这条写连接上的事务。它随时
                    # 可能 COMMIT，替它回滚就是一个协程丢掉另一个协程的工作。
                    # 保持原来的显式失败。
                    raise RuntimeError("嵌套 BEGIN：上一个事务还没结束")
                # 到这里说明上一个事务是**被遗弃**的：要么开它的 Task 已经结束
                # （异常/取消逃出了 BEGIN 块），要么就是本 Task 自己又来开一次。
                # 这是 F3——一个失败请求把整条写路径永久焊死的根因。真回滚掉它，
                # 把"永久停摆"降级成"那一个请求 500"。
                # ⚠ 必须**真的 rollback**，只清标志位不行：F3 的实际触发点
                #   （POST /api/tasks/release 的 DataError 由 asyncpg 客户端侧
                #   抛出）BEGIN 已经上了服务端，服务端留着一个 idle in
                #   transaction 的真事务，握着锁、钉着 xmin horizon。
                logger.error(
                    "写连接上发现被遗弃的事务（owner=%r, 服务端仍在事务中=%s），"
                    "回滚后继续本次 BEGIN。上一个写请求多半是异常/取消逃出了 "
                    "BEGIN 块而没回滚。", self._tx_owner,
                    self._safe_in_transaction())
                await self._abort_dangling()
            tx = self._conn.transaction()
            self._tx, self._tx_owner = tx, asyncio.current_task()
            try:
                await tx.start()
            except BaseException:
                # BEGIN 本身失败（连接断了、被取消……）。不能留下一个"幽灵外人
                # 事务"，否则路由和上面的守卫都会被它骗到。
                self._tx, self._tx_owner = None, None
                self._release_top_xact(tx)
                raise
        elif norm in _TX_COMMIT:
            if self._tx is None:
                return
            tx, self._tx, self._tx_owner = self._tx, None, None
            await tx.commit()
        else:  # rollback
            if self._tx is None:
                return
            tx, self._tx, self._tx_owner = self._tx, None, None
            await tx.rollback()

    def _safe_in_transaction(self) -> Optional[bool]:
        try:
            return self._conn.is_in_transaction()
        except Exception:  # noqa: BLE001
            return None

    def _release_top_xact(self, tx) -> None:
        """asyncpg 内部状态的兜底清理。

        ``Transaction.start()`` 会**先**把 ``conn._top_xact = self`` 再发 BEGIN；
        发失败时它只把自己标成 FAILED，``_top_xact`` 留着不动。下一次
        ``conn.transaction().start()`` 看见 ``_top_xact`` 非空就会以为是嵌套事务，
        改发 ``SAVEPOINT`` ——静默地把之后所有"事务"都变成假事务。
        ``Transaction.rollback()`` 在状态非 STARTED 时也会先抛 InterfaceError、
        同样清不掉。所以这里显式收尾。
        """
        if getattr(self._conn, "_top_xact", None) is tx:
            try:
                self._conn._top_xact = None
            except Exception:  # noqa: BLE001
                pass

    async def _abort_dangling(self) -> bool:
        """把挂着的事务真正回滚掉；返回是否确实回收了一个。

        调用方要么已经持有 ``_op_lock``（_exec_tx_control 路径），要么此刻没有
        并发（close()）。需要带锁的版本用 ``reclaim_abandoned_tx()``。
        """
        tx = self._tx
        if tx is None:
            return False
        self._tx, self._tx_owner = None, None
        try:
            await tx.rollback()
        except Exception:  # noqa: BLE001
            # 回滚本身失败（事务对象状态不对、连接断了……）。清 asyncpg 的
            # _top_xact，再直接对连接发一条裸 ROLLBACK 兜底——否则服务端可能
            # 留着一个开着的事务，而下一次 start() 会撞上 asyncpg 的
            # "cannot use Connection.transaction() in a manually started
            # transaction"，那又是一次永久停摆。
            self._release_top_xact(tx)
            if self._safe_in_transaction():
                try:
                    await self._conn.execute("ROLLBACK")
                except Exception:  # noqa: BLE001
                    pass
        else:
            self._release_top_xact(tx)
        return True

    async def reclaim_abandoned_tx(self) -> bool:
        """带 ``_op_lock`` 的 ``_abort_dangling``（供写锁释放时调用）。"""
        async with self._op_lock:
            return await self._abort_dangling()

    # ---- 明确不支持的东西：早失败，别静默给错数 ----
    @property
    def total_changes(self) -> int:
        raise NotImplementedError(
            "asyncpg 没有连接级的 total_changes 计数器。"
            "要'实际插入了几行'，改用单条 "
            "INSERT ... SELECT unnest(...) ON CONFLICT DO NOTHING 并读命令标签"
            "（rowcount_from_tag）。见 OWNERSHIP.md。")


# ============================================================
# 4) 写锁 —— 释放时回收被遗弃的事务（F3 的结构性修复）
# ============================================================

class WriteLock(TimedLock):
    """``TimedLock`` + "释放写锁时，写连接上不许还挂着事务"这条不变式。

    为什么必须在**锁释放**这个点做（决策 D-15）：

    本仓库里"在 ``_db`` 上开事务"的前置条件只有一个——持有 ``_write_lock``
    （pool._tx() 的 docstring、_db 的 docstring、server/app.py 的 7 个裸
    BEGIN 块全都是这么写的）。反过来说：**写锁被释放而事务还开着 = 那个事务
    再也不会有人去 COMMIT/ROLLBACK 了**，因为下一个拿到锁的协程只会开自己的。

    于是这里是唯一一个能"零误判"识别被遗弃事务的位置，而且它一次性覆盖所有
    出错形状，不用去每个调用点补 try/except：

      * 异常从 ``async with db._write_lock:`` 里逃出去（F3。app.py 的 7 个块
        后来补了 ``_rollback_quietly``，但 common/pgdb 里仍有只 catch
        ``Exception`` 的块，例如 tasks.py:214 的 pull_tasks）；
      * ``CancelledError``（客户端断开）从只 catch ``Exception`` 的块里逃出去
        （F4。SQLite 侧同样会卡死，所以这里是严格改善，不是分叉）；
      * 以后任何人新写的、忘了回滚的 BEGIN 块。

    正常路径上一条语句都不多：``_tx`` 是 None 时直接返回。回滚发生在**释放锁
    之前**，所以下一个等锁的协程醒来时连接一定是干净的。
    """

    def __init__(self, proxy_getter=None):
        super().__init__()
        # 延迟取写代理：PoolMixin.__init__ 建锁时 _write_proxy 还不存在。
        self._proxy_getter = proxy_getter

    async def _do_exit(self):
        try:
            proxy = self._proxy_getter() if self._proxy_getter else None
            if proxy is not None and proxy._tx is not None:
                try:
                    await proxy.reclaim_abandoned_tx()
                    logger.error(
                        "写锁释放时写连接上还挂着事务——已回滚。调用方多半是"
                        "异常/取消逃出了 BEGIN 块。若不回收，之后每一次 BEGIN "
                        "都会撞上'嵌套 BEGIN'守卫，整条写路径永久停摆。")
                except Exception:  # noqa: BLE001
                    # 绝不能盖掉正在传播的真实异常。
                    logger.exception("回收被遗弃事务失败")
        finally:
            # 计时/统计与锁释放照旧（LOCK_STATS 的形状是黄金基线 step 56 钉死的）
            await super()._do_exit()


# ============================================================
# 5) PoolMixin
# ============================================================

class PoolMixin:
    """Database 的连接层。MRO 里必须排第一，它的 __init__/_db/read 要赢。

    其余 mixin **不得**定义 __init__，也不得自己建连接；一律通过
    ``self._db`` / ``self.read()`` / ``self._tx()`` / ``self._write_lock``。
    """

    # 子类/实例属性声明（便于阅读，不是运行时约束）
    _pool: Optional[asyncpg.Pool]
    _write_conn: Optional[asyncpg.Connection]
    _write_proxy: Optional[ConnProxy]

    def __init__(self, db_path: str = None):
        # db_path 保留纯粹是为了签名兼容：server/app.py 与测试都可能传。
        # PG 后端忽略它，DSN 来自 config.PG_DSN / 环境变量 PG_DSN。
        self.db_path = db_path or config.DB_PATH
        self.dsn = os.environ.get("PG_DSN") or config.PG_DSN
        self._pool = None
        self._write_conn = None
        self._write_proxy = None
        # 与 SQLite 版共用同一个 TimedLock 基类和同一个 LOCK_STATS 全局容器，
        # /api/_debug/lock-stats 的 JSON 形状才不会变（黄金基线 step 56 钉死了
        # waits/holds 的三个 caller key 与 stage_timings 的四个 stage key）。
        # PG 侧多一条不变式：释放写锁时不许还挂着事务（见 WriteLock）。
        self._write_lock = WriteLock(lambda: self._write_proxy)
        # 兼容 SQLite 版的属性名（server 端有代码读过这些；保留但无意义）
        self._read_pool = None
        self._read_conns: List[Any] = []
        self._read_pool_size = config.PG_POOL_MAX
        self._maintenance_task: Optional[asyncio.Task] = None

    # ---------------- 生命周期 ----------------
    async def connect(self):
        """建池 + 建写连接 + 建表。对应 SQLite 版的 connect()。"""
        self._pool = await asyncpg.create_pool(
            dsn=self.dsn,
            min_size=config.PG_POOL_MIN,
            max_size=config.PG_POOL_MAX,
            # D-7：Phase 1 关预备语句缓存。动态拼接的 UPDATE/INSERT 文本组合爆炸，
            # 且 asyncpg 会冻结首次推断的参数类型 OID。
            statement_cache_size=0,
            command_timeout=config.PG_COMMAND_TIMEOUT,
            server_settings={"search_path": "public"},
        )
        self._write_conn = await asyncpg.connect(
            dsn=self.dsn,
            statement_cache_size=0,
            command_timeout=config.PG_COMMAND_TIMEOUT,
            server_settings={"search_path": "public"},
        )
        self._write_proxy = ConnProxy(self._write_conn, allow_tx=True, label="write",
                                      read_pool_getter=lambda: self._pool)

        await self.init_tables()          # SchemaMixin
        await self._warm_pool()
        logger.info("PostgreSQL 连接就绪：pool=%d~%d + 1 条写连接",
                    config.PG_POOL_MIN, config.PG_POOL_MAX)

    async def _warm_pool(self):
        """预热每条池连接。

        冷连接的第一次往返有握手/规划开销；黄金基线里 ``slow_holds_recent`` 是
        **空列表**，而 _record_hold 在持锁 >200ms 时会往里塞元素。第一笔
        accept_results_batch 撞上冷池就会让那个列表非空 → 列表长度 diff。
        """
        if not self._pool:
            return
        conns = []
        try:
            for _ in range(config.PG_POOL_MIN):
                c = await self._pool.acquire()
                await c.execute("SELECT 1")
                conns.append(c)
        finally:
            for c in conns:
                await self._pool.release(c)
        if self._write_conn:
            await self._write_conn.execute("SELECT 1")

    async def _open_read_pool(self):
        """SQLite 专有的只读连接池。PG 下由 asyncpg.Pool 承担，这里是 no-op。

        公开面保留是因为它在 API 清单里（#3），而且 connect() 之外可能有人调。
        """
        return None

    async def close(self):
        if self._maintenance_task and not self._maintenance_task.done():
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass
        self._maintenance_task = None
        if self._write_proxy is not None:
            await self._write_proxy._abort_dangling()
        if self._write_conn is not None:
            try:
                await self._write_conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._write_conn = None
            self._write_proxy = None
        if self._pool is not None:
            try:
                await self._pool.close()
            except Exception:  # noqa: BLE001
                pass
            self._pool = None

    # ---------------- 连接入口 ----------------
    @property
    def _db(self) -> Optional[ConnProxy]:
        """写连接代理。

        SQLite 版的 ``self._db`` 是**一条**连接，17 个 BEGIN/COMMIT 块靠
        ``_write_lock`` 串行化。这里刻意维持同一形状：一条专用写连接 + 真锁。
        server/app.py 的 ``async with db._write_lock: ... db._db.execute('BEGIN')
        ... db._db.execute('COMMIT')`` 因此逐字可用，并且 app.py:1510 那种
        "退出锁块之后才读 cursor.rowcount" 也仍然正确。

        ⚠ 任何写路径都必须在 ``self._write_lock`` 里使用 ``_db``。
        """
        return self._write_proxy

    @asynccontextmanager
    async def read(self):
        """从 asyncpg 池借一条连接（池满则排队，形成读侧背压）。

        与 SQLite 版形状一致：``async with db.read() as rc, rc.execute(sql, p) as c:``
        回退分支也保留（池未就绪时退化到写连接），与 database.py:363-364 一致。
        """
        _t0 = time.perf_counter()
        if self._pool is None:
            record_pool_wait((time.perf_counter() - _t0) * 1000)
            yield self._write_proxy
            return
        conn = await self._pool.acquire()
        # 池满时 acquire() 会在这里排队。这个数以前谁也看不到，诊断"页面慢"
        # 只能靠外部采 pg_stat_activity 反推池满没满 —— 现在它就在
        # /api/_debug/lock-stats 的 waits.read_pool 里。
        record_pool_wait((time.perf_counter() - _t0) * 1000)
        proxy = ConnProxy(conn, allow_tx=False, label="read")
        try:
            yield proxy
        finally:
            await self._pool.release(conn)

    @asynccontextmanager
    async def _tx(self):
        """写事务：``async with self._tx() as conn:`` —— 取代裸 BEGIN/COMMIT。

        调用方**必须**已经持有 ``self._write_lock``（与 SQLite 版一致）。
        asyncpg 的 transaction 上下文对 CancelledError 是安全的，顺带修掉了
        catalog_sync_audit.md:130 记的"取消让共享连接永久卡在事务里"。
        """
        proxy = self._write_proxy
        if proxy is None:
            raise RuntimeError("数据库未连接")
        # 走代理自己的事务控制（而不是 proxy.raw.transaction()），有两个原因：
        # 1) BEGIN/COMMIT 也必须过 _op_lock。raw.transaction() 直接在裸连接上
        #    发语句，会绕开语句级串行化，于是"另一个协程正在 fetch"时开事务
        #    就抛 InterfaceError —— 这正是 _callback_dispatcher 撞上的那条路径。
        # 2) 与 execute("BEGIN") 共用同一个 proxy._tx 状态位，两套事务写法
        #    因此可以互相看见（嵌套 BEGIN 仍会被显式挡下）。
        await proxy.begin()
        try:
            yield proxy
        except BaseException:
            # BaseException 而非 Exception：CancelledError 也必须回滚，
            # 否则那条唯一的写连接会永远卡在事务里
            # （catalog_sync_audit.md:130 记的就是这个故障）。
            await proxy.rollback()
            raise
        else:
            await proxy.commit()

    # ---------------- 供 mixin 使用的小工具 ----------------
    @staticmethod
    def rowcount_from_tag(tag: Any) -> int:
        return rowcount_from_tag(tag)

    @staticmethod
    def text_affinity(v: Any) -> Optional[str]:
        return text_affinity(v)

    @staticmethod
    def as_int(v: Any, default: Optional[int] = None) -> Optional[int]:
        return as_int(v, default)
