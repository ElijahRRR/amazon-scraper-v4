"""common/pgdb/batches.py —— 批次 + 回调机制。

OWNS（9 个方法，对应 common/database.py 的同名实现）:
    create_batch                 database.py:799
    create_batch_if_absent       database.py:812   ← 撞名判定的真源，create_batch 转调它
    get_batches                  database.py:828
    get_batch_by_name            database.py:846
    get_batch_completion_status  database.py:913
    mark_batch_completed         database.py:956
    list_callback_due            database.py:983
    mark_callback_attempt        database.py:1005
    reset_callback_for_retry     database.py:1067

不属于本文件：expand_batch_variants（→ media.py，它调 create_tasks）。

--------------------------------------------------------------------------
移植要点（每一条都在基线或调用点上有依据）
--------------------------------------------------------------------------
* create_batch 必须保持"INSERT OR IGNORE → 然后单独 SELECT id"两条语句的形状。
  → ``INSERT INTO batches(...) VALUES(...) ON CONFLICT DO NOTHING``
    （裸 DO NOTHING，不写冲突目标）然后 ``SELECT id FROM batches WHERE name = ?``。
    注意 ``DO NOTHING`` 只吞**唯一/排他**冲突，而 SQLite 的 ``INSERT OR IGNORE``
    吞的是**所有**约束冲突。差额（NOT NULL / CHECK / FK）由方法体里那个
    ``except IntegrityConstraintViolationError: pass`` 补齐 —— 不补的话
    ``create_batch(None)`` 会从 SQLite 的返回 0 变成 PG 抛 NotNullViolationError。
  **不要**改写成 ``ON CONFLICT DO NOTHING RETURNING id``：冲突时它不返回行，
  create_batch 就会从"返回已存在的 id"变成返回 0 —— 而撞名分支查回来的那个 id
  正是 ``POST /api/upload`` 409 响应体里的 ``batch_id``（调用方接着拿它轮询），
  必须查得到。
  ⚠ 这段以前写的是"同名重传静默 no-op 是被钉死的行为"，**那句已作废**：
  Phase 4.7 证明撞名不是 no-op（新 ASIN 被并进上一批、第二次的
  external_id/callback_url 被静默丢弃），本轮把它改成 409。
  基线 step ``upload_batch_a_duplicate`` 现在录的是 409，不再是 inserted=0 的 200。
  「撞名返回既有 id」这条**仍然钉死**，只是它现在服务的是 409 的响应体。
  SELECT 要在事务提交**之后**发（SQLite 版就是 COMMIT 后的自动提交读）。
  另外：冲突时 identity 序列**照样烧号**（实测 PG 与 SQLite 完全一致），
  基线里 golden_batch_b 的 id 是 3 不是 2 就是这么来的 —— 绝不允许
  "先 SELECT 再决定插不插"之类的预过滤。
* get_batches：``GROUP BY b.id`` 带裸列（b.name / b.needs_screenshot /
  b.created_at）在 PG 里合法，**仅因为** batches.id 是声明过的 PRIMARY KEY。
  schema.py 已经保证了这点。查询逐字移植即可，不要往 GROUP BY 里加列。
  零任务的批次：五个统计列**全是 0**（不是 NULL）。旧写法靠 LEFT JOIN 出来的
  那一行"t.* 全 NULL"凑出 0（COUNT 数非空 -> 0；SUM(CASE) 走 ELSE -> 0）；
  现在换成子查询聚合，那一行不存在了，靠逐列 COALESCE(…, 0) 保持同一结果。
  真源是 tests/pgdb/test_batches.py::test_get_batches_zero_task_batch。
  ``SUM(CASE WHEN ... THEN 1 ELSE 0 END)`` 的 CASE 分支是整数字面量 →
  结果 bigint → asyncpg 给 int。**绝不能**给 CASE 分支加 ::bigint 之类的转换，
  那会让 SUM 变 numeric → Decimal → FastAPI 序列化成 JSON 字符串。
* get_batch_by_name 是 ``SELECT * FROM batches``：18 列**全部**泄给调用方，
  schema.py 的列序就是为这个对齐的。返回 dict(row)。
  created_at / completed_at 必须是 str —— app.py:1168 与 487 对它做
  ``datetime.strptime(x[:19], '%Y-%m-%d %H:%M:%S')``。
* get_batch_completion_status 返回
  ``{"tasks": {total, done, failed, open}, "screenshots": {...}, "all_terminal": bool}``。
  SQL 里的别名是 ``open_``，字典 key 是 ``open``（无下划线）。别对齐它们。
  两条独立聚合查询各自一个快照（SQLite 读池就是这样），**不要**加
  REPEATABLE READ 把它变得更一致——那是行为改变，且会遮住排期在后面的 bug。
* mark_batch_completed：``UPDATE ... WHERE id=? AND status='running'``，
  rowcount 就是幂等闸门。纯 CAS，不需要额外加锁。
* list_callback_due 用的是 ``self._db``（写连接）而不是 ``self.read()``。
  保持不变——换成读连接是行为改变。
* mark_callback_attempt 是读改写（SELECT callback_attempts → +1 → UPDATE），
  分支还依赖读到的值。加 ``FOR UPDATE`` 到那条 SELECT 上（SQLite 靠全局锁
  免费拿到的原子性），其余逐字移植。``if row else 1`` 的兜底要留着：
  批次不存在时 attempts=1 且 UPDATE 匹配 0 行，这个组合是现状。
* ORDER BY 的 NULL 位置：SQLite 是 ASC→NULLS FIRST / DESC→NULLS LAST，
  PG 默认**正好相反**。凡是排序键可空，显式写出 NULLS FIRST/LAST。
  本文件的两处排序键都是 ``batches.id``（PRIMARY KEY，非空），
  所以**不需要**补 NULLS 子句；补了反而是无谓的偏离。

--------------------------------------------------------------------------
本文件实际做出的移植动作（逐条可核对）
--------------------------------------------------------------------------
1. ``INSERT OR IGNORE`` → ``INSERT ... ON CONFLICT DO NOTHING``（1 处）。
2. ``BEGIN`` / ``BEGIN IMMEDIATE`` + ``COMMIT`` + ``except: ROLLBACK; raise``
   → ``async with self._tx():``（4 处）。语义一致：正常退出提交、异常回滚后
   继续抛。顺带消掉 catalog_sync_audit.md:130 记的
   "CancelledError 让共享写连接永久卡在事务里"（asyncpg 的事务上下文对取消安全）。
   ``BEGIN IMMEDIATE`` 在 PG 里是语法错误，且"提前抢写锁"在 PG 无对应物。
3. ``cursor.rowcount`` → 垫片已经把命令标签解析成 int（pool.rowcount_from_tag）。
   ``changed = cursor.rowcount > 0`` 逐字保留。
4. 绑到 text 列的值一律过 ``text_affinity``；绑到 bigint/integer 列的过 ``as_int``。
   asyncpg 不做隐式转换，而这些值全部来自未经校验的 ``await request.json()``
   / 表单（SQLite 会按列 affinity 静默转换）。黄金夹具全是字符串，抓不到这一类。
5. ``SELECT callback_attempts ... FOR UPDATE``（mark_callback_attempt）。

--------------------------------------------------------------------------
已知问题（不在本文件修，见返回报告）
--------------------------------------------------------------------------
* ``list_callback_due`` 走 ``self._db``（专用写连接）却**不持 _write_lock``，
  与 SQLite 版一致。但 asyncpg 的单条连接不允许并发操作：若
  ``_callback_dispatcher`` 恰好与某个持锁写事务交错，会撞
  ``InterfaceError: another operation is in progress``（aiosqlite 会自己排队，
  不会）。这是 D-2"单写连接"带来的 PG 独有风险，不是本方法能单独解决的
  （加锁 = 行为改变 + 多记一条 "other" 样本）。已上报。
"""
from __future__ import annotations

from common.core.timeutil import now_ts
from typing import Any, Dict, List, Optional, Tuple

# 只借异常类型，不建连接（硬规矩 3 禁的是自己建连接）。
from asyncpg.exceptions import IntegrityConstraintViolationError

from common.pgdb._shared import LOCK_STATS, TimedLock, record_stage  # noqa: F401
from common.pgdb.pool import as_int, text_affinity


class BatchesMixin:
    """只定义方法，绝不定义 __init__。基础设施一律走 self._db / self.read() /
    self._write_lock / self._tx()。"""

    # ==================== 批次操作 ====================

    async def create_batch(self, name: str, needs_screenshot: bool = False,
                           is_auto: bool = False,
                           external_id: Optional[str] = None,
                           callback_url: Optional[str] = None,
                           expand_variants: bool = False) -> int:
        """创建批次，返回 batch_id。

        external_id: 调用方自己的批次 ID（原样回传，便于追踪）。
        callback_url: 采集完成时 POST 通知到此 URL。空表示不通知。
        expand_variants: 开启后，本批第一轮采完会把已采 ASIN 的同族变体（variation_asins）
            自动入队进【同一批次】继续采，直到无新增（正常 2 轮收敛）。默认关。

        ⚠ 撞名时返回既有批次的 id，分辨不出「新建 vs 命中已有」。要分辨的用
        ``create_batch_if_absent``（真源，本方法只是丢掉第二个返回值的薄壳）。
        与 SQLite 侧逐字同构。
        """
        batch_id, _created = await self.create_batch_if_absent(
            name, needs_screenshot, is_auto,
            external_id=external_id, callback_url=callback_url,
            expand_variants=expand_variants,
        )
        return batch_id

    async def create_batch_if_absent(self, name: str, needs_screenshot: bool = False,
                                     is_auto: bool = False,
                                     external_id: Optional[str] = None,
                                     callback_url: Optional[str] = None,
                                     expand_variants: bool = False) -> Tuple[int, bool]:
        """建批次，返回 ``(batch_id, created)``。语义与理由见 SQLite 侧同名方法。

        PG 侧的 ``created`` 从**命令标签**来：``ON CONFLICT DO NOTHING`` 命中冲突
        时标签是 ``INSERT 0 0``，真插进去是 ``INSERT 0 1``，``pool.Cursor.rowcount``
        已经把它解析成 int（``rowcount_from_tag``）。所以这一位与 SQLite 的
        ``cursor.rowcount`` 是同一个东西，不需要额外往返。

        ⚠ **不要**改写成 ``ON CONFLICT DO NOTHING RETURNING id`` 来"顺手把 id 也拿了"：
        冲突时它不返回行，``create_batch`` 就会从"返回已存在的 id"变成返回 0 ——
        撞名分支恰恰是 409 响应体里那个 ``batch_id`` 的来源，必须查得到。
        ``IntegrityConstraintViolationError``（NOT NULL / CHECK / FK）的补差
        照旧，``created`` 在那条路上是 False。
        """
        callback_status = "pending" if callback_url else None
        name_p = text_affinity(name)
        created = False
        async with self._write_lock:
            # ON CONFLICT DO NOTHING 只吞**唯一/排他**冲突，而 SQLite 的
            # INSERT OR IGNORE 吞的是**所有**约束冲突——NOT NULL / CHECK / FK 也算。
            # 于是 create_batch(None) 在 SQLite 下是"插不进去，然后 SELECT 查不到，
            # 返回 0"，在 PG 下却抛 NotNullViolationError。这里把差额补上：
            # 把完整性约束错误当成"这一行没插进去"，交给下面那条 SELECT 决定返回值。
            # 注意异常必须逃出 self._tx() 之后才吞——PG 的事务一旦 abort，同一个
            # 事务里再发语句就是 25P02，必须先让 _tx() 回滚干净。
            # identity 烧号不受影响（序列是非事务的，实测两个后端都烧掉 1 号）。
            try:
                async with self._tx():
                    # INSERT OR IGNORE → ON CONFLICT DO NOTHING（裸的、不写冲突目标）。
                    # 冲突时**不返回行**，所以 id 必须由下面那条独立 SELECT 取回 ——
                    # 这正是"同名重传返回已有 id"的来源，基线钉死了它。
                    cur = await self._db.execute(
                        "INSERT INTO batches "
                        "(name, needs_screenshot, is_auto, status, external_id, "
                        " callback_url, callback_status, expand_variants) "
                        "VALUES (?, ?, ?, 'running', ?, ?, ?, ?) "
                        "ON CONFLICT DO NOTHING",
                        (name_p, 1 if needs_screenshot else 0, 1 if is_auto else 0,
                         text_affinity(external_id), text_affinity(callback_url),
                         callback_status,
                         1 if expand_variants else 0)
                    )
                    # 'INSERT 0 1' -> 1（真插了）/ 'INSERT 0 0' -> 0（撞名被吞）
                    created = cur.rowcount > 0
            except IntegrityConstraintViolationError:
                created = False
            # 提交之后再读（SQLite 版就是 COMMIT 后的自动提交读），仍在写锁内
            async with self._db.execute(
                    "SELECT id FROM batches WHERE name = ?", (name_p,)) as c:
                row = await c.fetchone()
                batch_id = row[0] if row else 0
        return batch_id, (created and batch_id > 0)

    async def get_batches(self) -> List[Dict]:
        """获取所有批次及其统计"""
        # 先按 batch_id 把 tasks 聚合成"每批次一行"，再去 LEFT JOIN batches。
        #
        # ⚠ 原来的写法是 `batches LEFT JOIN tasks ... GROUP BY b.id`：先把
        # **每一条任务**连出来（170 万行）再分组。这条查询在每次打开采集结果页
        # 和任务页时都要跑一遍，实测线上约 650ms、且随任务总量线性增长。
        # 改成先聚合之后，tasks 只被扫一遍且能走 idx_tasks_batch_status 的
        # index-only scan，连出来的行数从"任务数"降到"批次数"。
        #
        # 语义**逐字保持**，两处容易改坏的地方：
        #   * 零任务批次必须是 (total_tasks, completed, failed, pending,
        #     processing) = (0, 0, 0, 0, 0)，**五个都是 0，不是 NULL**。
        #     老写法是这么来的：LEFT JOIN 给出一行 t.* 全 NULL 的行，
        #     COUNT(t.id) 数非空 -> 0，而 `SUM(CASE WHEN NULL='done' THEN 1
        #     ELSE 0 END)` 走的是 ELSE 分支 -> SUM(0) -> 0。
        #     换成子查询之后这一行**根本不存在**，五个全是 NULL，所以必须
        #     逐个 COALESCE 回 0。
        #     ⚠ 本文件模块头注释里"零任务批次 completed/... = NULL，别 COALESCE
        #     掉"那句是**错的**，已一并改正；真源是
        #     tests/pgdb/test_batches.py::test_get_batches_zero_task_batch。
        #   * COUNT(*) 与 SUM(CASE ... THEN 1 ELSE 0 END) 的分支都是整数字面量
        #     -> bigint -> asyncpg 给 int。**绝不能**加 ::bigint 之类的转换，
        #     那会让 SUM 变 numeric -> Decimal -> FastAPI 序列化成 JSON 字符串。
        #     COALESCE(x, 0) 的 0 是整数字面量，不影响这一点。
        sql = """
            SELECT b.id, b.name, b.needs_screenshot, b.created_at,
                   COALESCE(t.total_tasks, 0) as total_tasks,
                   COALESCE(t.completed, 0) as completed,
                   COALESCE(t.failed, 0) as failed,
                   COALESCE(t.pending, 0) as pending,
                   COALESCE(t.processing, 0) as processing
            FROM batches b
            LEFT JOIN (
                SELECT batch_id,
                       COUNT(*) as total_tasks,
                       SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) as completed,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
                       SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                       SUM(CASE WHEN status = 'processing' THEN 1 ELSE 0 END) as processing
                FROM tasks
                GROUP BY batch_id
            ) t ON t.batch_id = b.id
            ORDER BY b.id DESC
        """
        async with self.read() as rc, rc.execute(sql) as c:
            rows = await c.fetchall()
            return [dict(r) for r in rows]

    async def get_batch_by_name(self, name: str) -> Optional[Dict]:
        async with self.read() as rc, rc.execute(
                "SELECT * FROM batches WHERE name = ?",
                (text_affinity(name),)) as c:
            row = await c.fetchone()
            return dict(row) if row else None

    # ==================== 批次完成检测 + 回调 ====================

    async def get_batch_completion_status(self, batch_id: int) -> Dict[str, Any]:
        """返回批次完成度统计 + 是否已全部终态。

        完成判定：所有 task ∈ {done, failed} AND 所有 screenshot ∈ {done, failed}
        """
        bid = as_int(batch_id)
        async with self.read() as rc:
            async with rc.execute(
                """SELECT
                       SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done,
                       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN status NOT IN ('done','failed') THEN 1 ELSE 0 END) AS open_,
                       COUNT(*) AS total
                   FROM tasks WHERE batch_id=?""",
                (bid,)
            ) as c:
                t = await c.fetchone()
            async with rc.execute(
                """SELECT
                       SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done,
                       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN status NOT IN ('done','failed') THEN 1 ELSE 0 END) AS open_,
                       COUNT(*) AS total
                   FROM screenshots WHERE batch_id=?""",
                (bid,)
            ) as c:
                s = await c.fetchone()

        t_done = (t["done"] or 0) if t else 0
        t_failed = (t["failed"] or 0) if t else 0
        t_open = (t["open_"] or 0) if t else 0
        t_total = (t["total"] or 0) if t else 0
        s_done = (s["done"] or 0) if s else 0
        s_failed = (s["failed"] or 0) if s else 0
        s_open = (s["open_"] or 0) if s else 0
        s_total = (s["total"] or 0) if s else 0

        all_done = (t_total > 0 and t_open == 0 and s_open == 0)
        return {
            "tasks": {"total": t_total, "done": t_done, "failed": t_failed, "open": t_open},
            "screenshots": {"total": s_total, "done": s_done, "failed": s_failed, "open": s_open},
            "all_terminal": all_done,
        }

    async def mark_batch_completed(self, batch_id: int) -> bool:
        """标记批次为 completed（仅 running → completed 转移有效，幂等）。
        返回 True 表示本次确实做了状态转移，调用方可以触发回调入队。
        False 表示批次已经是 completed/failed/其他状态，不重复处理。
        """
        now = now_ts()
        async with self._write_lock:
            # BEGIN IMMEDIATE → 普通事务（PG 的行锁是惰性获取的，没有 SQLite
            # 那个"升级死锁"要绕，IMMEDIATE 在 PG 里还是语法错误）。
            # 原来的 try/except → ROLLBACK → raise 由事务上下文承担，语义一致。
            async with self._tx():
                cursor = await self._db.execute(
                    """UPDATE batches
                       SET status='completed',
                           completed_at=?,
                           updated_at=?
                       WHERE id=? AND status='running'""",
                    (now, now, as_int(batch_id))
                )
                # 纯 CAS：rowcount 就是幂等闸门（垫片已把命令标签解析成 int）
                changed = cursor.rowcount > 0
        return changed

    async def list_callback_due(self, now_str: str, limit: int = 50) -> List[Dict]:
        """查询所有需要立刻发送/重试的 callback。
        条件：
        - callback_status='pending'（待发送）
        - callback_url 非空
        - status='completed'（必须真正完成，避免在 _completion_watcher 跑完之前抢跑）
        - next_retry_at <= 当前时间（NULL 视为立即）
        """
        # 走 self._db（写连接）而不是 self.read()，与 SQLite 版一致
        async with self._db.execute(
            """SELECT id, name, external_id, callback_url, callback_attempts,
                      completed_at, status
               FROM batches
               WHERE callback_status='pending'
                 AND callback_url IS NOT NULL
                 AND status='completed'
                 AND (callback_next_retry_at IS NULL OR callback_next_retry_at <= ?)
               ORDER BY id ASC LIMIT ?""",
            (text_affinity(now_str), as_int(limit, 50))
        ) as c:
            rows = await c.fetchall()
        return [dict(r) for r in rows]

    async def mark_callback_attempt(self, batch_id: int, success: bool,
                                     error: Optional[str] = None,
                                     next_retry_at: Optional[str] = None,
                                     max_attempts: int = 5) -> Dict[str, Any]:
        """记录一次回调尝试。
        success=True:  callback_status='sent', sent_at=now
        success=False: attempts+1；若达到 max_attempts 则 status='failed'，
                       否则更新 next_retry_at 等下次扫描
        """
        now = now_ts()
        bid = as_int(batch_id)
        async with self._write_lock:
            async with self._tx():
                if success:
                    await self._db.execute(
                        """UPDATE batches
                           SET callback_status='sent',
                               callback_sent_at=?,
                               callback_last_error=NULL,
                               updated_at=?
                           WHERE id=?""",
                        (now, now, bid)
                    )
                    return {"final_status": "sent"}
                # 失败：判断是否达到上限
                # FOR UPDATE：SQLite 靠全局写锁免费拿到的"读到写之间没人插队"，
                # PG 要显式锁住这一行（分支走向依赖读到的 attempts）。
                async with self._db.execute(
                    "SELECT callback_attempts FROM batches WHERE id=? FOR UPDATE",
                    (bid,)
                ) as c:
                    row = await c.fetchone()
                attempts = (row["callback_attempts"] or 0) + 1 if row else 1
                if attempts >= max_attempts:
                    await self._db.execute(
                        """UPDATE batches
                           SET callback_attempts=?,
                               callback_status='failed',
                               callback_last_error=?,
                               updated_at=?
                           WHERE id=?""",
                        (attempts, text_affinity((error or "")[:500]), now, bid)
                    )
                    final = "failed"
                else:
                    await self._db.execute(
                        """UPDATE batches
                           SET callback_attempts=?,
                               callback_next_retry_at=?,
                               callback_last_error=?,
                               updated_at=?
                           WHERE id=?""",
                        (attempts, text_affinity(next_retry_at),
                         text_affinity((error or "")[:500]), now, bid)
                    )
                    final = "pending"
                return {"final_status": final, "attempts": attempts}

    async def delete_batches(self, batch_ids) -> List[str]:
        """删除若干批次及其全部关联数据，返回**待删除的截图物理文件路径**。

        ``DELETE /api/batches/{name}`` 与 ``POST /api/batches/delete-bulk``
        的库侧动作（Phase 3.8 批 (2) 把两处收成一处）。

        **文件不在这里删**：返回路径清单，由 handler 调 ``_remove_screenshot_files``。
        库方法删文件意味着一个失败的 unlink 能污染一次数据库操作，而且
        测试再也不能在不碰磁盘的前提下验它。

        入参去重 / 上限（bulk 端点的 500 上限）属于**请求校验**，留在 handler。
        这里只做 ``int()`` 归一 —— asyncpg 按 Python 类型推参数，路由给的
        str id 会 DataError。

        路径清单在写锁**外**、事务**前**取：截图行马上就要被删掉，取晚了就空了。
        收口前单条删除走的是 ``db._db``（写连接），bulk 走的是 ``db.read()``
        （读池）；这里统一成读池 —— 两者读的都是已提交数据，但读池不占写连接，
        也就不会跟正在跑的写事务抢那一条连接。

        删除顺序：先四张子表、最后 batches。PG 侧有真外键，顺序即正确性。
        """
        ids = [int(b) for b in batch_ids]
        if not ids:
            return []
        ph = ",".join("?" * len(ids))

        # 先收集截图物理文件路径（只读连接，不占写锁）
        # 同样逐批次取（理由见下面事务里那段注释），但**只借一次读连接** ——
        # 每个 id 各借一次会让 500 个批次变成 500 次 pool.acquire()。
        screenshot_files: List[str] = []
        async with self.read() as rc:
            for bid in ids:
                async with rc.execute(
                    "SELECT file_path FROM screenshots WHERE batch_id = ?"
                    " AND file_path IS NOT NULL", (bid,)
                ) as c:
                    screenshot_files.extend(row["file_path"] for row in await c.fetchall())

        async with self._write_lock:
            await self._db.execute("BEGIN")
            try:
                # ⚠ **逐批次**删，不是一条 `IN (全部 id)`。
                #
                # 超时是**按语句**计的（asyncpg 的 command_timeout，默认 60s，
                # 见 config.PG_COMMAND_TIMEOUT）。一条 `DELETE ... WHERE
                # batch_id IN (...500 个 id...)` 在百万行量级的库上是一次
                # 无上界的操作：删得越多越久，某一天就会跨过 60s，然后
                # 整个事务回滚、前端只看到一个 500。拆成每批次一条之后，
                # 每条语句的工作量被"单个批次的行数"钉住，与选了多少个批次无关。
                #
                # 仍在**同一个事务**里：原子性是这个方法的既有语义（要么全删、
                # 要么全不删），分块只改语句粒度，不改事务边界。
                for tbl in ("tasks", "batch_asins", "screenshots", "asin_changes"):
                    for bid in ids:
                        await self._db.execute(
                            f"DELETE FROM {tbl} WHERE batch_id = ?", (bid,))
                # batches 本身一行一个批次，一条语句就够，不需要拆
                await self._db.execute(
                    f"DELETE FROM batches WHERE id IN ({ph})", ids)
                await self._db.execute("COMMIT")
            except BaseException:
                try:
                    await self._db.execute("ROLLBACK")
                except BaseException:
                    pass
                raise
        return screenshot_files

    async def reset_callback_for_retry(self, batch_id: int) -> bool:
        """运维手动触发：把已经 failed 或 sent 的回调重置回 pending 立即重试。"""
        now = now_ts()
        async with self._write_lock:
            async with self._tx():
                cursor = await self._db.execute(
                    """UPDATE batches
                       SET callback_status='pending',
                           callback_attempts=0,
                           callback_next_retry_at=NULL,
                           callback_last_error=NULL,
                           updated_at=?
                       WHERE id=? AND callback_url IS NOT NULL""",
                    (now, as_int(batch_id))
                )
                changed = cursor.rowcount > 0
        return changed
