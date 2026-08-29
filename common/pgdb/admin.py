"""common/pgdb/admin.py —— SQLite 专有维护接口的 PG 侧等价物（全部实现完毕）。

OWNS（4 个方法）:
    run_startup_optimize   database.py:372   -> ANALYZE / 日志，不再是 PRAGMA optimize
    maintenance_loop       database.py:385   -> 空转（PG 没有 WAL checkpoint 饥饿问题）
    start_maintenance      database.py:402   -> **同步方法**（app.py:171 不 await）
    wal_checkpoint         database.py:426   -> 恒返回 None

这四个在 PG 下没有语义，但**公开面必须存在**：
  * server/app.py:171 ``db.start_maintenance(checkpoint_interval=120)`` —— 不 await，
    所以它必须是 ``def`` 而不是 ``async def``。
  * server/app.py:174 ``asyncio.create_task(db.run_startup_optimize())``。
  * server/app.py:306-310 ``res = await db.wal_checkpoint(mode)`` 之后读
    ``res[0] / res[1] / res[2]``，但外面裹了 ``if res:``，返回 None 是安全的。
  * tests/golden/harness.py 会按**类属性**把 start_maintenance 与
    run_startup_optimize 换成 no-op（已改成按 DB_BACKEND 选到的类打补丁）。

--------------------------------------------------------------------------
锁仪表：一个等价、一个无法等价
--------------------------------------------------------------------------
``run_startup_optimize`` 与 SQLite 版**完全一致**：``_write_lock("optimize")``
+ ``self._db``。因此活着的 PG 服务在 ``/api/_debug/lock-stats`` 里同样有
``optimize`` 这个 caller key。（黄金基线不受影响 —— harness 把这个方法整个
换成了 no-op，所以基线里本来就没有 optimize。）

``maintenance_loop`` 则**没有** ``checkpoint`` 这个 caller key，而 SQLite 版有。
这不是取舍，是 PG 里根本不存在对应操作：WAL 回收由服务端 checkpointer +
max_wal_size 负责，客户端无事可做。为了让指标"看起来一样"而去空转一个
``_write_lock("checkpoint")``，等于伪造观测数据，比诚实地缺这个 key 更糟。
影响面：跑满 120s 以上的 PG 服务，lock-stats 比 SQLite 少一个 ``checkpoint``
key。黄金覆盖不到（start_maintenance 被 harness no-op 掉了）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from common.pgdb._shared import CLEAR_TABLES

logger = logging.getLogger(__name__)


class AdminMixin:
    """只定义方法，绝不定义 __init__。"""

    async def run_startup_optimize(self):
        """SQLite 的 ``PRAGMA optimize`` 在 PG 里对应 ANALYZE（刷新规划器统计）。

        SQLite 版用 ``analysis_limit=400`` 把采样量压到毫秒级；PG 的 ANALYZE
        本来就是采样的（default_statistics_target × 300 行），不需要等价物。

        失败只告警、不抛，与 SQLite 版一致 —— 它绝不能挡住启动。
        （超时同样只是告警：写连接带 command_timeout，超大库上 ANALYZE 被打断
        只意味着这一轮统计没刷新，下次重启还有机会。）

        锁与连接的选择与 SQLite 版**逐字一致**：``_write_lock("optimize")``
        + ``self._db``。这样 ``/api/_debug/lock-stats`` 的 waits/holds 里同样
        会出现 ``optimize`` 这个 caller key（实测：不这么做，活着的 PG 服务
        比 SQLite 服务少一个 key）。

        早期版本这里刻意避开了写连接，因为当时并发用同一条 asyncpg 连接会抛
        ``InterfaceError: another operation is in progress``。那个根因已经在
        ``ConnProxy._op_lock``（pool.py）修掉了 —— 单条连接上的语句现在像
        aiosqlite 一样排队，所以这里不再需要特殊处理。
        走读池反而有个 SQLite 没有的风险：导出会长时间占用池连接
        （iter_results 一次导出可能占用数分钟），池被占满时 ANALYZE 会举着
        _write_lock 干等，把所有写路径拖死。
        """
        try:
            if self._write_proxy is None:
                return
            async with self._write_lock("optimize"):
                await self._db.execute("ANALYZE")
            logger.info("✅ 启动期 ANALYZE 完成")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"启动期 ANALYZE 异常: {e}")

    async def maintenance_loop(self, checkpoint_interval: int = 120):
        """PG 没有 WAL 文件膨胀 / checkpoint 饥饿的问题：WAL 回收由服务端的
        checkpointer + max_wal_size 负责，统计与死元组回收由 autovacuum 负责，
        都不需要客户端插手。

        保留协程形状（永不返回、按 checkpoint_interval 心跳），这样
        ``start_maintenance`` / ``close`` 的取消语义与 SQLite 版逐字一致。

        **Phase 6 起这个循环不再是纯空转**：每一跳问一次保留期要不要跑
        （``maybe_run_retention()`` 自己按 ``SYNC_RETENTION_INTERVAL_S`` 节流，
        默认 20 分钟一次）。这里是本进程唯一够得着的周期性入口 —— SQLite 版的
        对应位置也正是审计 §3.6 给保留期指定的挂载点。

        保留期的任何异常都**只记日志**：维护协程一旦因为它退出，
        ``close()`` 的取消语义、以及将来挂在这里的其它周期任务会一起没了，
        代价远大于「这一轮没裁成」。
        """
        logger.info(
            f"🔧 维护协程启动（PG 后端：无 WAL checkpoint 需求，"
            f"心跳 {checkpoint_interval}s/轮，兼保留期节拍）")
        while True:
            # SQLite 版在这里做 TRUNCATE checkpoint 并按 busy 标志重试；
            # PG 下没有任何对应动作，只保留心跳节奏与取消点。
            await asyncio.sleep(checkpoint_interval)
            if not hasattr(self, "maybe_run_retention"):
                continue
            try:
                await self.maybe_run_retention()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.error("保留期本轮异常（维护协程继续）: %s: %s",
                             type(e).__name__, e)

    def start_maintenance(self, checkpoint_interval: int = 120):
        """由 lifespan 调用：拉起后台维护协程（幂等）。

        **同步方法** —— server/app.py:171 是 ``db.start_maintenance(...)``，
        没有 await。改成 async 会静默变成一个从不被调度的协程对象。
        """
        if self._maintenance_task is None or self._maintenance_task.done():
            self._maintenance_task = asyncio.create_task(
                self.maintenance_loop(checkpoint_interval))

    async def wal_checkpoint(self, mode: str = "PASSIVE") -> Optional[tuple]:
        """PG 无对应物，恒返回 None。

        SQLite 版返回 ``(busy, log, checkpointed)``，出错时同样返回 None，
        所以"返回 None"本来就在契约之内。两个调用点都已守住：
          * server/app.py:307  ``if res:``
          * common/database.py:396 ``if res and res[0] == 1:``（SQLite 自己的
            维护协程，PG 侧的 maintenance_loop 根本不调用它）
        """
        return None

    # ==================== 管理（清库）====================

    #: SQLite 侧 5 张带 AUTOINCREMENT 的表。``batch_asins`` /
    #: ``seller_discoveries`` / ``search_discoveries`` 是复合主键，没有序列，
    #: 不在这里（后者是 F-010 新增的，与 seller 那张同构，同样无序列）。
    #: （Phase 3.8 批 (1) 之前这份清单写在 ``pool.py`` 的 ``_STATEMENT_OVERRIDES``
    #:  里，靠匹配 handler 那句 ``DELETE FROM sqlite_sequence`` 的字面量触发。）
    RESTART_IDENTITY_TABLES = ("batches", "asin_data", "asin_changes",
                               "tasks", "screenshots")

    async def clear_all_data(self) -> None:
        """清空全部业务数据，并把自增 id 归位。与 SQLite 版语义逐字等价。

        SQLite 版删 ``sqlite_sequence`` 里的行让下一个 id 回到 1；PG 的 identity
        必须显式 RESTART。删哪些表、按什么顺序删，两侧共用 ``_shared.CLEAR_TABLES``
        （子表在前、父表在后）—— PG 有真外键，顺序在这边是正确性问题。

        ``_tx()`` 已经带 BaseException 回滚，所以这里不再自己写回滚；
        ``_write_lock`` 不带 caller 名（即 ``other``），与收口前 handler 里那句
        ``async with db._write_lock:`` 一致，``/api/_debug/lock-stats`` 的
        caller key 不变。

        ``ALTER TABLE`` 不含 ``?``，``translate_sql`` 原样透传（``pool.py:378``），
        走 ``_db`` 而不是裸连接是为了同样过 ``_op_lock`` 与事务状态位。
        """
        async with self._write_lock:
            async with self._tx() as conn:
                for table in CLEAR_TABLES:
                    await conn.execute(f"DELETE FROM {table}")
                for table in self.RESTART_IDENTITY_TABLES:
                    await conn.execute(
                        f"ALTER TABLE {table} ALTER COLUMN id RESTART WITH 1")
