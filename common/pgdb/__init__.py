"""common/pgdb —— common/database.py 的 PostgreSQL 版实现（**增量**，不改原文件）。

对外只暴露一个东西：``Database``，公开面与 ``common.database.Database`` 逐字相同
（50 个属性：签名、默认值、返回形状、async/sync 性质全部一致）。

    from common.pgdb import Database     # 直接用
    from common.dbfactory import create_database   # 按 DB_BACKEND 选后端（推荐）

--------------------------------------------------------------------------
组装
--------------------------------------------------------------------------
MRO 里 PoolMixin **必须**排第一：__init__ / _db / read / _write_lock / _tx
都由它提供，其余 mixin 只准定义方法、绝不定义 __init__，也绝不自己建连接。

每个方法**有且只有一个** mixin 定义（单一真源）。谁拥有什么见 OWNERSHIP.md，
下面的 _assert_single_owner() 在导入时就把这条规则变成硬约束。
"""
from __future__ import annotations

from common.pgdb.admin import AdminMixin
from common.pgdb.batches import BatchesMixin
from common.pgdb.media import MediaMixin
from common.pgdb.pool import PoolMixin
from common.pgdb.relay import EventStreamMixin
from common.pgdb.results_read import ResultsReadMixin
from common.pgdb.results_write import ResultsWriteMixin
from common.pgdb.retention import RetentionMixin
from common.pgdb.schema import SchemaMixin
from common.pgdb.tasks import TasksMixin


class Database(PoolMixin, SchemaMixin, BatchesMixin, TasksMixin,
               ResultsWriteMixin, ResultsReadMixin, MediaMixin, AdminMixin,
               EventStreamMixin, RetentionMixin):
    """异步 PostgreSQL 数据库管理器 v3（与 SQLite 版公开面等价）。

    ``EventStreamMixin``（Phase 2 事件流）与 ``RetentionMixin``（Phase 6
    保留期 + ack 闩锁）排在最后，且它们的方法**一个都不在 PUBLIC_API 里**
    —— 那个元组是与 SQLite 的对等面契约，事件流与保留期都是 PG 独有的增量，
    不该出现在里面。两道导入期自检对此是安全的：``_assert_api_complete``
    只查"少了没有"，``_assert_single_owner`` 只遍历已在 PUBLIC_API 里的名字，
    所以新方法能干净地导进来，同时仍然受重复定义检查保护。

    ``RetentionMixin`` 排在 ``EventStreamMixin`` 之后是有意的：它复用后者的
    ``_seq_high_water()``（判「这个分区还会不会收到新行」的唯一正确判据），
    自己一个字都不重复实现。
    """


# ------------------------------------------------------------------
# 公开面自检：导入即执行，实现者写错了立刻炸，而不是等黄金校验才发现
# ------------------------------------------------------------------

#: common/database.py Database 的公开属性。
#: 这份清单是契约，改动它 = 改动 API。
PUBLIC_API = (
    # 生命周期 / 基础设施
    "__init__", "connect", "_open_read_pool", "read", "run_startup_optimize",
    "maintenance_loop", "start_maintenance", "close", "wal_checkpoint",
    "init_tables",
    # 批次
    "create_batch", "create_batch_if_absent", "get_batches", "get_batch_by_name",
    "expand_batch_variants",
    "get_batch_completion_status", "mark_batch_completed", "list_callback_due",
    "mark_callback_attempt", "reset_callback_for_retry", "delete_batches",
    # 任务
    "create_tasks", "pull_tasks", "reclaim_dead_worker_tasks",
    "auto_retry_failed_tasks", "retry_failed_tasks", "fail_task", "release_tasks",
    "prioritize_batch", "get_progress",
    # 卖家（F-009）
    "create_seller_batch", "accept_seller_discovery_result",
    "get_seller_batch_progress",
    # 结果写入
    "accept_success_result", "accept_results_batch", "accept_failed_result",
    "_save_result_inner_unlocked", "save_result", "save_results_batch",
    # 截图路径辅助
    "_get_done_screenshot_path", "_get_done_screenshot_paths",
    "_hydrate_screenshot_paths",
    # `/api/results?batch_id=` 的三列批次状态。与 _hydrate_screenshot_paths
    # 同族：都是 get_results 之后的原地补齐，两个后端必须同结果。
    "_hydrate_batch_task_status",
    # 结果读取
    "get_batch_failures", "get_results", "get_result_by_asin", "get_asin_changes",
    "iter_results", "find_asins_by_search", "get_batch_asin_set",
    # 截图
    "get_pending_screenshots", "update_screenshot_status", "get_screenshot_progress",
    "list_screenshots", "reconcile_orphan_screenshots",
    # 统计
    "get_total_asins", "get_all_asins", "get_change_stats",
    # 管理（Phase 3.8 收口）
    "clear_all_data", "delete_asins",
)

_MIXINS = (PoolMixin, SchemaMixin, BatchesMixin, TasksMixin, ResultsWriteMixin,
           ResultsReadMixin, MediaMixin, AdminMixin, EventStreamMixin,
           RetentionMixin)


def _assert_api_complete():
    missing = [n for n in PUBLIC_API if not hasattr(Database, n)]
    if missing:
        raise AssertionError(f"common.pgdb.Database 缺少公开方法: {missing}")


def _assert_nothing_unregistered():
    """反向：SQLite 侧的公开方法**必须**都登记进 PUBLIC_API。

    这条是补上来的，因为原来那三个方向里恰好漏了最要命的一个（Phase 3.8 审计实测）：

      * PUBLIC_API 里有 pgdb 没实现的名字      -> _assert_api_complete 当场炸  ✅
      * 只实现 pgdb 一侧、SQLite 没有          -> 导入照过，test_skeleton 测试期才红
      * **两侧都实现了、但忘了登记进 PUBLIC_API** -> **原来什么都不响**

    第三种最坏，而且不止是"名册少一个名字"：``test_signatures_match_sqlite`` 是
    **遍历 PUBLIC_API** 做签名比对的，漏登记的方法连签名一致性都不会被检查——
    于是两侧参数名/默认值可以悄悄分叉，而全套门禁一片绿。

    白名单为空是有意的：今天 SQLite 侧 50 个公开名一个不少全在册。将来真要加
    "不属于契约的公开方法"，把它写进 _NOT_IN_CONTRACT 并说明理由，别放宽这条断言。
    """
    import inspect

    try:
        from common.database import Database as _SqliteDatabase
    except ImportError:
        # SQLite 侧不可用（比如将来只装 asyncpg 不装 aiosqlite 的 PG-only 部署，
        # 或 SQLite 后端已退役）。这条断言的**前提**是「两个后端并存」，
        # 前提不在时跳过，不是放水：
        #   * 它要防的事故是「两侧都实现了但漏登记」——只有一侧时不存在；
        #   * 另一个方向（PUBLIC_API 里有 pgdb 没实现的名字）由
        #     _assert_api_complete 守着，那条不依赖 common.database，照常执行。
        #
        # ⚠ 这个 try 不是可有可无的：Phase 4.1 把共享符号搬进 common/core 之后，
        #   common.pgdb 本来已经不需要 import common.database 了；
        #   本函数（Phase 3.8 审计的产物）是当时**唯一**把那条边又接回来的地方。
        #   写成 try 让「PG-only 部署不装 aiosqlite」这条路重新走得通。
        return

    unregistered = sorted(
        n for n, _ in inspect.getmembers(_SqliteDatabase)
        if not n.startswith("_") and n not in PUBLIC_API and n not in _NOT_IN_CONTRACT
    )
    if unregistered:
        raise AssertionError(
            "SQLite 侧有公开方法没登记进 PUBLIC_API: "
            f"{unregistered}\n"
            "  后果不只是名册不全——test_signatures_match_sqlite 遍历 PUBLIC_API，\n"
            "  没登记的方法连两侧签名一致性都不会被检查。"
        )


#: SQLite 侧公开、但**有意**不属于双后端契约的名字。今天为空。
_NOT_IN_CONTRACT = frozenset()


def _assert_single_owner():
    """同一个方法只准被一个 mixin 定义。

    多重继承下重复定义不会报错，只会被 MRO 静默遮蔽——那正是"改了 A 文件却
    没生效"这类事故的来源，所以在导入期就把它挡掉。
    """
    seen = {}
    dupes = []
    for mixin in _MIXINS:
        for name in vars(mixin):
            if name.startswith("__") or name not in PUBLIC_API:
                continue
            if name in seen:
                dupes.append(f"{name}: {seen[name].__name__} 与 {mixin.__name__}")
            else:
                seen[name] = mixin
    if dupes:
        raise AssertionError("方法被多个 mixin 重复定义（单一真源被破坏）:\n  "
                             + "\n  ".join(dupes))


_assert_api_complete()
_assert_nothing_unregistered()
_assert_single_owner()

__all__ = ["Database", "PUBLIC_API"]
