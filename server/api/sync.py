"""Phase 3 —— catalog_sync 拉取契约的服务端实现（**PostgreSQL 专属**）。

计划 `.agent/pg_migration_plan.md` §5 是本文件的规格书；对外文档是
`docs/sync_contract.md`（交给沃尔玛侧的那一份）。四个端点：

    GET  /api/v1/sync/records?after_seq=&limit=&outcomes=
    GET  /api/v1/sync/status
    GET  /api/v1/sync/counts?from_seq=&to_seq=[&bucket=hour]
    POST /api/v1/sync/ack
    POST /api/v1/sync/ack-prune          （Phase 6 加的第五个：确认强制裁剪闩锁）

------------------------------------------------------------------------
承重约束（每一条都来自一次实测或一次事故推演，改之前先读完）
------------------------------------------------------------------------

1. **``/api/v1`` 前缀是结构性的，不是审美。**
   挂在 ``/api/results/*`` 或 ``/api/export/*`` 下会被 server/app.py:1856 的
   ``@app.get("/api/results/{asin}")`` 和 :2039 的
   ``@app.get("/api/export/{batch_name}")`` 吃掉 —— 它们对不认识的名字回
   **404**，而消费者按 §5.5 会把 404 读成「这个批次/ASIN 不存在」或者更糟：
   读成「暂无数据」，于是游标**永不推进**，静默停止同步。
   ``/api/v1/*`` 下没有任何 path-param 路由，不存在这个吞噬面。
   tests/pgdb/test_sync_api.py::test_the_api_v1_prefix_is_load_bearing 把
   「三个前缀分别会发生什么」钉死成用例。

2. **``db`` 必须惰性解析。**
   模块级 ``from server.app import db`` 是循环导入（server/app.py 会 import
   本模块来挂路由），启动即崩 —— 那等于**整个 erpAPI 全线下线**，代价远大于
   同步流本身。所以一律 ``from server import app as _srv; _srv.db``，
   在**每次请求里**取，顺带还解决了 lifespan 之前 ``db is None`` 的窗口。

3. **一个快照。** ``MIN(seq)`` / ``MAX(seq)`` / 页查询必须在同一个
   ``REPEATABLE READ READ ONLY`` 事务里。否则保留期（Phase 6 的 DROP 分区）
   可以卡在 MIN 与页查询之间跑完：MIN 读到裁剪前的旧下界 → 守卫放行 →
   页查询读到裁剪后的表 → 消费者拿到一个**跳过了被裁区间**的 200，
   而它需要的是 409。丢的数据没有任何一侧会察觉。
   页查询之后**再复核一次**下界，并取两次的**较大者**（保守方向）：
   多一次 409 的代价是一次全量对账，漏一次 409 的代价是永久丢数据。
   在 REPEATABLE READ 下两次读必然相等，复核因此是**零成本的断言** ——
   它守的是「有人把隔离级别降成 READ COMMITTED」这类未来改动。

4. **本端点永不对空结果返回 404。** 空 = ``200 + records: [] + has_more: false``。
   这与既有的导出端点（``/api/export/{batch_name}`` 找不到批次回 404）**故意不同**：
   那些端点面向人，本端点面向一个会把 404 当成终态的自动消费者。

5. **鉴权留口，不加中间件。** router 级 ``dependencies=SYNC_DEPENDENCIES``
   （现在是空列表）。加全局中间件会**当场**打死所有 worker 与 erpAPI ——
   本服务今天一处鉴权都没有。将来一行：
   ``SYNC_DEPENDENCIES.append(Depends(verify_sync_token))``。

6. **``include_in_schema=False``。** ``/openapi.json`` 是黄金基线 step 5，
   逐字节钉死，而 erpAPI 可能在消费它。新端点进 schema = 64 步当场挂。

7. **借一条池连接、用完还回去。** 不学 ``iter_results`` 那样为了一次导出
   把连接攥一整趟：那是 20 条池连接里的一条，消费者一慢就把写路径饿死。
   一页 = 一次借还。

8. **SQLite 上 503，而不是「不挂路由」。**
   理由见 ``_unavailable()`` 的注释。

9. **保留期的东西一律走 ``db.*`` 方法，本文件不 import ``common.pgdb.retention``。**
   理由与第 2 条同源、与 ``VALID_OUTCOMES`` 是字面量同源：本文件被 server/app.py
   在模块级 import，而 pgdb 包会把 asyncpg 拖进每一次启动（SQLite 部署根本没装）。
   ``db`` 在 PG 后端下就是 ``common.pgdb.Database``，它身上有 ``retention_observe``
   / ``acknowledge_forced_prunes`` —— 用 ``hasattr`` 探一下就够，一行 import 都不要。
"""
from __future__ import annotations

import json
import logging
import shutil
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse

from common.core import completeness as _completeness
from common.core.timeutil import iso_utc, now_iso_utc

logger = logging.getLogger(__name__)

# ============================================================
# 契约常量
# ============================================================

#: 契约版本。与 common/pgdb/schema.py:EVENT_CONTRACT_VERSION 同源，
#: 但对外是 **int**（sync_meta 存的是 text）。
CONTRACT_VERSION = 1

#: /records 的 limit 上限。原本是**独立于** /api/results 的（计划 §5.1 要求
#: 不去动那个既有端点的上限，当时它是 le=200）。现在两者同为 1000 ——
#: /api/results 的上限已按新库实测重定，出处见
#: server/api/results.py:MAX_PAGE_LIMIT 那段注释。**仍然是两个独立常量**：
#: 这里的 1000 是事件流契约的，那边的 1000 是响应体/内存口径推出来的，
#: 只是恰好落在同一个数上，别把其中一个改成引用另一个。
MAX_LIMIT = 1000
DEFAULT_LIMIT = 200

#: /counts 的区间宽度上限（计划 §5.3）。超出 422。
MAX_COUNTS_SPAN = 8_000_000

#: `seq` 是 bigint。超过这个值的入参必须在**到达 SQL 之前**挡下来 ——
#: asyncpg 会抛 `OverflowError`/`DataError`，那是 500，而这明明是个参数错误。
#: 消费者的游标存成 Python int 或 JS Number 时，一次坏加法就能造出这种值。
MAX_BIGINT = 2 ** 63 - 1

#: outcome 封闭集。**字面量，不 import `common.pgdb.schema`。**
#:
#: 本文件被 server/app.py 在**模块级**import，而 common/dbfactory.py 的设计约束是
#: 「SQLite 部署不需要装 asyncpg，只有真的选了 postgres 才加载 pgdb」。
#: 从 schema 取这五个字符串会把整个 pgdb 包（连带 asyncpg）拖进每一次启动，
#: 换来的只是省掉一行字面量 —— 不值。
#: 「两份真源不许漂」这件事由 tests/pgdb/test_sync_api.py 的
#: ``test_valid_outcomes_matches_the_schema`` 看守（那是测试期，随便 import）。
VALID_OUTCOMES = ("ok", "not_found", "blocked", "parse_failed", "stale")

#: 错误码封闭集。`_err` 的**第二个位置实参**只能取自这里。
#:
#: 为什么落在这个文件、而不是新建 `server/api/errors.py`：上面几行的
#: `VALID_OUTCOMES` 就是「封闭集 + 静态守卫用例」这个模式的原产地，
#: 而 `export_incremental.py` 本来就 `from server.api import sync as _sync`。
#: 多一个模块只多一条 import 边，不多一分保障。
#:
#: 前 9 个是今天 46 处 `_err` 调用点实际在用的码（AST 扫 `server/` 全树去重；
#: `server/api/sync.py` 40 处 + `export_incremental.py` 6 处）。
#: `internal_error` 是全局 500 处理器（`server/app.py` 的
#: `_unhandled_exception_handler`）用的，**不经过 `_err`**，最容易被漏掉。
#:
#: 漂移由 `tests/test_error_codes.py` 看守：调用点 ⊆ 本集合、
#: 文档里出现的码 ⊆ 本集合（**单向**，理由见该文件头）。
ERROR_CODES = frozenset({
    "ack_ahead_of_stream",
    # `POST /api/upload` 撞名 -> 409。走 HTTPException 而非 `_err`（它不属于
    # catalog_sync 契约，形状是 FastAPI 的 {"detail": {...}}），所以 AST 扫描
    # 扫不到调用点 —— 与 `internal_error` 同例，由 tests/test_error_codes.py 里
    # 一条显式断言看守。登记在这里是为了让「错误码封闭集」名副其实：
    # 一个对外发出去的机器读码不在册，这个集合就只是半个真源。
    "batch_name_conflict",
    # `GET /api/export/batch/{batch_name}/records` 的唯一 404 含义：**批次名不存在**。
    # 批次存在但一条事件都没有回 `200 + records: []` —— 404 在这一族端点里
    # 只能有一个含义，否则消费方分不清"打错名字"和"还没采"（见
    # export_incremental.py 文件头第 3 条）。
    "batch_not_found",
    # `POST /api/batches`（JSON 推送）里，同一次请求给同一个 ASIN 两个**不同**
    # 邮编 -> 400。库里表达不了（`tasks` 是 UNIQUE(batch_id, asin)，一个批次里
    # 一个 ASIN 只能有一个邮编），所以明确拒绝而不是静默取第一个。
    # 与 `batch_name_conflict` 同例：走 HTTPException 不走 `_err`，AST 扫不到，
    # 靠 tests/test_multi_zip_same_asin.py 的显式断言看守。
    "conflicting_zip_for_asin",
    "cursor_ahead_of_stream",
    "cursor_below_retention",
    "event_stream_unavailable",
    "export_token_not_configured",
    "gen_mismatch",
    "internal_error",
    "invalid_export_token",
    "invalid_parameter",
    "range_too_wide",
    # 取图端点（`GET /api/screenshots/{batch_name}/{asin}`）的两个可重试判据：
    #   screenshot_pending -> 409，还没截好，**回头再来**（带 Retry-After）
    #   screenshot_failed  -> 410，截失败了，**不会再有**
    # 这两个码存在的全部意义就是让调用方分清这两种情形（合并成 404 就等于
    # 让人无限轮询一个永远不会出现的文件）。同样走 HTTPException、AST 扫不到，
    # 由 tests/test_screenshot_api.py 的 test_all_four_outcomes_are_distinct 看守。
    #
    # ⚠ 登记它们不是为了过用例 —— `tests/test_error_codes.py` 的文档提取器当时
    # 并没有捞到这两个（它们在 md 表格单元格里）。是按本集合注释里那句
    # 「一个对外发出去的机器读码不在册，这个集合就只是半个真源」补的。
    "screenshot_failed",
    "screenshot_pending",
})

#: `_err` 自己组装的键。`extra` 里的同名键会被**丢掉**，不是覆盖。
_ERR_RESERVED_KEYS = frozenset({"error", "detail", "server_time_utc"})

#: ``completeness`` 位图。计划 Phase 2 收口时留给 Phase 3 的必答题：
#: Phase 2 观测不到 HTML 区块存在性，所有行都是 ``0`` = **未测量**。
#: 位 0/1/2 = 面包屑 / 详情表 / 主图集；位 3（值 8）= MEASURED。
#: ``completeness_ok`` 因此是 **合取**：既要「测过」，又要「三块齐全」。
#: Phase 4 落地之前这个值恒为 false —— 契约文档里必须写死这一点，
#: 否则沃尔玛侧的 catalog.products 会一直是空的而没人知道为什么。
#:
#: P4.5：数值与判据的唯一真源都是 ``common/core/completeness.py``。
#: 下面两个名字是**保留的别名**（tests/pgdb/test_phase4_fields.py:617 与
#: server/api/export_incremental.py 都按它们 import，外部可见面一字不变）。
COMPLETENESS_MEASURED_BIT = _completeness.MEASURED
COMPLETENESS_REQUIRED_MASK = _completeness.REQUIRED_MASK

#: 鉴权留口。**空列表是刻意的**（用户已决定暂不加鉴权）。
#: 加的时候只改这一行，不要去动 include_router，也不要加全局中间件。
SYNC_DEPENDENCIES: List[Any] = []

router = APIRouter(
    prefix="/api/v1/sync",
    tags=["sync"],
    dependencies=SYNC_DEPENDENCIES,   # 鉴权留口，暂空
    include_in_schema=False,          # 保 /openapi.json 逐字节不变
)

#: 事件流的列清单。**显式列出**而不是 ``SELECT *``：加一列就是改契约，
#: 必须是一次有意识的编辑（同样的理由让 verify_schema 钉死了 asin_data 的列序）。
_RECORD_COLUMNS = (
    "seq", "source_id", "gen", "asin",
    "marketplace", "zip_requested", "zip_observed", "zip_verify",
    "collected_at", "recorded_at",
    "outcome", "completeness", "error_type", "error_detail",
    "batch_id", "task_id", "worker_id", "attempt", "parse_engine",
    "review_hash", "slow_hash", "hash_ver",
    "payload",
)
_RECORD_SELECT = ", ".join(_RECORD_COLUMNS)

_META_KEYS = ("contract_version", "gen", "instance_id",
              "max_seq_ever", "ack_seq", "ack_at", "forced_prune_log")


# ============================================================
# 小工具
# ============================================================

# `_iso` / `_now_iso` 现在是 common/core/timeutil 的**别名**（Phase 4.4）。
# 之前 common/pgdb/retention.py 有一份逐字节相同的 `_iso`；真源落在 common/core
# 而不是这里 —— retention 属于 common，让它 import server.api.sync 是分层倒置。
# 别名保留原名：server/api/export_incremental.py 按 `_sync._iso(...)` 调它。
_iso = iso_utc
_now_iso = now_iso_utc


def _as_int(v: Any) -> Optional[int]:
    """sync_meta 的值是 text。非数字一律当「没有」，绝不抛。"""
    if v is None:
        return None
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _err(status: int, code: str, detail: str, **extra: Any) -> JSONResponse:
    """统一错误形状。

    ``error`` 是**机器读**的稳定枚举（§5.5 的伪码就是 ``ALARM(r.error)``），
    ``detail`` 是人读的。诊断用的边界值一并带上，让告警不必再发一次请求。
    """
    body: Dict[str, Any] = {"error": code, "detail": detail,
                            "server_time_utc": _now_iso()}
    # 曾经是裸的 `body.update(extra)`：`_err(503, "x", "y", error="oops")` 会把
    # 机器读的 `error` 静默改掉。这里**先剔除保留键**。
    #
    # 刻意**不**改成「命中保留键就 raise」：那会把一个本该回 409
    # `cursor_below_retention` 的请求变成 500 —— 用一个错误处理的 bug
    # 换掉一次本来正确的错误处理。剔除 + `tests/test_error_codes.py` 的
    # 守卫用例就够了。
    body.update({k: v for k, v in extra.items() if k not in _ERR_RESERVED_KEYS})
    return JSONResponse(status_code=status, content=body)


def _database():
    """惰性取 db。**不要**改成模块级 import —— 见文件头第 2 条。"""
    from server import app as _srv
    return _srv.db


def _unavailable() -> Optional[JSONResponse]:
    """事件流不可用时返回 503，可用时返回 None。

    为什么是 **503 而不是「SQLite 上不挂路由」**（两条路都被允许，这里选了 503）：

    * 路由表在 ``import server.app`` 的那一刻定型，而 ``DB_BACKEND`` 是运行期
      环境变量 —— 条件挂载会让「路由存不存在」变成 import 时序的函数。
      本仓库的测试与黄金夹具都在 import 之后改后端，那种路由表必然骗人。
    * 不挂载 = 404，而 404 正是第 1 条要躲的东西：消费者把它读成
      「暂无数据 / 没这个资源」并静默停摆。**503 是不会被误读的**。
    * 与既有的 ``/api/_debug/event-stream`` 口径一致：SQLite 上如实回
      「事件流是 PG 专属」，而不是 404/500。运维拿同一个 URL 探两种部署。

    黄金样本不受影响：64 步里没有任何一步碰 ``/api/v1/*``，而
    ``include_in_schema=False`` 让 ``/openapi.json`` 逐字节不变（有用例钉住）。
    """
    from common.dbfactory import get_backend, is_postgres

    db = _database()
    if not is_postgres():
        return _err(503, "event_stream_unavailable",
                    "事件流是 PostgreSQL 专属；当前后端不提供同步流。",
                    backend=get_backend())
    if db is None:
        return _err(503, "event_stream_unavailable",
                    "数据库尚未初始化（服务仍在启动中）。",
                    backend=get_backend())
    if not hasattr(db, "event_relay_metrics"):
        return _err(503, "event_stream_unavailable",
                    "当前 Database 实现没有事件流。", backend=get_backend())
    return None


@asynccontextmanager
async def _snapshot(*, readonly: bool = True, isolation: str = "repeatable_read"):
    """借一条池连接 + 开一个事务，退出即归还。

    默认 ``REPEATABLE READ READ ONLY`` —— 三个读端点要的就是「一个快照」。

    ``/ack`` 显式降到 ``read_committed``，这是实测逼出来的，不是偷懒：
    REPEATABLE READ 下两个并发 ack 打同一行 ``sync_meta``，后到的那个直接

        asyncpg.exceptions.SerializationError: could not serialize access
        due to concurrent update

    也就是 500。而 ack 的单调性根本不需要快照：``GREATEST`` 在**一条**
    ``ON CONFLICT DO UPDATE`` 里做，READ COMMITTED 下 ON CONFLICT 会重读被更新
    的行再算 GREATEST，语义正确且不会互相踩。用例
    ``test_ack_is_monotonic_under_concurrency`` 两边都钉住。

    产出**裸 asyncpg 连接**而不是 ConnProxy：这里要的是 ``$1`` 占位符与
    ``conn.transaction(...)``，ConnProxy 的 ``?`` 翻译层与 ``allow_tx=False``
    在这条路径上只会碍事。连接是从池里独占借出来的，没有并发，
    因此绕开 ``_op_lock`` 是安全的。

    **绝不碰写连接。** ``db.read()`` 在池未就绪时会退回 ``_write_proxy``
    （pool.py:940），那是 D-2 的那条唯一写连接，只能在 ``_write_lock`` 里用。
    在它上面开事务会把某个 accept_results_batch 的提交拖进我们的事务里。
    所以这里显式识别并拒绝，宁可 503。
    """
    db = _database()
    async with db.read() as rc:
        conn = getattr(rc, "raw", rc)
        if conn is getattr(db, "_write_conn", None):
            raise _PoolUnavailable()
        tx = conn.transaction(isolation=isolation, readonly=readonly)
        await tx.start()
        try:
            yield conn
        except BaseException:
            await tx.rollback()
            raise
        else:
            # 只读事务没有什么要提交的；rollback 更便宜也更诚实。
            # 写事务（/ack）必须 commit。
            if readonly:
                await tx.rollback()
            else:
                await tx.commit()


class _PoolUnavailable(RuntimeError):
    """连接池还没就绪 —— 唯一能借到的是那条写连接，不能用。"""


async def _read_meta(conn) -> Dict[str, str]:
    rows = await conn.fetch(
        "SELECT k, v FROM scraper.sync_meta WHERE k = ANY($1::text[])",
        list(_META_KEYS))
    return {r["k"]: r["v"] for r in rows}


async def _bounds(conn) -> Tuple[Optional[int], Optional[int]]:
    """``(min(seq), max(seq))``，空表时 ``(None, None)``。

    刻意**不**用 ``count(*)`` 判空：那是全表 seq scan，2000 万行的分区上
    每次拉取都跑一遍。``min(seq) IS NULL`` 是同一件事，而且实测走的是
    每个分区的 PK 上的 Index Only Scan（见 scratchpad/p3/probe_jsonb.py 的
    EXPLAIN 输出）。
    """
    row = await conn.fetchrow(
        "SELECT min(seq) AS min_seq, max(seq) AS max_seq FROM scraper.scrape_events")
    return (row["min_seq"], row["max_seq"]) if row else (None, None)


def _window(min_seq: Optional[int], max_seq: Optional[int],
            max_seq_ever: Optional[int]) -> Tuple[int, int]:
    """把 ``(min(seq), max(seq), max_seq_ever)`` 折成契约的 ``(min_available_seq, max_seq)``。

    **这段是 Phase 3 唯一一处「计划没写死、必须自己定」的语义**，三种状态
    都推演过（用例：test_empty_stream_* / test_pruned_to_empty_*）：

    ============ ================= ================= ==========================
    表状态        min_available_seq  max_seq           后果
    ============ ================= ================= ==========================
    非空          min(seq)          max(seq)          常态
    全新空库      1                 0                 after_seq=0 → 200 空；
                                                      after_seq=5 → 409 ahead
    被裁剪成空    ever+1            ever              游标 <ever → 409 below；
                                                      游标 =ever → 200 空
    ============ ================= ================= ==========================

    两个坑，都是「照直取 ``COALESCE(...,0)``」会踩的：

    * ``max_seq`` 掉到 0 会触发消费者自己的 ``max_seq < stored_max_seq_ever``
      硬停（§5.5 第 2 行）—— 一次正常的保留期裁剪就让全网对账。
      所以 ``max_seq`` 取 ``max(max(seq), max_seq_ever)``：表非空时两者相等
      （relay 每批都 bump ever，保留期只从底部裁），所以这不改变常态语义。
    * ``min_available_seq`` 取 0 会让 ``after_seq + 1 < min`` 永远不成立 ——
      被裁光的流对一个落后的消费者返回 200 空，它于是永远等下去。
      空表时「还能取到的最小 seq」就是「下一个会被发出来的号」= ``max_seq + 1``。
    """
    ever = max_seq_ever or 0
    if min_seq is None:                       # 空表
        head = max(0, ever)
        return head + 1, head
    return int(min_seq), max(int(max_seq or 0), ever)


#: §4.3 的 ``completeness_ok``，合取式，见 COMPLETENESS_* 常量的说明。
#:
#: P4.5：实现搬进 ``common/core/completeness.py``，这里只留别名。
#: ``server/api/export_incremental.py:_to_record`` 此前是**手工重算**同一个
#: 合取式的第二份实现 —— 判据改一次要改两处，漏一处就是
#: ``/api/v1/sync/records.completeness_ok`` 与
#: ``/api/export/incremental.completeness_ok`` 对同一行给出不同答案。
#: 现在两个端点走同一个函数对象（``_completeness_ok is completeness_ok`` 为真）。
_completeness_ok = _completeness.completeness_ok


def _forced_prune_log(raw: Optional[str]) -> List[Any]:
    """闩锁里**尚未被确认**的那些条目。Phase 6 写、Phase 3 起就在读。

    过滤 ``acknowledged_at`` 是 P6-3 的另一半：闩锁要「一直返回直到消费者逐条
    确认」，所以确认过的必须消失在 ``/status.forced_prune_log`` 里，否则
    ``retention_forced`` 一旦为真就永远为真，等于把这个信号烧掉。
    条目本身不删（``common/pgdb/retention.py`` 只打标记），事后还能复盘。

    解析不了的原样回出去而**不是**丢掉：它意味着有人手工写坏了闩锁，
    那正是必须让消费者看见的状态。
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return [{"raw": raw, "note": "forced_prune_log 不是合法 JSON"}]
    items = parsed if isinstance(parsed, list) else [parsed]
    return [e for e in items
            if not (isinstance(e, dict) and e.get("acknowledged_at"))]


def _record(row) -> Dict[str, Any]:
    """一行 asyncpg Record → 契约里的一条记录。

    ``payload`` 在库里是 jsonb，asyncpg 没装 codec 时取回来是 **str**
    （实测：``payload type: <class 'str'>``）。这里 loads 成对象再让
    JSONResponse 重新序列化 —— 多一次往返，但换来「payload 一定是合法 JSON
    对象」这条不变式；直接把字符串塞进去会让消费者拿到一个被引号包起来的
    payload，而那种 bug 只会在下游炸。
    """
    raw_payload = row["payload"]
    if isinstance(raw_payload, (str, bytes, bytearray)):
        try:
            payload = json.loads(raw_payload)
        except (TypeError, ValueError):
            payload = {"__unparsable_payload__": str(raw_payload)[:2000]}
    else:
        payload = raw_payload
    completeness = int(row["completeness"] or 0)
    return {
        "seq": int(row["seq"]),
        "source_id": row["source_id"],
        "gen": row["gen"],
        "asin": row["asin"],
        "marketplace": row["marketplace"],
        "zip_requested": row["zip_requested"],
        "zip_observed": row["zip_observed"],
        "zip_verify": row["zip_verify"],
        "collected_at": _iso(row["collected_at"]),
        "recorded_at": _iso(row["recorded_at"]),
        "outcome": row["outcome"],
        "completeness": completeness,
        # 服务端算，别让每个消费者各写一遍 §4.3 的合取式。
        "completeness_ok": _completeness_ok(completeness),
        "error_type": row["error_type"],
        "error_detail": row["error_detail"],
        "batch_id": row["batch_id"],
        "task_id": row["task_id"],
        "worker_id": row["worker_id"],
        "attempt": int(row["attempt"] or 0),
        "parse_engine": row["parse_engine"],
        "review_hash": row["review_hash"],
        "slow_hash": row["slow_hash"],
        "hash_ver": int(row["hash_ver"] or 1),
        "payload": payload,
    }


def _parse_outcomes(raw: Optional[str]) -> Tuple[Optional[List[str]], Optional[JSONResponse]]:
    """``outcomes=ok,not_found`` → ``['ok','not_found']``；非法值 422。

    封闭集校验是**服务端强制**的：静默忽略一个拼错的 outcome 会让消费者
    以为自己在筛，实际上拿到的是全集（或空集），两种都是数据错误。
    """
    if raw is None:
        return None, None
    names = [p.strip() for p in raw.split(",")]
    names = [n for n in names if n]
    if not names:
        return None, _err(422, "invalid_parameter",
                          "outcomes 给了但解析为空；要全部就别带这个参数。",
                          parameter="outcomes")
    bad = sorted({n for n in names if n not in VALID_OUTCOMES})
    if bad:
        return None, _err(422, "invalid_parameter",
                          f"未知的 outcome: {bad}",
                          parameter="outcomes", allowed=list(VALID_OUTCOMES))
    # 去重但保持稳定顺序（纯粹为了响应里的回显好读）
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out, None


def _guard(after_seq: int, min_available_seq: int, max_seq: int,
           meta: Dict[str, str]) -> Optional[JSONResponse]:
    """§5.1 的两个 409。**服务端强制**，不是消费者可选项。

    判据逐字来自计划：

    * ``after_seq + 1 < min_available_seq`` → ``cursor_below_retention``
      （消费者下一条要的是 ``after_seq + 1``，而它已经不在窗口里了）
    * ``after_seq > max_seq``               → ``cursor_ahead_of_stream``

    顺序不能反：被裁光的流上（``min = max + 1``）两条会同时成立，
    而「掉出保留窗口」是更准确、更可操作的那一条。

    ⚠ **已知的假阳性，是有意留着的。** seq 允许有空洞（序列非事务性，
    回滚的 relay 批次会烧号 —— 计划 Phase 2 收口表里实测过 40000001 被烧）。
    如果保留期边界正好落在一段被烧掉的号上，一个其实没掉窗的游标会拿到 409。
    代价是一次多余的全量对账；反过来（放宽判据）代价是静默丢数据。
    计划把这一条明确留给了 Phase 3，这里的取舍是**保守方向**。
    真正的修法要等 Phase 6：它持久记录实际裁掉的 floor，那时判据可以从
    「表里最小的 seq」换成「被裁掉的最高 seq」，假阳性归零。
    """
    if after_seq + 1 < min_available_seq:
        return _err(409, "cursor_below_retention",
                    f"游标 after_seq={after_seq} 的下一条 {after_seq + 1} 已经低于"
                    f"保留窗口下界 {min_available_seq}，这段数据已被裁剪。"
                    f"按契约 §5.5：告警 + 全量对账，不要跳过空洞继续拉。",
                    after_seq=after_seq, min_available_seq=min_available_seq,
                    max_seq=max_seq, gen=meta.get("gen"),
                    instance_id=meta.get("instance_id"))
    if after_seq > max_seq:
        return _err(409, "cursor_ahead_of_stream",
                    f"游标 after_seq={after_seq} 超过流头 max_seq={max_seq}。"
                    f"多半是服务端从备份恢复或整机快照回滚了。"
                    f"按契约 §5.5：告警 + 全量对账。",
                    after_seq=after_seq, min_available_seq=min_available_seq,
                    max_seq=max_seq, gen=meta.get("gen"),
                    instance_id=meta.get("instance_id"))
    return None


def _schema_missing(exc: BaseException) -> bool:
    import asyncpg
    return isinstance(exc, (asyncpg.UndefinedTableError,
                            asyncpg.InvalidSchemaNameError))


# ============================================================
# GET /api/v1/sync/records
# ============================================================

@router.get("/records")
async def sync_records(
    after_seq: int = Query(..., description="独占下界；要从头拉就传 0"),
    limit: int = Query(DEFAULT_LIMIT),
    outcomes: Optional[str] = Query(None, description="csv，例 ok,not_found"),
):
    """按 seq 严格递增地拉一页事件。空结果是 **200 + records: []**，永不 404。"""
    bad = _unavailable()
    if bad is not None:
        return bad
    if after_seq < 0 or after_seq > MAX_BIGINT:
        return _err(422, "invalid_parameter",
                    f"after_seq 必须在 0..{MAX_BIGINT}", parameter="after_seq")
    if limit < 1 or limit > MAX_LIMIT:
        return _err(422, "invalid_parameter",
                    f"limit 必须在 1..{MAX_LIMIT}", parameter="limit")
    outcome_list, bad = _parse_outcomes(outcomes)
    if bad is not None:
        return bad

    db = _database()
    try:
        async with _snapshot() as conn:
            # ---- 1) 同一快照里的边界 ----
            min_raw, max_raw = await _bounds(conn)
            meta = await _read_meta(conn)
            min_available_seq, max_seq = _window(
                min_raw, max_raw, _as_int(meta.get("max_seq_ever")))

            # ---- 2) 页查询之前先挡一次（省掉一次注定作废的扫描）----
            guard = _guard(after_seq, min_available_seq, max_seq, meta)
            if guard is not None:
                return guard

            # ---- 3) 页查询。多取一条来判 has_more，比 "seq < max_seq" 精确：
            #        带 outcomes 过滤时后者会谎报「还有」。----
            params: List[Any] = [after_seq, limit + 1]
            sql = (f"SELECT {_RECORD_SELECT} FROM scraper.scrape_events "
                   f"WHERE seq > $1")
            if outcome_list is not None:
                params.append(outcome_list)
                sql += f" AND outcome = ANY(${len(params)}::text[])"
            sql += " ORDER BY seq LIMIT $2"
            rows = await conn.fetch(sql, *params)
            has_more = len(rows) > limit
            rows = rows[:limit]

            # ---- 4) 页查询之后复核下界，取较大者（保守方向）----
            min_after, max_after = await _bounds(conn)
            min_available_after, max_seq_after = _window(
                min_after, max_after, _as_int(meta.get("max_seq_ever")))
            if min_available_after > min_available_seq:
                # REPEATABLE READ 下不该发生。真发生了说明隔离级别被降过，
                # 或者有人在同一连接上提前结束了事务 —— 两种都必须响。
                logger.error(
                    "同步快照不稳定：页查询前后的 min_available_seq 不同（%s -> %s）。"
                    "隔离级别被降级了？按保守方向返回 409。",
                    min_available_seq, min_available_after)
                min_available_seq = min_available_after
                max_seq = max(max_seq, max_seq_after)
            guard = _guard(after_seq, min_available_seq, max_seq, meta)
            if guard is not None:
                return guard

            # ---- 5) relay 滞后（同一快照，outbox 常驻只有一秒的量）----
            lag_row = await conn.fetchrow(
                "SELECT count(*) AS depth, "
                "COALESCE(EXTRACT(EPOCH FROM (now() - min(enqueued_at))), 0)::float8"
                " AS lag_s FROM scraper.scrape_outbox")
    except _PoolUnavailable:
        return _err(503, "event_stream_unavailable",
                    "连接池尚未就绪（只剩那条专用写连接，不能借）。")
    except Exception as exc:  # noqa: BLE001
        if _schema_missing(exc):
            return _err(503, "event_stream_unavailable",
                        f"事件流表还没建好: {type(exc).__name__}")
        raise

    records = [_record(r) for r in rows]
    # 游标**只推进到真正返回过的那一条**。空页不推进 —— 宁可下次重扫，
    # 也绝不让游标越过没交付的行（那是唯一会丢数据的方向）。
    next_after_seq = records[-1]["seq"] if records else after_seq
    forced = _forced_prune_log(meta.get("forced_prune_log"))

    return JSONResponse(content={
        "contract_version": CONTRACT_VERSION,
        "gen": meta.get("gen"),
        "instance_id": meta.get("instance_id"),
        "min_available_seq": min_available_seq,
        "max_seq": max_seq,
        "relay_lag_seconds": round(float(lag_row["lag_s"]), 3),
        "outbox_depth": int(lag_row["depth"]),
        "relay_state": db.event_relay_metrics().get("relay_state"),
        "server_time_utc": _now_iso(),
        "after_seq": after_seq,
        "next_after_seq": next_after_seq,
        "has_more": has_more,
        "count": len(records),
        "limit": limit,
        "outcomes": outcome_list,
        "retention_forced": bool(forced),
        "records": records,
    })


# ============================================================
# GET /api/v1/sync/status
# ============================================================

@router.get("/status")
async def sync_status():
    """§5.2 的全部字段。拉取循环每轮开头调一次。

    库侧的核心指标复用 ``db.event_stream_stats()``（Phase 2 收口时明确要求
    「Phase 3 直接吃这份，别再另写一份」），Phase 3 独有的那几项
    （保留期/磁盘/速率/时间窗）另借一条连接在一个快照里取。
    两次借还，一次调用 —— 这个端点 5 分钟才被调一次。

    ⚠ ``min_available_seq`` / ``max_seq`` 用 ``_window()`` **覆盖**
    ``event_stream_stats`` 的原始 ``COALESCE(...,0)``：那两个字段是
    ``/records`` 的 409 判据，两个端点必须逐字同源，否则消费者会看到
    「status 说还有，records 说 409」这种不可能状态。
    """
    bad = _unavailable()
    if bad is not None:
        return bad
    db = _database()

    try:
        stats = await db.event_stream_stats()
        async with _snapshot() as conn:
            meta = await _read_meta(conn)
            min_raw, max_raw = await _bounds(conn)
            times = await conn.fetchrow(
                "SELECT min(recorded_at) AS oldest, max(recorded_at) AS newest "
                "FROM scraper.scrape_events")
            db_size = await conn.fetchval(
                "SELECT pg_database_size(current_database())")
            # 24h 插入速率。**不是** count(*)：那在 2000 万行上是一次全索引扫描。
            # 取「最近 24h 内最早一行的 seq」（recorded_at 索引 + LIMIT 1，一次
            # MergeAppend 就出来），速率 = max_seq - 那个 seq + 1。
            # 因为 seq 会被回滚烧号，这是个**上界估计**，契约里写明了。
            oldest_seq_24h = await conn.fetchval(
                "SELECT seq FROM scraper.scrape_events "
                "WHERE recorded_at >= now() - interval '1 day' "
                "ORDER BY recorded_at LIMIT 1")
            # Phase 6 的保留期观测。**同一个快照里现算**，一个字都不从
            # sync_meta 读缓存 —— 见 common/pgdb/retention.py 承重约束 6。
            # 拿不到（老实现 / 别的 Database）就是 None，不影响其余字段。
            retention = None
            if hasattr(db, "retention_observe"):
                try:
                    retention = await db.retention_observe(conn)
                except Exception as e:  # noqa: BLE001
                    logger.warning("retention_observe 失败（/status 其余字段照常）: "
                                   "%s: %s", type(e).__name__, e)
                    retention = {"error": f"{type(e).__name__}: {e}"}
    except _PoolUnavailable:
        return _err(503, "event_stream_unavailable",
                    "连接池尚未就绪（只剩那条专用写连接，不能借）。")
    except Exception as exc:  # noqa: BLE001
        if _schema_missing(exc):
            return _err(503, "event_stream_unavailable",
                        f"事件流表还没建好: {type(exc).__name__}")
        raise

    min_available_seq, max_seq = _window(
        min_raw, max_raw, _as_int(meta.get("max_seq_ever")))
    ack_seq = _as_int(meta.get("ack_seq"))
    forced = _forced_prune_log(meta.get("forced_prune_log"))

    # 磁盘：PG 与本服务同机部署（计划 §0.1），所以量的是同一块盘。
    # 把路径一并回出去，免得看数字的人猜它量的是谁。
    try:
        from common import config
        disk_path = config.PROJECT_DIR
        free_disk_bytes = shutil.disk_usage(disk_path).free
    except Exception as e:  # noqa: BLE001
        disk_path, free_disk_bytes = None, None
        logger.warning("free_disk_bytes 取不到: %s", e)

    rate = None
    if oldest_seq_24h is not None and max_seq:
        rate = max(0, max_seq - int(oldest_seq_24h) + 1)

    return JSONResponse(content={
        "contract_version": CONTRACT_VERSION,
        # gen / instance_id 一律取【库里】的 sync_meta，不取进程内存。
        #
        # stats.get("gen") 来自 event_relay_metrics() -> _ev()["gen"]，那是
        # connect() 时 _bootstrap_identity 写进内存、此后**再不刷新**的值。
        # /records、/counts、/ack 三个兄弟端点都读 sync_meta，只有这里读内存，
        # 于是滚动重启（B2 专门修出来支持的那种部署）期间可以出现：
        #   实例 A 仍在跑，库被回滚 -> 实例 B 连上后重放检测器铸了新 gen
        #   -> A 的 /status 报旧 gen，A 的 /records 报新 gen，自相矛盾
        # 而 §7 的两条硬停探针（gen 变化 / max_seq 倒退）此时**都不会触发**，
        # 唯一会响的是 POST /ack 的 409 gen_mismatch，偏偏契约伪代码把它丢掉了。
        # gen 存在的意义就是抓这一种场景，所以它必须是库里那个权威值。
        #
        # 注意这只修了 Phase 3 这一半。另一半——relay 用从不刷新的 _ev()["gen"]
        # 给每一行盖戳（common/pgdb/relay.py）——属于 Phase 2 的账，见 F-GEN。
        "gen": meta.get("gen") or stats.get("gen"),
        "instance_id": meta.get("instance_id") or stats.get("instance_id"),
        "min_available_seq": min_available_seq,
        "max_seq": max_seq,
        "ack_seq": ack_seq,                       # 从没 ack 过就是 null，不是 0
        "ack_at": meta.get("ack_at"),
        "lag_records": (max_seq - ack_seq) if ack_seq is not None else None,
        "relay_lag_seconds": stats.get("relay_lag_s"),
        "relay_state": stats.get("relay_state"),
        "outbox_depth": stats.get("outbox_depth"),
        "dead_letters": stats.get("dead_letters"),
        "events_per_minute": stats.get("events_per_minute"),
        "oldest_recorded_at": _iso(times["oldest"] if times else None),
        "newest_recorded_at": _iso(times["newest"] if times else None),
        # 未确认的强制裁剪。**持久闩锁**：一条被记下来之后，`/status` 会一直
        # 返回它，直到消费者 POST /ack-prune 逐条确认（P6-3）。
        # 消费者宕机正是触发这件事的前提，所以「响应里一个瞬时布尔」是无效的。
        "retention_forced": bool(forced),
        "forced_prune_log": forced,
        "db_size_bytes": int(db_size) if db_size is not None else None,
        "free_disk_bytes": free_disk_bytes,
        "free_disk_path": disk_path,
        "observed_daily_insert_rate": rate,
        "partitions": stats.get("partitions"),
        "future_partitions": stats.get("future_partitions"),
        # P6-5：服务端替消费者留出的重叠余量。契约 §7 的 ``OVERLAP`` 必须 ≤ 它，
        # 否则 ``cursor - OVERLAP`` 会掉到保留窗口下面，每轮一个假 409。
        # 回出来是为了让消费侧**读这个数**、而不是把 200 硬编码在两个仓库里。
        "max_safe_overlap": (retention or {}).get("ack_slack_seq"),
        "retention": retention,
        "server_time_utc": _now_iso(),
    })


# ============================================================
# GET /api/v1/sync/counts
# ============================================================

@router.get("/counts")
async def sync_counts(
    from_seq: int = Query(..., description="闭区间下界"),
    to_seq: int = Query(..., description="闭区间上界"),
    bucket: Optional[str] = Query(None, description="只支持 hour"),
):
    """对账用。``[from_seq, to_seq]`` **闭区间**，宽度上限 800 万。

    消费者拿它跟自己 ``catalog.snapshots`` 的同区间计数比对，
    「抽样比对无漏采」这条验收标准靠它落地。
    """
    bad = _unavailable()
    if bad is not None:
        return bad
    if from_seq < 0 or from_seq > MAX_BIGINT:
        return _err(422, "invalid_parameter",
                    f"from_seq 必须在 0..{MAX_BIGINT}", parameter="from_seq")
    if to_seq > MAX_BIGINT:
        return _err(422, "invalid_parameter",
                    f"to_seq 必须 ≤ {MAX_BIGINT}", parameter="to_seq")
    if to_seq < from_seq:
        return _err(422, "invalid_parameter",
                    f"to_seq({to_seq}) 必须 ≥ from_seq({from_seq})",
                    parameter="to_seq")
    span = to_seq - from_seq + 1
    if span > MAX_COUNTS_SPAN:
        return _err(422, "range_too_wide",
                    f"区间宽度 {span} 超过上限 {MAX_COUNTS_SPAN}，请分段对账。",
                    span=span, max_span=MAX_COUNTS_SPAN)
    if bucket is not None and bucket != "hour":
        return _err(422, "invalid_parameter",
                    "bucket 只支持 'hour'（不带则不分桶）", parameter="bucket")

    try:
        async with _snapshot() as conn:
            meta = await _read_meta(conn)
            min_raw, max_raw = await _bounds(conn)
            agg = await conn.fetchrow(
                "SELECT count(*) AS n, min(seq) AS min_seq, max(seq) AS max_seq, "
                "min(recorded_at) AS min_rec, max(recorded_at) AS max_rec "
                "FROM scraper.scrape_events WHERE seq >= $1 AND seq <= $2",
                from_seq, to_seq)
            by_outcome = await conn.fetch(
                "SELECT outcome, count(*) AS n FROM scraper.scrape_events "
                "WHERE seq >= $1 AND seq <= $2 GROUP BY outcome ORDER BY outcome",
                from_seq, to_seq)
            buckets = None
            if bucket == "hour":
                # date_trunc 直接作用在 timestamptz 上会跟着会话 TimeZone 走。
                # 先 AT TIME ZONE 'UTC' 折成无时区值，桶边界因此永远是 UTC 整点。
                rows = await conn.fetch(
                    "SELECT date_trunc('hour', recorded_at AT TIME ZONE 'UTC')"
                    "         AS bucket_start, "
                    "       count(*) AS n, min(seq) AS min_seq, max(seq) AS max_seq "
                    "  FROM scraper.scrape_events "
                    " WHERE seq >= $1 AND seq <= $2 "
                    " GROUP BY 1 ORDER BY 1", from_seq, to_seq)
                buckets = [{
                    "bucket_start": _iso(r["bucket_start"]),
                    "count": int(r["n"]),
                    "min_seq": r["min_seq"],
                    "max_seq": r["max_seq"],
                } for r in rows]
    except _PoolUnavailable:
        return _err(503, "event_stream_unavailable",
                    "连接池尚未就绪（只剩那条专用写连接，不能借）。")
    except Exception as exc:  # noqa: BLE001
        if _schema_missing(exc):
            return _err(503, "event_stream_unavailable",
                        f"事件流表还没建好: {type(exc).__name__}")
        raise

    min_available_seq, max_seq = _window(
        min_raw, max_raw, _as_int(meta.get("max_seq_ever")))

    return JSONResponse(content={
        "contract_version": CONTRACT_VERSION,
        "gen": meta.get("gen"),
        "instance_id": meta.get("instance_id"),
        "from_seq": from_seq,
        "to_seq": to_seq,
        "span": span,
        "count": int(agg["n"]),
        "min_seq": agg["min_seq"],
        "max_seq": agg["max_seq"],
        "min_recorded_at": _iso(agg["min_rec"]),
        "max_recorded_at": _iso(agg["max_rec"]),
        "by_outcome": {r["outcome"]: int(r["n"]) for r in by_outcome},
        "bucket": bucket,
        "buckets": buckets,
        # 让调用方能判断「count 少了到底是漏采还是被裁剪」——光看 count 分不出来。
        #
        # 判据**只看下界**：保留期只从底部裁，所以「这段有没有被裁过」等价于
        # 「区间下界还在窗口里」。上界超过流头不是「没保留」，只是「还没采到」，
        # 调用方自己拿 stream_max_seq 判断即可。
        # ``max(from_seq, 1)``：seq 由 bigserial 发号，永远 ≥ 1，所以 from_seq=0
        # 与 from_seq=1 是同一个区间；不这么写，最自然的「从头对账」
        # （from_seq=0）会永远报 false。
        "stream_min_available_seq": min_available_seq,
        "stream_max_seq": max_seq,
        "range_fully_retained": max(from_seq, 1) >= min_available_seq,
        "server_time_utc": _now_iso(),
    })


# ============================================================
# POST /api/v1/sync/ack
# ============================================================

@router.post("/ack")
async def sync_ack(body: Dict[str, Any] = Body(...)):
    """``{"gen": "...", "ack_seq": 41808200}``。单调取 max，永不后退。

    为什么**不**走 ``db._write_lock`` / ``db._db``（D-2 的那条唯一写连接）：

    * ``sync_meta`` 已经在写锁之外被写了 —— relay 用它自己那条专用连接 bump
      ``max_seq_ever``。ack 走池连接不是新先例。
    * 这个端点由**远端消费者**触发。把它接到全局写锁上，等于给采集入库路径
      开了一个外部可触发的排队面：一个狂刷 ack 的消费者能拖慢 worker 交结果。
    * 整个更新是**一条**语句（``GREATEST`` 在 SQL 里做单调），
      不需要任何应用层锁就是原子的、并发安全的。

    三种拒绝，都必须响，不许静默宽容：
    * ``gen`` 不符 → 409 ``gen_mismatch``（对面在给另一个实例的流打点）
    * ``ack_seq`` 超过流头 → 409 ``ack_ahead_of_stream``。这条计划没写，
      但它是**数据丢失向量**：Phase 6 的保留期下界取 ``min(时间下界, ack_seq)``，
      收下一个我们从没发过的 ack_seq 就等于授权裁掉消费者其实没拿到的数据。
    * 参数形状不对 → 422

    **``ack_seq = 0`` 是收下但不落库的空操作**（200，``advanced: false``）。
    这不是宽容，是 P6-2 那条不变式的入口守卫：库里存进一个 ``'0'``，
    保留期的 ``min(时间下界, ack_seq)`` 就恒为 0，从此一行不裁直到磁盘满。
    而 0 的语义（「持久副本里一条都没有」）与「从未 ack」完全一致 ——
    计划 §5.4 对后者的规定就是「按纯时间 + 磁盘执行」，所以两者合流不丢信息。
    库层面还有一条 CHECK（``sync_meta_ack_seq_is_never_zero``）兜底，
    这里挡在前面只是为了不让一次合法请求撞成 500。
    """
    bad = _unavailable()
    if bad is not None:
        return bad
    if not isinstance(body, dict):
        return _err(422, "invalid_parameter", "请求体必须是 JSON 对象")
    gen = body.get("gen")
    raw_seq = body.get("ack_seq")
    if not isinstance(gen, str) or not gen.strip():
        return _err(422, "invalid_parameter", "gen 必须是非空字符串",
                    parameter="gen")
    if isinstance(raw_seq, bool) or not isinstance(raw_seq, int):
        return _err(422, "invalid_parameter",
                    "ack_seq 必须是整数（不是字符串、不是布尔）",
                    parameter="ack_seq")
    if raw_seq < 0 or raw_seq > MAX_BIGINT:
        return _err(422, "invalid_parameter",
                    f"ack_seq 必须在 0..{MAX_BIGINT}", parameter="ack_seq")

    try:
        async with _snapshot(readonly=False, isolation="read_committed") as conn:
            meta = await _read_meta(conn)
            min_raw, max_raw = await _bounds(conn)
            min_available_seq, max_seq = _window(
                min_raw, max_raw, _as_int(meta.get("max_seq_ever")))
            current_gen = meta.get("gen")
            if current_gen is None:
                return _err(503, "event_stream_unavailable",
                            "事件流还没引导（sync_meta 里没有 gen）。")
            if gen.strip() != current_gen:
                return _err(409, "gen_mismatch",
                            f"ack 带的 gen={gen!r} 与当前实例的 gen="
                            f"{current_gen!r} 不符。按契约 §5.5：告警 + 全量对账。",
                            gen=current_gen, sent_gen=gen,
                            instance_id=meta.get("instance_id"),
                            min_available_seq=min_available_seq, max_seq=max_seq)
            if raw_seq > max_seq:
                return _err(409, "ack_ahead_of_stream",
                            f"ack_seq={raw_seq} 超过流头 max_seq={max_seq}；"
                            f"不能确认一段本实例从未发出过的 seq。",
                            gen=current_gen, ack_seq=_as_int(meta.get("ack_seq")),
                            sent_ack_seq=raw_seq,
                            min_available_seq=min_available_seq, max_seq=max_seq)

            if raw_seq == 0:
                # 空操作：不写 ack_seq，也不写 ack_at（写了就是「有人 ack 过」
                # 的假证据，而 Phase 6 的自愈逻辑正是靠这对键判断幽灵值）。
                effective = meta.get("ack_seq")
                ack_at = meta.get("ack_at")
            else:
                # 单调：GREATEST 在 SQL 里做，所以并发 ack 也不会互相覆盖。
                # 正则那一段是防御性的：ack_seq 只由本端点写，理论上一定是数字，
                # 但一次手工 UPDATE 写进个非数字就会让 ``v::bigint`` 抛，
                # 而那时**整个 ack 通道**会挂掉，代价远大于这一行的开销。
                effective = await conn.fetchval(
                    "INSERT INTO scraper.sync_meta (k, v) VALUES ('ack_seq', $1) "
                    "ON CONFLICT (k) DO UPDATE SET v = GREATEST("
                    "  CASE WHEN scraper.sync_meta.v ~ '^[0-9]+$' "
                    "       THEN scraper.sync_meta.v::bigint ELSE 0 END, "
                    "  EXCLUDED.v::bigint)::text "
                    "RETURNING v", str(raw_seq))
                ack_at = _now_iso()
                await conn.execute(
                    "INSERT INTO scraper.sync_meta (k, v) VALUES ('ack_at', $1) "
                    "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v", ack_at)
    except _PoolUnavailable:
        return _err(503, "event_stream_unavailable",
                    "连接池尚未就绪（只剩那条专用写连接，不能借）。")
    except Exception as exc:  # noqa: BLE001
        if _schema_missing(exc):
            return _err(503, "event_stream_unavailable",
                        f"事件流表还没建好: {type(exc).__name__}")
        raise

    # ``stored`` 是生效值；**从未 ack 过是 null，不是 0**（与 /status 同口径）。
    # 只有这样消费侧才分得清「我 ack 过 0 条」与「服务端根本没收到过 ack」——
    # 而这两者在保留期眼里虽然等价，在排障时完全不是一回事。
    stored = _as_int(effective)
    return JSONResponse(content={
        "contract_version": CONTRACT_VERSION,
        "gen": current_gen,
        "instance_id": meta.get("instance_id"),
        "ack_seq": stored,              # 生效值（单调 max 之后的）
        "sent_ack_seq": raw_seq,
        "advanced": (stored is not None and stored == raw_seq
                     and raw_seq != _as_int(meta.get("ack_seq"))),
        "ack_at": ack_at,
        "min_available_seq": min_available_seq,
        "max_seq": max_seq,
        "lag_records": (max_seq - stored) if stored is not None else None,
        "server_time_utc": _now_iso(),
    })


# ============================================================
# POST /api/v1/sync/ack-prune
# ============================================================

@router.post("/ack-prune")
async def sync_ack_prune(body: Dict[str, Any] = Body(...)):
    """确认强制裁剪闩锁：``{"gen": "...", "prune_ids": [3, 4]}``（或 ``"all": true``）。

    为什么这是**第五个端点**而不是 ``/ack`` 上的一个可选字段：

    * ``/ack`` 每拉一页就发一次，是拉取循环里最热的调用。把「确认一次数据被
      强制丢弃」混进去，迟早会有人顺手把 ``prune_ids`` 也自动填上 ——
      那就等于把闩锁自动清掉，而闩锁存在的唯一目的就是**逼一个人来看一眼**。
    * 契约 §7 把 ``forced_prune_log`` 列成硬停项。硬停之后的处理动作理应是一次
      独立的、有意的调用，不是循环体的副作用。

    ``gen`` 必须匹配，理由与 ``/ack`` 相同：闩锁属于某一个实例的流。
    gen 变过之后消费者本来就该硬停 + 全量对账，这时清掉旧实例的闩锁没有意义。

    确认过的条目**不删除**，只打 ``acknowledged_at``：`/status` 不再返回它，
    但事后复盘「那次到底裁掉了哪一段」时还查得到。
    """
    bad = _unavailable()
    if bad is not None:
        return bad
    if not isinstance(body, dict):
        return _err(422, "invalid_parameter", "请求体必须是 JSON 对象")
    gen = body.get("gen")
    if not isinstance(gen, str) or not gen.strip():
        return _err(422, "invalid_parameter", "gen 必须是非空字符串", parameter="gen")

    take_all = body.get("all", False)
    if not isinstance(take_all, bool):
        return _err(422, "invalid_parameter", "all 必须是布尔", parameter="all")
    raw_ids = body.get("prune_ids")
    ids: List[Any] = []
    if raw_ids is not None:
        if not isinstance(raw_ids, list):
            return _err(422, "invalid_parameter",
                        "prune_ids 必须是数组", parameter="prune_ids")
        for i in raw_ids:
            if isinstance(i, bool) or not isinstance(i, (int, str)):
                return _err(422, "invalid_parameter",
                            "prune_ids 的元素必须是整数或字符串 id",
                            parameter="prune_ids")
            ids.append(i)
    if not ids and not take_all:
        return _err(422, "invalid_parameter",
                    "要么给非空的 prune_ids，要么显式 all=true。"
                    "一次什么都不确认的调用多半是消费端写错了。",
                    parameter="prune_ids")

    db = _database()
    if not hasattr(db, "acknowledge_forced_prunes"):
        return _err(503, "event_stream_unavailable",
                    "当前 Database 实现没有保留期闩锁。")

    try:
        # READ COMMITTED + 可写：闩锁的读改写自己在 retention.py 里用
        # ``SELECT … FOR UPDATE`` 串行化，不需要快照隔离（也不能用 ——
        # REPEATABLE READ 下两个并发确认会撞 SerializationError，与 /ack 同因）。
        async with _snapshot(readonly=False, isolation="read_committed") as conn:
            meta = await _read_meta(conn)
            current_gen = meta.get("gen")
            if current_gen is None:
                return _err(503, "event_stream_unavailable",
                            "事件流还没引导（sync_meta 里没有 gen）。")
            if gen.strip() != current_gen:
                return _err(409, "gen_mismatch",
                            f"ack-prune 带的 gen={gen!r} 与当前实例的 gen="
                            f"{current_gen!r} 不符。按契约 §7：告警 + 全量对账。",
                            gen=current_gen, sent_gen=gen,
                            instance_id=meta.get("instance_id"))
            result = await db.acknowledge_forced_prunes(
                conn, ids=ids or None, acknowledge_all=take_all)
    except _PoolUnavailable:
        return _err(503, "event_stream_unavailable",
                    "连接池尚未就绪（只剩那条专用写连接，不能借）。")
    except Exception as exc:  # noqa: BLE001
        if _schema_missing(exc):
            return _err(503, "event_stream_unavailable",
                        f"事件流表还没建好: {type(exc).__name__}")
        raise

    remaining = result.get("remaining") or []
    return JSONResponse(content={
        "contract_version": CONTRACT_VERSION,
        "gen": current_gen,
        "instance_id": meta.get("instance_id"),
        "acknowledged": result.get("acknowledged") or [],
        # 发来的 id 在闩锁里根本不存在。**不是错误**（重发是允许的），
        # 但如实回出去，免得消费者以为自己清掉了什么。
        "unknown_ids": result.get("unknown") or [],
        "forced_prune_log": remaining,
        "retention_forced": bool(remaining),
        "server_time_utc": _now_iso(),
    })
